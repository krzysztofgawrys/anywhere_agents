#!/usr/bin/env python3
"""Render the symbol to SVG straight from the same data table - a cheap visual
sanity check that complements the structural validator."""
import gen_ad7380 as g

SC = 8          # mm -> px
PAD = 70


def X(mm): return PAD + (mm + 20) * SC
def Y(mm): return PAD + (20 - mm) * SC   # flip y (KiCad y-up -> svg y-down)


def main():
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 '
             f'{X(20)+PAD} {Y(-20)+PAD}" font-family="monospace" font-size="11">']
    parts.append(f'<rect x="0" y="0" width="{X(20)+PAD}" height="{Y(-20)+PAD}" fill="#fffef5"/>')
    # body
    parts.append(f'<rect x="{X(-g.HALF_W)}" y="{Y(g.TOP)}" '
                 f'width="{2*g.HALF_W*SC}" height="{(g.TOP-g.BOTTOM)*SC}" '
                 f'fill="#fbf3c0" stroke="#7a0000" stroke-width="2"/>')
    parts.append(f'<text x="{X(0)}" y="{Y(g.TOP)-12}" text-anchor="middle" '
                 f'font-weight="bold">{g.PART}</text>')
    for p in g.PINS:
        x, y, rot = g.pin_geometry(p)
        # endpoint -> body edge
        ex, ey = {0: (x+g.PIN_LEN, y), 180: (x-g.PIN_LEN, y),
                  270: (x, y-g.PIN_LEN), 90: (x, y+g.PIN_LEN)}[rot]
        parts.append(f'<line x1="{X(x)}" y1="{Y(y)}" x2="{X(ex)}" y2="{Y(ey)}" '
                     f'stroke="#7a0000" stroke-width="2"/>')
        parts.append(f'<circle cx="{X(x)}" cy="{Y(y)}" r="3" fill="#c00"/>')
        # number near the endpoint
        parts.append(f'<text x="{X(x)}" y="{Y(y)-4}" text-anchor="middle" '
                     f'fill="#444">{p.number}</text>')
        # name inside the body
        anchor = {"L": "start", "R": "end", "T": "middle", "B": "middle"}[p.side]
        nx, ny = {"L": (ex+2, ey+3), "R": (ex-2, ey+3),
                  "T": (ex, ey+12), "B": (ex, ey-6)}[p.side]
        parts.append(f'<text x="{X(nx)}" y="{Y(0)+(Y(ny)-Y(0))}" '
                     f'text-anchor="{anchor}" fill="#003">{p.name}</text>')
    parts.append('</svg>')
    svg = "\n".join(parts)
    open("AD7380_preview.svg", "w").write(svg)
    print(svg)


if __name__ == "__main__":
    main()
