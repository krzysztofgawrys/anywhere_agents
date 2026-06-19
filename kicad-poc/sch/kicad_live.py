#!/usr/local/bin/python3
"""
kicad_live.py - place real library components into the OPEN KiCad schematic,
LIVE, via the bridge. The agent only supplies a netlist (same shape as the
file-based skill); this module does ALL the geometry/proto work.

  kicad_live.py push netlist.json [--bridge http://HOST:8761] [--libdirs /opt/kicad-symbols]

netlist.json: {"parts":[{"ref","lib_id","value","x_mm","y_mm"}],
               "nets":[{"name","pins":[...]}], "libdirs":[...]}

Requires: kiutils, kicad-python (kipy protos). Needs the bridge running on the
machine where KiCad is open, and scaffold.bin next to this file.
"""
import base64, copy, json, os, sys, urllib.request, urllib.error
from kiutils.symbol import SymbolLib
from kipy.proto.schematic import schematic_types_pb2 as S
from kipy.proto.common.types import base_types_pb2 as B

MM = 1_000_000
HERE = os.path.dirname(os.path.abspath(__file__))
SCAFFOLD = os.path.join(HERE, "scaffold.bin")

EPT = {"input": 1, "output": 2, "bidirectional": 3, "tri_state": 4, "passive": 5,
       "free": 6, "unspecified": 7, "power_in": 8, "power_out": 9,
       "open_collector": 10, "open_emitter": 11, "no_connect": 12}
SPO = {0: 1, 90: 3, 180: 2, 270: 4}          # lib pin angle -> SchematicPinOrientation
FILL = {"none": 1, "outline": 2, "background": 4}   # kicad_sym fill type -> GraphicFillType



def _rot(x, y, deg):
    """rotate lib coords CCW in lib space (Y-up) by deg in {0,90,180,270}."""
    d = deg % 360
    if d == 0:   return (x, y)
    if d == 90:  return (-y, x)
    if d == 180: return (-x, -y)
    return (y, -x)             # 270

_SSO = {0: 1, 90: 2, 180: 3, 270: 4}    # SchematicSymbolOrientation enum

def _d(mm):
    x = B.Distance(); x.value_nm = int(round(mm * MM)); return x


# ---------- .kicad_sym parsing (follows `extends`) ----------
def _load_symdir(libdirs, nick, name):
    for d in libdirs:
        for cand in (os.path.join(d, f"{nick}.kicad_symdir", f"{name}.kicad_sym"),
                     os.path.join(d, f"{nick}.kicad_sym")):
            if os.path.isfile(cand):
                for s in SymbolLib.from_file(cand).symbols:
                    if s.entryName == name:
                        return s, cand
    return None, None


def _match_local(p, name):
    """Resolve a symbol by entryName from a local_libs entry: a .kicad_sym FILE or
    a DIRECTORY of per-symbol .kicad_sym files."""
    files = []
    if os.path.isdir(p):
        files = [os.path.join(p, f) for f in sorted(os.listdir(p)) if f.endswith(".kicad_sym")]
    elif os.path.isfile(p):
        files = [p]
    for fp in files:
        for s in SymbolLib.from_file(fp).symbols:
            if s.entryName == name:
                return s
    return None


def load_symbol(lib_id, libdirs, local_libs=None):
    """Return a dict: {pins:[...], graphics:[...], props:{name:(x,y)}}, following extends.
    Resolves the nick via local_libs (nick -> .kicad_sym file OR a directory of them)
    first, otherwise via libdirs (<Nick>.kicad_symdir/<Name>.kicad_sym or flat
    <Nick>.kicad_sym) - matching the file-mode generator's resolver."""
    local_libs = local_libs or {}
    nick, name = lib_id.split(":", 1)
    if nick in local_libs:
        sym = _match_local(local_libs[nick], name)
        if sym is None:
            raise SystemExit(f"{lib_id}: no symbol {name!r} under {local_libs[nick]}")
    else:
        sym, _ = _load_symdir(libdirs, nick, name)
        if sym is None:
            raise SystemExit(f"{lib_id}: not found in {libdirs} / {list(local_libs)}")
    # gather pins+graphics across units; follow extends for geometry
    def collect(sym):
        pins, gfx = [], []
        for u in sym.units:
            for p in u.pins:
                pins.append(dict(number=p.number, name=p.name,
                                 x=p.position.X, y=p.position.Y,
                                 angle=int(p.position.angle or 0),
                                 length=p.length, etype=(p.electricalType or "passive")))
            for g in u.graphicItems:
                gfx.append(g)
        return pins, gfx
    pins, gfx = collect(sym)
    walk, guard = sym, 0
    while not pins and getattr(walk, "extends", None) and guard < 6:
        if nick in local_libs:
            parent = _match_local(local_libs[nick], walk.extends)
        else:
            parent, _ = _load_symdir(libdirs, nick, walk.extends)
        if parent is None:
            break
        pins, gfx = collect(parent)
        walk = parent
        guard += 1
    props = {}
    for pr in sym.properties:          # fields from the resolved (derived) symbol
        props[pr.key] = (pr.position.X, pr.position.Y)
    return dict(nick=nick, name=name, pins=pins, graphics=gfx, props=props)


# ---------- proto building ----------
def _set_polygon(gshape, pts_lib, closed_fill):
    pl = gshape.polygon.polygons.add().outline
    for (x, y) in pts_lib:
        rx, ry = _rot(x, y, _ORIENT[0])
        n = pl.nodes.add(); n.point.x_nm = int(round(rx * MM)); n.point.y_nm = int(round(-ry * MM))
    pl.closed = False


_ORIENT = [0]
def _graphic_child(scaffold_gfx_child, g):
    """Translate a kiutils graphic item -> a SchematicSymbolChild(SchematicGraphicShape).
    Returns None for unsupported shapes."""
    c = copy.deepcopy(scaffold_gfx_child)
    gs = S.SchematicGraphicShape(); c.item.Unpack(gs); gs.ClearField("id")
    for f in ("rectangle", "segment", "circle", "arc", "polygon", "bezier"):
        gs.shape.ClearField(f)
    c.unit.unit = 0
    cls = type(g).__name__
    fill = getattr(getattr(g, "fill", None), "type", "none") or "none"
    stroke_w = 0.254
    try: stroke_w = float(g.stroke.width) or 0.254
    except Exception: pass
    def V(dst, X, Y):
        rx, ry = _rot(X, Y, _ORIENT[0])
        dst.x_nm = int(round(rx * MM)); dst.y_nm = int(round(-ry * MM))
    if cls == "SyPolyLine":
        _set_polygon(gs.shape, [(p.X, p.Y) for p in g.points], fill)
    elif cls == "SyRect":
        a = _rot(g.start.X, g.start.Y, _ORIENT[0]); b = _rot(g.end.X, g.end.Y, _ORIENT[0])
        xs = (a[0], b[0]); ys = (-a[1], -b[1])     # schematic frame (Y-down)
        gs.shape.rectangle.top_left.x_nm = int(round(min(xs) * MM))
        gs.shape.rectangle.top_left.y_nm = int(round(min(ys) * MM))
        gs.shape.rectangle.bottom_right.x_nm = int(round(max(xs) * MM))
        gs.shape.rectangle.bottom_right.y_nm = int(round(max(ys) * MM))
    elif cls == "SyCircle":
        V(gs.shape.circle.center, g.center.X, g.center.Y)
        V(gs.shape.circle.radius_point, g.center.X + g.radius, g.center.Y)   # point ON the circle
    elif cls == "SyArc":
        V(gs.shape.arc.start, g.start.X, g.start.Y)
        V(gs.shape.arc.mid, g.mid.X, g.mid.Y)
        V(gs.shape.arc.end, g.end.X, g.end.Y)
    else:
        return None  # SyText / beziers: TODO (rare in symbols)
    gs.shape.attributes.stroke.width.CopyFrom(_d(stroke_w))
    gs.shape.attributes.stroke.style = 1
    gs.shape.attributes.fill.fill_type = FILL.get(fill, 1)
    c.item.Pack(gs)
    return c


def build_symbol_proto(sym, ref, value, x_mm, y_mm, scaffold_bytes, orient=0, field_angle=None, field_pos=None):
    """Return base64 of a SchematicSymbolInstance for sym placed at (x_mm,y_mm)."""
    tx, ty = int(round(x_mm * MM)), int(round(y_mm * MM))
    tpl = S.SchematicSymbolInstance(); tpl.ParseFromString(scaffold_bytes)
    pin_tpl = next(c for c in tpl.definition.items if c.item.Is(S.SchematicPin.DESCRIPTOR))
    gfx_tpl = next(c for c in tpl.definition.items if c.item.Is(S.SchematicGraphicShape.DESCRIPTOR))
    inst = copy.deepcopy(tpl)
    inst.ClearField("id"); inst.ClearField("path")
    inst.position.x_nm = tx; inst.position.y_nm = ty
    d = inst.definition
    d.id.library_nickname = sym["nick"]; d.id.entry_name = sym["name"]
    del d.items[:]
    # graphics (relative, Y-flip)
    _ORIENT[0] = 0   # graphics stay lib-relative; KiCad transform rotates them
    for g in sym["graphics"]:
        try:
            child = _graphic_child(gfx_tpl, g)
            if child is not None:
                d.items.append(child)
        except Exception:
            pass   # skip a shape we cannot map rather than failing the whole symbol
    # pins (absolute = instance + (x,-y))
    for p in sym["pins"]:
        c = copy.deepcopy(pin_tpl); pp = S.SchematicPin(); c.item.Unpack(pp); pp.ClearField("id")
        c.unit.unit = 1
        rx, ry = _rot(p["x"], p["y"], orient)
        pp.number = str(p["number"]); pp.name = p["name"] or ""
        pp.position.x_nm = tx + int(round(rx * MM)); pp.position.y_nm = ty - int(round(ry * MM))
        pp.length.CopyFrom(_d(p["length"])); pp.orientation = SPO.get(p["angle"] % 360, 1)   # raw lib; KiCad rotates via transform
        pp.electrical_type = EPT.get(p["etype"], 5); pp.shape = 1
        c.item.Pack(pp); d.items.append(c)
    # fields (use lib property positions, Y-flipped)
    def setf(fld, val, key):
        fld.text.text = val
        if field_pos and key in field_pos:                       # absolute mm override (autoplaced)
            ax, ay = field_pos[key]
            fld.text.position.x_nm = int(round(ax * MM)); fld.text.position.y_nm = int(round(ay * MM))
        else:
            px, py = sym["props"].get(key, (2.54, 0.0))
            fld.text.position.x_nm = tx + int(round(px * MM)); fld.text.position.y_nm = ty - int(round(py * MM))
        fa = (-orient) if field_angle is None else field_angle
        fld.text.attributes.angle.value_degrees = float(fa)   # keep field text horizontal/upright
    setf(inst.reference_field, ref, "Reference")
    setf(inst.value_field, value, "Value")
    inst.transform.orientation = _SSO.get(orient % 360, 1)
    return base64.b64encode(inst.SerializeToString()).decode()


# ---------- bridge client ----------
class Bridge:
    def __init__(self, url):
        self.url = url.rstrip("/")
    def _post(self, ep, payload):
        req = urllib.request.Request(self.url + ep, json.dumps(payload).encode(),
                                     {"Content-Type": "application/json"})
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": e.read().decode()[:300], "http": e.code}
    def health(self):
        return json.load(urllib.request.urlopen(self.url + "/health", timeout=10))
    def place_raw(self, b64, x_mm, y_mm, ref, value):
        return self._post("/place_raw", {"proto_b64": b64, "x_mm": x_mm, "y_mm": y_mm,
                                          "reference": ref, "value": value})
    def label(self, text, x_mm, y_mm, global_=True):
        return self._post("/label", {"text": text, "x_mm": x_mm, "y_mm": y_mm, "global": global_})
    def clear(self):
        return self._post("/clear", {})
    def delete(self, refs):
        return self._post("/delete", {"refs": refs if isinstance(refs, list) else [refs]})
    def save(self):
        return self._post("/save", {})
    def erc(self):
        """Save the open schematic and run kicad-cli ERC headless; returns a
        flattened violation list (errors/warnings/violations)."""
        req = urllib.request.Request(self.url + "/erc", b"{}",
                                     {"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, timeout=200))   # ERC can be slow


# ---------- netlist push ----------
def _pin_abs(placed, ref_pin):
    """Absolute (x_mm,y_mm) of a 'REF.PIN' connection point, from tracked placements."""
    ref, pin = ref_pin.split(".", 1)
    p = placed.get(ref)
    if not p:
        return None
    x, y, sym = p
    for pn in sym["pins"]:
        if str(pn["number"]) == pin:
            return (x + pn["x"], y - pn["y"])   # (x, -y) transform, matches placed pins
    return None


def push_netlist_live(spec, bridge, libdirs, origin=(60.0, 60.0), dx=30.0, dy=30.0, cols=5):
    """Place every part (symbol with full geometry), then drop one net label on
    EACH pin of every net (connect-by-name -> KiCad merges them). Agent supplies
    only the netlist."""
    scaffold = open(SCAFFOLD, "rb").read()
    local_libs = spec.get("local_libs") or {}
    ox, oy = origin
    report = {"parts": [], "labels": 0, "label_errors": []}
    placed = {}
    for i, part in enumerate(spec["parts"]):
        x = part.get("x_mm", ox + (i % cols) * dx)
        y = part.get("y_mm", oy + (i // cols) * dy)
        try:
            sym = load_symbol(part["lib_id"], libdirs, local_libs)
            b64 = build_symbol_proto(sym, part["ref"], part.get("value", ""), x, y, scaffold)
            r = bridge.place_raw(b64, x, y, part["ref"], part.get("value", ""))
            if r.get("ok"):
                placed[part["ref"]] = (x, y, sym)
        except Exception as e:
            r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        report["parts"].append({"ref": part["ref"], "lib_id": part["lib_id"], "at": [x, y], "result": r})
    # I: pin-accurate net labels
    for net in spec.get("nets", []):
        for ref_pin in net.get("pins", []):
            pos = _pin_abs(placed, ref_pin)
            if pos is None:
                report["label_errors"].append(f"{net['name']}: {ref_pin} (pin not placed)")
                continue
            lr = bridge.label(net["name"], pos[0], pos[1], global_=True)
            if lr.get("ok"):
                report["labels"] += 1
            else:
                report["label_errors"].append(f"{net['name']}@{ref_pin}: {lr}")
    return report


def main(argv):
    if len(argv) < 2 or argv[1] != "push":
        print(__doc__); return 2
    spec_path = argv[2]
    bridge_url = "http://172.23.48.1:8761"
    libdirs = ["/opt/kicad-symbols"]
    a = argv[3:]
    while a:
        if a[0] == "--bridge": bridge_url = a[1]; a = a[2:]
        elif a[0] == "--libdirs": libdirs = a[1].split(","); a = a[2:]
        else: a = a[1:]
    spec = json.load(open(spec_path))
    libdirs = spec.get("libdirs", libdirs)
    base = os.path.dirname(os.path.abspath(spec_path))
    spec["local_libs"] = {k: (v if os.path.isabs(v) else os.path.join(base, v))
                          for k, v in (spec.get("local_libs") or {}).items()}
    bridge = Bridge(bridge_url)
    h = bridge.health(); print("bridge:", h.get("project"), h.get("symbols"), "symbols")
    rep = push_netlist_live(spec, bridge, libdirs)
    for r in rep["parts"]:
        ok = r["result"].get("ok")
        print(f"  {'OK ' if ok else 'ERR'} {r['ref']:>4} {r['lib_id']:<34} -> {r['result'].get('created', r['result'])}")
    print(f"  net labels placed: {rep['labels']}" + (f"  (errors: {rep['label_errors']})" if rep['label_errors'] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
