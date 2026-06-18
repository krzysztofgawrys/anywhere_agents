#!/usr/local/bin/python3
"""
placement.py - resolve LOGICAL layout hints into coordinates, then place live.

The agent gives each part a `role` (and optional `near`); this engine puts them
in I/O bands (input left -> core -> output right), clusters `near` parts beside
their anchor, spaces by real symbol bounding box (no overlaps), and places via
the bridge. Power symbols (GND/+V/-V) go in a band by sign.

netlist part fields: ref, lib_id, value, role, near?
  role: input | core | output | decoupling | power | connector_in | connector_out
"""
import json, sys, base64, urllib.request, urllib.error
import kicad_live as KL
import base64
from kipy.proto.schematic import schematic_types_pb2 as S2

GRID = 1.27
def snap(v): return round(v / GRID) * GRID


def bbox(sym):
    """(xmin,xmax,ymin,ymax) in lib mm: pin connection points + graphics extent."""
    xs, ys = [], []
    for p in sym["pins"]:
        xs.append(p["x"]); ys.append(p["y"])
    for g in sym["graphics"]:
        cls = type(g).__name__
        if cls == "SyPolyLine":
            for q in g.points: xs.append(q.X); ys.append(q.Y)
        elif cls == "SyRect":
            xs += [g.start.X, g.end.X]; ys += [g.start.Y, g.end.Y]
        elif cls == "SyCircle":
            xs += [g.center.X - g.radius, g.center.X + g.radius]
            ys += [g.center.Y - g.radius, g.center.Y + g.radius]
        elif cls == "SyArc":
            xs += [g.start.X, g.mid.X, g.end.X]; ys += [g.start.Y, g.mid.Y, g.end.Y]
    if not xs: xs = [0]; ys = [0]
    return min(xs), max(xs), min(ys), max(ys)


def _wh(sym):
    x0, x1, y0, y1 = bbox(sym)
    return (x1 - x0), (y1 - y0)


parts_spec_nets=[[]]
def layout(parts, libdirs, origin=(80.0, 80.0), col_gap=35.0, row_gap=10.0):
    """Return {ref: (x_mm, y_mm)} for the parts, by role bands."""
    syms = {p["ref"]: KL.load_symbol(p["lib_id"], libdirs) for p in parts}
    pos = {}
    ox, oy = origin
    # columns by role
    COL = {"connector_in": 0, "input": 0, "core": 1, "output": 2, "connector_out": 2}
    col_x = {0: ox, 1: ox + col_gap, 2: ox + 2 * col_gap}
    # main parts first (skip near/decoupling/power - placed relative after)
    main = [p for p in parts if p.get("role") in COL and not p.get("near")]
    cursor = {0: oy, 1: oy, 2: oy}
    for p in sorted(main, key=lambda p: COL[p["role"]]):
        c = COL[p["role"]]
        w, h = _wh(syms[p["ref"]])
        x = col_x[c]
        y = cursor[c] + h / 2
        pos[p["ref"]] = (snap(x), snap(y))
        cursor[c] += h + row_gap
    # `near` parts: place OUTWARD from the anchor pins they share a net with.
    # nets tell us which anchor pins each near-part connects to -> we put the
    # part on that side (Rg by U1's Rg pins on the left, decoupling at V+/V-).
    nets = parts_spec_nets[0]
    def anchor_pins_for(part_ref, anchor_ref):
        """lib (x,y) of anchor pins sharing a net with part_ref."""
        out = []
        for net in nets:
            pins = net.get("pins", [])
            refs = [pp.split(".")[0] for pp in pins]
            if part_ref in refs and anchor_ref in refs:
                for pp in pins:
                    r, n = pp.split(".", 1)
                    if r == anchor_ref:
                        for ap in syms[anchor_ref]["pins"]:
                            if str(ap["number"]) == n:
                                out.append((ap["x"], ap["y"]))
        return out
    # group near-parts by anchor+side so multiples on one side stack
    side_bucket = {}
    for p in parts:
        if not p.get("near") or p["near"] not in pos:
            continue
        a = p["near"]; aps = anchor_pins_for(p["ref"], a)
        if not aps:                      # no net info -> fall back: right of anchor
            cx, cy = (_wh(syms[a])[0] / 2 + 8.0, 0.0)
        else:
            cx = sum(q[0] for q in aps) / len(aps); cy = sum(q[1] for q in aps) / len(aps)
        import math
        mag = math.hypot(cx, cy) or 1.0
        margin = 10.0
        plx = cx + margin * cx / mag; ply = cy + margin * cy / mag
        side = (a, round(plx, 1), round(ply, 1) > 0)
        side_bucket.setdefault(side, []).append((p, a, plx, ply, cx, cy))
    for side, group in side_bucket.items():
        for i, (p, a, plx, ply, cx, cy) in enumerate(group):
            ax, ay = pos[a]
            # stack extras perpendicular to the outward direction
            off = (i - (len(group) - 1) / 2) * 7.62
            perp = (-ply, plx)
            import math
            pm = math.hypot(*perp) or 1.0
            sx = plx + off * perp[0] / pm; sy = ply + off * perp[1] / pm
            pos[p["ref"]] = (snap(ax + sx), snap(ay - sy))   # schematic Y = anchor - lib_y
    return pos, syms


def place_all(spec, bridge_url):
    libdirs = spec["libdirs"]
    bridge = KL.Bridge(bridge_url)
    h = bridge.health(); print("sheet:", h.get("project"), h.get("symbols"), "symbols")
    parts_spec_nets[0] = spec.get("nets", [])
    pos, syms = layout(spec["parts"], libdirs)
    scaffold = open(KL.SCAFFOLD, "rb").read()
    for p in spec["parts"]:
        if p["ref"] not in pos:
            print("  SKIP (no role/pos):", p["ref"]); continue
        x, y = pos[p["ref"]]
        b64 = KL.build_symbol_proto(syms[p["ref"]], p["ref"], p.get("value", ""), x, y, scaffold)
        r = bridge.place_raw(b64, x, y, p["ref"], p.get("value", ""))
        print(f"  {'OK ' if r.get('ok') else 'ERR'} {p['ref']:>4} {p.get('role',''):<12} @({x:.1f},{y:.1f}) {r.get('created', r)}")
    route(spec, bridge, pos, syms)




# ---------------- routing ----------------
import re as _re
_POWER = _re.compile(r"^(gnd|ground|vcc|vdd|vee|vss|vbus|v\+|v-|[+-]?\d+v\d*)$", _re.I)
_SPO = {0: "R", 90: "U", 180: "L", 270: "D"}

def _pin_abs(ref_pin, pos, syms):
    ref, pin = ref_pin.split(".", 1)
    if ref not in pos: return None, None
    px, py = pos[ref]
    for p in syms[ref]["pins"]:
        if str(p["number"]) == pin:
            return (px + p["x"], py - p["y"]), _SPO.get(p["angle"], "R")
    return None, None

def _l_route(a, b, orient=None):
    if abs(a[0]-b[0]) < 1e-6 or abs(a[1]-b[1]) < 1e-6:
        return [(a, b)]
    c = (b[0], a[1])                     # horizontal first (stub ends already off-pin)
    return [(a, c), (c, b)]


_FLAG_CACHE = {}
def _flag_orient(psym, lib_id, pin_dir):
    """Pick the orient that makes the power-flag graphic point in pin_dir (R/L/U/D)
    so the flag sits beyond the pin, in the pin's outward direction."""
    if lib_id not in _FLAG_CACHE:
        sc = open(KL.SCAFFOLD, "rb").read(); m = {}
        for o in (0, 90, 180, 270):
            b = base64.b64decode(KL.build_symbol_proto(psym, "#X", "x", 100, 100, sc, orient=o))
            inst = S2.SchematicSymbolInstance(); inst.ParseFromString(b)
            xs = []; ys = []
            for c in inst.definition.items:
                if c.item.Is(S2.SchematicGraphicShape.DESCRIPTOR):
                    g = S2.SchematicGraphicShape(); c.item.Unpack(g)
                    if g.shape.HasField("polygon"):
                        for n in g.shape.polygon.polygons[0].outline.nodes:
                            xs.append(n.point.x_nm); ys.append(n.point.y_nm)
            if xs:
                cx = sum(xs) / len(xs); cy = sum(ys) / len(ys)
                d = ("R" if cx > 3e5 else "L" if cx < -3e5 else
                     "D" if cy > 3e5 else "U" if cy < -3e5 else "?")
                m[d] = o
        _FLAG_CACHE[lib_id] = m
    return _FLAG_CACHE[lib_id].get(pin_dir, 0)


def route(spec, bridge, pos, syms):
    """Power nets -> a power symbol (GND/V+/V-) flag on every pin (no wire).
    Other nets -> wires that LEAVE every pin along its axis (a stub outward),
    then connect the stub ends orthogonally - so a wire never enters a pin
    through the component body."""
    STUB = 2.54
    OUT = {"R": (1, 0), "L": (-1, 0), "U": (0, -1), "D": (0, 1)}   # schematic dirs
    scaffold = open(KL.SCAFFOLD, "rb").read()
    wires = flags = 0
    pwr_idx = [0]
    def stub_end(a, o):
        dx, dy = OUT.get(o, (1, 0)); return (a[0] + STUB * dx, a[1] + STUB * dy)
    for net in spec.get("nets", []):
        pins = [( _pin_abs(pp, pos, syms), pp) for pp in net.get("pins", [])]
        pins = [(a, o, pp) for (a, o), pp in pins if a is not None]
        pw = net.get("power")
        if pw:                                   # power flag: IRON RULE - GND/positive=0, negative=180
            psym = KL.load_symbol(pw, spec["libdirs"])
            nm = pw.split(":")[-1].upper()
            negative = nm.startswith("-") or nm in ("VEE", "VSS", "V-")
            for a, o, pp in pins:
                pwr_idx[0] += 1
                ref = f"#PWR{pwr_idx[0]:02d}"
                if negative:                       # 0 if ABOVE the element, 180 if BELOW
                    owner = pp.split(".")[0]; ocy = pos.get(owner, (a[0], a[1]))[1]
                    fo = 0 if a[1] < ocy else 180
                else:
                    fo = 0                          # GND + positive: always 0
                b64 = KL.build_symbol_proto(psym, ref, net["name"], a[0], a[1], scaffold, orient=fo)
                if bridge.place_raw(b64, a[0], a[1], ref, net["name"]).get("ok"): flags += 1
            continue
        if len(pins) < 2:
            continue
        # IRON RULE 6: wire exits the pin TIP outward (radially away from the part
        # body), never through the component. Direction = pin - component center.
        def radial_stub(a, ref_pin):
            owner = ref_pin.split(".")[0]
            cx, cy = pos.get(owner, (a[0], a[1]))
            dx, dy = a[0] - cx, a[1] - cy
            if abs(dx) >= abs(dy):
                u = (STUB if dx >= 0 else -STUB, 0)
            else:
                u = (0, STUB if dy >= 0 else -STUB)
            return (a[0] + u[0], a[1] + u[1])
        stubs = [(a, radial_stub(a, pp), o) for a, o, pp in pins]
        # draw each pin's stub
        for a, se, o in stubs:
            if bridge._post("/wire", {"x1": a[0], "y1": a[1], "x2": se[0], "y2": se[1]}).get("ok"): wires += 1
        # connect stub ends: star from stubs[0]. IRON RULE: the segment ARRIVING
        # at a stub-end must be perpendicular to that stub, so the stub is the
        # sole colinear path into the pin tip (no overshoot past the pin).
        s0 = stubs[0][1]
        for a, se, o in stubs[1:]:
            vert = abs(se[1] - a[1]) > abs(se[0] - a[0])   # target stub vertical?
            if abs(s0[0]-se[0]) < 1e-6 or abs(s0[1]-se[1]) < 1e-6:
                segs = [(s0, se)]
            elif vert:                                     # approach vertical stub horizontally
                corner = (s0[0], se[1]); segs = [(s0, corner), (corner, se)]
            else:                                          # approach horizontal stub vertically
                corner = (se[0], s0[1]); segs = [(s0, corner), (corner, se)]
            for (p, q) in segs:
                if bridge._post("/wire", {"x1": p[0], "y1": p[1], "x2": q[0], "y2": q[1]}).get("ok"): wires += 1
        if len(stubs) > 2:
            bridge._post("/junction", {"x": s0[0], "y": s0[1]})
    print(f"  routed: {wires} wire segs, {flags} power flags")


if __name__ == "__main__":
    spec = json.load(open(sys.argv[1]))
    url = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--bridge" else "http://172.23.48.1:8761"
    place_all(spec, url)
