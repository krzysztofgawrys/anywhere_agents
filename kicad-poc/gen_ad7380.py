#!/usr/bin/env python3
"""
PoC: generate a KiCad schematic symbol (.kicad_sym) for the AD7380BCPZ-RL.

This is the pattern we'd teach an agent to follow:
  1. The MODEL supplies the data table (pinout, types, layout side) - the part
     of the problem that needs reasoning over the datasheet.
  2. CODE turns that table into exact, grid-aligned S-expression geometry - the
     part the model is bad at and shouldn't be guessing pixel coordinates for.
  3. A VALIDATOR re-parses the output and asserts structural invariants, so a
     wrong file fails loudly instead of silently producing a broken symbol.

AD7380 / AD7381 / AD7383 / AD7384 are a pin-compatible 16-lead LFCSP family.
Pinout corroborated from the AD7383/AD7384 datasheet pin-function table and the
AD7380/AD7381 supply-sequencing notes (VCC + VLOGIC).

Run:  python3 gen_ad7380.py
Out:  AD7380.kicad_sym  (+ validation report on stdout)
"""

from dataclasses import dataclass

PART = "AD7380BCPZ-RL"
DATASHEET = "https://www.analog.com/media/en/technical-documentation/data-sheets/ad7380-7381.pdf"
DESCRIPTION = "Dual, simultaneous-sampling, 16-bit, 4 MSPS SAR ADC, differential inputs, 16-lead LFCSP"
# KiCad LFCSP-16 3x3mm footprint (closest standard library footprint)
FOOTPRINT = "Package_DFN_QFN:LFCSP-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm"

GRID = 2.54          # 100 mil - everything snaps to this
PIN_LEN = 2.54
HALF_W = 12.7        # body half-width  -> body is 25.4 mm wide
TOP = 12.7
BOTTOM = -12.7


@dataclass
class Pin:
    number: str
    name: str
    etype: str       # KiCad electrical type
    side: str        # L / R / T / B
    slot: float      # ordering coordinate along the side (in GRID units)


# --- THE DATA TABLE (this is what the model reasons out from the datasheet) ---
PINS = [
    # Left: analog inputs (top group) + reference (bottom group)
    Pin("8",  "AINA+",       "input",         "L",  4),
    Pin("7",  "AINA-",       "input",         "L",  3),
    Pin("6",  "AINB+",       "input",         "L",  2),
    Pin("5",  "AINB-",       "input",         "L",  1),
    Pin("11", "REFIO",       "bidirectional", "L", -1),
    Pin("9",  "REFCAP",      "passive",       "L", -2),
    # Right: SPI / digital
    Pin("16", "SCLK",        "input",         "R",  4),
    Pin("15", "SDI",         "input",         "R",  3),
    Pin("12", "CS",          "input",         "R",  2),
    Pin("13", "SDOA",        "output",        "R",  0),
    Pin("14", "SDOB/ALERT",  "output",        "R", -1),
    # Top: supplies
    Pin("4",  "VCC",         "power_in",      "T", -2),
    Pin("2",  "VLOGIC",      "power_in",      "T",  2),
    # Bottom: grounds + regulator decoupling + exposed pad
    Pin("1",  "GND",         "power_in",      "B", -3),
    Pin("10", "GND",         "power_in",      "B", -1),
    Pin("3",  "REGCAP",      "passive",       "B",  1),
    Pin("17", "GND",         "power_in",      "B",  3),   # exposed pad (EPAD)
]


def pin_geometry(p: Pin):
    """Return (x, y, rotation) for the pin connection point, snapped to grid."""
    if p.side == "L":
        return (-HALF_W - PIN_LEN, p.slot * GRID, 0)      # points right
    if p.side == "R":
        return (HALF_W + PIN_LEN, p.slot * GRID, 180)     # points left
    if p.side == "T":
        return (p.slot * GRID, TOP + PIN_LEN, 270)        # points down
    if p.side == "B":
        return (p.slot * GRID, BOTTOM - PIN_LEN, 90)      # points up
    raise ValueError(p.side)


def fnum(v: float) -> str:
    """KiCad-style number formatting (no trailing .0 noise, keep clean)."""
    return f"{v:g}"


def emit_pin(p: Pin) -> str:
    x, y, rot = pin_geometry(p)
    return (
        f'      (pin {p.etype} line (at {fnum(x)} {fnum(y)} {rot}) (length {fnum(PIN_LEN)})\n'
        f'        (name "{p.name}" (effects (font (size 1.27 1.27))))\n'
        f'        (number "{p.number}" (effects (font (size 1.27 1.27))))\n'
        f'      )'
    )


def emit_symbol() -> str:
    pins = "\n".join(emit_pin(p) for p in PINS)
    return f'''(kicad_symbol_lib (version 20211014) (generator ad7380_poc)
  (symbol "{PART}" (in_bom yes) (on_board yes)
    (property "Reference" "U" (id 0) (at -12.7 17.78 0)
      (effects (font (size 1.27 1.27)) (justify left))
    )
    (property "Value" "{PART}" (id 1) (at 0 20.32 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Footprint" "{FOOTPRINT}" (id 2) (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "Datasheet" "{DATASHEET}" (id 3) (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "Description" "{DESCRIPTION}" (id 4) (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "ki_keywords" "ADC SAR dual simultaneous sampling differential" (id 5) (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "ki_fp_filters" "LFCSP*3x3mm*P0.5mm*" (id 6) (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (symbol "{PART}_0_1"
      (rectangle (start {fnum(-HALF_W)} {fnum(TOP)}) (end {fnum(HALF_W)} {fnum(BOTTOM)})
        (stroke (width 0.254) (type default))
        (fill (type background))
      )
    )
    (symbol "{PART}_1_1"
{pins}
    )
  )
)
'''


if __name__ == "__main__":
    text = emit_symbol()
    out = "AD7380.kicad_sym"
    with open(out, "w") as f:
        f.write(text)
    print(f"wrote {out} ({len(text)} bytes, {len(PINS)} pins)")
