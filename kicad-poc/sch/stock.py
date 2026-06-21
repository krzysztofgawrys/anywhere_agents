"""On-hand component STOCK integration for the KiCad skill.

Query the user's parts inventory (what they physically have) and turn a stock
part's `package` into a KiCad footprint, so generated netlists prefer parts you
actually own - with the real MPN + footprint + a quantity check.

The DB is MongoDB behind a REST API; the worker reaches it ONLY through the
parts frontend proxy (the backend/mongo sit on a separate docker network).
Read-only. Default URL works from any worker on the host network.
"""
import json, re, urllib.request

STOCK_URL = "http://172.23.60.65:8180/api/parts"


def fetch_stock(url=STOCK_URL, limit=1000):
    """Full parts list (handles the {parts,total,page,limit} pagination)."""
    with urllib.request.urlopen("%s?limit=%d" % (url, limit), timeout=20) as r:
        d = json.load(r)
    return d.get("parts", d if isinstance(d, list) else [])


# ---- package string -> KiCad footprint --------------------------------------
_PASSIVE_SIZE = {                       # imperial code -> IPC metric body
    "0201": "0603Metric", "0402": "1005Metric", "0603": "1608Metric",
    "0805": "2012Metric", "1206": "3216Metric", "1210": "3225Metric",
    "1812": "4532Metric", "2010": "5025Metric", "2512": "6332Metric",
}
_PASSIVE_LIB = {"R": ("Resistor_SMD", "R"), "C": ("Capacitor_SMD", "C"),
                "L": ("Inductor_SMD", "L"), "LED": ("LED_SMD", "LED")}
_DISCRETE = {                           # package -> full KiCad footprint
    "SOT-23": "Package_TO_SOT_SMD:SOT-23",
    "SOT-23-3": "Package_TO_SOT_SMD:SOT-23",
    "SOT-23-5": "Package_TO_SOT_SMD:SOT-23-5",
    "SOT-23-6": "Package_TO_SOT_SMD:SOT-23-6",
    "SOT-323": "Package_TO_SOT_SMD:SOT-323_SC-70",
    "SC-70-6": "Package_TO_SOT_SMD:SC-70-6",
    "SOIC-8": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
    "SOIC-14": "Package_SO:SOIC-14_3.9x8.7mm_P1.27mm",
    "SOIC-16": "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm",
    "TSSOP-8": "Package_SO:TSSOP-8_3x3mm_P0.65mm",
    "MSOP-8": "Package_SO:MSOP-8_3x3mm_P0.65mm",
}


def _kind(lib_id=None, description=""):
    """R / C / L / LED from a Device lib_id or a description keyword; else None.
    Needed because a passive size (0402) maps to a different footprint LIBRARY
    for a resistor vs a capacitor vs an inductor."""
    name = (lib_id or "").split(":")[-1]
    if name == "R" or name.startswith("R_"):
        return "R"
    if name == "C" or (name.startswith("C_") and not name.startswith("CP")):
        return "C"
    if name == "L" or name.startswith("L_"):
        return "L"
    de = (description or "").lower()
    for kw, k in (("rezystor", "R"), ("resistor", "R"), ("kondensator", "C"),
                  ("capacitor", "C"), ("dławik", "L"), ("inductor", "L"), ("led", "LED")):
        if kw in de:
            return k
    return None


def package_to_footprint(package, lib_id=None, description=""):
    """Map a stock `package` to a KiCad footprint. Passive sizes (0402/0805/...)
    need the KIND (R/C/L from lib_id/description). Returns '' if unknown - the
    caller/agent then fills it in (the DB has free-form packages like
    'SMD,P=1mm' that need human/agent judgement)."""
    pk = (package or "").strip()
    if not pk:
        return ""
    m = re.search(r"\b(0201|0402|0603|0805|1206|1210|1812|2010|2512)\b", pk)
    if m:
        kind = _kind(lib_id, description)
        if kind in _PASSIVE_LIB:
            lib, pref = _PASSIVE_LIB[kind]
            return "%s:%s_%s_%s" % (lib, pref, m.group(1), _PASSIVE_SIZE[m.group(1)])
    return _DISCRETE.get(pk.upper(), _DISCRETE.get(pk, ""))


# ---- matching / selection ----------------------------------------------------
def find_by_mpn(stock, mpn):
    """Stock part whose manufacturer OR supplier part number equals `mpn`."""
    m = (mpn or "").strip().lower()
    if not m:
        return None
    for p in stock:
        if (p.get("manufacturerPartNumber") or "").lower() == m \
           or (p.get("supplierPartNumber") or "").lower() == m:
            return p
    return None


def search_stock(stock, text=None, package=None, in_stock=True):
    """Filter stock by case-insensitive description substring and/or package, so
    the agent can pick a real part (e.g. text='10k resistor', package='0402')."""
    out = stock
    if text:
        for t in text.lower().split():
            out = [p for p in out if t in (p.get("description") or "").lower()]
    if package:
        pk = package.lower()
        out = [p for p in out if pk in (p.get("package") or "").lower()]
    if in_stock:
        out = [p for p in out if int(p.get("quantity") or 0) > 0]
    return out


def apply_stock(spec, stock=None, url=STOCK_URL):
    """For each netlist part carrying "mpn" (= you chose a stock part), look it up
    and fill `footprint` (from its package) + `manufacturerPartNumber`, and record
    on-hand `quantity`. An explicit netlist "footprint" is never overwritten.
    Returns {ref: {mpn, footprint, quantity, low}} for what was applied; `low` is
    True when stock quantity < the count of that MPN used in the netlist."""
    if stock is None:
        stock = fetch_stock(url)
    from collections import Counter
    used = Counter(p.get("mpn") or p.get("manufacturerPartNumber") for p in spec.get("parts", []))
    applied = {}
    for p in spec.get("parts", []):
        mpn = p.get("mpn") or p.get("manufacturerPartNumber")
        if not mpn:
            continue
        sp = find_by_mpn(stock, mpn)
        if not sp:
            applied[p["ref"]] = {"mpn": mpn, "error": "not in stock DB"}
            continue
        if not p.get("footprint"):
            fp = package_to_footprint(sp.get("package"), p.get("lib_id"), sp.get("description"))
            if fp:
                p["footprint"] = fp
        p.setdefault("manufacturerPartNumber", sp.get("manufacturerPartNumber"))
        qty = int(sp.get("quantity") or 0)
        applied[p["ref"]] = {"mpn": mpn, "footprint": p.get("footprint"),
                             "quantity": qty, "low": qty < used[mpn]}
    return applied
