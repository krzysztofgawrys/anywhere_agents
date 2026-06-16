#!/usr/bin/env python3
"""
Validator for the generated symbol. This is the half that makes an
agent-in-the-loop trustworthy: structural invariants are checked
mechanically, so a hallucinated pin or an off-grid coordinate fails here
instead of silently shipping a broken part.

Checks:
  * file round-trips through kiutils (== syntactically valid KiCad symbol lib)
  * exactly the expected pin numbers are present, none duplicated
  * every pin connection point lies on the 1.27 mm grid (KiCad ERC needs this
    or wires can't attach)
  * power/ground pins carry a power electrical type
"""

import sys
from kiutils.symbol import SymbolLib

EXPECTED_NUMBERS = {str(n) for n in range(1, 18)}  # 1..16 + EPAD=17
GRID = 1.27


def main(path: str) -> int:
    lib = SymbolLib.from_file(path)          # raises on malformed s-expr
    assert len(lib.symbols) == 1, "expected exactly one symbol"
    sym = lib.symbols[0]

    pins = [p for u in sym.units for p in u.pins]
    numbers = [p.number for p in pins]

    errors = []

    # 1. pin set
    got = set(numbers)
    if got != EXPECTED_NUMBERS:
        errors.append(f"pin numbers mismatch: missing {EXPECTED_NUMBERS - got}, "
                      f"extra {got - EXPECTED_NUMBERS}")
    if len(numbers) != len(got):
        dupes = [n for n in got if numbers.count(n) > 1]
        errors.append(f"duplicate pin numbers: {dupes}")

    # 2. grid alignment
    for p in pins:
        for axis, v in (("x", p.position.X), ("y", p.position.Y)):
            if round(v / GRID) != v / GRID:
                errors.append(f"pin {p.number} ({p.name}) off-grid on {axis}: {v}")

    # 3. power pins typed as power
    for p in pins:
        if p.name in ("VCC", "VLOGIC", "GND") and p.electricalType != "power_in":
            errors.append(f"pin {p.number} ({p.name}) should be power_in, "
                          f"got {p.electricalType}")

    print(f"parsed OK: symbol '{sym.entryName}', {len(pins)} pins")
    print("pins: " + ", ".join(f"{p.number}:{p.name}({p.electricalType})" for p in
                                sorted(pins, key=lambda x: int(x.number))))
    if errors:
        print("\nFAIL:")
        for e in errors:
            print("  - " + e)
        return 1
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "AD7380.kicad_sym"))
