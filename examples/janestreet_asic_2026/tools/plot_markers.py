#!/usr/bin/env python3
"""Plot the layer-200/0 marker cells from puzzle.gds as a morse strip.

Reads artifacts/markers.json (written by tools/gds_recon.py) or re-reads the
GDS directly, and renders the INTERNAL_3 (dot) / INTERNAL_7 (dash) boxes to
artifacts/layer200_markers.png together with the decoded morse text.

Usage:
    python tools/plot_markers.py
    python tools/plot_markers.py --gds ../puzzle.gds --out ../artifacts/layer200_markers.png
"""
from __future__ import annotations

import argparse
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ART = os.path.join(ROOT, "artifacts")

MORSE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F",
    "--.": "G", "....": "H", "..": "I", ".---": "J", "-.-": "K", ".-..": "L",
    "--": "M", "-.": "N", "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R",
    "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X",
    "-.--": "Y", "--..": "Z",
}


def load(gds, jsonpath):
    if os.path.exists(jsonpath):
        with open(jsonpath) as fh:
            d = json.load(fh)
        return d["widths"], d["instances"]
    import gdstk
    lib = gdstk.read_gds(gds)
    tc = lib.top_level()[0]
    marker = {}
    for c in lib.cells:
        if any(p.layer == 200 for p in c.polygons):
            bb = c.bounding_box()
            marker[c.name] = (round(bb[1][0] - bb[0][0], 4), round(bb[1][1] - bb[0][1], 4))
    inst = [{"cell": r.cell.name, "x": r.origin[0], "y": r.origin[1],
             "rot": r.rotation, "xrefl": bool(r.x_reflection), "mag": r.magnification}
            for r in tc.references if r.cell.name in marker]
    return marker, inst


def decode(items, widths, unit):
    """items: sorted list of (x, cell). Returns (symbols, morse_words, text)."""
    syms, gaps, prev_end = [], [], None
    for x, nm in items:
        wd = widths[nm][0]
        syms.append("." if wd < 2 * unit else "-")
        if prev_end is not None:
            gaps.append(round((x - prev_end) / unit))
        prev_end = x + wd
    letters, cur = [], ""
    for i, s in enumerate(syms):
        cur += s
        g = gaps[i] if i < len(gaps) else 99
        if g >= 7:
            letters.append(cur); letters.append("/"); cur = ""
        elif g >= 3:
            letters.append(cur); cur = ""
    if cur:
        letters.append(cur)
    text = "".join(" " if L == "/" else MORSE.get(L, "?") for L in letters)
    return syms, gaps, letters, text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gds", default=os.path.join(ROOT, "puzzle.gds"))
    ap.add_argument("--json", default=os.path.join(ART, "markers_puzzle.json"))
    ap.add_argument("--out", default=os.path.join(ART, "layer200_markers.png"))
    args = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    widths, inst = load(args.gds, args.json)
    unit = min(w for w, _h in widths.values())  # 1.38 um == one morse time unit

    ys = sorted({round(i["y"], 3) for i in inst}, reverse=True)
    fig, axes = plt.subplots(len(ys), 1, figsize=(16, 2.2 * len(ys) + 1.2), squeeze=False)
    summary = []
    for ax, y in zip(axes[:, 0], ys):
        items = sorted((round(i["x"], 3), i["cell"]) for i in inst if round(i["y"], 3) == y)
        syms, gaps, letters, text = decode(items, widths, unit)
        summary.append((y, "".join(syms), " ".join(letters), text))
        for x, nm in items:
            wd, ht = widths[nm]
            ax.add_patch(Rectangle((x, 0), wd, ht,
                                   facecolor="#1f4ea1" if wd < 2 * unit else "#c0392b",
                                   edgecolor="black", linewidth=0.6))
        xmin = min(x for x, _ in items)
        xmax = max(x + widths[n][0] for x, n in items)
        ax.set_xlim(xmin - 3, xmax + 3)
        ax.set_ylim(-1.2, max(h for _w, h in widths.values()) + 2.6)
        ax.set_yticks([])
        ax.set_xlabel("x (um) in top cell 'puzzle'")
        ax.set_title(f"layer 200/0 markers at y={y} um   |   {''.join(syms)}\n"
                     f"morse -> {text!r}", fontsize=11)
        ax.text(xmin, -1.0, f"unit = {unit} um (INTERNAL_3 = dot, INTERNAL_7 = dash)",
                fontsize=8, va="top")
    fig.suptitle("Jane Street ASIC puzzle: hidden morse on GDS layer 200/0", fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(args.out, dpi=150)
    print(f"wrote {args.out}")
    for y, s, l, t in summary:
        print(f"  y={y}: {s}\n    letters: {l}\n    TEXT: {t!r}")


if __name__ == "__main__":
    main()
