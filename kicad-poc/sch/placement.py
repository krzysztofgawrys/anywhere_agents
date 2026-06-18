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
import json, sys, os, base64, urllib.request, urllib.error
import kicad_live as KL
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
_local_libs=[{}]          # nick -> .kicad_sym file/dir, set by place_all (parity with kicad_live)
def layout(parts, libdirs, origin=(80.0, 80.0), col_gap=35.0, row_gap=10.0, local_libs=None):
    """Return {ref: (x_mm, y_mm)} for the parts, by role bands."""
    local_libs = local_libs if local_libs is not None else _local_libs[0]
    syms = {p["ref"]: KL.load_symbol(p["lib_id"], libdirs, local_libs) for p in parts}
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
    _local_libs[0] = spec.get("local_libs") or {}
    pos, syms = layout(spec["parts"], libdirs, local_libs=_local_libs[0])
    scaffold = open(KL.SCAFFOLD, "rb").read()
    # plan all geometry BEFORE placing, so fields can dodge wires/flags/bodies
    wire_segs, flags, juncs = plan_geometry(spec, pos, syms)
    obs = build_obstacles(spec, pos, syms, wire_segs, flags)

    # main symbols, with each Reference/Value auto-placed clear of obstacles
    for p in spec["parts"]:
        if p["ref"] not in pos:
            print("  SKIP (no role/pos):", p["ref"]); continue
        x, y = pos[p["ref"]]; sym = syms[p["ref"]]
        fp = autoplace_fields(sym, x, y, p["ref"], p.get("value", ""), obs)
        b64 = KL.build_symbol_proto(sym, p["ref"], p.get("value", ""), x, y, scaffold, field_pos=fp)
        r = bridge.place_raw(b64, x, y, p["ref"], p.get("value", ""))
        print(f"  {'OK ' if r.get('ok') else 'ERR'} {p['ref']:>4} {p.get('role',''):<12} @({x:.1f},{y:.1f}) {r.get('created', r)}")

    # power flags, value field auto-placed too (Reference #PWRxx is hidden)
    nflag = 0
    for f in flags:
        psym = KL.load_symbol(f["libid"], libdirs, _local_libs[0])
        fp = autoplace_fields(psym, f["x"], f["y"], f["ref"], f["value"], obs, orient=f["orient"])
        b64 = KL.build_symbol_proto(psym, f["ref"], f["value"], f["x"], f["y"], scaffold,
                                    orient=f["orient"], field_angle=0, field_pos=fp)
        if bridge.place_raw(b64, f["x"], f["y"], f["ref"], f["value"]).get("ok"): nflag += 1

    # wires + junctions last
    nw = 0
    for (a, b) in wire_segs:
        if bridge._post("/wire", {"x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]}).get("ok"): nw += 1
    for j in juncs:
        bridge._post("/junction", {"x": j[0], "y": j[1]})
    print(f"  routed: {nw} wire segs, {nflag} power flags")




# ---------------- routing geometry (planning, no I/O) ----------------
_SPO = {0: "R", 90: "U", 180: "L", 270: "D"}
STUB = 2.54

def _pin_abs(ref_pin, pos, syms):
    ref, pin = ref_pin.split(".", 1)
    if ref not in pos: return None, None
    px, py = pos[ref]
    for p in syms[ref]["pins"]:
        if str(p["number"]) == pin:
            return (px + p["x"], py - p["y"]), _SPO.get(p["angle"], "R")
    return None, None

def _radial_stub(a, ref_pin, pos):
    """Stub end: leave the pin tip outward, radially away from the part center."""
    owner = ref_pin.split(".")[0]
    cx, cy = pos.get(owner, (a[0], a[1]))
    dx, dy = a[0] - cx, a[1] - cy
    if abs(dx) >= abs(dy):
        u = (STUB if dx >= 0 else -STUB, 0)
    else:
        u = (0, STUB if dy >= 0 else -STUB)
    return (a[0] + u[0], a[1] + u[1])

def plan_geometry(spec, pos, syms):
    """Compute every wire segment, power-flag spec and junction WITHOUT posting,
    so field auto-placement can treat them all as obstacles first."""
    wire_segs = []; flags = []; juncs = []; pwr_idx = [0]
    for net in spec.get("nets", []):
        pins = [(_pin_abs(pp, pos, syms), pp) for pp in net.get("pins", [])]
        pins = [(a, o, pp) for (a, o), pp in pins if a is not None]
        pw = net.get("power")
        if pw:                                       # IRON RULE: GND/positive=0, negative 0 above / 180 below
            nm = pw.split(":")[-1].upper()
            negative = nm.startswith("-") or nm in ("VEE", "VSS", "V-")
            for a, o, pp in pins:
                pwr_idx[0] += 1
                if negative:
                    ocy = pos.get(pp.split(".")[0], (a[0], a[1]))[1]
                    fo = 0 if a[1] < ocy else 180
                else:
                    fo = 0
                flags.append({"libid": pw, "ref": f"#PWR{pwr_idx[0]:02d}",
                              "value": net["name"], "x": a[0], "y": a[1], "orient": fo})
            continue
        if len(pins) < 2:
            continue
        stubs = [(a, _radial_stub(a, pp, pos)) for a, o, pp in pins]
        for a, se in stubs:
            wire_segs.append((a, se))
        s0 = stubs[0][1]
        for a, se in stubs[1:]:                       # connect: approach a stub perpendicular to it
            vert = abs(se[1] - a[1]) > abs(se[0] - a[0])
            if abs(s0[0]-se[0]) < 1e-6 or abs(s0[1]-se[1]) < 1e-6:
                segs = [(s0, se)]
            elif vert:
                corner = (s0[0], se[1]); segs = [(s0, corner), (corner, se)]
            else:
                corner = (se[0], s0[1]); segs = [(s0, corner), (corner, se)]
            wire_segs.extend(segs)
        if len(stubs) > 2:
            juncs.append(s0)
    return wire_segs, flags, juncs


# ---------------- field auto-placement (collision avoidance) ----------------
def _body_rect(ref, pos, syms):
    x, y = pos[ref]; x0, x1, y0, y1 = bbox(syms[ref])
    return (x + x0, x + x1, y - y1, y - y0)          # abs, schematic Y-down

def _seg_rect(p, q, t=0.5):
    return (min(p[0], q[0]) - t, max(p[0], q[0]) + t,
            min(p[1], q[1]) - t, max(p[1], q[1]) + t)

def build_obstacles(spec, pos, syms, wire_segs, flags):
    obs = []
    for ref in pos:
        obs.append(_body_rect(ref, pos, syms))
    for (p, q) in wire_segs:
        obs.append(_seg_rect(p, q))
    for f in flags:                                   # flag graphic ~3.8mm tall + stem
        obs.append((f["x"] - 1.8, f["x"] + 1.8, f["y"] - 3.81, f["y"] + 3.81))
    return obs

def _fbox(c, text, m=0.3):
    w = max(len(str(text)), 1) * 1.27 * 0.85; h = 1.27
    return (c[0] - w/2 - m, c[0] + w/2 + m, c[1] - h/2 - m, c[1] + h/2 + m)

def _hit(b, o):
    return not (b[1] <= o[0] or b[0] >= o[1] or b[3] <= o[2] or b[2] >= o[3])

_G = 0.635
def _snap(c): return (round(c[0]/_G)*_G, round(c[1]/_G)*_G)

def _slide(c, text, obs, d, step=_G, maxd=20):
    """Slide the field center in direction d until its bbox clears all obstacles."""
    dx, dy = {"R": (1, 0), "L": (-1, 0), "U": (0, -1), "D": (0, 1)}[d]
    cx, cy = c; n = 0
    while n * step <= maxd:
        if not any(_hit(_fbox((cx, cy), text), o) for o in obs):
            return (cx, cy)
        cx += dx * step; cy += dy * step; n += 1
    return None

def _place_field(c, text, obs):
    """Iron rule: a field may not overlap a wire/element/pin. If the default
    position collides, move it to the NEAREST fully-clear side (R/L/U/D)."""
    if not any(_hit(_fbox(c, text), o) for o in obs):
        return _snap(c)
    best = None; bd = 1e9
    for d in ("R", "L", "U", "D"):
        p = _slide(c, text, obs, d)
        if p is not None:
            dist = abs(p[0]-c[0]) + abs(p[1]-c[1])
            if dist < bd: bd = dist; best = p
    return _snap(best if best is not None else c)

def autoplace_fields(sym, x, y, ref_text, val_text, obs, orient=0):
    """Return {'Reference':(x,y),'Value':(x,y)} (abs mm), each clear of obs.
    The chosen boxes are appended to obs so later fields dodge them too."""
    def deflt(key, fb):
        px, py = sym["props"].get(key, fb)
        rx, ry = KL._rot(px, py, orient)
        return (x + rx, y - ry)
    rpos = _place_field(deflt("Reference", (2.54, 1.27)), ref_text, obs)
    obs.append(_fbox(rpos, ref_text))
    vpos = _place_field(deflt("Value", (2.54, -1.27)), val_text, obs)
    obs.append(_fbox(vpos, val_text))
    return {"Reference": rpos, "Value": vpos}


if __name__ == "__main__":
    spec_path = sys.argv[1]
    spec = json.load(open(spec_path))
    # resolve local_libs paths relative to the spec file (parity with kicad_live)
    base = os.path.dirname(os.path.abspath(spec_path))
    spec["local_libs"] = {k: (v if os.path.isabs(v) else os.path.join(base, v))
                          for k, v in (spec.get("local_libs") or {}).items()}
    url = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--bridge" else "http://172.23.48.1:8761"
    place_all(spec, url)
