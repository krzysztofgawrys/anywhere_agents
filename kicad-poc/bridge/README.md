# KiCad live-edit bridge

Thin HTTP bridge that lets a worker read/modify the OPEN schematic in KiCad
(nightly 10.99+) via the IPC API. Two forms:

- `bridge_entry.py` (+ `plugin.json`, `requirements.txt`): a KiCad IPC API
  plugin. Drop the dir in `<KICAD_DOCUMENTS>/<ver>/plugins/`; KiCad builds a
  venv from requirements.txt and the action launches the server (port 8761).
  Needs a Python 3.11+ interpreter (Preferences > Plugins) and the custom kipy
  wheel (built from gitlab.com/kicad/code/kicad-python; the entrypoint installs
  the bundled wheel).
- `kicad_bridge_standalone.py`: same endpoints, run by hand in a venv with kipy
  + flask.

Endpoints (8761): GET /health, /symbols, /dump?ref=; POST /text, /label,
/symbol, /place_raw (place_raw injects the open sheet path -> works on an empty
sheet). All mutations are one undoable commit.

Driven from the worker by `../sch/kicad_live.py`.

NOTE: requires a kipy built with the schematic IPC API (see KB / the custom
wheel) - pip kicad-python 0.7.1 does NOT have working schematic bindings.
