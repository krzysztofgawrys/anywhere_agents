# kicad_live.py - place library components into the OPEN schematic, live

The agent writes a netlist (same shape as the file skill); this module does ALL
the .kicad_sym -> proto geometry and pushes via the bridge. No pin-position math
in the agent.

## Run (on the worker, with the bridge up on the KiCad machine)
    python kicad_live.py push netlist.json --bridge http://<WIN_HOST>:8761

## Deploy deps (once, on the elec worker)
    pip install kiutils
    pip install kicad_python-0.8.0.dev0-py3-none-any.whl   # the bundled wheel

## How it works (the distilled recipe)
- parse .kicad_sym (kiutils), follow `extends` for geometry
- pins: ABSOLUTE = instance + (lib_x, -lib_y); orientation {0:R,90:U,180:L,270:D}
- body graphics: RELATIVE (lib_x, -lib_y); polyline->polygon (send full incl. closing
  point, KiCad drops the dup), rectangle/circle mapped; fill background/none
- scaffold.bin: a real symbol proto captured once, supplies the structural
  scaffolding KiCad requires; the bridge injects the open sheet's path so it
  works on an EMPTY schematic too
- create_items via bridge /place_raw (undoable commit)

## v1 limits
- graphics: polyline, rectangle, circle (arcs/beziers/text: TODO)
- nets: symbols placed; net labels are future (pin-accurate via /dump)
- single body style; multi-unit symbols place unit 1
