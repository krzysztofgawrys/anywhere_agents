"""
kicad_bridge.py - cienki most HTTP miedzy workerem (Linux) a otwartym KiCadem.

Uruchamiasz na WINDOWS, w venv312 (kipy + flask), z otwartym nightly i
wlaczonym API (Preferences > Plugins > Enable KiCad API):

    pip install flask
    python kicad_bridge.py        # slucha na 0.0.0.0:8765

Worker na Linuksie wola endpointy ponizej. Kazda mutacja idzie w commit
(undo w GUI). Jednostki w mm; pozycje symboli zwracane w mm.
"""
from flask import Flask, request, jsonify
from kipy import KiCad
import kipy.schematic_types as st
from kipy.geometry import Vector2

app = Flask(__name__)


@app.errorhandler(Exception)
def _on_error(e):
    return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


def schematic():
    # swieze polaczenie na zadanie - odporne na restart KiCada / zmiane dokumentu
    k = KiCad()
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
    out = [dict(ref=_field_text(s.reference_field),
                value=_field_text(s.value_field),
                x_mm=s.position.x / 1e6, y_mm=s.position.y / 1e6)
           for s in sch.get_symbols()]
    return jsonify(symbols=out)


@app.post("/text")
def add_text():
    d = request.get_json(force=True)
    _, sch = schematic()
    t = st.SchematicText()
    t.value = d["text"]
    t.position = Vector2.from_xy_mm(d["x_mm"], d["y_mm"])
    s = d.get("size_mm", 1.27)
    t.text.attributes.size = Vector2.from_xy_mm(s, s)
    created = _commit(sch, t, d.get("commit", "bridge: text"))
    return jsonify(ok=True, created=len(created))


@app.post("/label")
def add_label():
    d = request.get_json(force=True)
    _, sch = schematic()
    lbl = st.GlobalLabel() if d.get("global") else st.LocalLabel()
    lbl.text.value = d["text"]
    lbl.text.attributes.size = Vector2.from_xy_mm(1.27, 1.27)
    lbl.position = Vector2.from_xy_mm(d["x_mm"], d["y_mm"])
    created = _commit(sch, lbl, d.get("commit", "bridge: label"))
    return jsonify(ok=True, created=len(created))



@app.post("/symbol")
def add_symbol():
    """Place a library symbol by CLONING an existing placed instance of the same
    lib_id. KiCad cannot materialize a symbol from a lib_id alone (it returns an
    empty item), so a part of this type must already be on the sheet."""
    d = request.get_json(force=True)
    _, sch = schematic()
    lib, name = d["lib_id"].split(":", 1)
    src = next((s for s in sch.get_symbols()
                if s.definition.id.library == lib and s.definition.id.name == name), None)
    if src is None:
        return jsonify(ok=False, error=(
            f"no existing '{d['lib_id']}' on the sheet to clone; KiCad can't create "
            "a symbol from lib_id alone - place one of this type manually first")), 409
    inst = src.clone()
    inst.proto.ClearField("id")
    inst.position = Vector2.from_xy_mm(d["x_mm"], d["y_mm"])
    if d.get("reference"):
        inst.reference_field.text.value = d["reference"]
    if d.get("value"):
        inst.value_field.text.value = d["value"]
    return jsonify(ok=True, created=len(_commit(sch, inst, d.get("commit", "bridge: symbol"))))

if __name__ == "__main__":
    # PoC: 0.0.0.0 wystawia na LAN. Most MUTUJE schemat - patrz uwaga o bezp.
    app.run(host="0.0.0.0", port=8761)
