#!/usr/bin/env python3
"""
Headless KiCad IPC API PoC via kipy (kicad-python).

Proves the end-to-end live path WITHOUT a GUI:
  KiCad(headless=True) -> spawns `kicad-cli api-server` -> kipy connects over the
  socket -> read (and attempt to write) the live schematic document.

Run inside a KiCad *nightly* image:  python3 kipy_poc.py some.kicad_sch
Each step is guarded so one run reports the maximum about what works.
"""
import sys
import traceback


def step(name, fn):
    try:
        r = fn()
        print(f"PASS  {name}: {r!r}")
        return r
    except Exception as e:
        print(f"FAIL  {name}: {type(e).__name__}: {e}")
        traceback.print_exc()
        return None


def main():
    path = sys.argv[1]
    from kipy import KiCad
    try:
        from kipy.common_types import KiCadObjectType
    except Exception:
        from kipy.proto.common.types.enums_pb2 import KiCadObjectType  # fallback

    print("=== 1. connect headless (starts kicad-cli api-server) ===")
    kc = step("KiCad(headless=True, file_path=...)",
              lambda: KiCad(headless=True, file_path=path))
    if kc is None:
        sys.exit(1)

    step("get_version", lambda: kc.get_version().full_version)

    print("=== 2. get the live schematic + READ items ===")
    sch = step("get_schematic", lambda: kc.get_schematic())
    if sch is None:
        sys.exit(2)
    step("schematic.name", lambda: sch.name)
    syms = step("read symbols (KOT_SCH_SYMBOL)",
                lambda: list(sch.get_items([KiCadObjectType.KOT_SCH_SYMBOL])))
    if syms is not None:
        print(f"      -> {len(syms)} symbols on the live schematic")
    lines = step("read wires (KOT_SCH_LINE)",
                 lambda: list(sch.get_items([KiCadObjectType.KOT_SCH_LINE])))
    if lines is not None:
        print(f"      -> {len(lines)} line/wire items")

    print("=== 3. WRITE attempt: create a SchematicText + save ===")
    def make_text():
        from kipy.schematic_types import SchematicText
        t = SchematicText()
        for attr, val in (("value", "HELLO from kipy headless"),
                          ("text", "HELLO from kipy headless")):
            try:
                setattr(t, attr, val)
            except Exception:
                pass
        return t
    txt = step("construct SchematicText", make_text)
    if txt is not None:
        before = step("count texts before",
                      lambda: len(list(sch.get_items([KiCadObjectType.KOT_SCH_TEXT]))))
        step("create_items([text])", lambda: sch.create_items([txt]))
        step("save", lambda: sch.save())
        after = step("count texts after",
                     lambda: len(list(sch.get_items([KiCadObjectType.KOT_SCH_TEXT]))))
        if before is not None and after is not None:
            print(f"      -> texts: {before} -> {after} "
                  f"({'WRITE CONFIRMED' if after > before else 'no change'})")

    print("=== DONE ===")


if __name__ == "__main__":
    main()
