#!/usr/local/bin/python3
"""
Generate a KiCad schematic (.kicad_sch) from a netlist spec, using REAL symbols
pulled from KiCad symbol libraries.

Same division of labour as the symbol skill:
  MODEL  -> the netlist (parts + nets): which lib parts, which pins connect.
  CODE   -> this script: resolve each lib_id to its real symbol, embed it,
            place instances on a grid, and realise connectivity.
  VALIDATOR (validate_schematic.py) -> proves lib_ids resolve, net pins exist.

Connectivity strategy: NET LABELS, not wire routing. Every pin on a net gets a
`global_label` with the net's name, placed exactly on that pin's connection
point. KiCad merges same-named global labels into one net - so layout is free
and there is no fragile wire-to-pin geometry to get wrong. The only geometry
that matters is label-on-pin, which is pure arithmetic from the symbol's pin
coordinates.

Coordinate transform (library space -> schematic space): x' = px + x_sym,
y' = py - y_sym. Symbol libs are Y-up; schematics are Y-down, so Y is negated.
Instances are placed unrotated to keep this exact.

Netlist spec (JSON):
{
  "title": "AD7380 decoupling + SPI header",
  "project": "ad7380_poc",
  "libdirs": ["/tmp/kicad-symbols"],          # dirs of <Nick>.kicad_symdir/<Sym>.kicad_sym (or flat <Nick>.kicad_sym)
  "local_libs": {"Custom": "../AD7380BCPZ-RL.kicad_sym"},  # nick -> file
  "parts": [
    {"ref": "U1", "lib_id": "Custom:AD7380BCPZ-RL", "value": "AD7380BCPZ-RL"},
    {"ref": "C1", "lib_id": "Device:C", "value": "1uF"}
  ],
  "nets": [
    {"name": "VCC", "pins": ["U1.4", "C1.1"]},
    {"name": "GND", "pins": ["U1.1", "U1.10", "U1.17", "C1.2"]}
  ]
}

Usage:  gen_schematic.py netlist.json [out.kicad_sch]
"""
import json
import math
import os
import sys
import uuid

from kiutils.symbol import SymbolLib

GRID = 2.54


def uid():
    return str(uuid.uuid4())


def label_dir(pin_angle):
    """Map a symbol pin's library angle to the (label_angle, justify) that makes
    the net label extend OUTWARD from the pin (away from the body), the way KiCad
    itself writes labels: text angle is ONLY ever 0 or 90 (never 180/270, which
    render upside-down/mirrored) and the OUTWARD side is chosen with justify.

    KiCad pin angle = the direction the pin points, away from the body:
      0   -> pin on the left,  points left  (-x) -> label angle 0,  justify right
      180 -> pin on the right, points right (+x) -> label angle 0,  justify left
      90  -> pin at the bottom, points down (+y) -> label angle 90, justify right
      270 -> pin at the top,    points up   (-y) -> label angle 90, justify left
    (justify left = text grows along the reading direction; right = opposite.)"""
    return {
        0:   (0, "right"),
        180: (0, "left"),
        90:  (90, "right"),
        270: (90, "left"),
    }.get(pin_angle % 360, (0, "left"))


def global_label(name, x, y, angle, justify):
    return (f'\t(global_label "{name}" (shape input) (at {x:g} {y:g} {angle:g})'
            f' (effects (font (size 1.27 1.27)) (justify {justify})) (uuid {uid()}))')


def wire(x1, y1, x2, y2):
    return (f'\t(wire (pts (xy {x1:g} {y1:g}) (xy {x2:g} {y2:g}))'
            f' (stroke (width 0) (type default) (color 0 0 0 0)) (uuid {uid()}))')


def junction(x, y):
    return f'\t(junction (at {x:g} {y:g}) (diameter 0) (color 0 0 0 0) (uuid {uid()}))'


def resolve_symbol(lib_id, libdirs, local_libs):
    """Find the kiutils Symbol for a lib_id like 'Device:C' or 'FRA:LMH6518SQ/NOPB'.

    A local_libs entry may be EITHER a single .kicad_sym file OR a directory of
    per-symbol files - in the directory case the symbol is matched by its own
    name (which can differ from the filename, e.g. file LMH6518SQ.kicad_sym holds
    symbol "LMH6518SQ/NOPB"). lib_id names are matched exactly via entryName.
    """
    nick, name = lib_id.split(":", 1) if ":" in lib_id else (None, lib_id)

    def match_in(path):
        for s in SymbolLib.from_file(path).symbols:
            if s.entryName == name:
                return s
        return None

    if nick and nick in local_libs:
        p = local_libs[nick]
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                if f.endswith(".kicad_sym"):
                    s = match_in(os.path.join(p, f))
                    if s:
                        return s
        elif os.path.isfile(p):
            s = match_in(p)
            if s:
                return s
        raise SystemExit(f"cannot resolve {lib_id!r}: no symbol {name!r} under {p}")

    for d in libdirs:
        for cand in (os.path.join(d, f"{nick}.kicad_symdir", f"{name}.kicad_sym"),
                     os.path.join(d, f"{nick}.kicad_sym")):
            if os.path.isfile(cand):
                s = match_in(cand)
                if s:
                    return s
    raise SystemExit(f"cannot resolve lib_id {lib_id!r} in {libdirs} / {list(local_libs)}")


def _pin_map(sym):
    return {p.number: (p.position.X, p.position.Y, p.position.angle)
            for u in sym.units for p in u.pins}


def load_symbol(lib_id, libdirs, local_libs):
    """Return (sym, {pin_number: (x, y, angle)}, sym) for a placeable symbol.

    Follows `extends` and FLATTENS: a derived symbol (e.g. Transistor_FET:2N7002
    extends Q_NMOS_GSD) carries NO geometry of its own - its pins and graphics
    live on the parent in the same library. We walk the extends-chain to the
    ancestor that holds the pins, then copy that geometry (the unit sub-symbols)
    INTO the derived symbol and drop the `extends`. The derived symbol keeps its
    own identity (name, footprint, properties) but becomes self-contained: the
    emitted `lib_symbol` carries real pins, so KiCad renders it with pins
    ANYWHERE, with no dependency on resolving `extends` against an installed
    library. (Embedding the bare parent + `(extends ...)` instead is the other
    KiCad-native option, but stock KiCad does not expand an in-file extends for
    a v20211014 schematic, leaving the pins - and any net labels on them -
    dangling; flattening sidesteps that entirely.)

    If pins still can't be found, FAIL LOUDLY instead of emitting a pinless part.
    """
    import copy
    sym = resolve_symbol(lib_id, libdirs, local_libs)
    sym.libId = lib_id
    name = lib_id.split(":", 1)[1] if ":" in lib_id else lib_id
    nick = lib_id.split(":", 1)[0] if ":" in lib_id else None
    src = sym
    guard = 0
    while not _pin_map(src) and getattr(src, "extends", None) and nick and guard < 6:
        src = resolve_symbol(f"{nick}:{src.extends}", libdirs, local_libs)
        guard += 1
    pins = _pin_map(src)
    if not pins:
        ext = getattr(sym, "extends", None)
        raise SystemExit(f"{lib_id!r}: symbol has 0 pins"
                         + (f" (extends {ext!r} - parent not resolvable)"
                            if ext else " (cannot place a pinless part)"))
    if src is not sym:
        # flatten the ancestor's geometry into the derived symbol; rename the
        # unit sub-symbols ("Q_NMOS_GSD_1_1" -> "2N7002_1_1") so their names
        # track the owning symbol, as KiCad writes them.
        parent_entry = src.entryName
        units = copy.deepcopy(src.units)
        for u in units:
            if u.libId and u.libId.startswith(parent_entry + "_"):
                u.libId = name + u.libId[len(parent_entry):]
        sym.units = units
        sym.extends = None
    return sym, pins, sym


def _is_gnd(net):
    return bool(net) and (net.upper() in ("GND", "AGND", "DGND", "VSS", "GNDA", "GNDD")
                          or "GND" in net.upper())


def _is_vert_2pin(pm):
    """pm: {num: (x, y, ang)}. True for a vertically-aligned 2-pin part (both
    pins share X, differ in Y) - i.e. a Device:C / C_Small / R style cap/resistor
    that can be wired into a horizontal rail."""
    if len(pm) != 2:
        return False
    (x1, y1, _), (x2, y2, _) = pm.values()
    return x1 == x2 and y1 != y2


def _is_cap_part(lib_id):
    """True ONLY for capacitor symbols. The decoupling-cap rail wiring must key on
    part TYPE, not just geometry: a vertical 2-pin part with one pin on a rail and
    one on GND can also be a Conn_01x02 power inlet, a resistor, an inductor, etc.
    Wiring those into a cap rail mis-connects them (e.g. a 9V/GND inlet connector
    grabbed onto a decoupling row -> dangling pins). Restrict to real caps."""
    name = lib_id.split(":")[-1]
    return name == "C" or name.startswith("C_") or name in ("CP", "CP_Small")


# --- layout geometry -------------------------------------------------------
# A4 landscape (KiCad schematic A4 is 297x210 landscape by default). We pack the
# whole design into the usable area first; only if it genuinely does not fit do
# we grow the sheet (length first, keeping A4 width) instead of ballooning it.
A4L_W, A4L_H = 297.0, 210.0
PAGE_X0, PAGE_Y0 = 12.7, 12.7          # top-left of the content area (on grid)
RIGHT_MARGIN, BOTTOM_RESERVE = 12.7, 25.4   # right edge / title-block strip
USABLE_W = A4L_W - PAGE_X0 - RIGHT_MARGIN
GAP = 5.08                              # clearance between adjacent blocks
CHAR_W = 1.1                            # approx advance of a 1.27mm glyph
CAP_STEP = 10.16                        # cap pitch in a rail row (multiple of GRID)
_OUT = {0: (-1, 0), 180: (1, 0), 90: (0, 1), 270: (0, -1)}   # label outward dir


def _snap(v):
    return round(v / GRID) * GRID


def _reach(net):
    """How far a net label sticks out past its pin: flag + text."""
    return 2.54 + (len(net) * CHAR_W if net else 0)


def _member_bbox(dx, pmap, ref, value, pin_net):
    """Bbox of one part (pins + its net labels + ref/value text), in schematic
    coords relative to the part origin, shifted right by dx. Returns
    (xmin, xmax, ymin, ymax). Schematic offset of a library pin (x, y) is (x, -y)."""
    xs = [x for x, _, _ in pmap.values()]
    ys = [y for _, y, _ in pmap.values()]
    xmin, xmax = min(xs) + dx, max(xs) + dx
    ymin, ymax = -max(ys), -min(ys)
    for num, (x, y, ang) in pmap.items():
        net = pin_net.get(f"{ref}.{num}")
        if not net:
            continue
        ox, oy = _OUT.get(ang % 360, (1, 0))
        ex, ey = x + dx + ox * _reach(net), -y + oy * _reach(net)
        xmin, xmax = min(xmin, ex), max(xmax, ex)
        ymin, ymax = min(ymin, ey), max(ymax, ey)
    vw = max(len(ref), len(value or "")) * CHAR_W / 2 + 0.5     # ref/value text
    xmin, xmax = min(xmin, dx - vw), max(xmax, dx + vw)
    # ref sits 1.27mm above the top pin row, value 1.27mm below the bottom row
    ymin = min(ymin, -max(ys) - 1.27 - 1.6)
    ymax = max(ymax, -min(ys) + 1.27 + 1.6)
    return xmin, xmax, ymin, ymax


def plan_placement(parts, nets, sym_cache):
    """Place parts without overlaps and identify decoupling-cap rail groups.

    Each part (and each multi-cap rail, kept as one horizontal row so emit() can
    wire it under a single label) becomes a BLOCK whose bounding box includes its
    body, its outward net labels and its ref/value text. Blocks are shelf-packed
    left-to-right, wrapping into rows, into the A4-landscape usable width - so no
    block (component or label) overlaps another. Returns (pos, cap_groups, page)
    where page = (width, height): A4 landscape if everything fits, otherwise the
    sheet grown in length (A4 width kept) up to KiCad's 1200mm limit."""
    pin_net = {pr: n["name"] for n in nets for pr in n["pins"]}

    def pmap(p):
        return sym_cache[p["lib_id"]][1]

    # classify decoupling caps -> {ref: {rail, gnd, rail_pin, gnd_pin}}
    caps = {}
    for p in parts:
        pm = pmap(p)
        if not _is_cap_part(p["lib_id"]) or not _is_vert_2pin(pm):
            continue
        on = {pn: pin_net.get(f"{p['ref']}.{pn}") for pn in pm}
        gp = [pn for pn, nn in on.items() if _is_gnd(nn)]
        rp = [pn for pn, nn in on.items() if nn and not _is_gnd(nn)]
        if len(gp) == 1 and len(rp) == 1:
            caps[p["ref"]] = {"rail": on[rp[0]], "gnd": on[gp[0]],
                              "rail_pin": rp[0], "gnd_pin": gp[0]}

    # group caps by rail net, preserving netlist order
    rail_order, rail_members = [], {}
    for p in parts:
        if p["ref"] in caps:
            r = caps[p["ref"]]["rail"]
            if r not in rail_members:
                rail_order.append(r)
                rail_members[r] = []
            rail_members[r].append(p["ref"])

    by_ref = {p["ref"]: p for p in parts}

    # build the block list: non-cap parts first, then cap rails. A rail with >= 2
    # consistent caps is one wired row block; otherwise its caps are single blocks.
    def make_block(refs):
        members = []
        for j, r in enumerate(refs):
            p = by_ref[r]
            members.append((j * CAP_STEP, pmap(p), r, p.get("value", "")))
        bb = [_member_bbox(dx, pm, r, v, pin_net) for dx, pm, r, v in members]
        return {"members": members,
                "xmin": min(b[0] for b in bb), "xmax": max(b[1] for b in bb),
                "ymin": min(b[2] for b in bb), "ymax": max(b[3] for b in bb)}

    blocks = []
    cap_groups = []
    for p in parts:
        if p["ref"] not in caps:
            blocks.append(make_block([p["ref"]]))
    for rail in rail_order:
        members = rail_members[rail]
        c0 = caps[members[0]]
        rpin, gpin = c0["rail_pin"], c0["gnd_pin"]
        consistent = all(caps[r]["rail_pin"] == rpin and caps[r]["gnd_pin"] == gpin
                         for r in members)
        if len(members) >= 2 and consistent:
            blocks.append(make_block(members))
            cap_groups.append({"rail": rail, "gnd": c0["gnd"], "refs": members,
                               "rail_pin": rpin, "gnd_pin": gpin})
        else:
            for r in members:
                blocks.append(make_block([r]))

    # shelf-pack blocks into the A4-landscape usable width
    pos = {}
    cx, cy, rowh, right = PAGE_X0, PAGE_Y0, 0.0, PAGE_X0
    for blk in blocks:
        w, h = blk["xmax"] - blk["xmin"], blk["ymax"] - blk["ymin"]
        if cx > PAGE_X0 and cx + w > PAGE_X0 + USABLE_W:
            cx, cy, rowh = PAGE_X0, cy + rowh + GAP, 0.0
        ox, oy = -blk["xmin"], -blk["ymin"]
        for dx, _pm, r, _v in blk["members"]:
            pos[r] = (_snap(cx + ox + dx), _snap(cy + oy))
        right = max(right, cx + w)
        cx, rowh = cx + w + GAP, max(rowh, h)
    content_bottom = cy + rowh

    need_w, need_h = right + RIGHT_MARGIN, content_bottom + BOTTOM_RESERVE
    if need_w <= A4L_W and need_h <= A4L_H:
        page = (A4L_W, A4L_H)
    else:
        page = (min(1200.0, max(A4L_W, math.ceil(need_w))),
                min(1200.0, max(A4L_H, math.ceil(need_h))))
        if need_w > 1200 or need_h > 1200:
            sys.stderr.write("warning: content exceeds KiCad's 1200mm page limit; "
                             "split the netlist into multiple sheets.\n")
    return pos, cap_groups, page


def emit(spec, base_dir):
    libdirs = spec.get("libdirs", [])
    local = {k: os.path.join(base_dir, v) if not os.path.isabs(v) else v
             for k, v in spec.get("local_libs", {}).items()}
    project = spec.get("project", "schematic")
    root = uid()

    # resolve every unique lib_id once
    sym_cache = {}
    for part in spec["parts"]:
        lid = part["lib_id"]
        if lid not in sym_cache:
            sym_cache[lid] = load_symbol(lid, libdirs, local)

    # --- ERC cleanliness: drive undriven power nets with PWR_FLAG ---
    # A power_in pin (VCC/GND/...) raises "power_pin_not_driven" unless something
    # of an output kind drives the net. If a net has a power_in pin but no driver,
    # inject a power:PWR_FLAG (a power_out stub) onto it.
    def pin_etype(lib_id, pin):
        for u in sym_cache[lib_id][2].units:
            for p in u.pins:
                if p.number == pin:
                    return p.electricalType
        return None

    DRIVERS = {"power_out", "power_output", "output", "bidirectional",
               "open_collector", "tri_state"}
    parts = list(spec["parts"])
    ref_lib = {p["ref"]: p["lib_id"] for p in parts}
    added_flags = []
    for net in spec["nets"]:
        etypes = [pin_etype(ref_lib[r], pn) for r, pn in
                  (pr.split(".") for pr in net["pins"])]
        if any(t == "power_in" for t in etypes) and not any(t in DRIVERS for t in etypes):
            fref = f"#FLG{len(added_flags) + 1}"
            parts.append({"ref": fref, "lib_id": "power:PWR_FLAG", "value": "PWR_FLAG"})
            ref_lib[fref] = "power:PWR_FLAG"
            sym_cache.setdefault("power:PWR_FLAG", load_symbol("power:PWR_FLAG", libdirs, local))
            net["pins"] = list(net["pins"]) + [f"{fref}.1"]
            added_flags.append((fref, net["name"]))

    # place parts (augmented list, including any PWR_FLAGs); decoupling caps
    # are grouped into wired rail rows; everything is packed without overlaps.
    pos, cap_groups, page = plan_placement(parts, spec["nets"], sym_cache)
    placed = {}
    for part in parts:
        px, py = pos[part["ref"]]
        placed[part["ref"]] = (part, px, py)

    # build pin->abs-position index for label placement and validation
    def abs_pin(ref, pin):
        part, px, py = placed[ref]
        pins = sym_cache[part["lib_id"]][1]
        if pin not in pins:
            raise SystemExit(f"net references {ref}.{pin} but {part['lib_id']} has no pin {pin}")
        x, y, ang = pins[pin]
        return (px + x, py - y, ang)        # Y-up library -> Y-down schematic

    # page: A4 landscape if the packed layout fit, else the sheet grown in length
    pw, ph = page
    paper = '(paper "A4")' if (pw, ph) == (A4L_W, A4L_H) else f'(paper "User" {pw:g} {ph:g})'

    lines = ['(kicad_sch',
             f'\t(version 20211014)',
             f'\t(generator kicad_schematic_skill)',
             f'\t(uuid {root})',
             f'\t{paper}',
             f'\t(title_block (title "{spec.get("title", project)}"))',
             '\t(lib_symbols']
    for lid, (sym, _, _) in sym_cache.items():
        body = sym.to_sexpr(indent=4)
        lines.append("\t\t" + body.rstrip("\n").replace("\n", "\n\t\t"))
    lines.append('\t)')

    # component instances
    for part in parts:
        ref = part["ref"]
        _, px, py = placed[ref]
        pins = sym_cache[part["lib_id"]][1]
        lines.append('\t(symbol')
        lines.append(f'\t\t(lib_id "{part["lib_id"]}")')
        lines.append(f'\t\t(at {px:g} {py:g} 0)')
        lines.append('\t\t(unit 1)')
        lines.append('\t\t(in_bom yes) (on_board yes)')
        lines.append(f'\t\t(uuid {uid()})')
        # Reference above the symbol's top edge, Value below the bottom edge
        # (derived from pin extent). Placing them at origin +/- 2.54 buries the
        # text inside large multi-pin IC bodies, where it is invisible.
        ys_l = [y for _, y, _ in pins.values()]
        ref_y, val_y = py - max(ys_l) - 1.27, py - min(ys_l) + 1.27
        lines.append(f'\t\t(property "Reference" "{ref}" (at {px:g} {ref_y:g} 0)'
                     f' (effects (font (size 1.27 1.27))))')
        lines.append(f'\t\t(property "Value" "{part.get("value", "")}" (at {px:g} {val_y:g} 0)'
                     f' (effects (font (size 1.27 1.27))))')
        if part.get("footprint"):
            lines.append(f'\t\t(property "Footprint" "{part["footprint"]}" (at {px:g} {py:g} 0)'
                         f' (effects (font (size 1.27 1.27)) hide))')
        for pnum in pins:
            lines.append(f'\t\t(pin "{pnum}" (uuid {uid()}))')
        lines.append('\t\t(instances')
        lines.append(f'\t\t\t(project "{project}"')
        lines.append(f'\t\t\t\t(path "/{root}" (reference "{ref}") (unit 1))')
        lines.append('\t\t\t)')
        lines.append('\t\t)')
        lines.append('\t)')

    # --- decoupling-cap rails: wire each group + ONE label per rail ---
    # Caps sharing a supply rail are laid in a row (plan_placement). Connect their
    # rail pins with one horizontal wire and their GND pins with another, then put
    # a SINGLE net label on each wire instead of one on every cap pin. The owning
    # IC keeps its own (oriented) label, so the nets still merge by name.
    wired_pins = set()
    for g in cap_groups:
        rpin, gpin = g["rail_pin"], g["gnd_pin"]
        rail_pts = [abs_pin(r, rpin) for r in g["refs"]]
        gnd_pts = [abs_pin(r, gpin) for r in g["refs"]]
        for r in g["refs"]:
            wired_pins.add(f"{r}.{rpin}")
            wired_pins.add(f"{r}.{gpin}")
        for pts, name in ((rail_pts, g["rail"]), (gnd_pts, g["gnd"])):
            spts = sorted(pts, key=lambda p: p[0])
            x0, y0, ang0 = spts[0]
            lines.append(wire(x0, y0, spts[-1][0], y0))    # one wire spans all pins
            # KiCad does NOT connect a pin sitting mid-span on a wire without a
            # junction; the two end pins connect as wire endpoints, the rest need
            # an explicit junction.
            for jx, jy, _ in spts[1:-1]:
                lines.append(junction(jx, jy))
            la, lj = label_dir(ang0)                        # one label at the left pin
            lines.append(global_label(name, x0, y0, la, lj))

    # net labels: one oriented global_label per remaining (non-wired) pin
    for net in spec["nets"]:
        for pinref in net["pins"]:
            if pinref in wired_pins:
                continue
            ref, pin = pinref.split(".")
            x, y, ang = abs_pin(ref, pin)
            la, lj = label_dir(ang)
            lines.append(global_label(net["name"], x, y, la, lj))

    # --- ERC cleanliness: no_connect on pins the netlist leaves unconnected ---
    # The netlist is the complete connection spec; any pin not in a net is
    # intentionally open, so mark it no_connect to silence "pin_not_connected".
    connected = {pr for net in spec["nets"] for pr in net["pins"]}
    no_connects = []
    for part in parts:
        if part["lib_id"] == "power:PWR_FLAG":
            continue
        ref = part["ref"]
        for pnum in sym_cache[part["lib_id"]][1]:
            if f"{ref}.{pnum}" not in connected:
                x, y, _ = abs_pin(ref, pnum)
                lines.append(f'\t(no_connect (at {x:g} {y:g}) (uuid {uid()}))')
                no_connects.append(f"{ref}.{pnum}")

    emit.report = {"flags": added_flags, "no_connects": no_connects, "parts": parts}

    lines.append('\t(sheet_instances')
    lines.append('\t\t(path "/" (page "1"))')
    lines.append('\t)')
    lines.append(')')
    return "\n".join(lines) + "\n"


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    spec_path = sys.argv[1]
    spec = json.load(open(spec_path))
    base_dir = os.path.dirname(os.path.abspath(spec_path))
    text = emit(spec, base_dir)
    out = sys.argv[2] if len(sys.argv) > 2 else f'{spec.get("project", "schematic")}.kicad_sch'
    open(out, "w").write(text)
    rep = getattr(emit, "report", {"flags": [], "no_connects": [], "parts": spec["parts"]})
    print(f'wrote {out}: {len(rep["parts"])} parts '
          f'(+{len(rep["flags"])} PWR_FLAG), {len(spec["nets"])} nets')
    if rep["flags"]:
        print("  PWR_FLAG added to nets: " + ", ".join(n for _, n in rep["flags"]))
    if rep["no_connects"]:
        print(f'  no_connect on {len(rep["no_connects"])} open pins: '
              + ", ".join(rep["no_connects"]))


if __name__ == "__main__":
    main()
