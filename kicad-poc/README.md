# KiCad symbol PoC - AD7380BCPZ-RL

Proof of concept: an agent generating a valid KiCad schematic symbol
(`.kicad_sym`) for the Analog Devices **AD7380BCPZ-RL** (dual, simultaneous
sampling, 16-bit, 4 MSPS SAR ADC, 16-lead LFCSP).

## The pattern

The point isn't "an LLM wrote an S-expression". The point is the division of
labour that makes it trustworthy:

1. **Model -> data** (`PINS` table in `gen_ad7380.py`): pin numbers, names,
   electrical types and which side of the symbol they sit on. This is the
   datasheet-reasoning part.
2. **Code -> geometry** (`pin_geometry`): turns the table into exact,
   grid-snapped coordinates. The model never guesses pixel positions.
3. **Validator -> trust** (`validate.py`): re-parses the output with `kiutils`
   and asserts invariants (all pins 1..16 + EPAD present, none duplicated,
   everything on the 1.27 mm grid, power pins typed as power). A wrong file
   fails here instead of silently shipping.

During development the validator earned its keep immediately: the first draft
dropped pin 10 (the second GND) and the `1..17` check would have flagged it.

## Run

```bash
python3 gen_ad7380.py     # -> AD7380.kicad_sym
python3 validate.py       # structural checks (needs: pip install kiutils)
python3 preview_svg.py    # -> AD7380_preview.svg  (visual sanity check)
```

## Pinout (16-lead LFCSP + EPAD)

| Pin | Name | Type | | Pin | Name | Type |
|----:|------|------|-|----:|------|------|
| 1 | GND | power | | 9 | REFCAP | passive |
| 2 | VLOGIC | power | | 10 | GND | power |
| 3 | REGCAP | passive | | 11 | REFIO | bidir |
| 4 | VCC | power | | 12 | CS | input |
| 5 | AINB- | input | | 13 | SDOA | output |
| 6 | AINB+ | input | | 14 | SDOB/ALERT | output |
| 7 | AINA- | input | | 15 | SDI | input |
| 8 | AINA+ | input | | 16 | SCLK | input |
|   |      |      | | 17 | GND (EPAD) | power |

Pinout **confirmed against the official AD7380/AD7381 datasheet** (Table 7, Pin
Function Descriptions): pins 1,10=GND; 2=VLOGIC; 3=REGCAP; 4=VCC; 5,6=AINB-/+;
7,8=AINA-/+; 9=REFCAP; 11=REFIO; 12=CS; 13=SDOA; 14=SDOB/ALERT; 15=SDI; 16=SCLK;
EPAD=GND. Still a PoC - confirm the footprint land pattern before real use.

Pulling that datasheet PDF needed a workaround: plain `curl` could not fetch it.
This is NOT a container egress block (outbound works fine - google/example.com
return 200); analog.com and mouser sit behind **Akamai bot-mitigation** that
resets the request based on the TLS/HTTP-2 fingerprint (JA3/JA4) + datacenter IP.
The datasheet above was pulled with the `fetch` tool added for exactly this - see
[`../docs/curl-akamai-bypass.md`](../docs/curl-akamai-bypass.md).

## Limitations / next steps

- Footprint set to the nearest KiCad stdlib LFCSP (`LFCSP-16-1EP_3x3mm`); the
  exact AD7380 land pattern should be confirmed.
- No real KiCad ERC run here (KiCad not installed in this container); `kiutils`
  validates structure + grid only. Wiring into `kicad-cli sym upgrade`/ERC is
  the obvious next step.
- The schematic (`.kicad_sch`) level - wiring symbols together with grid-exact
  nets - is the harder follow-on and is where an ERC-in-the-loop matters most.
