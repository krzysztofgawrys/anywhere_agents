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
from kipy.schematic import Schematic
from kipy.board import Board
from kipy.proto.common.types import DocumentType, DocumentSpecifier
import kipy.schematic_types as st
from kipy.proto.schematic import schematic_types_pb2 as _schpb
from kipy.geometry import Vector2, Angle
from kipy.util import from_mm

app = Flask(__name__)


@app.errorhandler(Exception)
def _on_error(e):
    return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


def _sheet_path_str(d):
    try:
        return d.sheet_path.path_human_readable or "/".join(d.sheet_path.path)
    except Exception:
        return ""


def _doc_label(d):
    return d.board_filename or _sheet_path_str(d) or ""


def _req_sheet():
    """Pull an optional `sheet` selector from the current request (?sheet= or JSON
    body). Lets EVERY endpoint target a specific hierarchical sheet with no per-
    endpoint change - schematic() reads it here."""
    try:
        if request.method == "POST":
            return (request.get_json(force=True, silent=True) or {}).get("sheet")
        return request.args.get("sheet")
    except Exception:
        return None


def _find_sheet_instance(top_sheets, sheet):
    """Recursively find the SheetInstance whose name OR filename matches `sheet`
    (case-insensitive substring). Returns the wrapper or None."""
    s = str(sheet).lower()
    for si in top_sheets:
        if s in (si.name or "").lower() or s in (si.filename or "").lower():
            return si
        hit = _find_sheet_instance(si.children, sheet)
        if hit is not None:
            return hit
    return None


def schematic(sheet=None):
    """k, Schematic for the selected sheet. Default = the ACTIVE document (docs[0]).
    With `sheet` (explicit arg or request ?sheet=/JSON "sheet"), we target a specific
    HIERARCHICAL sub-sheet by copying the active DocumentSpecifier and stamping its
    sheet_path with the sub-sheet's KIID path (from get_hierarchy). create_items /
    get_items then act on THAT sheet's screen - no open_document, no GUI navigation.
    (KiCad gates open_document to headless, but honours header.document.sheet_path
    for create/get even in GUI - api_handler_sch.cpp targetPath logic.)"""
    k = KiCad()                # picks up KICAD_API_SOCKET/TOKEN from env
    if sheet is None:
        sheet = _req_sheet()
    docs = k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
    if not docs:
        raise RuntimeError("no open schematic document")
    base = docs[0]
    if not sheet:
        return k, Schematic(k._client, base)
    si = _find_sheet_instance(Schematic(k._client, base).get_hierarchy(), sheet)
    if si is None:
        raise RuntimeError(f"sheet '{sheet}' not found in hierarchy")
    doc = DocumentSpecifier(); doc.CopyFrom(base)
    doc.sheet_path.CopyFrom(si._proto.path)          # route ops to this sub-sheet
    return k, Schematic(k._client, doc)


@app.get("/sheets")
def sheets():
    """Two views of the hierarchy. `open_documents` = what get_open_documents returns
    (in GUI mode that is ONLY the ACTIVE sheet). `hierarchy` = get_hierarchy()'s full
    tree (read-only: name/filename/page per sheet). In GUI mode the bridge can only
    EDIT the active sheet; open_document (sheet switching) is headless-only."""
    k = KiCad()
    docs = k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
    opened = [{"index": i, "board_filename": d.board_filename,
               "sheet_path": _sheet_path_str(d)} for i, d in enumerate(docs)]
    sch = Schematic(k._client, docs[0])
    active = sch.name
    try:
        active = sch._doc.project.name
    except Exception:
        pass

    def walk(si, depth=0):
        try:
            phr = si.path.path_human_readable
        except Exception:
            phr = ""
        node = [{"depth": depth, "name": si.name, "filename": si.filename,
                 "page": si.page_number, "path": phr}]
        for c in si.children:
            node += walk(c, depth + 1)
        return node

    tree = []
    err = None
    try:
        for top in sch.get_hierarchy():
            tree += walk(top)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    # ?open=<path-or-filename> -> actually TEST open_document on THIS nightly (the
    # kipy docstring says GUI-mode unsupported, but verify rather than trust it).
    probe = None
    op = request.args.get("open")
    if op:
        probe = {}
        for cand in [op, os.path.join(os.path.dirname(_sch_file(sch)), op)]:
            try:
                d = k.open_document(cand, DocumentType.DOCTYPE_SCHEMATIC)
                s2 = Schematic(k._client, d)
                probe[cand] = {"ok": True, "name": s2.name, "symbols": len(s2.get_symbols())}
            except Exception as e:
                probe[cand] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return jsonify(ok=True, active_symbols=len(sch.get_symbols()),
                   open_documents=opened, hierarchy=tree, hierarchy_error=err,
                   open_probe=probe)


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
    """Read back the placed symbols of the active (or ?sheet=) sheet, INCLUDING the
    footprint - so a manual footprint/value edit in eeschema can be synced back into
    the source netlist (round-trip) and survive the next /clear + re-place."""
    _, sch = schematic()
    return jsonify(symbols=[
        dict(ref=_field_text(s.reference_field), value=_field_text(s.value_field),
             footprint=_field_text(s.footprint_field),
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


@app.post("/no_connect")
def add_no_connect():
    """Place no-connect (X) marker(s) on intentionally-unconnected pins, so ERC
    stops flagging pin_not_connected. Body: {"x_mm","y_mm"} for one, or
    {"points":[[x,y],...]} for many. Coords are the PIN tip (mm), sheet-aware."""
    d = request.get_json(force=True)
    _, sch = schematic()
    pts = d.get("points") or [[d["x_mm"], d["y_mm"]]]
    items = []
    for x, y in pts:
        nc = st.NoConnectMarker()
        nc.proto.position.x_nm = from_mm(x); nc.proto.position.y_nm = from_mm(y)
        items.append(nc)
    return jsonify(ok=True, created=len(_commit(sch, items, d.get("commit", "bridge: no_connect"))))


# ===================== PCB (pcbnew) =====================
def board():
    """k, Board for the OPEN PCB. Same idea as schematic() but DOCTYPE_PCB."""
    k = KiCad()
    docs = k.get_open_documents(DocumentType.DOCTYPE_PCB)
    if not docs:
        raise RuntimeError("no open PCB document (open the .kicad_pcb in pcbnew)")
    return k, Board(k._client, docs[0])


@app.get("/pcb/health")
def pcb_health():
    try:
        k, b = board()
        return jsonify(ok=True, kicad=str(k.get_version()),
                       project=b.get_project().name, footprints=len(b.get_footprints()))
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 503


def _sheet_map(k):
    """Map sheet-KIID -> human sheet name from the open schematic hierarchy, so the
    placer can group footprints by their source schematic sheet. A footprint's
    sheet_path.path[0] is the KIID of the sheet it lives on (== that sheet's own
    path[-1] in get_hierarchy)."""
    smap = {}
    try:
        docs = k.get_open_documents(DocumentType.DOCTYPE_SCHEMATIC)
        if not docs:
            return smap
        sch = Schematic(k._client, docs[0])

        def walk(si):
            try:
                p = si.path.path
                if p:
                    smap[p[-1].value] = si.name
            except Exception:
                pass
            for c in si.children:
                walk(c)
        for t in sch.get_hierarchy():
            walk(t)
    except Exception:
        pass
    return smap


def _fp_extent(f):
    """Footprint width/height (mm) at its CURRENT orientation, from the union of its
    graphic shapes (courtyard/fab/silk enclose the pads). Used to size placement
    cells so parts don't overlap. Falls back to pad spread, then 1x1mm."""
    xs, ys = [], []
    for sh in f.definition.shapes:
        try:
            bb = sh.bounding_box()
            xs += [bb.pos.x, bb.pos.x + bb.size.x]
            ys += [bb.pos.y, bb.pos.y + bb.size.y]
        except Exception:
            pass
    if not xs:
        for p in f.definition.pads:
            try:
                xs.append(p.position.x); ys.append(p.position.y)
            except Exception:
                pass
    if not xs:
        return 1.0, 1.0
    return (max(xs) - min(xs)) / 1e6, (max(ys) - min(ys)) / 1e6


@app.get("/pcb/footprints")
def pcb_footprints():
    """Read every footprint: ref, value, position (mm), orientation (deg), layer,
    SHEET (source schematic sheet name), and w_mm/h_mm (current-orientation extent).
    This is the placer's input - the live board state plus grouping + sizing."""
    k, b = board()
    smap = _sheet_map(k)
    out = []
    for f in b.get_footprints():
        try:
            p = f.sheet_path.path
            sheet = smap.get(p[0].value, "") if p else ""
            w, h = _fp_extent(f)
            out.append({"ref": f.reference_field.text.value,
                        "value": f.value_field.text.value,
                        "x_mm": f.position.x / 1e6, "y_mm": f.position.y / 1e6,
                        "orient": f.orientation.degrees, "layer": int(f.layer),
                        "sheet": sheet, "w_mm": round(w, 3), "h_mm": round(h, 3)})
        except Exception:
            pass
    return jsonify(ok=True, count=len(out), footprints=out)


@app.get("/pcb/pads")
def pcb_pads():
    """Per-footprint pad geometry + net, for true pin-level placement. Each pad's
    (x_mm,y_mm) is FOOTPRINT-LOCAL in the orient-0 frame (like .kicad_mod / GetPos0):
    local0 = R(-orient) . (pad_abs - fp_pos). So the caller reconstructs the absolute
    pad position for ANY placement it assigns via /pcb/move as:
        pad_abs = fp_pos + R(orient) . (x_mm, y_mm)
    with R(t) = [[cos t, -sin t],[sin t, cos t]] in KiCad's Y-DOWN mm frame (verified
    round-trip against /pcb/move's Angle.from_degrees). `net` is the flat board net
    name on the pad (e.g. "GND", "/CH1/IF_HP"); `num` matches the symbol pin number.
    Footprint-level x_mm/y_mm/orient is the pad frame's current origin on the board."""
    import math
    _, b = board()
    out = []
    for f in b.get_footprints():
        try:
            fx, fy = f.position.x, f.position.y
            ang = f.orientation.degrees
            rad = math.radians(-ang); ca, sa = math.cos(rad), math.sin(rad)
            pads = []
            for p in f.definition.pads:
                try:
                    dx = p.position.x - fx; dy = p.position.y - fy
                    lx = dx * ca - dy * sa; ly = dx * sa + dy * ca
                    try:
                        net = p.net.name or ""
                    except Exception:
                        net = ""
                    pads.append({"num": p.number, "x_mm": round(lx / 1e6, 4),
                                 "y_mm": round(ly / 1e6, 4), "net": net})
                except Exception:
                    pass
            out.append({"ref": f.reference_field.text.value,
                        "x_mm": fx / 1e6, "y_mm": fy / 1e6, "orient": ang,
                        "pads": pads})
        except Exception:
            pass
    return jsonify(ok=True, count=len(out), footprints=out)


@app.post("/pcb/move")
def pcb_move():
    """Move/rotate footprints. Body: {"moves":[{"ref","x_mm","y_mm","orient"?}]}
    (or a single {ref,x_mm,y_mm,orient?}). One undoable commit."""
    d = request.get_json(force=True)
    moves = d.get("moves") or [d]
    by_ref = {m["ref"]: m for m in moves if m.get("ref")}
    k, b = board()
    upd, changed = [], []
    for f in b.get_footprints():
        try:
            ref = f.reference_field.text.value
        except Exception:
            continue
        m = by_ref.get(ref)
        if not m:
            continue
        f.position = Vector2.from_xy_mm(m["x_mm"], m["y_mm"])
        if m.get("orient") is not None:
            f.orientation = Angle.from_degrees(float(m["orient"]))
        upd.append(f); changed.append(ref)
    c = b.begin_commit()
    try:
        if upd:
            b.update_items(upd)
        b.push_commit(c, d.get("commit", "bridge: pcb move"))
    except Exception:
        b.drop_commit(c); raise
    return jsonify(ok=True, moved=changed, not_found=sorted(set(by_ref) - set(changed)))


@app.post("/pcb/import")
def pcb_import():
    """Update PCB from Schematic - INCREMENTAL full sync: save the schematic, export
    its netlist, then board.import_netlist matching by REFERENCE (R1<->R1) so a
    re-import UPDATES existing footprints in place (our live-regenerated schematics
    get fresh UUIDs, so UUID matching would dupe). delete_extra defaults TRUE so the
    board always equals the schematic - stale footprints from earlier schematic
    versions are removed (else the board accumulates cruft). Body opts: match
    ("reference" default | "uuid"), delete_extra (default true), update (default
    true), dry_run."""
    from kipy.proto.board.board_commands_pb2 import NetlistMatchMode
    d = request.get_json(force=True, silent=True) or {}
    mm = NetlistMatchMode.NMM_UUID if str(d.get("match", "reference")).lower() == "uuid" \
        else NetlistMatchMode.NMM_REFERENCE
    k, sch = schematic()
    sch.save()
    cli = k.get_kicad_binary_path("kicad-cli")
    net = os.path.join(tempfile.gettempdir(), "bridge_pcb.net")
    cmd = [cli, "sch", "export", "netlist", "--format", "kicadsexpr", "-o", net, _sch_file(sch)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if not os.path.exists(net):
        return jsonify(ok=False, error="netlist export failed",
                       stderr=(r.stderr or "")[-1500:]), 500
    _, b = board()
    res = b.import_netlist(net, dry_run=bool(d.get("dry_run")), match_mode=mm,
                           delete_extra_footprints=bool(d.get("delete_extra", True)),
                           update_footprints=bool(d.get("update", True)))
    return jsonify(ok=True, netlist=net, match="uuid" if mm == NetlistMatchMode.NMM_UUID else "reference",
                   footprints_after=len(b.get_footprints()), result=str(res)[:600])


@app.post("/pcb/check_footprints")
def pcb_check_footprints():
    """List schematic refs whose assigned footprint does NOT resolve in the board's
    footprint library table (these would be skipped on import). Runs a dry-run
    import and parses KiCad's own resolver. Returns bad:[{ref,footprint}]."""
    from kipy.proto.board.board_commands_pb2 import NetlistMatchMode
    k, sch = schematic()
    sch.save()
    cli = k.get_kicad_binary_path("kicad-cli")
    net = os.path.join(tempfile.gettempdir(), "bridge_pcb.net")
    cmd = [cli, "sch", "export", "netlist", "--format", "kicadsexpr", "-o", net, _sch_file(sch)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if not os.path.exists(net):
        return jsonify(ok=False, error="netlist export failed",
                       stderr=(r.stderr or "")[-1500:]), 500
    _, b = board()
    res = b.import_netlist(net, dry_run=True, match_mode=NetlistMatchMode.NMM_REFERENCE,
                           delete_extra_footprints=True)
    import re as _re
    bad = {}
    for m in _re.finditer(r"Cannot add (\S+) \(footprint '([^']+)' not found\)", res.report or ""):
        bad[m.group(1)] = m.group(2)
    return jsonify(ok=True, count=len(bad),
                   bad=[{"ref": r2, "footprint": f} for r2, f in sorted(bad.items())])


@app.post("/pcb/clear")
def pcb_clear():
    """Remove board items in ONE undoable commit. Default: all FOOTPRINTS (reset
    placement before a fresh re-import). Opts: tracks (also tracks+vias), zones,
    all (footprints+tracks+vias+zones+shapes+text). dry_run -> just count, no change."""
    d = request.get_json(force=True, silent=True) or {}
    _, b = board()
    items = list(b.get_footprints())
    if d.get("tracks") or d.get("all"):
        items += list(b.get_tracks()) + list(b.get_vias())
    if d.get("zones") or d.get("all"):
        items += list(b.get_zones())
    if d.get("all"):
        items += list(b.get_shapes()) + list(b.get_text())
    if d.get("dry_run"):
        return jsonify(ok=True, would_remove=len(items), dry_run=True)
    n = 0
    if items:
        c = b.begin_commit()
        try:
            b.remove_items(items)
            b.push_commit(c, d.get("commit", "bridge: pcb clear"))
            n = len(items)
        except Exception:
            b.drop_commit(c); raise
    return jsonify(ok=True, removed=n)


@app.post("/pcb/edge")
def pcb_edge():
    """Draw the board OUTLINE on Edge.Cuts (closed loop of line segments). Body:
    {"rect":[x,y,w,h]} | {"poly":[[x,y],...]} | {"segments":[[x1,y1,x2,y2],...]};
    width_mm (default 0.1); clear (wipe existing Edge.Cuts first). All mm."""
    from kipy.proto.board.board_types_pb2 import BoardLayer
    import kipy.board_types as bt
    d = request.get_json(force=True)
    _, b = board()
    EDGE = BoardLayer.BL_Edge_Cuts
    w_nm = from_mm(d.get("width_mm", 0.1))

    def seg(x1, y1, x2, y2):
        s = bt.BoardSegment(); s.layer = EDGE
        s.start = Vector2.from_xy_mm(x1, y1); s.end = Vector2.from_xy_mm(x2, y2)
        try:
            s.proto.shape.attributes.stroke.width.value_nm = w_nm
        except Exception:
            pass
        return s

    pts = None
    if d.get("rect"):
        x, y, w, h = d["rect"]; pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    elif d.get("poly"):
        pts = [tuple(p) for p in d["poly"]]
    items = []
    if pts:
        for i in range(len(pts)):
            a = pts[i]; c2 = pts[(i + 1) % len(pts)]
            items.append(seg(a[0], a[1], c2[0], c2[1]))
    for s4 in d.get("segments", []):
        items.append(seg(*s4))
    if not items:
        return jsonify(ok=False, error="give rect / poly / segments"), 400
    commit = b.begin_commit()
    try:
        if d.get("clear"):
            old = [sh for sh in b.get_shapes() if sh.layer == EDGE]
            if old:
                b.remove_items(old)
        created = b.create_items(items)
        b.push_commit(commit, d.get("commit", "bridge: edge"))
    except Exception:
        b.drop_commit(commit); raise
    return jsonify(ok=True, created=len(created))


@app.post("/pcb/text")
def pcb_text():
    """Add text label(s) to the board (e.g. group/sheet names on a silk/comment
    layer). Body: {"texts":[{"text","x_mm","y_mm","size_mm"?,"layer"?}]} or a single
    {text,x_mm,y_mm,...}. layer: untranslated name (default "F.Silkscreen"); also
    accepts "Cmts.User"/"User.Comments". clear -> wipe board text on that layer first."""
    from kipy.proto.board.board_types_pb2 import BoardLayer
    import kipy.board_types as bt
    d = request.get_json(force=True)
    _, b = board()
    lname = d.get("layer", "F.Silkscreen")
    layer = {"F.Silkscreen": BoardLayer.BL_F_SilkS,
             "B.Silkscreen": BoardLayer.BL_B_SilkS,
             "Cmts.User": BoardLayer.BL_Cmts_User,
             "User.Comments": BoardLayer.BL_Cmts_User,
             "F.Fab": BoardLayer.BL_F_Fab}.get(lname, BoardLayer.BL_F_SilkS)
    texts = d.get("texts") or [d]
    items = []
    for t in texts:
        bx = bt.BoardText()
        bx.layer = layer
        bx.value = t["text"]
        bx.position = Vector2.from_xy_mm(t["x_mm"], t["y_mm"])
        s = t.get("size_mm", 2.0)
        try:
            bx.attributes.size = Vector2.from_xy_mm(s, s)
            bx.attributes.stroke_width.value_nm = from_mm(t.get("thick_mm", s * 0.15))
        except Exception:
            pass
        items.append(bx)
    commit = b.begin_commit()
    try:
        if d.get("clear"):
            old = [x for x in b.get_text() if int(x.layer) == int(layer)]
            if old:
                b.remove_items(old)
        created = b.create_items(items)
        b.push_commit(commit, d.get("commit", "bridge: pcb text"))
    except Exception:
        b.drop_commit(commit); raise
    return jsonify(ok=True, created=len(created))


@app.post("/pcb/save")
def pcb_save():
    _, b = board()
    b.save()
    return jsonify(ok=True)


def _board_file(b):
    bf = b.name                                  # board_filename (may be relative)
    if not os.path.isabs(bf):
        bf = os.path.join(b.get_project().path, bf)
    return bf


@app.post("/pcb/plot")
def pcb_plot():
    """Render the PCB to PDF via kicad-cli (live counterpart to schematic /plot),
    base64 in files:[{name,b64}]. Defaults to a top-side placement view (F.Cu +
    silkscreen + edge, single page, with border). Opts: layers (CSV untranslated
    names e.g. F.Cu,B.Cu,Edge.Cuts), mode (single|multipage|separate), mirror,
    black_and_white."""
    d = request.get_json(force=True, silent=True) or {}
    k, b = board()
    # board.save() is broken in this nightly's API ("document not open"), so we
    # SERIALIZE the live in-memory board (get_as_string) to a temp .kicad_pcb and
    # plot that - reflects uncommitted live moves without needing a disk save.
    cli = k.get_kicad_binary_path("kicad-cli")
    pcbdir = tempfile.mkdtemp(prefix="bridge_pcbsrc_")
    boardfile = os.path.join(pcbdir, "live.kicad_pcb")
    with open(boardfile, "w", encoding="utf-8") as f:
        f.write(b.get_as_string())
    layers = d.get("layers", "F.Cu,F.Silkscreen,Edge.Cuts")
    mode = d.get("mode", "single")
    outdir = tempfile.mkdtemp(prefix="bridge_pcbplot_")
    out = os.path.join(outdir, "board.pdf")
    cmd = [cli, "pcb", "export", "pdf", "--" + ("mode-" + mode), "-l", layers,
           "--include-border-title", "-o", out, boardfile]
    if d.get("mirror"):
        cmd.append("--mirror")
    if d.get("black_and_white"):
        cmd.append("--black-and-white")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    files = []
    if os.path.isfile(out):
        files = [out]
    elif os.path.isdir(outdir):
        files = [os.path.join(outdir, f) for f in sorted(os.listdir(outdir)) if f.endswith(".pdf")]
    if not files:
        return jsonify(ok=False, exit_code=r.returncode, stderr=(r.stderr or "")[-2000:]), 500
    blobs = []
    for fp in files:
        with open(fp, "rb") as f:
            blobs.append({"name": os.path.basename(fp),
                          "b64": base64.b64encode(f.read()).decode("ascii")})
    return jsonify(ok=True, layers=layers, mode=mode, files=blobs,
                   stderr=(r.stderr or "")[-800:])


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


def _is_power_net(name):
    """Classify a net name as a power/supply rail (conservative: +/- prefix or a
    known ground/supply token, to avoid flagging signal nets)."""
    import re as _re
    n = (name or "").strip()
    if _re.match(r'^[+-][0-9A-Za-z]', n):
        return True
    return n.upper() in {"GND", "GNDA", "AGND", "DGND", "PGND",
                         "VCC", "VDD", "VEE", "VSS", "VBAT"}


def _parse_netlist(text):
    """Parse a kicadsexpr netlist -> (ref->sheet, [nets]). Each net:
    {name, pins, sheets:[sheetpath names]}. Tolerant of tab/newline layout
    (comp/net entries have a newline after the tag)."""
    import re as _re
    ci = text.find("(components"); li = text.find("(libparts"); ni = text.find("(nets")
    comps_txt = text[ci:(li if li > 0 else ni)] if ci >= 0 else ""
    nets_txt = text[ni:] if ni >= 0 else ""
    ref_sheet = {}
    for blk in comps_txt.split("(comp")[1:]:
        mref = _re.search(r'\(ref\s+"([^"]+)"', blk)
        msh = _re.search(r'\(sheetpath\s*\(names\s+"([^"]*)"', blk)
        if mref:
            ref_sheet[mref.group(1)] = msh.group(1) if msh else "/"
    nets = []
    for blk in _re.split(r'\(net\b', nets_txt)[1:]:
        mname = _re.search(r'\(name\s+"([^"]*)"', blk)
        if not mname:
            continue
        refs = _re.findall(r'\(node\s*\(ref\s+"([^"]+)"', blk)
        nets.append({"name": mname.group(1), "pins": len(refs),
                     "sheets": sorted({ref_sheet.get(r, "?") for r in refs})})
    return ref_sheet, nets


@app.post("/audit_nets")
def audit_nets():
    """HIERARCHY-LEVEL net audit (catches what the per-sheet validator can't):
    export the full netlist and flag POWER rails that are produced on ONE sheet but
    not consumed elsewhere. Classic failure: the POWER sheet builds per-domain rails
    (+5V_CH1, -5V_CH2, ...) but the channel sheets pull the GENERIC rail (+5V), so
    the domained rails dangle and the channels share one supply despite a separate-
    supply design. A power rail on a single sheet whose generic base rail is consumed
    on OTHER sheets is an ERROR (the consumers likely belong on the domained name);
    a single-sheet power net with no generic base is a WARN (verify it is an
    intentionally-local supply). Also flags any net with < 2 pins (dangling)."""
    k, sch = schematic()
    sch.save()
    cli = k.get_kicad_binary_path("kicad-cli")
    net = os.path.join(tempfile.gettempdir(), "bridge_audit.net")
    cmd = [cli, "sch", "export", "netlist", "--format", "kicadsexpr", "-o", net, _sch_file(sch)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if not os.path.exists(net):
        return jsonify(ok=False, error="netlist export failed",
                       stderr=(r.stderr or "")[-1500:]), 500
    with open(net, encoding="utf-8") as f:
        text = f.read()
    ref_sheet, nets = _parse_netlist(text)
    pnames = {n["name"] for n in nets if _is_power_net(n["name"])}
    power_findings, dangling = [], []
    for n in nets:
        if n["pins"] < 2:
            dangling.append({"net": n["name"], "pins": n["pins"], "sheets": n["sheets"]})
        if not _is_power_net(n["name"]) or len(n["sheets"]) >= 2:
            continue
        cands = [g for g in pnames if g != n["name"]
                 and n["name"].startswith(g) and len(n["name"]) > len(g)]
        base = min(cands, key=len) if cands else None
        gsheets = next((m["sheets"] for m in nets if m["name"] == base), []) if base else []
        power_findings.append({
            "net": n["name"], "sheets": n["sheets"], "pins": n["pins"],
            "base_rail": base, "base_on_sheets": gsheets,
            "severity": "ERROR" if base else "WARN",
            "why": (f"domained rail produced on {n['sheets']} but consumed nowhere; "
                    f"generic base '{base}' is used on {gsheets} - those consumers "
                    f"likely belong on '{n['name']}'") if base else
                   "power net confined to one sheet (verify it is an intentionally-"
                   "local supply, else it should be distributed)"})
    errors = sum(1 for f in power_findings if f["severity"] == "ERROR")
    warns = sum(1 for f in power_findings if f["severity"] == "WARN")
    sheets = sorted(set(ref_sheet.values()))
    return jsonify(ok=True, sheets=sheets, net_count=len(nets), comp_count=len(ref_sheet),
                   errors=errors, warnings=warns,
                   power_distribution=sorted(power_findings,
                                             key=lambda z: (z["severity"] != "ERROR", z["net"])),
                   dangling=dangling)


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


@app.post("/plot")
def plot_doc():
    """Save the open schematic, then render it with `kicad-cli sch export <fmt>`
    (same KiCad install, headless) and return the bytes base64-encoded. This is
    the LIVE counterpart to /erc: a worker gets a PDF/SVG of the EXACT open layout
    over HTTP, without reaching the Windows filesystem. Exporting the ROOT sheet
    pulls every hierarchical page into one multi-page PDF. fmt: pdf (default)|svg.
    Client: POST {"fmt":"pdf"} -> {ok, files:[{name, b64}]}; decode b64 to a file."""
    d = request.get_json(force=True, silent=True) or {}
    fmt = (d.get("fmt") or "pdf").lower()
    if fmt not in ("pdf", "svg"):
        return jsonify(ok=False, error="fmt must be pdf or svg"), 400
    k, sch = schematic()
    sch.save()
    schfile = _sch_file(sch)
    cli = k.get_kicad_binary_path("kicad-cli")
    outdir = tempfile.mkdtemp(prefix="bridge_plot_")
    if fmt == "pdf":
        out = os.path.join(outdir, "out.pdf")
        cmd = [cli, "sch", "export", "pdf", "-o", out, schfile]
    else:
        out = outdir
        cmd = [cli, "sch", "export", "svg", "--no-background-color", "-o", outdir, schfile]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if fmt == "pdf":
        files = [os.path.join(outdir, "out.pdf")] if os.path.exists(out) else []
    else:
        files = [os.path.join(outdir, f) for f in sorted(os.listdir(outdir)) if f.endswith(".svg")]
    if not files:
        return jsonify(ok=False, exit_code=r.returncode, schfile=schfile,
                       stderr=(r.stderr or "")[-2000:]), 500
    blobs = []
    for fp in files:
        with open(fp, "rb") as f:
            blobs.append({"name": os.path.basename(fp),
                          "b64": base64.b64encode(f.read()).decode("ascii")})
    return jsonify(ok=True, fmt=fmt, schfile=schfile, files=blobs,
                   stderr=(r.stderr or "")[-1000:])


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


# standard page sizes in mm, landscape (w, h); PS_USER reads user_page_size
_PAGE_MM = {1: (210, 148), 2: (297, 210), 3: (420, 297), 4: (594, 420),
            5: (841, 594), 6: (1189, 841), 7: (279.4, 215.9), 8: (431.8, 279.4),
            9: (558.8, 431.8), 10: (863.6, 558.8), 11: (1117.6, 863.6),
            13: (279.4, 215.9), 14: (355.6, 215.9), 15: (431.8, 279.4)}


@app.get("/page")
def page():
    """Return the open sheet's drawing-area size in mm (paper size + orientation)."""
    _, sch = schematic()
    p = sch.get_page_settings().proto
    if p.page_size == 16 and p.user_page_size.x_nm:        # PS_USER
        w, h = p.user_page_size.x_nm / 1e6, p.user_page_size.y_nm / 1e6
    else:
        w, h = _PAGE_MM.get(p.page_size, (297, 210))
    if p.orientation == 2:                                 # PO_PORTRAIT
        w, h = min(w, h), max(w, h)
    else:
        w, h = max(w, h), min(w, h)
    return jsonify(ok=True, width_mm=round(w, 1), height_mm=round(h, 1),
                   page_size=p.page_size, orientation=p.orientation)


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
