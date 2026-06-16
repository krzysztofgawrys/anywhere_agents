#!/usr/local/bin/python3
"""Render the netlist to SVG from the same data the generator uses: each part a
box at its placed position, each connected pin a stub tagged with its net name
(one colour per net). Visual sanity check for the label-based connectivity."""
import json
import os
import sys

import gen_schematic as g

PALETTE = ["#c0392b", "#2980b9", "#27ae60", "#8e44ad", "#d35400", "#16a085",
           "#2c3e50", "#c2185b", "#00838f", "#558b2f", "#6d4c41"]


def main():
    spec_path = sys.argv[1] if len(sys.argv) > 1 else "ad7380_app.netlist.json"
    spec = json.load(open(spec_path))
    base = os.path.dirname(os.path.abspath(spec_path))
    local = {k: os.path.join(base, v) for k, v in spec.get("local_libs", {}).items()}
    libdirs = spec.get("libdirs", [])

    sym = {p["lib_id"]: g.load_symbol(p["lib_id"], libdirs, local) for p in spec["parts"]}
    pos = dict(zip((p["ref"] for p in spec["parts"]),
                   g.grid_layout(len(spec["parts"]))))
    netcolor = {n["name"]: PALETTE[i % len(PALETTE)] for i, n in enumerate(spec["nets"])}
    pin_net = {}
    for n in spec["nets"]:
        for pr in n["pins"]:
            pin_net[pr] = n["name"]

    SC, OX, OY = 4.2, -180, -160
    def X(mm): return (mm + 0) * SC + OX
    def Y(mm): return (mm + 0) * SC + OY

    W, H = 980, 760
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="monospace" font-size="10">',
           f'<rect width="{W}" height="{H}" fill="#fffdf5"/>',
           f'<text x="12" y="20" font-size="13" font-weight="bold">{spec.get("title","")}</text>']

    for part in spec["parts"]:
        ref = part["ref"]
        px, py = pos[ref]
        _, pins = sym[part["lib_id"]]
        xs = [px + x for x, y, a in pins.values()]
        ys = [py - y for x, y, a in pins.values()]
        x0, x1 = min(xs) - 3, max(xs) + 3
        y0, y1 = min(ys) - 6, max(ys) + 6
        out.append(f'<rect x="{X(x0):.0f}" y="{Y(y0):.0f}" width="{(x1-x0)*SC:.0f}" '
                   f'height="{(y1-y0)*SC:.0f}" fill="#fbf3c0" stroke="#7a5c00" stroke-width="1.3"/>')
        out.append(f'<text x="{X((x0+x1)/2):.0f}" y="{Y(y0)-2:.0f}" text-anchor="middle" '
                   f'font-weight="bold">{ref} {part.get("value","")}</text>')
        for pnum, (lx, ly, ang) in pins.items():
            ax, ay = px + lx, py - ly
            net = pin_net.get(f"{ref}.{pnum}")
            col = netcolor.get(net, "#bbb")
            out.append(f'<circle cx="{X(ax):.1f}" cy="{Y(ay):.1f}" r="2.3" fill="{col}"/>')
            if net:
                # label offset to the side the pin points
                dx = 7 if ax <= px else -7
                anc = "start" if dx > 0 else "end"
                out.append(f'<text x="{X(ax)+dx:.0f}" y="{Y(ay)+3:.0f}" text-anchor="{anc}" '
                           f'fill="{col}">{net}</text>')
            out.append(f'<text x="{X(ax):.0f}" y="{Y(ay)-3:.0f}" text-anchor="middle" '
                       f'fill="#999" font-size="8">{pnum}</text>')

    # legend
    lx, ly = 760, 60
    out.append(f'<text x="{lx}" y="{ly-8}" font-weight="bold">nets</text>')
    for i, n in enumerate(spec["nets"]):
        c = netcolor[n["name"]]
        out.append(f'<rect x="{lx}" y="{ly+i*16-9}" width="10" height="10" fill="{c}"/>')
        out.append(f'<text x="{lx+16}" y="{ly+i*16}" fill="#222">{n["name"]} '
                   f'({len(n["pins"])})</text>')
    out.append('</svg>')
    open("ad7380_app_preview.svg", "w").write("\n".join(out))
    print("\n".join(out))


if __name__ == "__main__":
    main()
