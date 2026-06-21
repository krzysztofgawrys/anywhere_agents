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
from collections import defaultdict, deque
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
    """Anchor at local (0,0). Satellites are grouped by which SIDE of the anchor
    their connecting pin faces (L/R/T/B) and laid out as ALIGNED banks (columns
    on the L/R sides, rows on the T/B sides) - not a radial fan, which produced
    ugly diagonal 'legs'. Returns ({ref:(x,y)}, (x0,x1,y0,y1) bbox)."""
    sats = [s for s in sats if s != anchor]
    local = {anchor: (0.0, 0.0)}
    ax0, ax1, ay0, ay1 = _abs_rect(anchor, 0.0, 0.0, syms)
    ahw = (ax1-ax0)/2; ahh = (ay1-ay0)/2
    def sdir(s):
        pts = _anchor_pin_pts(s, anchor, syms, nets)
        if pts:
            cx = sum(p[0] for p in pts)/len(pts); cy = sum(p[1] for p in pts)/len(pts)
        else:
            cx, cy = (ahw + 5.0, 0.0)
        return (cx, -cy)                              # lib Y-up -> schematic Y-down
    # along-edge / column spacing. For a HIGH-FANOUT IC (20+ passives) widen both:
    # a packed cell leaves no room for each part's ref+value text, so the cell reads
    # as label soup even when there is empty sheet around it. Spreading lets the
    # density relaxation push the (now larger) cell into that whitespace.
    f = 1.0 + min(0.6, max(0.0, (len(sats) - 8) / 30.0))
    PITCH = 11.43 * f; CROSS = 13.97 * f
    # A satellite is BANKED beside the anchor only if it actually connects to the
    # anchor. One that connects to ANOTHER satellite (a gate pulldown next to its
    # transistor, an RC pair) has no anchor pin - defer it and stick it next to that
    # partner, so it lands by its target instead of piling into a generic bank.
    banked = [s for s in sats if _anchor_pin_pts(s, anchor, syms, nets)]
    deferred = [s for s in sats if s not in banked]
    side = {}
    for s in banked:
        dx, dy = sdir(s)
        side[s] = ('R' if dx >= 0 else 'L') if abs(dx) >= abs(dy) else ('B' if dy >= 0 else 'T')
    for sd in ('L', 'R', 'T', 'B'):
        grp = sorted([s for s in banked if side[s] == sd])
        if not grp:
            continue
        if sd in ('L', 'R'):                          # vertical column(s) beside the IC
            # column height tracks the IC edge, but CAP it so a high-fanout IC
            # (20+ passives) wraps into several short columns -> a near-square cell
            # instead of one 120mm strip. Target ~ sqrt(n) tall for a balanced block.
            base = max(1, int((2*ahh + 6) // PITCH))
            per = max(1, min(base, max(4, int(math.ceil(len(grp) ** 0.5)))))
            sign = -1.0 if sd == 'L' else 1.0
            for i, s in enumerate(grp):
                c, r = divmod(i, per); cnt = min(per, len(grp) - c*per)
                x = sign * (ahw + gap + 4.0 + c*CROSS)
                y = (r - (cnt-1)/2) * PITCH
                local[s] = (snap(x), snap(y))
        else:                                         # horizontal row(s) above/below
            # row width tracks the IC edge, but for a NARROW IC with many
            # bottom/top passives (AD8330: 14 on one side) that forces deep stacks
            # of 7+ rows -> a 100mm column. Widen the row to >= sqrt(n) so the bank
            # is a near-square block, not a tall strip.
            base = max(1, int((2*ahw + 6) // PITCH))
            per = max(base, int(math.ceil(len(grp) ** 0.5)))
            sign = -1.0 if sd == 'T' else 1.0
            for i, s in enumerate(grp):
                r, c = divmod(i, per); cnt = min(per, len(grp) - r*per)
                y = sign * (ahh + gap + 4.0 + r*CROSS)
                x = (c - (cnt-1)/2) * PITCH
                local[s] = (snap(x), snap(y))
    # deferred: place each next to a cell-mate it shares a net with (BFS from placed)
    cellset = set(sats) | {anchor}
    cadj = defaultdict(set)
    for net in nets:
        if net.get("power") or _is_gnd_net(net):
            continue
        nm = [pp.split(".")[0] for pp in net.get("pins", [])]
        inside = [x for x in nm if x in cellset]
        for a in inside:
            for b in inside:
                if a != b:
                    cadj[a].add(b)
    def free(cand, s):
        w, h = _wh(syms[s]); r = (cand[0]-w/2, cand[0]+w/2, cand[1]-h/2, cand[1]+h/2)
        a0, a1, b0, b1 = _abs_rect(anchor, 0.0, 0.0, syms)
        if _overlap(r, (a0, a1, b0, b1), 1.0):
            return False
        for o in local:
            if o == s:
                continue
            ox0, ox1, oy0, oy1 = _abs_rect(o, local[o][0], local[o][1], syms)
            if _overlap(r, (ox0, ox1, oy0, oy1), 1.0):
                return False
        return True
    pending = list(deferred)
    for _ in range(len(pending) + 1):
        progress = False
        for s in list(pending):
            partners = [q for q in cadj[s] if q in local]
            if not partners:
                continue
            qx, qy = local[partners[0]]
            # grow a chain HORIZONTALLY first (signal-flow left->right) so a long
            # filter ladder reads as a row, not a tall column; chains off different
            # partners then stack into a compact block instead of one 200mm strip.
            # Bias the horizontal direction AWAY from the anchor (sign of qx).
            hx = 1 if qx >= 0 else -1
            for dx, dy in [(hx, 0), (-hx, 0), (0, 1), (0, -1), (hx, 1), (hx, -1)]:
                cand = (snap(qx + dx*CROSS), snap(qy + dy*PITCH))
                if free(cand, s):
                    local[s] = cand; break
            else:
                local[s] = (snap(qx + hx*CROSS), snap(qy))
            pending.remove(s); progress = True
        if not progress:
            break
    for s in pending:                                 # truly isolated -> a generic slot
        local[s] = (snap(ahw + gap + 4.0), snap(0.0))
    xs = [ax0, ax1]; ys = [ay0, ay1]
    for s in sats:
        lx, ly = local[s]; w, h = _wh(syms[s])
        xs += [lx-w/2, lx+w/2]; ys += [ly-h/2, ly+h/2]
    return local, (min(xs), max(xs), min(ys), max(ys))

def _spread_cells(anchor, lb, page, margin, n=10, iters=140, relax=True):
    """Push apart any overlapping cells (intact). With relax=True also first flattens
    the ink-density grid (analytical-placement style) - but that re-centres cells and
    UNDOES the area-balanced column packing, so the packer passes relax=False to keep
    its columns and only de-overlap. anchor:{a:(x,y)}, lb:{a:cell-local bbox}."""
    W, H = page
    lo_x, hi_x = margin, W - margin; lo_y, hi_y = margin, H - margin
    gw = (hi_x - lo_x) / n; gh = (hi_y - lo_y) / n
    keys = list(anchor)
    area = {a: max(1.0, (lb[a][1]-lb[a][0]) * (lb[a][3]-lb[a][2])) for a in keys}
    def gidx(x, y):
        return (max(0, min(n-1, int((x-lo_x)/gw))), max(0, min(n-1, int((y-lo_y)/gh))))
    for it in range(iters if relax else 0):
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

def _decoup_rail(ref, nets):
    """If ref is a 2-pin cap across a power rail and GND, return the rail net name
    (e.g. '+5V'); else None. Such decoupling caps go in a bottom bank, not cells."""
    rail = None; gnd = False
    for net in nets:
        names = [pp.split(".")[0] for pp in net.get("pins", [])]
        if ref not in names:
            continue
        if _is_gnd_net(net):
            gnd = True
        elif net.get("power"):
            rail = net.get("name")
    return rail if (gnd and rail) else None

def layout(parts, libdirs, origin=(20.0, 20.0), local_libs=None, page=None, margin=15.0):
    """Cell-based layout: every role-bearing part is a cell anchor; its near-chain
    satellites cluster around the anchor pins they connect to. Decoupling caps are
    pulled out into a horizontal bank at the BOTTOM, grouped by supply rail."""
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
    deco = {}                                          # decoupling caps -> supply rail
    for r, p in by.items():
        if p.get("lib_id", "").startswith(("Device:C", "Device:CP")):
            rail = _decoup_rail(r, nets)
            if rail:
                deco[r] = rail
    anchors = [r for r, p in by.items() if p.get("role") in COL]
    roleset = set(anchors)
    # GROUPING IS THE AGENT'S CALL: a part's `group`/`near` (pointing at the IC /
    # relay / block it belongs to) is BINDING and never overridden. Only when a
    # part carries NO grouping hint do we infer it - nearest role-bearing part by
    # signal-net BFS (this adjacency), as a safety net.
    adj = defaultdict(set)
    for net in nets:
        if net.get("power") or _is_gnd_net(net):
            continue
        names = [pp.split(".")[0] for pp in net.get("pins", [])]
        for a in names:
            for b in names:
                if a != b:
                    adj[a].add(b)
    def nearest_role(r):
        if r in roleset:
            return r
        seen = {r}; q = deque(adj[r])
        while q:
            x = q.popleft()
            if x in roleset:
                return x
            if x not in seen:
                seen.add(x); q.extend(adj[x])
        return None
    def signal_partner(r):
        # A 2-pin passive tied to GND/a rail (pulldown, pullup, termination, HPF cap)
        # has exactly ONE signal net; its "target" is the part on the other end of
        # that net. Return it so the passive cells WITH its target (placed on the
        # connecting pin by _place_cell). Only when unambiguous: a lone partner, or a
        # single role-bearing partner on a multi-drop net. Pure topology - no value
        # heuristic, so a 50ohm termination groups by its target just like a pulldown.
        rnets = [n for n in nets if r in {x.split(".")[0] for x in n.get("pins", [])}]
        sig = [n for n in rnets if not n.get("power") and not _is_gnd_net(n)]
        if len(sig) != 1:
            return None
        partners = {x.split(".")[0] for x in sig[0].get("pins", [])} - {r}
        if len(partners) == 1:
            q = next(iter(partners)); return q if q in by else None
        role_p = [q for q in partners if q in roleset]
        return role_p[0] if len(role_p) == 1 else None
    members = defaultdict(list)
    for r, p in by.items():
        if p.get("role") in COL or r in deco:         # decoupling -> bottom bank, not a satellite
            continue
        # agent's grouping is BINDING; else anchor a to-GND/rail passive to its target;
        # else fall back to nearest role by signal-net BFS.
        g = p.get("group") or p.get("near") or signal_partner(r)
        members[(root(g) if g in by else (nearest_role(r) or r))].append(r)
    cell_roots = list(anchors) + [r for r in members if r not in anchors]
    cells = {a: _place_cell(a, members.get(a, []), syms, nets) for a in cell_roots}
    band = lambda a: COL.get(by[a].get("role"), 5)    # orphan roots last
    seq = list(by)
    order = sorted(cell_roots, key=lambda a: (band(a), seq.index(a)))
    cw = lambda a: cells[a][1][1] - cells[a][1][0]
    ch = lambda a: cells[a][1][3] - cells[a][1][2]
    # reserve a bottom strip for the decoupling bank; cells fill the rest
    BANK_H = 34.0 if deco else 0.0
    cell_page = (page[0], page[1] - BANK_H) if page else None
    pw, ph = cell_page if cell_page else (250.0 + 2*margin, 1e9)
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
    if cell_page:
        K = max(1, len(order))
        cols = max(1, round(math.sqrt(K * (x_avail / y_avail))))
        nrow = math.ceil(K / cols)
        for idx, a in enumerate(order):
            c = idx // nrow; r = idx % nrow           # column-major: left column first
            cx = margin + (c + 0.5) * x_avail / cols
            cy = margin + (r + 0.5) * y_avail / nrow
            x0, x1, y0, y1 = cells[a][1]
            anchor[a] = (cx - (x0+x1)/2, cy - (y0+y1)/2)   # put cell center at grid point
        anchor = _spread_cells(anchor, lb, cell_page, margin)
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
    # decoupling bank: a horizontal row at the bottom, grouped by supply rail
    if deco and page:
        PW, PH = page
        by_rail = defaultdict(list)
        for r, rr in deco.items():
            by_rail[rr].append(r)
        rails = sorted(by_rail, key=lambda rr: (rr.startswith("-"), rr))
        PITCH = 7.62; GGAP = 12.0
        total = sum(len(by_rail[rr]) * PITCH for rr in rails) + GGAP * (len(rails) - 1)
        x = max(margin + 4.0, (PW - total) / 2)
        yb = PH - margin - 9.0
        for rr in rails:
            for r in sorted(by_rail[rr]):
                pos[r] = (snap(x), snap(yb)); x += PITCH
            x += GGAP
    return pos, syms


def sync_footprints(spec, bridge_url, sheet=None, store=None, only_nonempty=True):
    """ROUND-TRIP: read footprints the user set by hand in eeschema and fold them
    into the netlist `spec` IN PLACE (so a /clear + re-place keeps them). Reads the
    live sheet via GET /symbols. If `store` (a path) is given, ALSO persist them to a
    durable footprint store keyed by ref AND lib_id|value - that store survives even
    a from-scratch netlist regeneration (apply it with apply_footprints). Returns
    {ref: footprint} of what changed in `spec`."""
    bridge = KL.Bridge(bridge_url, sheet=sheet or spec.get("sheet"))
    live = {s["ref"]: s for s in bridge.symbols().get("symbols", [])}
    by = {p["ref"]: p for p in spec.get("parts", [])}
    changed = {}; recs = []
    for ref, s in live.items():
        fp = (s.get("footprint") or "").strip()
        p = by.get(ref)
        if p and (fp or not only_nonempty) and p.get("footprint", "") != fp:
            p["footprint"] = fp; changed[ref] = fp
        if fp:
            recs.append({"ref": ref, "lib_id": (p or s).get("lib_id", ""),
                         "value": (p or {}).get("value", s.get("value", "")), "footprint": fp})
    if store and recs:
        _save_footprint_store(store, recs)
    return changed


def _save_footprint_store(path, recs):
    """Merge footprint records into the JSON store at `path` (created if absent).
    Keyed by ref; newest wins."""
    try:
        data = json.load(open(path)) if os.path.exists(path) else {"overrides": []}
    except Exception:
        data = {"overrides": []}
    idx = {r["ref"]: r for r in data.get("overrides", [])}
    for r in recs:
        idx[r["ref"]] = r
    json.dump({"overrides": list(idx.values())}, open(path, "w"), indent=1)


def apply_footprints(spec, store):
    """Stamp saved footprints onto `spec` IN PLACE - the durable layer that survives
    netlist REGENERATION. `store` = a path to the JSON store or its dict/list. Match
    by ref first (stable for the same design), then by lib_id+value (so a regenerated
    netlist with the same part still gets it). Only fills parts without an explicit
    footprint already. Returns {ref: footprint} applied."""
    if isinstance(store, str):
        store = json.load(open(store)) if os.path.exists(store) else {}
    recs = store.get("overrides", store) if isinstance(store, dict) else store
    by_ref = {r["ref"]: r["footprint"] for r in recs if r.get("footprint")}
    by_lv = {}
    for r in recs:
        if r.get("footprint"):
            by_lv.setdefault((r.get("lib_id", ""), r.get("value", "")), r["footprint"])
    applied = {}
    for p in spec.get("parts", []):
        if p.get("footprint"):
            continue
        fp = by_ref.get(p["ref"]) or by_lv.get((p.get("lib_id", ""), p.get("value", "")))
        if fp:
            p["footprint"] = fp; applied[p["ref"]] = fp
    return applied


def place_all(spec, bridge_url, pos_override=None, clear=True, sheet=None):
    libdirs = spec["libdirs"]
    # `sheet` (name/filename of a hierarchical sub-sheet) routes ALL bridge ops to
    # that sheet via its sheet_path - so a big design splits cleanly across sheets.
    sheet = sheet or spec.get("sheet")
    bridge = KL.Bridge(bridge_url, sheet=sheet)
    h = bridge.health(); print("sheet:", h.get("project"), h.get("symbols"), "symbols")
    if clear and h.get("symbols"):                      # wipe the sheet first or a re-run
        r = bridge.clear()                              # stacks a 2nd copy of every symbol
        print(f"  cleared {r.get('removed', '?')} existing items")
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
    if pos_override:                                   # refined positions from the loop
        pos = {r: tuple(v) for r, v in pos_override.items() if r in pos}
    _orients[0] = compute_orients(spec["parts"], syms, spec.get("nets", []))
    scaffold = open(KL.SCAFFOLD, "rb").read()
    # plan all geometry BEFORE placing, so fields can dodge wires/flags/bodies/labels
    wire_segs, flags, juncs, labels = plan_geometry(spec, pos, syms)
    fpos, _fbm, obs = field_layout(spec, pos, syms, wire_segs, flags, labels)

    # main symbols, with each Reference/Value auto-placed clear of obstacles
    for p in spec["parts"]:
        if p["ref"] not in pos:
            print("  SKIP (no role/pos):", p["ref"]); continue
        x, y = pos[p["ref"]]; sym = syms[p["ref"]]; o = _orients[0].get(p["ref"], 0)
        fa = 0 if o % 180 == 0 else -o                # keep field text upright
        fp = fpos[p["ref"]]
        b64 = KL.build_symbol_proto(sym, p["ref"], p.get("value", ""), x, y, scaffold,
                                    orient=o, field_angle=fa, field_pos=fp,
                                    footprint=p.get("footprint"))
        r = bridge.place_raw(b64, x, y, p["ref"], p.get("value", ""))
        print(f"  {'OK ' if r.get('ok') else 'ERR'} {p['ref']:>4} {p.get('role',''):<12} @({x:.1f},{y:.1f}) {r.get('created', r)}")

    # power flags, value field auto-placed too (Reference #PWRxx is hidden)
    nflag = 0
    for f in flags:
        psym = KL.load_symbol(f["libid"], libdirs, _local_libs[0])
        fp = autoplace_fields(psym, f["x"], f["y"], f["ref"], f["value"], obs, orient=f["orient"])
        b64 = KL.build_symbol_proto(psym, f["ref"], f["value"], f["x"], f["y"], scaffold,
                                    orient=f["orient"], field_angle=0, field_pos=fp,
                                    footprint="")   # power flags/symbols carry no footprint
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
    from collections import Counter
    V = violations(spec, pos, syms, wire_segs, flags, labels)
    print("  VIOLATIONS:", dict(Counter(x["type"] for x in V)), "total", len(V))
    return V




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

def _pt_on_seg(pt, p, q, tol=0.4):
    """True if point pt lies on (within tol of) orthogonal segment p-q."""
    x, y = pt
    if abs(p[1]-q[1]) < 1e-6:                               # horizontal
        return abs(y-p[1]) < tol and min(p[0], q[0])-tol <= x <= max(p[0], q[0])+tol
    if abs(p[0]-q[0]) < 1e-6:                               # vertical
        return abs(x-p[0]) < tol and min(p[1], q[1])-tol <= y <= max(p[1], q[1])+tol
    return False

def _hits_foreign_pin(segs, own, pinpts, tol=0.4):
    """IRON RULE A: a wire must not pass through/touch a pin that is not on its
    own net (KiCad would read that pin-tip coincidence as an intended connection
    = a short). `own` = set of (ref, num) pin keys legitimately on this net."""
    for (p, q) in segs:
        for k, pt in pinpts.items():
            if k not in own and _pt_on_seg(pt, p, q, tol):
                return True
    return False

def _route_stub(a, u, existing, boxes, owner, half, forbidden_pts=(), length=STUB):
    """Route a stub from pin `a` outward (dir u) to an endpoint (for a flag or
    label) on a free track: not colinear with any existing wire, not crossing a
    foreign box, not touching a forbidden point (a different-net pin OR label,
    which would short), endpoint box clear. Jogs laterally. Returns (segs, end)."""
    perp = (-u[1], u[0])
    for k in range(0, 16):                                 # lateral track search
        for s in ((0,) if k == 0 else (k, -k)):
            jog = (a[0] + perp[0]*s*1.27, a[1] + perp[1]*s*1.27)
            for L in (length, length+1.27, length+2.54, length+3.81, length+5.08):
                end = (jog[0] + u[0]*L, jog[1] + u[1]*L)
                segs = ([(a, jog)] if s else []) + [(jog, end)]
                if any(_colinear(sg, e) for sg in segs for e in existing):
                    continue
                if any(_seg_hits_box(p, q, boxes[r]) for (p, q) in segs
                       for r in boxes if r != owner):
                    continue
                if any(_pt_on_seg(pt, p, q, 0.4) for (p, q) in segs for pt in forbidden_pts):
                    continue
                ib = (end[0]-half[0], end[0]+half[0], end[1]-half[1], end[1]+half[1])
                if any(_overlap(ib, boxes[r]) for r in boxes if r != owner):
                    continue
                return segs, end
    # no clear track: a straight stub could touch a foreign pin (= short). Safer to
    # sit the label/flag right on the pin with NO wire - it connects only this pin.
    return [], a

def _orient_to_side(sym, npn, opn, side):
    """deg in {0,180} placing pin `npn` on `side` ('T' top / 'B' bottom for a row
    trunk, 'L'/'R' for a column trunk). None if the passive can't align that way."""
    pins = {str(p["number"]): p for p in sym["pins"]}
    np_, op = pins.get(npn), pins.get(opn)
    if not np_ or not op:
        return None
    vertical = abs(np_["y"] - op["y"]) > abs(np_["x"] - op["x"])
    if side in ("T", "B") and not vertical:
        return None                                   # horizontal passive: flip won't stack it
    if side in ("L", "R") and vertical:
        return None
    for deg in (0, 180):
        rx, ry = KL._rot(np_["x"], np_["y"], deg)      # schematic offset = (rx, -ry)
        sy, sx = -ry, rx
        if (side == "T" and sy < 0) or (side == "B" and sy > 0) \
           or (side == "L" and sx < 0) or (side == "R" and sx > 0):
            return deg
    return None

def _consolidate(nets, pos, syms):
    """Local wire-consolidation (rotate-to-align) rule. For a signal net that would
    otherwise be LABEL-routed, find a compact cluster (>=2) of 2-pin passives in a
    row/column, FLIP the ones free of an orientation lock so their net-pins meet on
    one trunk axis, and plan a single straight trunk wire through them. Net members
    outside the cluster keep their labels (the trunk gets one label too, so the net
    still merges by name). Mutates _orients[0]; returns {netname:{pins,segs,label}}."""
    plan = {}
    locked = set(_orients[0])                          # GND-at-bottom etc. - never flip these
    ebox = {r: _body_rect(r, pos, syms) for r in pos}
    allpins = {}                                       # pk -> abs, for foreign-short checks
    for r in pos:
        for pn in syms[r]["pins"]:
            a, _ = _pin_abs(f"{r}.{pn['number']}", pos, syms)
            if a: allpins[f"{r}.{pn['number']}"] = a
    for net in nets:
        if net.get("power") or _is_gnd_net(net):
            continue
        name = net.get("name", ""); pks = net.get("pins", [])
        ap = [(pk, _pin_abs(pk, pos, syms)[0]) for pk in pks]
        ap = [(pk, a) for pk, a in ap if a]
        if len(ap) < 2:
            continue
        xs = [a[0] for _, a in ap]; ys = [a[1] for _, a in ap]
        span = max(max(xs) - min(xs), max(ys) - min(ys))
        if not (len(ap) >= LABEL_FANOUT or span > LONG_NET_MM):
            continue                                   # small/local: normal wiring already fine
        cand = []                                      # 2-pin passive owners on this net
        seen = set()
        for pk in pks:
            ref, num = pk.split(".", 1); sym = syms.get(ref)
            if sym and len(sym["pins"]) == 2 and ref in pos and ref not in seen:
                seen.add(ref); cand.append((ref, num, pk))
        if len(cand) < 2:
            continue
        for axis in ("row", "col"):
            i0 = 0 if axis == "row" else 1                 # along-axis index (row=x, col=y)
            ci = 1 if axis == "row" else 0                 # cross-axis (constant) index
            grp = sorted(cand, key=lambda c: pos[c[0]][i0])
            line = [grp[0]]
            for c in grp[1:]:                              # same cross coord + adjacent along axis
                if abs(pos[c[0]][ci] - pos[line[-1][0]][ci]) <= 6.0 \
                   and abs(pos[c[0]][i0] - pos[line[-1][0]][i0]) <= 18.0:
                    line.append(c)
            if len(line) < 2:
                continue
            side = ("T" if axis == "row" else "L")         # trunk above a row / left of a column
            for ref, num, pk in line:                      # honour a locked member's side
                if ref in locked:
                    a = _pin_abs(pk, pos, syms)[0]
                    if axis == "row":
                        side = "T" if a[1] < pos[ref][1] else "B"
                    else:
                        side = "L" if a[0] < pos[ref][0] else "R"
                    break
            degs = {}; ok = True; members = []
            for ref, num, pk in line:
                opn = next(str(p["number"]) for p in syms[ref]["pins"] if str(p["number"]) != num)
                if ref in locked:                          # keep orient; include only if it matches
                    a = _pin_abs(pk, pos, syms)[0]
                    on = (a[1] < pos[ref][1]) if axis == "row" and side == "T" else \
                         (a[1] > pos[ref][1]) if axis == "row" else \
                         (a[0] < pos[ref][0]) if side == "L" else (a[0] > pos[ref][0])
                    if on:
                        degs[ref] = _orients[0][ref]; members.append((ref, num, pk))
                    continue
                d = _orient_to_side(syms[ref], num, opn, side)
                if d is None:
                    continue
                degs[ref] = d; members.append((ref, num, pk))
            if len(members) < 2:
                continue
            sv = dict(_orients[0])                          # tentatively apply to read pin coords
            _orients[0].update(degs)
            npos = {pk: _pin_abs(pk, pos, syms)[0] for _, _, pk in members}
            if axis == "row":
                yk = (min(p[1] for p in npos.values()) - STUB) if side == "T" \
                     else (max(p[1] for p in npos.values()) + STUB)
                xs2 = [p[0] for p in npos.values()]
                trunk = ((min(xs2), yk), (max(xs2), yk))
                stubs = [((p[0], p[1]), (p[0], yk)) for p in npos.values()]
                jpts = [(p[0], yk) for p in npos.values()]    # T-tap needs a junction dot
                lbl = (min(xs2), yk)
            else:
                xk = (min(p[0] for p in npos.values()) - STUB) if side == "L" \
                     else (max(p[0] for p in npos.values()) + STUB)
                ys2 = [p[1] for p in npos.values()]
                trunk = ((xk, min(ys2)), (xk, max(ys2)))
                stubs = [((p[0], p[1]), (xk, p[1])) for p in npos.values()]
                jpts = [(xk, p[1]) for p in npos.values()]    # T-tap needs a junction dot
                lbl = (xk, min(ys2))
            segs = [trunk] + stubs
            mrefs = {r for r, _, _ in members}
            mpins = {pk for _, _, pk in members}
            bad = any(_seg_hits_box(p, q, ebox[r]) for (p, q) in segs
                      for r in ebox if r not in mrefs)
            foreign = [a for pk, a in allpins.items() if pk.split(".")[0] not in mrefs
                       and pin2net_name(nets, pk) != name]   # a member's own other pin isn't foreign
            bad = bad or any(_pt_on_seg(pt, p, q, 0.4) for (p, q) in segs for pt in foreign)
            if bad:
                _orients[0].clear(); _orients[0].update(sv)   # revert, keep labels
                continue
            plan[name] = {"pins": mpins, "segs": segs, "label": lbl, "juncs": jpts}
            break
    return plan

def pin2net_name(nets, pk):
    for n in nets:
        if pk in n.get("pins", []):
            return n.get("name")
    return None

def plan_geometry(spec, pos, syms):
    """Compute wire segments, power-flag specs, junctions and net LABELS WITHOUT
    posting. IRON RULES: no power node overlaps another; no label lands on a part
    or power symbol; no wire crosses another element's box; no two wires share a
    path (each stub gets a free track). Uncrossable local nets fall back to labels."""
    wire_segs = []; flags = []; juncs = []; labels = []; pwr_idx = [0]
    nets = spec.get("nets", [])
    consol = _consolidate(nets, pos, syms)                 # rotate-to-align: flips orients NOW
    eboxes = {r: _body_rect(r, pos, syms) for r in pos}    # element box = body+pins
    allboxes = dict(eboxes)                                # + placed flags/labels
    pin2net = {pp: n["name"] for n in nets for pp in n.get("pins", [])}   # "ref.num" -> net
    pinpts = {}                                            # "ref.num" -> abs pin point
    for ref in pos:
        for pn in syms[ref]["pins"]:
            pk = f"{ref}.{pn['number']}"
            ab, _ = _pin_abs(pk, pos, syms)
            if ab is not None:
                pinpts[pk] = ab
    # every pin will sprout an outward stub/label lane; reserve that endpoint too so a
    # FOREIGN net's wire trunk can't be routed through the spot where this pin's stub
    # will land (the classic cross-column short: an IC pin's stub vs a neighbour's trunk).
    pinstub = {pk: _radial_stub(ab, pk, pos) for pk, ab in pinpts.items()}
    label_pts = []                                         # [(netname, point)] placed so far
    def forbidden_for(netname):
        # points a wire/stub of `netname` must NOT touch: any DIFFERENT-net pin or
        # label (KiCad would merge them = a short / multiple_net_names). Same-net is fine.
        f = [pt for pk, pt in pinpts.items() if pin2net.get(pk) != netname]
        f += [pt for pk, pt in pinstub.items() if pin2net.get(pk) != netname]
        f += [pt for nn, pt in label_pts if nn != netname]
        return f
    def crosses_any_box(segs):
        # RULE B: a wire must not pass through ANY element body - not even its own
        # net's part (a wire reaching a pin on the far side of a box goes through
        # the body = use a label instead). Stubs leave pins outward so they don't
        # trip this; only a connect spanning across a box does.
        for (p, q) in segs:
            for bx in eboxes.values():
                if _seg_hits_box(p, q, bx):
                    return True
        return False
    # 1) power flags first - never overlap a body or each other
    flg_idx = [0]
    for net in nets:
        pw = net.get("power")
        if not pw:
            continue
        nm = pw.split(":")[-1].upper()
        negative = nm.startswith("-") or nm in ("VEE", "VSS", "V-")
        first_pin = None
        for pp in net.get("pins", []):
            a, _ = _pin_abs(pp, pos, syms)
            if a is None:
                continue
            if first_pin is None:
                first_pin = (a, pp)
            pwr_idx[0] += 1
            if negative:
                ocy = pos.get(pp.split(".")[0], (a[0], a[1]))[1]
                fo = 0 if a[1] < ocy else 180
            else:
                fo = 0
            owner = pp.split(".", 1)[0]; u = _outdir(a, pp, pos)
            ref = f"#PWR{pwr_idx[0]:02d}"
            segs, end = _route_stub(a, u, wire_segs, allboxes, owner, _FLAG_HALF,
                                    forbidden_pts=forbidden_for(net["name"]))
            wire_segs.extend(segs)
            flags.append({"libid": pw, "ref": ref, "value": net["name"],
                          "x": end[0], "y": end[1], "orient": fo})
            # IPC can't emit (power), so the power SYMBOL isn't recognised as power
            # and won't connect by name. A coincident local LABEL names the node, so
            # all same-rail pins unify (and a PWR_FLAG below drives the net).
            labels.append({"text": net["name"], "x": end[0], "y": end[1], "hide": True})
            label_pts.append((net["name"], end))
            allboxes["#F:" + ref] = (end[0]-_FLAG_HALF[0], end[0]+_FLAG_HALF[0],
                                     end[1]-_FLAG_HALF[1], end[1]+_FLAG_HALF[1])
        if first_pin:                                  # ONE PWR_FLAG per rail -> ERC "driven"
            a, pp = first_pin; owner = pp.split(".", 1)[0]
            ud = _outdir(a, pp, pos); perp = (-ud[1], ud[0])   # perpendicular to the rail flag
            flg_idx[0] += 1; fref = f"#FLG{flg_idx[0]:02d}"
            segs, end = _route_stub(a, perp, wire_segs, allboxes, owner, (1.6, 1.6),
                                    forbidden_pts=forbidden_for(net["name"]))
            wire_segs.extend(segs)
            flags.append({"libid": "power:PWR_FLAG", "ref": fref, "value": "",
                          "x": end[0], "y": end[1], "orient": 0})
            allboxes["#F:" + fref] = (end[0]-1.6, end[0]+1.6, end[1]-1.6, end[1]+1.6)
    # 2) signal nets: labels for wide/long nets, wires for small local ones
    for net in nets:
        if net.get("power"):
            continue
        pins = [(_pin_abs(pp, pos, syms), pp) for pp in net.get("pins", [])]
        pins = [(a, o, pp) for (a, o), pp in pins if a is not None]
        if len(pins) < 2:
            # single-pin named net = an off-sheet signal (goes to a connector/MCU not
            # on this sheet). Drop a local label so the pin reads as connected to a
            # named net instead of ERC pin_not_connected. Skip auto-generated names.
            nm = net.get("name", "")
            if len(pins) == 1 and nm and not nm.startswith("Net-") and not nm.startswith("unconnected"):
                a, o, pp = pins[0]
                owner = pp.split(".", 1)[0]; u = _outdir(a, pp, pos)
                hw = max(len(nm), 1) * 1.27 * 0.85 / 2 + 0.4
                segs, end = _route_stub(a, u, wire_segs, allboxes, owner, (hw, 1.06),
                                        forbidden_pts=forbidden_for(nm))
                wire_segs.extend(segs)
                labels.append({"text": nm, "x": end[0], "y": end[1]})
                label_pts.append((nm, end))
                allboxes["#L:%d" % len(labels)] = (end[0]-hw, end[0]+hw, end[1]-1.06, end[1]+1.06)
            continue
        cp = consol.get(net["name"])
        if cp:                                         # rotate-to-align: cluster is WIRED
            wire_segs.extend(cp["segs"])               # one straight trunk through the row/col
            juncs.extend(cp["juncs"])                  # junction dots at interior T-taps
            lp = cp["label"]
            labels.append({"text": net["name"], "x": lp[0], "y": lp[1]})
            label_pts.append((net["name"], lp))
            for a, o, pp in pins:                      # every NON-cluster pin keeps its label
                if pp in cp["pins"]:
                    continue
                owner = pp.split(".", 1)[0]; u = _outdir(a, pp, pos)
                hw = max(len(net["name"]), 1) * 1.27 * 0.85 / 2 + 0.4
                segs, end = _route_stub(a, u, wire_segs, allboxes, owner, (hw, 1.06),
                                        forbidden_pts=forbidden_for(net["name"]))
                wire_segs.extend(segs)
                labels.append({"text": net["name"], "x": end[0], "y": end[1]})
                label_pts.append((net["name"], end))
                allboxes["#L:%d" % len(labels)] = (end[0]-hw, end[0]+hw, end[1]-1.06, end[1]+1.06)
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
            colinear = any(_colinear(sg, e) for sg in net_segs for e in wire_segs)
            forb = forbidden_for(net["name"])         # foreign pins + labels (avoid shorts)
            touches = any(_pt_on_seg(pt, p, q, 0.4) for (p, q) in net_segs for pt in forb)
            # RULE B: don't cross any box; RULE A: don't touch a foreign pin/label
            if crosses_any_box(net_segs) or colinear or touches:
                use_labels = True
        if use_labels:                                # connect-by-name label per pin
            for a, o, pp in pins:
                owner = pp.split(".", 1)[0]; u = _outdir(a, pp, pos)
                hw = max(len(net["name"]), 1) * 1.27 * 0.85 / 2 + 0.4
                segs, end = _route_stub(a, u, wire_segs, allboxes, owner, (hw, 1.06),
                                        forbidden_pts=forbidden_for(net["name"]))
                wire_segs.extend(segs)
                labels.append({"text": net["name"], "x": end[0], "y": end[1]})
                label_pts.append((net["name"], end))   # so later wires/labels avoid it
                allboxes["#L:%d" % len(labels)] = (end[0]-hw, end[0]+hw, end[1]-1.06, end[1]+1.06)
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

def field_layout(spec, pos, syms, wire_segs, flags, labels):
    """Auto-place every part's Reference/Value clear of bodies/pins/wires/labels
    and each other. Returns ({ref: fieldpos}, {ref: {name: box}}, obs) - shared by
    place_all (to emit) and violations (to verify)."""
    obs = build_obstacles(spec, pos, syms, wire_segs, flags, labels)
    fpos = {}; fbox = {}
    for p in spec["parts"]:
        r = p["ref"]
        if r not in pos:
            continue
        x, y = pos[r]; o = _orients[0].get(r, 0)
        fp = autoplace_fields(syms[r], x, y, r, p.get("value", ""), obs, orient=o)
        fpos[r] = fp
        fbox[r] = {"Reference": _fbox(fp["Reference"], r),
                   "Value": _fbox(fp["Value"], p.get("value", ""))}
    return fpos, fbox, obs


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
    for (p, q) in wire_segs:                                # wire crosses a foreign element box
        for r, E in eb.items():
            if _seg_hits_box(p, q, E):
                if not _seg_hits_box(p, q, (E[0]+1.0, E[1]-1.0, E[2]+1.0, E[3]-1.0)):
                    continue
                v.append({"type": "WIRE_CROSS_BOX", "box": r, "seg": [p, q]}); break
    for i, j in itertools.combinations(range(len(wire_segs)), 2):   # two wires share a path
        if _colinear(wire_segs[i], wire_segs[j]):
            v.append({"type": "WIRE_COLINEAR", "segs": [wire_segs[i], wire_segs[j]]})
    # RULE A: a wire segment touching pins of MORE THAN ONE net = a false short
    pin2net = {pp: n["name"] for n in spec.get("nets", []) for pp in n.get("pins", [])}
    pinpts = {}
    for ref in pos:
        for pn in syms[ref]["pins"]:
            ab, _ = _pin_abs(f"{ref}.{pn['number']}", pos, syms)
            if ab is not None:
                pinpts["%s.%s" % (ref, pn["number"])] = ab
    for (p, q) in wire_segs:
        on = [pk for pk, pt in pinpts.items() if _pt_on_seg(pt, p, q, 0.3)]
        if len({pin2net.get(pk) for pk in on}) > 1:
            v.append({"type": "WIRE_HITS_PIN", "pins": on})
    fb = [(f, (f["x"]-_FLAG_HALF[0]+0.1, f["x"]+_FLAG_HALF[0]-0.1, f["y"]-_FLAG_HALF[1]+0.1, f["y"]+_FLAG_HALF[1]-0.1)) for f in flags]
    for (f1, b1), (f2, b2) in itertools.combinations(fb, 2):
        if _overlap(b1, b2):
            v.append({"type": "FLAG_OVERLAP", "refs": [f1["ref"], f2["ref"]]})
    # FIELD: a Reference/Value must not overlap any element box, pin or wire
    _, fbox, _ = field_layout(spec, pos, syms, wire_segs, flags, labels)
    wrects = [_seg_rect(p, q, 0.25) for (p, q) in wire_segs]
    pinbx = [(pt[0]-0.5, pt[0]+0.5, pt[1]-0.5, pt[1]+0.5) for pt in pinpts.values()]
    for r, boxes in fbox.items():
        for nm, B in boxes.items():
            if (any(_overlap(B, eb[r2]) for r2 in eb) or
                    any(_overlap(B, w) for w in wrects) or
                    any(_overlap(B, pb) for pb in pinbx)):
                v.append({"type": "FIELD_OVERLAP", "field": "%s.%s" % (r, nm), "ref": r})
    return v


if __name__ == "__main__":
    spec_path = sys.argv[1]
    spec = json.load(open(spec_path))
    # resolve local_libs paths relative to the spec file (parity with kicad_live)
    base = os.path.dirname(os.path.abspath(spec_path))
    spec["local_libs"] = {k: (v if os.path.isabs(v) else os.path.join(base, v))
                          for k, v in (spec.get("local_libs") or {}).items()}
    url = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--bridge" else "http://172.23.48.1:8761"
    place_all(spec, url)
