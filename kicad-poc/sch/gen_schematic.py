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


def find_symbol_file(lib_id, libdirs, local_libs):
    """Return (filepath, symbol_name) for a lib_id like 'Device:C'."""
    nick, name = lib_id.split(":", 1) if ":" in lib_id else (None, lib_id)
    if nick and nick in local_libs:
        return local_libs[nick], name
    for d in libdirs:
        cand = os.path.join(d, f"{nick}.kicad_symdir", f"{name}.kicad_sym")
        if os.path.isfile(cand):
            return cand, name
        flat = os.path.join(d, f"{nick}.kicad_sym")
        if os.path.isfile(flat):
            return flat, name
    raise SystemExit(f"cannot resolve lib_id {lib_id!r} in {libdirs} / {list(local_libs)}")


def load_symbol(lib_id, libdirs, local_libs):
    """Return (kiutils Symbol with libId set, {pin_number: (x, y, angle)})."""
    path, name = find_symbol_file(lib_id, libdirs, local_libs)
    lib = SymbolLib.from_file(path)
    sym = next((s for s in lib.symbols if s.entryName == name), None)
    if sym is None:  # local single-symbol file: take the only one
        sym = lib.symbols[0]
    sym.libId = lib_id
    pins = {}
    for u in sym.units:
        for p in u.pins:
            pins[p.number] = (p.position.X, p.position.Y, p.position.angle)
    return sym, pins


def grid_layout(n, cols=4, dx=38.1, dy=38.1, x0=63.5, y0=63.5):
    """Simple wrapped-grid placement; layout is irrelevant to connectivity."""
    out = []
    for i in range(n):
        r, c = divmod(i, cols)
        out.append((round((x0 + c * dx) / GRID) * GRID,
                    round((y0 + r * dy) / GRID) * GRID))
    return out


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

    # place parts
    positions = grid_layout(len(spec["parts"]))
    placed = {}
    for part, (px, py) in zip(spec["parts"], positions):
        placed[part["ref"]] = (part, px, py)

    # build pin->abs-position index for label placement and validation
    def abs_pin(ref, pin):
        part, px, py = placed[ref]
        _, pins = sym_cache[part["lib_id"]]
        if pin not in pins:
            raise SystemExit(f"net references {ref}.{pin} but {part['lib_id']} has no pin {pin}")
        x, y, _ = pins[pin]
        return (px + x, py - y)        # Y-up library -> Y-down schematic

    lines = ['(kicad_sch',
             f'\t(version 20211014)',
             f'\t(generator kicad_schematic_skill)',
             f'\t(uuid {root})',
             f'\t(paper "A4")',
             f'\t(title_block (title "{spec.get("title", project)}"))',
             '\t(lib_symbols']
    for lid, (sym, _) in sym_cache.items():
        body = sym.to_sexpr(indent=4)
        lines.append("\t\t" + body.rstrip("\n").replace("\n", "\n\t\t"))
    lines.append('\t)')

    # component instances
    for part in spec["parts"]:
        ref = part["ref"]
        _, px, py = placed[ref]
        sym, pins = sym_cache[part["lib_id"]]
        lines.append('\t(symbol')
        lines.append(f'\t\t(lib_id "{part["lib_id"]}")')
        lines.append(f'\t\t(at {px:g} {py:g} 0)')
        lines.append('\t\t(unit 1)')
        lines.append('\t\t(in_bom yes) (on_board yes)')
        lines.append(f'\t\t(uuid {uid()})')
        lines.append(f'\t\t(property "Reference" "{ref}" (at {px:g} {py-2.54:g} 0)'
                     f' (effects (font (size 1.27 1.27))))')
        lines.append(f'\t\t(property "Value" "{part.get("value", "")}" (at {px:g} {py+2.54:g} 0)'
                     f' (effects (font (size 1.27 1.27))))')
        for pnum in pins:
            lines.append(f'\t\t(pin "{pnum}" (uuid {uid()}))')
        lines.append('\t\t(instances')
        lines.append(f'\t\t\t(project "{project}"')
        lines.append(f'\t\t\t\t(path "/{root}" (reference "{ref}") (unit 1))')
        lines.append('\t\t\t)')
        lines.append('\t\t)')
        lines.append('\t)')

    # net labels: one global_label per pin on each net
    for net in spec["nets"]:
        for pinref in net["pins"]:
            ref, pin = pinref.split(".")
            x, y = abs_pin(ref, pin)
            lines.append(f'\t(global_label "{net["name"]}" (shape input) (at {x:g} {y:g} 0)'
                         f' (effects (font (size 1.27 1.27)) (justify left)) (uuid {uid()}))')

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
    npins = sum(len(n["pins"]) for n in spec["nets"])
    print(f'wrote {out}: {len(spec["parts"])} parts, {len(spec["nets"])} nets, {npins} pin-connections')


if __name__ == "__main__":
    main()
