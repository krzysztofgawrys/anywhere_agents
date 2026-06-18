#!/usr/local/bin/python3
"""
kicad-api sidecar service: a thin HTTP wrapper around `kicad-cli` (and later
kipy), so a worker that has no local KiCad can get real ERC / rendering / netlist
over HTTP. Same pattern as the Excel add-in bridge.

Runs INSIDE the kicad-api:master image (kicad-cli + env baked in). The worker and
this sidecar share a volume so schematic paths resolve on both sides.

Endpoints:
  GET  /health                       -> {"status":"ok","kicad":"<version>"}
  POST /erc     {"path": "..."}       -> {"ok","counts":{messages,errors,warnings},"report"}
  POST /export  {"path","fmt":"svg"}  -> {"ok","files":[...]}  (svg|pdf|dxf|ps|netlist|bom)

Run:  python3 kicad_service.py   (listens on 0.0.0.0:8010)
"""
import base64
import os
import re
import subprocess
import tempfile

from flask import Flask, jsonify, request

app = Flask(__name__)
CLI = "kicad-cli"          # on PATH inside the image
TIMEOUT = 180


def _run(args):
    return subprocess.run([CLI, *args], capture_output=True, text=True, timeout=TIMEOUT)


@app.get("/health")
def health():
    try:
        v = _run(["version"]).stdout.strip()
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
    return jsonify({"status": "ok", "kicad": v})


@app.post("/erc")
def erc():
    path = (request.json or {}).get("path")
    if not path or not os.path.isfile(path):
        return jsonify({"error": f"schematic not found: {path}"}), 400
    rpt = tempfile.mktemp(suffix=".erc.rpt")
    r = _run(["sch", "erc", path, "-o", rpt])
    report = open(rpt).read() if os.path.isfile(rpt) else ""
    m = re.search(r"ERC messages:\s*(\d+)\s+Errors\s+(\d+)\s+Warnings\s+(\d+)", report)
    counts = {}
    if m:
        # breakdown by violation type. lib_symbol_issues is benign noise here:
        # ERC has no sym-lib-table so it flags every embedded symbol as
        # "differs from library". Separate it so real warnings surface.
        by_type = {}
        for t in re.findall(r"\[([a-z_]+)\]", report):
            by_type[t] = by_type.get(t, 0) + 1
        benign = by_type.get("lib_symbol_issues", 0)
        warnings = int(m[3])
        counts = {"messages": int(m[1]), "errors": int(m[2]), "warnings": warnings,
                  "by_type": by_type, "benign_warnings": benign,
                  "real_warnings": max(0, warnings - benign)}
    return jsonify({
        "ok": bool(counts) and counts.get("errors", 1) == 0,
        "counts": counts,
        "report": report,
        "stderr": r.stderr[-1000:],
    })


TEXT_FMTS = {"svg", "netlist", "bom"}


@app.post("/export")
def export():
    body = request.json or {}
    path = body.get("path")
    fmt = (body.get("fmt") or "svg").lower()   # case-insensitive: PNG == png
    if not path or not os.path.isfile(path):
        return jsonify({"error": f"schematic not found: {path}"}), 400
    outdir = tempfile.mkdtemp()
    # PDF/netlist/bom are SINGLE-file exports: kicad-cli wants `-o <file>` and
    # FAILS ("Failed to create file ...") if given a directory. svg/dxf/ps are
    # per-sheet exports that take `-o <dir>`.
    if fmt in ("pdf", "netlist", "bom"):
        r = _run(["sch", "export", fmt, path, "-o", os.path.join(outdir, f"out.{fmt}")])
    else:
        r = _run(["sch", "export", fmt, path, "-o", outdir])
    # Return the rendered CONTENT over HTTP (the sidecar /tmp is not shared, so
    # the caller can't read files by path). Text for svg/netlist/bom, base64 else.
    results = []
    for name in sorted(os.listdir(outdir)):
        fp = os.path.join(outdir, name)
        if not os.path.isfile(fp):
            continue
        data = open(fp, "rb").read()
        if fmt in TEXT_FMTS:
            results.append({"name": name, "encoding": "text",
                            "content": data.decode("utf-8", "replace")})
        else:
            results.append({"name": name, "encoding": "base64",
                            "content": base64.b64encode(data).decode()})
    if not results:
        # kicad-cli produced nothing -> almost always an unknown/unsupported fmt.
        # Surface its message instead of failing silently.
        return jsonify({"ok": False, "fmt": fmt,
                        "error": f"export '{fmt}' produced no output (unknown format? "
                                 f"try svg/pdf/dxf/ps/netlist/bom)",
                        "stderr": (r.stderr or r.stdout)[-1000:]}), 400
    return jsonify({"ok": r.returncode == 0, "fmt": fmt,
                    "names": [x["name"] for x in results], "results": results,
                    "stderr": r.stderr[-1000:]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8010)
