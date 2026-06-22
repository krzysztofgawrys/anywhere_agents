#!/usr/local/bin/python3
"""
pcb_place.py - 2D PCB placer driven by the live bridge. Groups footprints by their
SOURCE SCHEMATIC SHEET (CH1, CH2, POWER, ...) into tidy rectangular blocks, packs
each block with non-overlapping shelf packing, tiles the blocks across the board,
draws the Edge.Cuts outline around everything and labels each block on silkscreen.

  pcb_place.py [--bridge http://HOST:8761] [--dry] [--width MM]

The agent only needs to call this after a /pcb/import: the live board already knows
each footprint's sheet + courtyard extent (GET /pcb/footprints), so the placer is
pure geometry. Nothing here is schematic-specific - it works for any imported board.
"""
import argparse, json, sys, os, urllib.request, urllib.error


class Bridge:
    """Minimal PCB HTTP client (no kipy/kiutils dependency, so the placer runs
    anywhere). Mirrors the pcb_* methods of kicad_live.Bridge."""
    def __init__(self, url):
        self.url = url.rstrip("/")

    def _post(self, ep, payload):
        req = urllib.request.Request(self.url + ep, json.dumps(payload).encode(),
                                     {"Content-Type": "application/json"})
        try:
            return json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": e.read().decode()[:300], "http": e.code}

    def pcb_footprints(self):
        return json.load(urllib.request.urlopen(self.url + "/pcb/footprints", timeout=30))

    def pcb_move(self, moves):
        return self._post("/pcb/move", {"moves": moves})

    def pcb_edge(self, rect=None, poly=None, clear=True, width_mm=0.1):
        body = {"clear": clear, "width_mm": width_mm}
        if rect is not None:
            body["rect"] = rect
        if poly is not None:
            body["poly"] = poly
        return self._post("/pcb/edge", body)

    def pcb_text(self, texts, layer="F.Silkscreen", clear=False):
        return self._post("/pcb/text", {"texts": texts, "layer": layer, "clear": clear})

# --- tunables (mm) ---
PART_GAP = 1.2      # clear space between adjacent parts (courtyard already in w/h)
GROUP_PAD = 9.0     # gap between group blocks
GROUP_HEADER = 4.5  # band reserved at the top of each block for its silk label
BOARD_MARGIN = 6.0  # gap between outermost parts and the board edge
ORIGIN = (25.0, 25.0)  # top-left of the placement area on the board (mm)

# rough signal-flow order so related blocks sit near each other; unknown sheets
# fall to the end in size order
SHEET_ORDER = ["SOURCE_DDS", "CLOCK", "SOURCE_OUTPUT", "COMMON",
               "CH1", "CH2", "POWER"]


def shelf_pack(items, size_of, target_w):
    """Left-to-right, top-to-bottom shelf packing. items: list; size_of(item)->(w,h)
    cell size INCLUDING gaps. Returns (placements, total_w, total_h) where each
    placement is (item, x, y) = the cell's top-left corner relative to (0,0)."""
    placements = []
    x = y = 0.0
    shelf_h = 0.0
    used_w = 0.0
    for it in items:
        w, h = size_of(it)
        if x > 0 and x + w > target_w:        # wrap to next shelf
            y += shelf_h
            x = 0.0
            shelf_h = 0.0
        placements.append((it, x, y))
        x += w
        used_w = max(used_w, x)
        shelf_h = max(shelf_h, h)
    total_h = y + shelf_h
    return placements, used_w, total_h


def cell_size(fp):
    """Cell footprint occupies: extent + gap on each side."""
    return fp["w_mm"] + PART_GAP, fp["h_mm"] + PART_GAP


def pack_group(parts):
    """Pack one sheet's parts into a block. Returns (placements, block_w, block_h)
    where placements are (fp, cx, cy) cell-centre offsets from the block origin."""
    # biggest first -> tighter shelves; stable by ref for determinism
    ordered = sorted(parts, key=lambda f: (-(f["h_mm"]), -(f["w_mm"]), f["ref"]))
    total_area = sum((f["w_mm"] + PART_GAP) * (f["h_mm"] + PART_GAP) for f in parts)
    # aim for a slightly-wider-than-tall block
    target_w = max((total_area ** 0.5) * 1.25,
                   max((f["w_mm"] + PART_GAP) for f in parts))
    cells, w, h = shelf_pack(ordered, cell_size, target_w)
    placements = []
    for fp, x, y in cells:
        cw, ch = cell_size(fp)
        placements.append((fp, x + cw / 2.0, y + GROUP_HEADER + ch / 2.0))
    return placements, max(w, target_w), h + GROUP_HEADER


def plan(footprints, board_target_w=None):
    """Full board plan. Returns (moves, labels, board_w, board_h)."""
    groups = {}
    for fp in footprints:
        groups.setdefault(fp["sheet"] or "MISC", []).append(fp)

    # order the blocks: known signal-flow order first, then remaining by size
    def gkey(name):
        return (SHEET_ORDER.index(name) if name in SHEET_ORDER else len(SHEET_ORDER),
                -len(groups[name]), name)
    names = sorted(groups, key=gkey)

    blocks = []   # (name, placements, w, h)
    for name in names:
        pl, w, h = pack_group(groups[name])
        blocks.append([name, pl, w, h])

    if board_target_w is None:
        total_area = sum((w + GROUP_PAD) * (h + GROUP_PAD) for _, _, w, h in blocks)
        board_target_w = (total_area ** 0.5) * 1.45

    # shelf-pack the blocks themselves across the board
    def block_size(b):
        return b[2] + GROUP_PAD, b[3] + GROUP_PAD
    arranged, used_w, used_h = shelf_pack(blocks, block_size, board_target_w)

    ox, oy = ORIGIN
    moves, labels = [], []
    for b, bx, by in arranged:
        name, placements, w, h = b
        gx, gy = ox + bx, oy + by
        labels.append({"text": name, "x_mm": round(gx + 1.0, 3),
                       "y_mm": round(gy + GROUP_HEADER / 2.0, 3), "size_mm": 2.2})
        for fp, cx, cy in placements:
            moves.append({"ref": fp["ref"], "x_mm": round(gx + cx, 3),
                          "y_mm": round(gy + cy, 3), "orient": 0})
    board_w = used_w + ox  # right extent of content
    board_h = used_h + oy
    return moves, labels, board_w, board_h, names, blocks


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bridge", default="http://172.23.48.1:8761")
    ap.add_argument("--width", type=float, default=None, help="board target width mm")
    ap.add_argument("--dry", action="store_true", help="plan only, no live moves")
    ap.add_argument("--no-edge", action="store_true")
    ap.add_argument("--no-labels", action="store_true")
    args = ap.parse_args(argv[1:])

    br = Bridge(args.bridge)
    data = br.pcb_footprints()
    fps = data["footprints"]
    print(f"board: {len(fps)} footprints")

    moves, labels, bw, bh, names, blocks = plan(fps, args.width)
    print("groups (block w x h mm):")
    for name, _, w, h in blocks:
        n = sum(1 for m in moves)  # placeholder
    for name, pl, w, h in blocks:
        print(f"  {name:<14} {len(pl):3d} parts   {w:6.1f} x {h:6.1f}")
    # board outline rectangle around the content
    ox, oy = ORIGIN
    rx = ox - BOARD_MARGIN
    ry = oy - BOARD_MARGIN
    rw = (bw - ox) + 2 * BOARD_MARGIN
    rh = (bh - oy) + 2 * BOARD_MARGIN
    print(f"board outline: [{rx:.1f}, {ry:.1f}, {rw:.1f}, {rh:.1f}] mm")
    print(f"moves: {len(moves)}  labels: {len(labels)}")

    if args.dry:
        return 0

    r = br.pcb_move(moves)
    print("move:", {k: (len(v) if isinstance(v, list) else v)
                     for k, v in r.items() if k in ("moved", "not_found", "ok")})
    if not args.no_edge:
        e = br.pcb_edge(rect=[round(rx, 2), round(ry, 2), round(rw, 2), round(rh, 2)],
                        clear=True)
        print("edge:", e)
    if not args.no_labels:
        t = br.pcb_text(labels, layer="F.Silkscreen", clear=True)
        print("labels:", t)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
