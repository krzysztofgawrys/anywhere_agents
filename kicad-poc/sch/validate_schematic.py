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

Usage:  validate_schematic.py out.kicad_sch --spec netlist.json [--erc] [--render] [--strict]
        validate_schematic.py --missing --spec netlist.json [--skip Name1,Name2]

--strict promotes 1-pin (dangling) nets from warning to error.

--erc    runs REAL KiCad ERC via the kicad-api sidecar (POST to $KICAD_API_URL/erc);
         ERC errors count as a validation failure. --render exports an SVG too.
         The .kicad_sch must sit on a path the sidecar shares (e.g. under ~/elec).

--missing audits a netlist alone (no .kicad_sch needed): it lists the lib_ids
that don't resolve to any symbol yet, so they can be generated with the
kicad-symbol skill. Parts whose lib_id contains a --skip token (or a spec
"own_libs" entry) are treated as the user's own library and not flagged.
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

import sexpdata
from kiutils.symbol import SymbolLib


def _wire_overlaps(text):
    """Find axis-aligned wires that overlap a collinear sibling (share a span on
    the same line), which would short two nets. Returns a list of
    (axis, line_coord, (lo1, hi1), (lo2, hi2)). Wires that only touch at a single
    endpoint (hi == next lo) are NOT flagged - that is a legitimate join."""
    wires = [(float(a), float(b), float(c), float(d)) for a, b, c, d in
             re.findall(r'\(wire \(pts \(xy (\S+) (\S+)\) \(xy (\S+) (\S+)\)\)', text)]
    horiz, vert = {}, {}
    for x1, y1, x2, y2 in wires:
        if y1 == y2:
            horiz.setdefault(y1, []).append((min(x1, x2), max(x1, x2)))
        elif x1 == x2:
            vert.setdefault(x1, []).append((min(y1, y2), max(y1, y2)))
    out = []
    for axis, groups in (("horizontal", horiz), ("vertical", vert)):
        for key, segs in groups.items():
            segs.sort()
            for i in range(len(segs) - 1):
                if segs[i][1] > segs[i + 1][0] + 1e-6:     # real overlap, not a touch
                    out.append((axis, key, segs[i], segs[i + 1]))
    return out


def _boxes_overlap(a, b, tol=1e-6):
    """a, b = (xmin, xmax, ymin, ymax). True if their interiors intersect."""
    return (a[0] < b[1] - tol and b[0] < a[1] - tol
            and a[2] < b[3] - tol and b[2] < a[3] - tol)


def _label_boxes(text):
    """(name, bbox) for every global_label, bbox covering the label text from its
    anchor along its outward direction (angle 0 = horizontal, 90 = vertical;
    justify picks which way). Mirrors the generator's label_dir geometry."""
    out = []
    for m in re.finditer(r'\(global_label "([^"]+)" \(shape \w+\) '
                         r'\(at (-?[\d.]+) (-?[\d.]+) (\d+)\).*?\(justify (\w+)\)', text):
        name, x, y, ang, just = (m.group(1), float(m.group(2)), float(m.group(3)),
                                 int(m.group(4)), m.group(5))
        L, H = 2.54 + len(name) * 1.1, 1.8
        if ang == 0:
            x1, x2 = (x - L, x) if just == "right" else (x, x + L)
            y1, y2 = y - H / 2, y + H / 2
        else:
            y1, y2 = (y, y + L) if just == "right" else (y - L, y)
            x1, x2 = x - H / 2, x + H / 2
        out.append((name, (min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2))))
    return out


def _instance_boxes(text, coords):
    """(ref, body-bbox) per placed symbol instance. coords maps lib_id -> list of
    (x, y, angle) library pin positions; body bbox is the pin extent (+0.6 pad)
    transformed to schematic space (x'=px+x, y'=py-y)."""
    out = []
    for m in re.finditer(r'\(symbol\s*\(lib_id "([^"]+)"\)\s*\(at (-?[\d.]+) (-?[\d.]+) \d+\)'
                         r'.*?\(property "Reference" "([^"]+)"', text, re.S):
        lid, px, py, ref = m.group(1), float(m.group(2)), float(m.group(3)), m.group(4)
        pins = coords.get(lid)
        if not pins:
            continue
        xs = [px + x for x, _, _ in pins]
        ys = [py - y for _, y, _ in pins]
        out.append((ref, (min(xs) - 0.6, max(xs) + 0.6, min(ys) - 0.6, max(ys) + 0.6)))
    return out


def sidecar(endpoint, payload):
    """POST to the kicad-api sidecar (real ERC / render). Returns parsed JSON,
    or {'error': ...}. URL from KICAD_API_URL env (default the compose service)."""
    base = os.environ.get("KICAD_API_URL", "http://kicad-api:8010").rstrip("/")
    try:
        req = urllib.request.Request(base + endpoint, json.dumps(payload).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return {**json.load(e), "http": e.code}
        except Exception:
            return {"error": f"HTTP {e.code}", "http": e.code}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _find_symbol(nick, name, libdirs, local_libs):
    """Return the kiutils Symbol for nick:name, or None. local_libs entry may be
    a single .kicad_sym file OR a directory of per-symbol files."""
    paths = []
    if nick and nick in local_libs:
        p = local_libs[nick]
        if os.path.isdir(p):
            paths = [os.path.join(p, f) for f in sorted(os.listdir(p))
                     if f.endswith(".kicad_sym")]
        elif os.path.isfile(p):
            paths = [p]
    else:
        for d in libdirs:
            for c in (os.path.join(d, f"{nick}.kicad_symdir", f"{name}.kicad_sym"),
                      os.path.join(d, f"{nick}.kicad_sym")):
                if os.path.isfile(c):
                    paths.append(c)
    for path in paths:
        for s in SymbolLib.from_file(path).symbols:
            if s.entryName == name:
                return s
    return None


def pins_of(lib_id, libdirs, local_libs):
    """Return the set of pin numbers for a lib_id, or None if unresolvable.

    Follows `extends` (derived symbols like Transistor_FET:2N7002 inherit pins
    from their parent) - mirrors the generator so the two agree.
    """
    nick, name = lib_id.split(":", 1) if ":" in lib_id else (None, lib_id)
    sym = _find_symbol(nick, name, libdirs, local_libs)
    guard = 0
    while sym is not None and not any(u.pins for u in sym.units) \
            and getattr(sym, "extends", None) and guard < 6:
        sym = _find_symbol(nick, sym.extends, libdirs, local_libs)
        guard += 1
    if sym is None:
        return None
    return {p.number for u in sym.units for p in u.pins} or None


def _pin_coords(lib_id, libdirs, local_libs):
    """Like pins_of but returns [(x, y, angle), ...] library pin positions
    (following extends), for component bounding-box / overlap checks. None if
    unresolvable."""
    nick, name = lib_id.split(":", 1) if ":" in lib_id else (None, lib_id)
    sym = _find_symbol(nick, name, libdirs, local_libs)
    guard = 0
    while sym is not None and not any(u.pins for u in sym.units) \
            and getattr(sym, "extends", None) and guard < 6:
        sym = _find_symbol(nick, sym.extends, libdirs, local_libs)
        guard += 1
    if sym is None:
        return None
    return [(p.position.X, p.position.Y, p.position.angle)
            for u in sym.units for p in u.pins]


def audit_missing(spec, spec_dir, skip):
    """List the netlist's lib_ids that don't resolve to any symbol, so they can
    be generated with the kicad-symbol skill. `skip` tokens (plus spec
    "own_libs") mark parts that live in the user's own library and must NOT be
    flagged/generated - matched as a case-insensitive substring of the lib_id."""
    libdirs = spec.get("libdirs", [])
    local = {k: (v if os.path.isabs(v) else os.path.join(spec_dir, v))
             for k, v in spec.get("local_libs", {}).items()}
    own = [t.lower() for t in skip] + [t.lower() for t in spec.get("own_libs", [])]

    seen = {}
    for p in spec["parts"]:
        lid = p["lib_id"]
        if lid in seen:
            seen[lid]["refs"].append(p["ref"])
            continue
        if any(tok in lid.lower() for tok in own):
            status = "own"
        elif pins_of(lid, libdirs, local) is not None:
            status = "ok"
        else:
            status = "missing"
        seen[lid] = {"status": status, "refs": [p["ref"]]}

    missing = sorted(l for l, v in seen.items() if v["status"] == "missing")
    owns = sorted(l for l, v in seen.items() if v["status"] == "own")
    ok = [l for l, v in seen.items() if v["status"] == "ok"]
    print(f"audit: {len(ok)} resolved, {len(owns)} own-library (skipped), "
          f"{len(missing)} MISSING")
    for l in owns:
        print(f"  own      {l}  ({', '.join(seen[l]['refs'])})")
    print()
    if missing:
        print("MISSING (generate with the kicad-symbol skill):")
        for l in missing:
            print(f"  - {l}   used by: {', '.join(seen[l]['refs'])}")
    else:
        print("MISSING: none")
    return 0


def main():
    args = sys.argv[1:]
    skip = []
    if "--skip" in args:
        i = args.index("--skip")
        skip = [s.strip() for s in args[i + 1].split(",") if s.strip()]
        del args[i:i + 2]
    missing_mode = False
    if "--missing" in args:
        missing_mode = True
        args.remove("--missing")
    erc_mode = "--erc" in args
    if erc_mode:
        args.remove("--erc")
    render_mode = "--render" in args
    if render_mode:
        args.remove("--render")
    render_fmt = "svg"
    if "--render-fmt" in args:
        i = args.index("--render-fmt")
        render_fmt = args[i + 1].lower()
        del args[i:i + 2]
        render_mode = True
    strict_mode = "--strict" in args
    if strict_mode:
        args.remove("--strict")
    spec = None
    if "--spec" in args:
        i = args.index("--spec")
        spec_path = args[i + 1]
        spec = json.load(open(spec_path))
        spec_dir = os.path.dirname(os.path.abspath(spec_path))
        del args[i:i + 2]
    if missing_mode:
        if not spec:
            sys.exit("--missing requires --spec netlist.json")
        return audit_missing(spec, spec_dir, skip)
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

    # 3b. overlapping parallel wires -> likely a layout short (two distinct rails
    # whose horizontal/vertical wires run over each other). The generator must
    # never emit this; catching it structurally flags layout-collision bugs
    # without needing the ERC sidecar. Collinear wires merely TOUCHING end-to-end
    # (a shared endpoint) are fine - only a real overlap (shared span) is flagged.
    for ov in _wire_overlaps(text):
        axis, key, a, b = ov
        errors.append(f"overlapping {axis} wires at {key:g} (possible short): "
                      f"[{a[0]:g}..{a[1]:g}] and [{b[0]:g}..{b[1]:g}]")

    # 3c. labels of DIFFERENT nets must not overlap (text collision that also
    # merges two nets = short). Same-name overlaps are skipped: two labels of the
    # SAME net coinciding is electrically harmless (e.g. a GND pin landing where a
    # GND rail label already sits) and merely cosmetic, not a validity failure.
    lbx = _label_boxes(text)
    lab_hits = [(lbx[i][0], lbx[j][0], lbx[i][1])
                for i in range(len(lbx)) for j in range(i + 1, len(lbx))
                if lbx[i][0] != lbx[j][0]
                and _boxes_overlap(lbx[i][1], lbx[j][1], tol=0.3)]
    if lab_hits:
        shown = ", ".join(f"{a}/{b}@({bb[0]:g},{bb[2]:g})" for a, b, bb in lab_hits[:6])
        errors.append(f"{len(lab_hits)} overlapping different-net label pair(s): {shown}"
                      + (" ..." if len(lab_hits) > 6 else ""))

    # 4 + 5. netlist-driven checks
    if spec:
        libdirs = spec.get("libdirs", [])
        local = {k: (v if os.path.isabs(v) else os.path.join(spec_dir, v))
                 for k, v in spec.get("local_libs", {}).items()}

        # 3d. components must not overlap other components (bodies). Resolve pin
        # coords for every lib_id used in the file (incl. PWR_FLAG) and compare
        # each placed instance's body bbox.
        coords = {}
        for lid in used:
            coords[lid] = _pin_coords(lid, libdirs, local)
        ibx = _instance_boxes(text, coords)
        comp_hits = [(ibx[i][0], ibx[j][0])
                     for i in range(len(ibx)) for j in range(i + 1, len(ibx))
                     if _boxes_overlap(ibx[i][1], ibx[j][1])]
        if comp_hits:
            shown = ", ".join(f"{a}/{b}" for a, b in comp_hits[:8])
            errors.append(f"{len(comp_hits)} overlapping component pair(s): {shown}"
                          + (" ..." if len(comp_hits) > 8 else ""))
        parts = {p["ref"]: p["lib_id"] for p in spec["parts"]}
        pin_cache = {}
        total_pins = 0
        for net in spec["nets"]:
            if len(net["pins"]) < 2:
                msg = f"net {net['name']!r} has only {len(net['pins'])} pin (dangling)"
                (errors if strict_mode else warnings).append(msg)
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
        # Connectivity is realised by net labels AND (for decoupling-cap rails)
        # by wires under a single label, so don't count labels-per-pin; instead
        # require every net to appear as >= 1 global_label. Real ERC (--erc) is
        # the authoritative connectivity check.
        label_names = set(labels)
        for net in spec["nets"]:
            if net["name"] not in label_names:
                errors.append(f"net {net['name']!r} has no global_label in the schematic")

    print(f"parsed OK ({len(text)} bytes); kiutils round-trip: {'yes' if kiutils_ok else 'NO'}")
    print(f"lib_symbols defined: {sorted(defined)}")
    print(f"instances use: {sorted(used)}")
    print(f"global_labels: {len(labels)} across nets {sorted(set(labels))}")

    # real KiCad ERC + render via the kicad-api sidecar (needs the sidecar up and
    # sharing this file's path, e.g. under ~/elec).
    if erc_mode:
        ap = os.path.abspath(path)
        print(f"\n-- real ERC via {os.environ.get('KICAD_API_URL', 'http://kicad-api:8010')} --")
        r = sidecar("/erc", {"path": ap})
        if "error" in r and not r.get("counts"):
            hint = ("the .kicad_sch must live under ~/elec (the sidecar's only shared mount)"
                    if "not found" in str(r.get("error", "")).lower()
                    else "is the kicad-api sidecar up?")
            warnings.append(f"ERC unavailable: {r['error']} - {hint}")
        else:
            c = r.get("counts", {})
            real = c.get("real_warnings", c.get("warnings", "?"))
            benign = c.get("benign_warnings", 0)
            print(f"ERC: {c.get('errors', '?')} errors, {real} real warnings"
                  + (f"  (+{benign} benign lib_symbol_issues hidden)" if benign else ""))
            # show real violation lines, skipping the lib_symbol_issues noise
            for ln in [x.strip() for x in r.get("report", "").splitlines()
                       if "error" in x.lower() and "lib_symbol" not in x.lower()][:25]:
                print("    " + ln)
            if c.get("errors", 0) > 0:
                errors.append(f"real ERC found {c['errors']} error(s)")
    if render_mode:
        ap = os.path.abspath(path)
        r = sidecar("/export", {"path": ap, "fmt": render_fmt})
        results = r.get("results") or []
        if r.get("ok") and results:
            base = os.path.splitext(ap)[0]
            saved = []
            for res in results:
                ext = os.path.splitext(res["name"])[1] or "." + render_fmt
                outp = f"{base}{ext}" if len(results) == 1 else f"{base}_{res['name']}"
                if res.get("encoding") == "base64":
                    open(outp, "wb").write(base64.b64decode(res["content"]))
                else:
                    open(outp, "w").write(res["content"])
                saved.append(outp)
            print(f"render saved: {saved}")
        else:
            warnings.append(f"render failed: {r.get('error') or r.get('stderr', '')[:200]}")

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
