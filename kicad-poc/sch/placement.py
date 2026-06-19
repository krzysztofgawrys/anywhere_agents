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
import json, sys, os, base64, math, urllib.request, urllib.error
from collections import defaultdict
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
_ROLE_COL = {"connector_in": 0, "input": 1, "core": 2, "output": 3, "connector_out": 4}

def _abs_rect(ref, x, y, syms):
    x0, x1, y0, y1 = bbox(syms[ref])
    return (x + x0, x + x1, y - y1, y - y0)           # schematic frame (Y down)

def _overlap(a, b, m=0.0):
    return not (a[1]+m <= b[0] or a[0]-m >= b[1] or a[3]+m <= b[2] or a[2]-m >= b[3])

def _anchor_pin_pts(sat, anchor, syms, nets):
    """lib (x,y) of anchor pins that share a net with satellite `sat`."""
    out = []
    for net in nets:
        refs = [pp.split(".")[0] for pp in net.get("pins", [])]
        if sat in refs and anchor in refs:
            for pp in net["pins"]:
                r, n = pp.split(".", 1)
                if r == anchor:
                    for ap in syms[anchor]["pins"]:
                        if str(ap["number"]) == n:
                            out.append((ap["x"], ap["y"]))
    return out

def _place_cell(anchor, sats, syms, nets, gap=2.54):
    """Anchor at local (0,0); each satellite is pushed outward from the anchor
    pin it connects to until clear. Returns ({ref:(x,y)}, (x0,x1,y0,y1) bbox)."""
    sats = [s for s in sats if s != anchor]
    local = {anchor: (0.0, 0.0)}
    obs = [_abs_rect(anchor, 0.0, 0.0, syms)]
    ahw = (obs[0][1]-obs[0][0])/2; ahh = (obs[0][3]-obs[0][2])/2
    def sdir(s):
        pts = _anchor_pin_pts(s, anchor, syms, nets)
        if pts:
            cx = sum(p[0] for p in pts)/len(pts); cy = sum(p[1] for p in pts)/len(pts)
        else:
            cx, cy = (ahw + 5.0, 0.0)
        return (cx, -cy)                              # lib Y-up -> schematic Y-down
    for s in sorted(sats, key=lambda s: math.atan2(sdir(s)[1], sdir(s)[0])):
        w, h = _wh(syms[s])
        dx, dy = sdir(s); mag = math.hypot(dx, dy) or 1.0
        ux, uy = dx/mag, dy/mag
        cx = ux * (ahw + gap + w/2); cy = uy * (ahh + gap + h/2)
        for _ in range(60):                           # slide outward until clear
            r = (cx-w/2-gap/2, cx+w/2+gap/2, cy-h/2-gap/2, cy+h/2+gap/2)
            if not any(_overlap(r, o) for o in obs):
                break
            cx += ux*1.27; cy += uy*1.27
        local[s] = (snap(cx), snap(cy))
        obs.append((cx-w/2-gap/2, cx+w/2+gap/2, cy-h/2-gap/2, cy+h/2+gap/2))
    xs = [o[0] for o in obs] + [o[1] for o in obs]
    ys = [o[2] for o in obs] + [o[3] for o in obs]
    return local, (min(xs), max(xs), min(ys), max(ys))

def _spread_cells(anchor, lb, page, margin, n=10, iters=140):
    """Move whole cells (intact) to flatten the ink-density grid (analytical-
    placement style), then push apart any that overlap. anchor:{a:(x,y)},
    lb:{a:(x0,x1,y0,y1) cell-local bbox with the anchor at local (0,0)}."""
    W, H = page
    lo_x, hi_x = margin, W - margin; lo_y, hi_y = margin, H - margin
    gw = (hi_x - lo_x) / n; gh = (hi_y - lo_y) / n
    keys = list(anchor)
    area = {a: max(1.0, (lb[a][1]-lb[a][0]) * (lb[a][3]-lb[a][2])) for a in keys}
    def gidx(x, y):
        return (max(0, min(n-1, int((x-lo_x)/gw))), max(0, min(n-1, int((y-lo_y)/gh))))
    for it in range(iters):
        t = 1.0 - it/iters
        grid = [[0.0]*n for _ in range(n)]
        for a in keys:
            gx, gy = gidx(*anchor[a]); grid[gy][gx] += area[a]
        for a in keys:
            x, y = anchor[a]; gx, gy = gidx(x, y)
            wall = max(1.0, grid[gy][gx]) * 4.0           # boundary repels (high density)
            D = lambda ix, iy: grid[iy][ix] if 0 <= ix < n and 0 <= iy < n else wall
            fx = D(gx-1, gy) - D(gx+1, gy); fy = D(gx, gy-1) - D(gx, gy+1)
            mag = math.hypot(fx, fy)
            if mag > 1e-9:
                mv = t * 0.55 * gw
                x += fx/mag*mv; y += fy/mag*mv
            anchor[a] = (min(hi_x, max(lo_x, x)), min(hi_y, max(lo_y, y)))
    def box(a):
        x, y = anchor[a]; b = lb[a]
        return (x+b[0], x+b[1], y+b[2], y+b[3])
    for _ in range(400):                                  # de-overlap, cells intact
        moved = False
        for i in range(len(keys)):
            for j in range(i+1, len(keys)):
                A, B = box(keys[i]), box(keys[j])
                ox = min(A[1], B[1]) - max(A[0], B[0]); oy = min(A[3], B[3]) - max(A[2], B[2])
                if ox > 0 and oy > 0:
                    moved = True
                    ax, ay = anchor[keys[i]]; bx, by = anchor[keys[j]]
                    if ox < oy:
                        d = (ox/2 + 0.4) * (1 if (A[0]+A[1]) >= (B[0]+B[1]) else -1)
                        anchor[keys[i]] = (ax+d, ay); anchor[keys[j]] = (bx-d, by)
                    else:
                        d = (oy/2 + 0.4) * (1 if (A[2]+A[3]) >= (B[2]+B[3]) else -1)
                        anchor[keys[i]] = (ax, ay+d); anchor[keys[j]] = (bx, by-d)
        if not moved:
            break
    return anchor

def layout(parts, libdirs, origin=(20.0, 20.0), local_libs=None, page=None, margin=15.0):
    """Cell-based layout: every role-bearing part is a cell anchor; its near-chain
    satellites cluster around the anchor pins they connect to; cells are shelf-
    packed by role band (input -> core -> output), left to right then wrapping."""
    local_libs = local_libs if local_libs is not None else _local_libs[0]
    syms = {p["ref"]: KL.load_symbol(p["lib_id"], libdirs, local_libs) for p in parts}
    by = {p["ref"]: p for p in parts}
    nets = parts_spec_nets[0]
    COL = _ROLE_COL
    def root(r, seen=None):                           # follow near-chain to a role part
        seen = seen or set(); p = by.get(r)
        if not p or r in seen or p.get("role") in COL:
            return r
        seen.add(r); n = p.get("near")
        return root(n, seen) if n in by else r
    anchors = [r for r, p in by.items() if p.get("role") in COL]
    members = defaultdict(list)
    for r, p in by.items():
        if p.get("role") not in COL:
            members[root(r)].append(r)
    cell_roots = list(anchors) + [r for r in members if r not in anchors]
    cells = {a: _place_cell(a, members.get(a, []), syms, nets) for a in cell_roots}
    band = lambda a: COL.get(by[a].get("role"), 5)    # orphan roots last
    seq = list(by)
    order = sorted(cell_roots, key=lambda a: (band(a), seq.index(a)))
    cw = lambda a: cells[a][1][1] - cells[a][1][0]
    ch = lambda a: cells[a][1][3] - cells[a][1][2]
    # group cells into rows by a width budget (the page width if known)
    pw, ph = page if page else (250.0 + 2*margin, 1e9)
    row_budget = pw - 2*margin
    rows = []; cur = []; curw = 0.0
    for a in order:
        if cur and curw + cw(a) > row_budget:
            rows.append(cur); cur = []; curw = 0.0
        cur.append(a); curw += cw(a) + 8.0
    if cur: rows.append(cur)
    # initial placement: spread cell CENTERS over a grid filling the whole sheet
    # (column-major so the signal-flow order runs left -> right), then de-overlap.
    anchor = {}; lb = {a: cells[a][1] for a in cell_roots}
    x_avail = pw - 2*margin; y_avail = ph - 2*margin
    if page:
        K = len(order)
        cols = max(1, round(math.sqrt(K * (x_avail / y_avail))))
        nrow = math.ceil(K / cols)
        for idx, a in enumerate(order):
            c = idx // nrow; r = idx % nrow           # column-major: left column first
            cx = margin + (c + 0.5) * x_avail / cols
            cy = margin + (r + 0.5) * y_avail / nrow
            x0, x1, y0, y1 = cells[a][1]
            anchor[a] = (cx - (x0+x1)/2, cy - (y0+y1)/2)   # put cell center at grid point
        anchor = _spread_cells(anchor, lb, page, margin)
    else:
        cx = margin; cy = margin; rowh = 0.0
        for a in order:
            x0, x1, y0, y1 = cells[a][1]
            anchor[a] = (cx - x0, cy - y0); cx += (x1-x0) + 12.0; rowh = max(rowh, y1-y0)
    pos = {}
    for a in cell_roots:
        ax, ay = anchor[a]
        for ref, (lx, ly) in cells[a][0].items():
            pos[ref] = (snap(ax + lx), snap(ay + ly))
    return pos, syms


def place_all(spec, bridge_url):
    libdirs = spec["libdirs"]
    bridge = KL.Bridge(bridge_url)
    h = bridge.health(); print("sheet:", h.get("project"), h.get("symbols"), "symbols")
    parts_spec_nets[0] = spec.get("nets", [])
    _local_libs[0] = spec.get("local_libs") or {}
    page = None
    try:
        pg = bridge.page()
        if pg.get("ok"):
            page = (pg["width_mm"], pg["height_mm"]); print(f"  page: {page[0]}x{page[1]} mm")
    except Exception as e:
        print("  page: unavailable, using default budget (", e, ")")
    pos, syms = layout(spec["parts"], libdirs, local_libs=_local_libs[0], page=page)
    _orients[0] = compute_orients(spec["parts"], syms, spec.get("nets", []))
    scaffold = open(KL.SCAFFOLD, "rb").read()
    # plan all geometry BEFORE placing, so fields can dodge wires/flags/bodies/labels
    wire_segs, flags, juncs, labels = plan_geometry(spec, pos, syms)
    obs = build_obstacles(spec, pos, syms, wire_segs, flags, labels)

    # main symbols, with each Reference/Value auto-placed clear of obstacles
    for p in spec["parts"]:
        if p["ref"] not in pos:
            print("  SKIP (no role/pos):", p["ref"]); continue
        x, y = pos[p["ref"]]; sym = syms[p["ref"]]; o = _orients[0].get(p["ref"], 0)
        fa = 0 if o % 180 == 0 else -o                # keep field text upright
        fp = autoplace_fields(sym, x, y, p["ref"], p.get("value", ""), obs, orient=o)
        b64 = KL.build_symbol_proto(sym, p["ref"], p.get("value", ""), x, y, scaffold,
                                    orient=o, field_angle=fa, field_pos=fp)
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

    # wires + junctions + net labels last
    nw = 0
    for (a, b) in wire_segs:
        if bridge._post("/wire", {"x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]}).get("ok"): nw += 1
    for j in juncs:
        bridge._post("/junction", {"x": j[0], "y": j[1]})
    nlab = 0
    for lb in labels:
        if bridge.label(lb["text"], lb["x"], lb["y"], global_=False).get("ok"): nlab += 1
    print(f"  routed: {nw} wire segs, {nflag} power flags, {nlab} net labels")




# ---------------- routing geometry (planning, no I/O) ----------------
_SPO = {0: "R", 90: "U", 180: "L", 270: "D"}
STUB = 2.54
_orients = [{}]          # {ref: degrees}, set by place_all

def _is_gnd_net(net):
    nm = (net.get("name") or "").upper(); pw = (net.get("power") or "").upper()
    return nm == "GND" or pw.endswith("GND")

def compute_orients(parts, syms, nets):
    """IRON RULE for 2-pin passives (R/C/L/D): if one pin is GND, the part is
    vertical with the GND pin at the BOTTOM. Returns {ref: 0|180}."""
    o = {}
    for p in parts:
        ref = p["ref"]; sym = syms.get(ref)
        if not sym or len(sym["pins"]) != 2:
            continue
        gpin = next((pp.split(".", 1)[1] for net in nets if _is_gnd_net(net)
                     for pp in net.get("pins", []) if pp.split(".", 1)[0] == ref), None)
        if gpin is None:
            continue
        pins = {str(pp["number"]): pp for pp in sym["pins"]}
        gp = pins.get(gpin)
        other = next((pp for k, pp in pins.items() if k != gpin), None)
        if not gp or not other:
            continue
        if abs(gp["y"] - other["y"]) > abs(gp["x"] - other["x"]):   # vertical passive
            # schematic bottom = larger schematic y = smaller lib y; flip if GND on top
            o[ref] = 180 if gp["y"] > other["y"] else 0
    return o

def _pin_abs(ref_pin, pos, syms):
    ref, pin = ref_pin.split(".", 1)
    if ref not in pos: return None, None
    px, py = pos[ref]; deg = _orients[0].get(ref, 0)
    for p in syms[ref]["pins"]:
        if str(p["number"]) == pin:
            rx, ry = KL._rot(p["x"], p["y"], deg)
            return (px + rx, py - ry), _SPO.get((p["angle"] + deg) % 360, "R")
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

# hybrid connectivity: physical wires only for small, LOCAL nets; everything
# wider or longer is connected by name with net labels (avoids the rat's nest).
LABEL_FANOUT = 4
LONG_NET_MM = 45.0

def _outdir(a, ref_pin, pos):
    """Unit direction from the owning part's center outward through pin `a`."""
    owner = ref_pin.split(".")[0]; cx, cy = pos.get(owner, (a[0], a[1]))
    dx, dy = a[0] - cx, a[1] - cy
    if abs(dx) >= abs(dy):
        return (1.0 if dx >= 0 else -1.0, 0.0)
    return (0.0, 1.0 if dy >= 0 else -1.0)

def _slide_clear(x, y, half, u, obs, step=1.27, maxd=30):
    """Slide (x,y) along u until its (hw,hh) box clears obs. Returns (x,y,moved)."""
    hw, hh = half
    for n in range(int(maxd/step) + 1):
        if not any(_overlap((x-hw, x+hw, y-hh, y+hh), o) for o in obs):
            return x, y, n > 0
        x += u[0]*step; y += u[1]*step
    return x, y, True

_FLAG_HALF = (1.2, 2.6)

def _seg_hits_box(p, q, box, m=0.3):
    """True if orthogonal segment p-q passes THROUGH box (touching an edge is ok)."""
    x0, x1, y0, y1 = box[0]+m, box[1]-m, box[2]+m, box[3]-m
    if x1 <= x0 or y1 <= y0:
        return False
    ax, ay = p; bx, by = q
    if abs(ay - by) < 1e-6:                                 # horizontal
        return y0 < ay < y1 and min(ax, bx) < x1 and max(ax, bx) > x0
    if abs(ax - bx) < 1e-6:                                 # vertical
        return x0 < ax < x1 and min(ay, by) < y1 and max(ay, by) > y0
    return False

def plan_geometry(spec, pos, syms):
    """Compute wire segments, power-flag specs, junctions and net LABELS WITHOUT
    posting. IRON RULES: no power node overlaps another; no label lands on a part
    or power symbol; no wire crosses another element's box (body+pins) - a net
    that cannot be wired clear falls back to connect-by-name labels."""
    wire_segs = []; flags = []; juncs = []; labels = []; pwr_idx = [0]
    nets = spec.get("nets", [])
    eboxes = {r: _body_rect(r, pos, syms) for r in pos}    # element box = body+pins
    obs = list(eboxes.values())                            # hard obstacles
    def crosses_foreign(segs, owners):
        for (p, q) in segs:
            for r, bx in eboxes.items():
                if r not in owners and _seg_hits_box(p, q, bx):
                    return True
        return False
    # 1) power flags first - never overlap a body or each other
    for net in nets:
        pw = net.get("power")
        if not pw:
            continue
        nm = pw.split(":")[-1].upper()
        negative = nm.startswith("-") or nm in ("VEE", "VSS", "V-")
        for pp in net.get("pins", []):
            a, _ = _pin_abs(pp, pos, syms)
            if a is None:
                continue
            pwr_idx[0] += 1
            if negative:
                ocy = pos.get(pp.split(".")[0], (a[0], a[1]))[1]
                fo = 0 if a[1] < ocy else 180
            else:
                fo = 0
            u = _outdir(a, pp, pos)
            fx, fy, moved = _slide_clear(a[0], a[1], _FLAG_HALF, u, obs, maxd=24)
            if moved:
                wire_segs.append((a, (fx, fy)))
            flags.append({"libid": pw, "ref": f"#PWR{pwr_idx[0]:02d}",
                          "value": net["name"], "x": fx, "y": fy, "orient": fo})
            obs.append((fx-_FLAG_HALF[0], fx+_FLAG_HALF[0], fy-_FLAG_HALF[1], fy+_FLAG_HALF[1]))
    # 2) signal nets: labels for wide/long nets, wires for small local ones
    for net in nets:
        if net.get("power"):
            continue
        pins = [(_pin_abs(pp, pos, syms), pp) for pp in net.get("pins", [])]
        pins = [(a, o, pp) for (a, o), pp in pins if a is not None]
        if len(pins) < 2:
            continue
        xs = [a[0] for a, o, pp in pins]; ys = [a[1] for a, o, pp in pins]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        owners = {pp.split(".", 1)[0] for a, o, pp in pins}
        use_labels = len(pins) >= LABEL_FANOUT or span > LONG_NET_MM
        net_segs = []; net_junc = None
        if not use_labels:                            # try to wire it; build candidate path
            stubs = [(a, _radial_stub(a, pp, pos)) for a, o, pp in pins]
            for a, se in stubs:
                net_segs.append((a, se))
            s0 = stubs[0][1]
            for a, se in stubs[1:]:                   # approach a stub perpendicular to it
                vert = abs(se[1] - a[1]) > abs(se[0] - a[0])
                if abs(s0[0]-se[0]) < 1e-6 or abs(s0[1]-se[1]) < 1e-6:
                    net_segs.append((s0, se))
                elif vert:
                    c = (s0[0], se[1]); net_segs += [(s0, c), (c, se)]
                else:
                    c = (se[0], s0[1]); net_segs += [(s0, c), (c, se)]
            if len(stubs) > 2:
                net_junc = s0
            if crosses_foreign(net_segs, owners):     # cannot wire clear -> labels
                use_labels = True
        if use_labels:                                # connect-by-name label per pin
            for a, o, pp in pins:
                u = _outdir(a, pp, pos)
                hw = max(len(net["name"]), 1) * 1.27 * 0.85 / 2 + 0.4
                sx, sy = a[0] + u[0]*STUB, a[1] + u[1]*STUB
                lx, ly, _ = _slide_clear(sx, sy, (hw, 1.27/2 + 0.4), u, obs)
                wire_segs.append((a, (lx, ly)))
                labels.append({"text": net["name"], "x": lx, "y": ly})
                obs.append((lx-hw, lx+hw, ly-1.06, ly+1.06))
            continue
        wire_segs.extend(net_segs)
        if net_junc is not None:
            juncs.append(net_junc)
    return wire_segs, flags, juncs, labels


# ---------------- field auto-placement (collision avoidance) ----------------
def _body_rect(ref, pos, syms):
    x, y = pos[ref]; x0, x1, y0, y1 = bbox(syms[ref])
    return (x + x0, x + x1, y - y1, y - y0)          # abs, schematic Y-down

def _seg_rect(p, q, t=0.5):
    return (min(p[0], q[0]) - t, max(p[0], q[0]) + t,
            min(p[1], q[1]) - t, max(p[1], q[1]) + t)

def build_obstacles(spec, pos, syms, wire_segs, flags, labels=()):
    obs = []
    for ref in pos:
        obs.append(_body_rect(ref, pos, syms))
    for (p, q) in wire_segs:
        obs.append(_seg_rect(p, q))
    for f in flags:                                   # flag graphic ~3.8mm tall + stem
        obs.append((f["x"] - 1.8, f["x"] + 1.8, f["y"] - 3.81, f["y"] + 3.81))
    for lb in labels:
        obs.append(_fbox((lb["x"], lb["y"]), lb["text"]))
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


# ---------------- violation detector (input for the agentic refine loop) ----------------
def _colinear(s1, s2, tol=0.06, minov=0.6):
    (ax, ay), (bx, by) = s1; (cx, cy), (dx, dy) = s2
    if abs(ay-by) < tol and abs(cy-dy) < tol and abs(ay-cy) < tol:      # both horizontal, same y
        lo1, hi1 = sorted([ax, bx]); lo2, hi2 = sorted([cx, dx])
        return min(hi1, hi2) - max(lo1, lo2) > minov
    if abs(ax-bx) < tol and abs(cx-dx) < tol and abs(ax-cx) < tol:      # both vertical, same x
        lo1, hi1 = sorted([ay, by]); lo2, hi2 = sorted([cy, dy])
        return min(hi1, hi2) - max(lo1, lo2) > minov
    return False

def violations(spec, pos, syms, wire_segs, flags, labels):
    """Structured list of rule breaks for the refine loop to act on."""
    import itertools
    v = []
    eb = {r: _body_rect(r, pos, syms) for r in pos}
    for a, b in itertools.combinations(list(pos), 2):
        if _overlap(eb[a], eb[b]):
            v.append({"type": "ELEM_OVERLAP", "refs": [a, b]})
    # wire crosses a foreign element box
    for (p, q) in wire_segs:
        for r, E in eb.items():
            if _seg_hits_box(p, q, E):
                # exclude segments that merely start/end on r's own pins
                if not (_seg_hits_box(p, q, (E[0]+1.0, E[1]-1.0, E[2]+1.0, E[3]-1.0))):
                    continue
                v.append({"type": "WIRE_CROSS_BOX", "box": r, "seg": [p, q]}); break
    # two wires sharing a path (colinear overlap)
    for i, j in itertools.combinations(range(len(wire_segs)), 2):
        if _colinear(wire_segs[i], wire_segs[j]):
            v.append({"type": "WIRE_COLINEAR", "segs": [wire_segs[i], wire_segs[j]]})
    # flag boxes overlapping (touching allowed)
    fb = [(f, (f["x"]-_FLAG_HALF[0]+0.1, f["x"]+_FLAG_HALF[0]-0.1, f["y"]-_FLAG_HALF[1]+0.1, f["y"]+_FLAG_HALF[1]-0.1)) for f in flags]
    for (f1, b1), (f2, b2) in itertools.combinations(fb, 2):
        if _overlap(b1, b2):
            v.append({"type": "FLAG_OVERLAP", "refs": [f1["ref"], f2["ref"]]})
    return v
