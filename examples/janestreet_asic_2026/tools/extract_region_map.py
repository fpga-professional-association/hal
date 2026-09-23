#!/usr/bin/env python3
"""
extract_region_map.py -- read the Star Battle region map straight out of the
puzzle's gate netlist.

The puzzle chip streams 121 input bits as 11 "characters" of 11 bits.  Two
4-bit counters track where in that stream we are:

    column counter (bit index in the character):  n86,n6,n93,n182  (b0..b3)
    row counter    (character index):             n87,n89,n88,n102 (b0..b3)

A 4-bit combinational function {n165,n199,n207,n216} of those eight flop
outputs selects one of 11 "bucket" counters.  That function *is* the region
map of an 11x11 Star Battle grid, baked into ~130 gates of random logic.
This script evaluates it for all 121 (row, column) pairs.

Writes ``artifacts/region_map.txt``.
"""

from __future__ import annotations

import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import symbolic as S
from symlift import Lift

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COL_BITS = ['n86', 'n6', 'n93', 'n182']      # b0..b3 of the column counter
ROW_BITS = ['n87', 'n89', 'n88', 'n102']     # b0..b3 of the row counter
SEL_BITS = ['n165', 'n199', 'n207', 'n216']  # b0..b3 of the region selector

N = 11


def region_map(path=None):
    path = path or os.path.join(ROOT, 'artifacts', 'puzzle_netlist.json')
    lift = Lift(path)
    sel = [lift.net(n) for n in SEL_BITS]
    grid = []
    for r in range(N):
        row = []
        for c in range(N):
            env = {}
            for i in range(4):
                env[COL_BITS[i]] = (c >> i) & 1
                env[ROW_BITS[i]] = (r >> i) & 1
            row.append(sum(S.evaluate(sel[i], env) << i for i in range(4)))
        grid.append(row)
    return grid


def main():
    g = region_map()
    cnt = collections.Counter(v for row in g for v in row)
    out = [' '.join('%X' % v for v in row) for row in g]
    text = ('Star Battle region map recovered from the gate netlist\n'
            '(rows = character index, columns = bit index)\n\n'
            + '\n'.join(out) + '\n\nregion sizes: '
            + ', '.join('%X:%d' % kv for kv in sorted(cnt.items())) + '\n')
    p = os.path.join(ROOT, 'artifacts', 'region_map.txt')
    open(p, 'w').write(text)
    print(text)


if __name__ == '__main__':
    main()
