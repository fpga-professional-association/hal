#!/usr/bin/env python3
"""Structural / side-channel recon on puzzle.gds (Jane Street ASIC puzzle).

Reports:
  * library + cell inventory, bounding boxes, layer/datatype histogram
  * every GDS property attached to the library, cells, references, polygons,
    paths and labels (gdstk `properties` / `get_gds_property`)
  * all top-cell labels (ports) with layer/datatype and position
  * every instance of the marker cells on layer 200/0 (INTERNAL_3 / INTERNAL_7
    or whatever cells live on that layer), with origin, rotation, reflection,
    magnification, and a decode attempt treating narrow=dot / wide=dash (morse)
  * a raw byte scan for printable ASCII runs that are not boilerplate

Usage:
    python tools/gds_recon.py [path/to/puzzle.gds] [--outdir ../artifacts]
"""
from __future__ import annotations

import argparse
import os
import re
from collections import Counter, defaultdict

import gdstk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_GDS = os.path.join(ROOT, "puzzle.gds")
DEFAULT_OUT = os.path.join(ROOT, "artifacts")

MARKER_LAYER = 200
MORSE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E", "..-.": "F",
    "--.": "G", "....": "H", "..": "I", ".---": "J", "-.-": "K", ".-..": "L",
    "--": "M", "-.": "N", "---": "O", ".--.": "P", "--.-": "Q", ".-.": "R",
    "...": "S", "-": "T", "..-": "U", "...-": "V", ".--": "W", "-..-": "X",
    "-.--": "Y", "--..": "Z", "-----": "0", ".----": "1", "..---": "2",
    "...--": "3", "....-": "4", ".....": "5", "-....": "6", "--...": "7",
    "---..": "8", "----.": "9", "--..--": ",", ".-.-.-": ".", "..--..": "?",
    "-..-.": "/", "-....-": "-", "-.--.": "(", "-.--.-": ")", ".----.": "'",
    "---...": ":", "-...-": "=", ".-.-.": "+", ".--.-.": "@", "..--.-": "_",
    "-.-.--": "!", ".-..-.": '"', "...-..-": "$", ".-...": "&", "...---...": "SOS",
}


def dump_properties(obj):
    """Return a readable list of gdstk properties on any object."""
    out = []
    props = getattr(obj, "properties", None)
    if props:
        for p in props:
            vals = []
            for v in p[1:]:
                if isinstance(v, bytes):
                    try:
                        vals.append(v.decode("ascii"))
                    except UnicodeDecodeError:
                        vals.append(repr(v))
                else:
                    vals.append(str(v))
            out.append(f"{p[0]}: " + " | ".join(vals))
    return out


def scan_ascii(path, minlen=6):
    data = open(path, "rb").read()
    runs = re.findall(rb"[ -~]{%d,}" % minlen, data)
    seen, out = set(), []
    for r in runs:
        s = r.decode("ascii")
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out, len(data)


BOILER = re.compile(
    r"^(sky130_fd_sc_hd__|sky130_fd_|VIA_|INTERNAL_|puzzle$|GDSII|KLayout|gdstk|"
    r"LIB|libname|\$\$\$)", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gds", nargs="?", default=DEFAULT_GDS)
    ap.add_argument("--outdir", default=DEFAULT_OUT)
    ap.add_argument("--marker-layer", type=int, default=MARKER_LAYER)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    lib = gdstk.read_gds(args.gds)
    lines = []
    w = lines.append

    w("=" * 78)
    w(f"LIBRARY: name={lib.name!r} unit={lib.unit} precision={lib.precision}")
    w("=" * 78)
    for p in dump_properties(lib):
        w("  libprop " + p)
    top = lib.top_level()
    w(f"top-level cells: {[c.name for c in top]}")
    w(f"total cells: {len(lib.cells)}")
    w("")

    # ---- cell inventory -------------------------------------------------
    w("=" * 78)
    w("CELL INVENTORY (non sky130 std cells + anything with properties)")
    w("=" * 78)
    marker_cells = []
    for c in sorted(lib.cells, key=lambda x: x.name):
        layers = Counter()
        for pol in c.polygons:
            layers[(pol.layer, pol.datatype)] += 1
        for pth in c.paths:
            for lay, dt in zip(pth.layers, pth.datatypes):
                layers[(lay, dt)] += 1
        props = dump_properties(c)
        interesting = (not c.name.startswith("sky130_fd_sc_hd__")) or props
        if any(l == args.marker_layer for l, _ in layers):
            marker_cells.append(c.name)
        if interesting:
            bb = c.bounding_box()
            size = (round(bb[1][0] - bb[0][0], 4), round(bb[1][1] - bb[0][1], 4)) if bb else None
            w(f"  {c.name:<28} bbox_size={size} polys={len(c.polygons)} "
              f"paths={len(c.paths)} labels={len(c.labels)} refs={len(c.references)}")
            if layers:
                w("      layers: " + ", ".join(f"{l}/{d}x{n}" for (l, d), n in sorted(layers.items())))
            for p in props:
                w("      PROPERTY " + p)
    w("")
    w(f"cells containing layer {args.marker_layer}: {marker_cells}")
    w("")

    # ---- all properties anywhere ---------------------------------------
    w("=" * 78)
    w("ALL GDS PROPERTIES FOUND ANYWHERE (cells, refs, polys, paths, labels)")
    w("=" * 78)
    found_any = False
    for c in lib.cells:
        for kind, items in (("cell", [c]), ("ref", c.references),
                            ("poly", c.polygons), ("path", c.paths),
                            ("label", c.labels)):
            for it in items:
                for p in dump_properties(it):
                    found_any = True
                    nm = getattr(getattr(it, "cell", None), "name", None)
                    w(f"  [{c.name}] {kind}{'->' + nm if nm else ''}: {p}")
    if not found_any:
        w("  (none -- no GDS PROPATTR/PROPVALUE records survive in this file)")
    w("")

    # ---- top cell labels -------------------------------------------------
    tc = top[0]
    w("=" * 78)
    w(f"TOP CELL {tc.name!r} LABELS")
    w("=" * 78)
    for lb in sorted(tc.labels, key=lambda l: (l.layer, l.texttype, l.origin[1], l.origin[0])):
        w(f"  {lb.text!r:<16} layer={lb.layer}/{lb.texttype} "
          f"origin=({lb.origin[0]:.3f},{lb.origin[1]:.3f}) rot={lb.rotation} "
          f"mag={lb.magnification} anchor={lb.anchor}")
    w("")

    # ---- marker instances -----------------------------------------------
    w("=" * 78)
    w(f"MARKER INSTANCES (cells drawing on layer {args.marker_layer})")
    w("=" * 78)
    widths = {}
    for name in marker_cells:
        c = lib[name][0] if isinstance(lib[name], list) else lib[name]
        bb = c.bounding_box()
        widths[name] = (round(bb[1][0] - bb[0][0], 4), round(bb[1][1] - bb[0][1], 4))
        w(f"  cell {name}: size={widths[name]} polys={len(c.polygons)}")
        for pol in c.polygons:
            w(f"      poly layer={pol.layer}/{pol.datatype} bbox={pol.bounding_box()}")
    w("")

    insts = []
    for ref in tc.references:
        if ref.cell.name in marker_cells:
            if ref.repetition and ref.repetition.size:
                for off in ref.repetition.offsets:
                    insts.append((ref.cell.name, (ref.origin[0] + off[0], ref.origin[1] + off[1]),
                                  ref.rotation, ref.x_reflection, ref.magnification))
                continue
            insts.append((ref.cell.name, tuple(ref.origin), ref.rotation,
                          ref.x_reflection, ref.magnification))
    w(f"marker instances in top cell: {len(insts)}")
    insts_sorted = sorted(insts, key=lambda t: (-round(t[1][1], 3), round(t[1][0], 3)))
    w(f"{'#':>4} {'cell':<12} {'x(um)':>10} {'y(um)':>10} {'rot':>6} {'xrefl':>6} {'mag':>5}")
    for i, (nm, org, rot, xr, mag) in enumerate(insts_sorted):
        w(f"{i:>4} {nm:<12} {org[0]:>10.3f} {org[1]:>10.3f} {rot:>6} {str(xr):>6} {mag:>5}")
    w("")

    # ---- group into rows and decode -------------------------------------
    rows = defaultdict(list)
    for nm, org, rot, xr, mag in insts:
        rows[round(org[1], 3)].append((round(org[0], 3), nm))
    w("=" * 78)
    w("MARKER ROWS (grouped by y), left-to-right, with x-gaps")
    w("=" * 78)
    all_symbols = []
    for y in sorted(rows, reverse=True):
        items = sorted(rows[y])
        w(f"  y={y}  n={len(items)}")
        prev_end = None
        syms, gaps = [], []
        for x, nm in items:
            wdt = widths[nm][0]
            sym = "." if wdt < 2.0 else "-"
            syms.append(sym)
            if prev_end is not None:
                gaps.append(round(x - prev_end, 3))
            prev_end = x + wdt
            w(f"     x={x:>9.3f} w={wdt:<6} {nm:<12} sym={sym}")
        w("     gaps: " + str(gaps))
        w("     symbol string: " + "".join(syms))
        all_symbols.append((y, syms, gaps))
    w("")

    # morse attempt with several gap thresholds
    w("=" * 78)
    w("MORSE DECODE ATTEMPTS (narrow=dot, wide=dash)")
    w("=" * 78)
    for y, syms, gaps in all_symbols:
        if not gaps:
            continue
        uniq = sorted(set(gaps))
        w(f"  row y={y}: distinct gaps = {uniq}")
        # try every pair of thresholds derived from the distinct gaps
        for li in range(len(uniq)):
            for wi in range(li, len(uniq)):
                lg, wg = uniq[li], uniq[wi]
                letters, cur, words, txt = [], "", [], []
                for i, s in enumerate(syms):
                    cur += s
                    g = gaps[i] if i < len(gaps) else 1e9
                    if g >= wg and wg > lg:
                        letters.append(cur); cur = ""
                        letters.append("/")
                    elif g >= lg:
                        letters.append(cur); cur = ""
                if cur:
                    letters.append(cur)
                dec = "".join(MORSE.get(L, "/" if L == "/" else "?") if L != "/" else " "
                              for L in letters if L)
                if "?" not in dec and len(dec.strip()) >= 3:
                    w(f"    letter_gap>={lg} word_gap>={wg}: {dec!r}  ({' '.join(letters)})")
    w("")

    # binary attempt
    w("=" * 78)
    w("BINARY DECODE ATTEMPTS (narrow=0/wide=1 and inverse), per row and global")
    w("=" * 78)

    def bin_try(bits, tag):
        for lsb in (False, True):
            chars = []
            for k in range(0, len(bits) - 7, 8):
                byte = bits[k:k + 8]
                if lsb:
                    byte = byte[::-1]
                v = int("".join(byte), 2)
                chars.append(chr(v) if 32 <= v < 127 else ".")
            if chars:
                w(f"  {tag} {'lsb' if lsb else 'msb'}: {''.join(chars)!r}")
        # 5-bit baudot-ish / a-z 1..26
        for k in (5, 6):
            chars = []
            for j in range(0, len(bits) - k + 1, k):
                v = int("".join(bits[j:j + k]), 2)
                chars.append(chr(64 + v) if 1 <= v <= 26 else "?")
            w(f"  {tag} {k}-bit A1Z26: {''.join(chars)!r}")

    for y, syms, gaps in all_symbols:
        bits = ["0" if s == "." else "1" for s in syms]
        bin_try(bits, f"row y={y} (.=0)")
        bin_try(["1" if b == "0" else "0" for b in bits], f"row y={y} (.=1)")
    flat = []
    for y, syms, gaps in all_symbols:
        flat.extend("0" if s == "." else "1" for s in syms)
    bin_try(flat, "ALL ROWS (.=0)")
    bin_try(["1" if b == "0" else "0" for b in flat], "ALL ROWS (.=1)")
    w("")

    # ---- raw ASCII scan --------------------------------------------------
    strs, nbytes = scan_ascii(args.gds)
    w("=" * 78)
    w(f"RAW ASCII SCAN of {os.path.basename(args.gds)} ({nbytes} bytes), runs >= 6 chars")
    w("=" * 78)
    w(f"  {len(strs)} distinct runs; non-boilerplate ones:")
    for s in strs:
        if not BOILER.match(s.strip()):
            w(f"    {s!r}")
    w("")
    w("  boilerplate-looking runs (first 40 distinct):")
    for s in strs[:40]:
        w(f"    {s!r}")

    stem = os.path.splitext(os.path.basename(args.gds))[0]
    out = os.path.join(args.outdir, f"gds_recon_{stem}.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"wrote {out}  ({len(lines)} lines)")

    # machine-readable marker dump for the plotter
    import json
    with open(os.path.join(args.outdir, f"markers_{stem}.json"), "w") as fh:
        json.dump({"widths": widths,
                   "instances": [{"cell": n, "x": o[0], "y": o[1], "rot": r,
                                  "xrefl": bool(xr), "mag": m}
                                 for n, o, r, xr, m in insts]}, fh, indent=1)
    print(f"wrote markers_{stem}.json")


if __name__ == "__main__":
    main()
