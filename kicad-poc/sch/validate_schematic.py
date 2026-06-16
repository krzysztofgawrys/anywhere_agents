#!/usr/local/bin/python3
"""
Validate a generated .kicad_sch structurally + against its netlist spec.

Checks:
  * file is well-formed s-expression (sexpdata parse)
  * kiutils can round-trip it (extra signal it's a real KiCad schematic)
  * every instance lib_id has a matching definition in (lib_symbols ...)
  * every net pin "REF.PIN": REF is a placed part, PIN exists in that symbol
  * every net has >= 2 pins (a 1-pin net is dangling) -> reported
  * one global_label emitted per pin-connection (count matches the netlist)

Usage:  validate_schematic.py out.kicad_sch --spec netlist.json
"""
import json
import os
import re
import sys

import sexpdata
from kiutils.symbol import SymbolLib


def find_symbol_file(lib_id, libdirs, local_libs):
    nick, name = lib_id.split(":", 1) if ":" in lib_id else (None, lib_id)
    if nick and nick in local_libs:
        return local_libs[nick], name
    for d in libdirs:
        c = os.path.join(d, f"{nick}.kicad_symdir", f"{name}.kicad_sym")
        if os.path.isfile(c):
            return c, name
        f = os.path.join(d, f"{nick}.kicad_sym")
        if os.path.isfile(f):
            return f, name
    return None, name


def pins_of(lib_id, libdirs, local_libs):
    path, name = find_symbol_file(lib_id, libdirs, local_libs)
    if not path:
        return None
    lib = SymbolLib.from_file(path)
    sym = next((s for s in lib.symbols if s.entryName == name), lib.symbols[0])
    return {p.number for u in sym.units for p in u.pins}


def main():
    args = sys.argv[1:]
    spec = None
    if "--spec" in args:
        i = args.index("--spec")
        spec_path = args[i + 1]
        spec = json.load(open(spec_path))
        spec_dir = os.path.dirname(os.path.abspath(spec_path))
        del args[i:i + 2]
    if not args:
        sys.exit(__doc__)
    path = args[0]
    text = open(path).read()
    errors, warnings = [], []

    # 1. well-formed s-expr
    try:
        sexpdata.loads(text)
    except Exception as e:
        sys.exit(f"FAIL: not valid s-expression: {e}")

    # 2. kiutils round-trip (best-effort signal)
    kiutils_ok = True
    try:
        from kiutils.schematic import Schematic
        Schematic().from_file(path)
    except Exception as e:
        kiutils_ok = False
        warnings.append(f"kiutils round-trip failed (format version drift?): {type(e).__name__}: {e}")

    # 3. lib_symbols vs instance lib_ids (regex over the flat text - good enough)
    defined = set(re.findall(r'\(symbol "([^"]+:[^"]+)"', text))   # names with a colon = lib entries
    used = set(re.findall(r'\(lib_id "([^"]+)"', text))
    missing = used - defined
    if missing:
        errors.append(f"instances reference lib_ids absent from lib_symbols: {sorted(missing)}")

    labels = re.findall(r'\(global_label "([^"]+)"', text)

    # 4 + 5. netlist-driven checks
    if spec:
        libdirs = spec.get("libdirs", [])
        local = {k: (v if os.path.isabs(v) else os.path.join(spec_dir, v))
                 for k, v in spec.get("local_libs", {}).items()}
        parts = {p["ref"]: p["lib_id"] for p in spec["parts"]}
        pin_cache = {}
        total_pins = 0
        for net in spec["nets"]:
            if len(net["pins"]) < 2:
                warnings.append(f"net {net['name']!r} has only {len(net['pins'])} pin (dangling)")
            for pr in net["pins"]:
                total_pins += 1
                ref, pin = pr.split(".")
                if ref not in parts:
                    errors.append(f"net {net['name']}: unknown part {ref}")
                    continue
                lid = parts[ref]
                if lid not in pin_cache:
                    pin_cache[lid] = pins_of(lid, libdirs, local)
                ps = pin_cache[lid]
                if ps is None:
                    errors.append(f"cannot resolve symbol {lid} for {ref}")
                elif pin not in ps:
                    errors.append(f"net {net['name']}: {ref}.{pin} - {lid} has no pin {pin}")
        if len(labels) != total_pins:
            errors.append(f"expected {total_pins} global_labels, found {len(labels)}")

    print(f"parsed OK ({len(text)} bytes); kiutils round-trip: {'yes' if kiutils_ok else 'NO'}")
    print(f"lib_symbols defined: {sorted(defined)}")
    print(f"instances use: {sorted(used)}")
    print(f"global_labels: {len(labels)} across nets {sorted(set(labels))}")
    for w in warnings:
        print("  warning: " + w)
    if errors:
        print("\nFAIL:")
        for e in errors:
            print("  - " + e)
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
