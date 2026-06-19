"""
KiCad IPC plugin entrypoint: starts an HTTP bridge to the open schematic.

KiCad launches this (venv python) with KICAD_API_SOCKET / KICAD_API_TOKEN in
the environment, so kipy's KiCad() auto-connects to THIS instance. The worker
(elsewhere) calls the endpoints below. Each mutation goes in a commit (undo).
"""
import base64
import os, sys, socket, subprocess, traceback, base64, json, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8761


def _log(msg):
    try:
        with open(os.path.join(HERE, "bridge.log"), "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except Exception:
        pass
    print(msg, flush=True)


def _ensure_kipy():
    try:
        import kipy  # noqa
        return
    except ImportError:
        pass
    whl = [f for f in os.listdir(HERE) if f.startswith("kicad_python") and f.endswith(".whl")]
    if not whl:
        raise SystemExit("bundled kipy wheel not found next to bridge_entry.py")
    _log(f"installing bundled kipy: {whl[0]}")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--no-input",
                           "--no-deps", os.path.join(HERE, whl[0])])


_ensure_kipy()

from flask import Flask, request, jsonify
from kipy import KiCad
import kipy.schematic_types as st
from kipy.proto.schematic import schematic_types_pb2 as _schpb
from kipy.geometry import Vector2
from kipy.util import from_mm

app = Flask(__name__)


@app.errorhandler(Exception)
def _on_error(e):
    return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


def schematic():
    k = KiCad()                # picks up KICAD_API_SOCKET/TOKEN from env
    return k, k.get_schematic()


def _field_text(field):
    try:
        return field.text.value
    except Exception:
        return None


def _commit(sch, items, msg):
    c = sch.begin_commit()
    try:
        created = sch.create_items(items)
        sch.push_commit(c, msg)
        return created
    except Exception:
        sch.drop_commit(c)
        raise


@app.get("/health")
def health():
    try:
        k, sch = schematic()
        return jsonify(ok=True, kicad=str(k.get_version()),
                       project=sch.document.project.name,
                       symbols=len(sch.get_symbols()))
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 503


@app.get("/symbols")
def symbols():
    _, sch = schematic()
    return jsonify(symbols=[
        dict(ref=_field_text(s.reference_field), value=_field_text(s.value_field),
             lib_id=f"{s.definition.id.library}:{s.definition.id.name}",
             def_items=len(s.definition.items),
             x_mm=s.position.x / 1e6, y_mm=s.position.y / 1e6)
        for s in sch.get_symbols()])


@app.post("/text")
def add_text():
    d = request.get_json(force=True)
    _, sch = schematic()
    t = st.SchematicText()
    t.value = d["text"]
    t.position = Vector2.from_xy_mm(d["x_mm"], d["y_mm"])
    s = d.get("size_mm", 1.27)
    t.text.attributes.size = Vector2.from_xy_mm(s, s)
    return jsonify(ok=True, created=len(_commit(sch, t, d.get("commit", "bridge: text"))))


@app.post("/label")
def add_label():
    d = request.get_json(force=True)
    _, sch = schematic()
    lbl = st.GlobalLabel() if d.get("global") else st.LocalLabel()
    lbl.text.value = d["text"]
    lbl.text.attributes.size = Vector2.from_xy_mm(1.27, 1.27)
    lbl.position = Vector2.from_xy_mm(d["x_mm"], d["y_mm"])
    return jsonify(ok=True, created=len(_commit(sch, lbl, d.get("commit", "bridge: label"))))



def _shift_pins(inst_proto, dx, dy):
    """Shift every SchematicPin in the instance definition by (dx,dy) nm so the
    pins follow the repositioned body. Returns the count shifted."""
    n = 0
    for child in inst_proto.definition.items:
        if child.item.Is(_schpb.SchematicPin.DESCRIPTOR):
            pin = _schpb.SchematicPin()
            child.item.Unpack(pin)
            pin.position.x_nm += dx
            pin.position.y_nm += dy
            child.item.Pack(pin)
            n += 1
    return n


_FIELDS = ("reference_field", "value_field", "footprint_field",
           "datasheet_field", "description_field")


def _shift_fields(inst_proto, dx, dy):
    """Shift each present instance field (ref/value/footprint/...) by (dx,dy) nm
    so the field texts follow the repositioned symbol."""
    n = 0
    for fname in _FIELDS:
        if inst_proto.HasField(fname):
            pos = getattr(inst_proto, fname).text.position
            pos.x_nm += dx
            pos.y_nm += dy
            n += 1
    return n


@app.get("/dump")
def dump():
    """Diagnostic: instance position + pin positions (mm) for a given ref, to
    verify the pin coordinate frame (absolute vs relative)."""
    ref = request.args.get("ref", "")
    _, sch = schematic()
    s = next((x for x in sch.get_symbols() if _field_text(x.reference_field) == ref), None)
    if s is None:
        return jsonify(ok=False, error=f"no symbol ref={ref}"), 404
    pins, others = [], []
    for child in s.definition.items:
        if child.proto.item.Is(_schpb.SchematicPin.DESCRIPTOR):
            pin = _schpb.SchematicPin(); child.proto.item.Unpack(pin)
            pins.append({"num": pin.number, "x_mm": pin.position.x_nm/1e6,
                         "y_mm": pin.position.y_nm/1e6})
        else:
            others.append(child.proto.item.type_url.split(".")[-1])
    flds = {}
    for fn in ("reference_field", "value_field"):
        if s.proto.HasField(fn):
            tp = getattr(s.proto, fn).text.position
            flds[fn] = [tp.x_nm/1e6, tp.y_nm/1e6]
    return jsonify(ok=True, ref=ref,
                   instance_pos_mm=[s.position.x/1e6, s.position.y/1e6],
                   pins=pins, fields=flds, other_items=others)


@app.post("/symbol")
def add_symbol():
    """Clone a placed instance of lib_id to a new spot, pins shifted to stay
    bonded. Optional source_ref pins the clone source to a KNOWN-GOOD symbol
    (otherwise the first matching lib_id is used - avoid cloning a prior clone)."""
    d = request.get_json(force=True)
    _, sch = schematic()
    lib, name = d["lib_id"].split(":", 1)
    syms = sch.get_symbols()
    if d.get("source_ref"):
        src = next((s for s in syms if _field_text(s.reference_field) == d["source_ref"]), None)
    else:
        src = next((s for s in syms
                    if s.definition.id.library == lib and s.definition.id.name == name), None)
    if src is None:
        return jsonify(ok=False, error=f"no clone source for '{d['lib_id']}'"), 409
    nx, ny = from_mm(d["x_mm"]), from_mm(d["y_mm"])
    dx, dy = nx - src.position.x, ny - src.position.y
    inst = st.SchematicSymbolInstance()
    inst.proto.CopyFrom(src.proto)
    inst.proto.ClearField("id")
    do_shift = d.get("shift", "pins") != "none"
    shifted = _shift_pins(inst.proto, dx, dy) if do_shift else 0
    fields_shifted = _shift_fields(inst.proto, dx, dy) if do_shift else 0
    if d.get("move_body", True):
        inst.position = Vector2.from_xy_mm(d["x_mm"], d["y_mm"])
    if d.get("reference"):
        inst.proto.reference_field.text.text = d["reference"]
    if d.get("value"):
        inst.proto.value_field.text.text = d["value"]
    created = _commit(sch, inst, d.get("commit", "bridge: symbol"))
    return jsonify(ok=True, created=len(created), shifted_pins=shifted, shifted_fields=fields_shifted,
                   src_ref=_field_text(src.reference_field), delta_mm=[dx/1e6, dy/1e6])

@app.get("/grab")
def grab():
    """Serialize a placed symbol's full proto (definition incl. pins) to base64,
    so it can be replanted into another schematic. Open the DONOR schematic."""
    ref = request.args.get("ref", "")
    _, sch = schematic()
    s = next((x for x in sch.get_symbols() if _field_text(x.reference_field) == ref), None)
    if s is None:
        return jsonify(ok=False, error=f"no symbol ref={ref}"), 404
    return jsonify(ok=True, ref=ref,
                   src_pos_mm=[s.position.x / 1e6, s.position.y / 1e6],
                   proto_b64=base64.b64encode(s.proto.SerializeToString()).decode())


@app.post("/place_raw")
def place_raw():
    """Replant a grabbed symbol proto into the OPEN schematic at (x_mm,y_mm),
    shifting pins+fields so they stay bonded. KiCad parsed the definition, so
    the pins render correctly (unlike a hand-built proto)."""
    d = request.get_json(force=True)
    _, sch = schematic()
    inst = st.SchematicSymbolInstance()
    inst.proto.ParseFromString(base64.b64decode(d["proto_b64"]))
    inst.proto.ClearField("id")
    # adopt the OPEN schematic's sheet path -> sheet-agnostic (works on empty sheets,
    # no need to scaffold from a symbol on the target sheet)
    try:
        inst.proto.path.CopyFrom(sch._doc.sheet_path)
    except Exception:
        pass
    sx, sy = inst.position.x, inst.position.y
    nx, ny = from_mm(d["x_mm"]), from_mm(d["y_mm"])
    dx, dy = nx - sx, ny - sy
    sp = _shift_pins(inst.proto, dx, dy)
    sf = _shift_fields(inst.proto, dx, dy)
    inst.position = Vector2.from_xy_mm(d["x_mm"], d["y_mm"])
    if d.get("reference"):
        inst.proto.reference_field.text.text = d["reference"]
    if d.get("value"):
        inst.proto.value_field.text.text = d["value"]
    created = _commit(sch, inst, d.get("commit", "bridge: place_raw"))
    return jsonify(ok=True, created=len(created), shifted_pins=sp, shifted_fields=sf)

@app.post("/wire")
def add_wire():
    d = request.get_json(force=True)
    _, sch = schematic()
    ln = st.SchematicLine()
    ln.proto.start.x_nm = from_mm(d["x1"]); ln.proto.start.y_nm = from_mm(d["y1"])
    ln.proto.end.x_nm = from_mm(d["x2"]); ln.proto.end.y_nm = from_mm(d["y2"])
    ln.proto.type = 1   # SLT_WIRE
    return jsonify(ok=True, created=len(_commit(sch, ln, d.get("commit", "bridge: wire"))))


@app.post("/junction")
def add_junction():
    d = request.get_json(force=True)
    _, sch = schematic()
    j = st.Junction()
    j.proto.position.x_nm = from_mm(d["x"]); j.proto.position.y_nm = from_mm(d["y"])
    return jsonify(ok=True, created=len(_commit(sch, j, d.get("commit", "bridge: junction"))))


def _sch_file(sch):
    """Absolute path of the open root schematic on disk."""
    proj = sch._doc.project
    return os.path.join(proj.path, sch.name)   # sch.name = project.name + '.kicad_sch'


@app.post("/save")
def save_doc():
    """Persist the open schematic to its .kicad_sch file."""
    _, sch = schematic()
    sch.save()
    return jsonify(ok=True, path=_sch_file(sch))


@app.post("/erc")
def run_erc():
    """Save the open schematic, then run `kicad-cli sch erc` on it (headless,
    same KiCad install) and return a flattened violation list. There is no ERC
    in the IPC API, so we drive the CLI - which needs a file - hence the save."""
    k, sch = schematic()
    sch.save()
    schfile = _sch_file(sch)
    cli = k.get_kicad_binary_path("kicad-cli")          # locate kicad-cli(.exe)
    out = os.path.join(tempfile.gettempdir(), "bridge_erc.json")
    cmd = [cli, "sch", "erc", "--format", "json", "-o", out,
           "--severity-all", "--exit-code-violations", schfile]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    report = None
    if os.path.exists(out):
        with open(out, encoding="utf-8") as f:
            report = json.load(f)
    viols = []
    for sh in (report or {}).get("sheets", []):
        for v in sh.get("violations", []):
            viols.append({
                "severity": v.get("severity"),
                "type": v.get("type"),
                "description": v.get("description"),
                "items": [it.get("description") for it in v.get("items", [])],
            })
    errors = sum(1 for v in viols if v["severity"] == "error")
    warnings = sum(1 for v in viols if v["severity"] == "warning")
    return jsonify(ok=True, exit_code=r.returncode, schfile=schfile,
                   errors=errors, warnings=warnings, violation_count=len(viols),
                   violations=viols, stderr=(r.stderr or "")[-2000:])


# top-level schematic object types to wipe on /clear (children like pins/fields
# go with their parent symbol; sheets/sheet-pins are left to preserve hierarchy)
_CLEAR_TYPES = [35, 23, 19, 30, 31, 32, 33, 27, 26, 24, 20, 25, 21, 22, 28]
#  35 SYMBOL  23 LINE(wire)  19 JUNCTION  30/31/32/33 LABEL/GLOBAL/HIER/DIRECTIVE
#  27 TEXT  26 TEXTBOX  24 SHAPE  20 NO_CONNECT  25 BITMAP  21/22 BUS_ENTRY  28 TABLE


def _remove(sch, items, msg):
    if not items:
        return 0
    c = sch.begin_commit()
    try:
        sch.remove_items(items)
        sch.push_commit(c, msg)
        return len(items)
    except Exception:
        sch.drop_commit(c)
        raise


@app.post("/clear")
def clear_sheet():
    """Delete every top-level item on the open sheet (one undoable commit)."""
    _, sch = schematic()
    items = list(sch.get_items(_CLEAR_TYPES))
    return jsonify(ok=True, removed=_remove(sch, items, "bridge: clear sheet"))


@app.post("/delete")
def delete_refs():
    """Delete symbols by reference: {"ref": "U1"} or {"refs": ["U1","C2"]}."""
    d = request.get_json(force=True) or {}
    refs = set(d.get("refs") or ([d["ref"]] if d.get("ref") else []))
    _, sch = schematic()
    syms = [s for s in sch.get_symbols() if _field_text(s.reference_field) in refs]
    removed = [_field_text(s.reference_field) for s in syms]
    _remove(sch, syms, "bridge: delete " + ",".join(removed))
    return jsonify(ok=True, removed=removed, not_found=sorted(refs - set(removed)))


def main():
    probe = socket.socket()
    try:
        probe.bind(("0.0.0.0", PORT))
        probe.close()
    except OSError:
        _log(f"bridge already running on :{PORT}, not starting a second one")
        return
    _log(f"worker-bridge listening on :{PORT}")
    try:
        app.run(host="0.0.0.0", port=PORT)
    except Exception:
        _log(traceback.format_exc())


if __name__ == "__main__":
    main()
