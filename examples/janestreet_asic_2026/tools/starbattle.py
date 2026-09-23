#!/usr/bin/env python3
"""
starbattle.py -- plain backtracking Star Battle solver (no SAT).

Solves the 11x11, two-stars-per-row/column/region puzzle whose region map was
read out of the puzzle chip's gate netlist by ``extract_region_map.py``.

Rules implemented (all of them are literally gates in the netlist):
  * exactly 2 stars in every row      -- the n312/n327/n284 per-row checker
  * exactly 2 stars in every column   -- the 11 two-bit column counters
  * exactly 2 stars in every region   -- the 11 two-bit region counters
  * 22 stars in total                 -- the 8-bit population counter == 22
  * no two stars orthogonally or diagonally adjacent -- the n418 checker
    fed from the 12-deep input shift register

Placing row by row with column/region/adjacency pruning is enough; the search
finishes in milliseconds.
"""

from __future__ import annotations

import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

N = 11
K = 2


def solve(regions, n=N, k=K, limit=None):
    """Yield solutions as lists of row tuples of column indices."""
    pairs = [p for p in itertools.combinations(range(n), k)
             if all(b - a > 1 for a, b in zip(p, p[1:]))]
    col_count = [0] * n
    reg_count = {}
    for row in regions:
        for v in row:
            reg_count[v] = 0
    sols = []
    rows_left_for_col = [n] * n

    def rec(r, prev):
        if limit is not None and len(sols) >= limit:
            return
        if r == n:
            if all(c == k for c in col_count) and all(v == k for v in reg_count.values()):
                sols.append(list(cur))
            return
        rows_remaining = n - r
        for c in range(n):
            if col_count[c] + rows_remaining < k:
                return
        for p in pairs:
            # column capacity
            if any(col_count[c] == k for c in p):
                continue
            # adjacency with the previous row (incl. diagonals)
            if any(abs(c - pc) <= 1 for c in p for pc in prev):
                continue
            regs = [regions[r][c] for c in p]
            if any(reg_count[g] == k for g in regs):
                continue
            if regs[0] == regs[1] and reg_count[regs[0]] + 2 > k:
                continue
            for c in p:
                col_count[c] += 1
            for g in regs:
                reg_count[g] += 1
            cur.append(p)
            rec(r + 1, p)
            cur.pop()
            for c in p:
                col_count[c] -= 1
            for g in regs:
                reg_count[g] -= 1

    cur = []
    rec(0, ())
    return sols


def to_bits(solution, n=N):
    """Row-major bit stream, MSB..: bit (r*n + c) is 1 iff (r,c) has a star."""
    bits = []
    for r in range(n):
        row = [0] * n
        for c in solution[r]:
            row[c] = 1
        bits.extend(row)
    return bits


def render(solution, regions, n=N):
    out = []
    for r in range(n):
        line = []
        for c in range(n):
            line.append('*' if c in solution[r] else '%X' % regions[r][c])
        out.append(' '.join(line))
    return '\n'.join(out)


def main():
    from extract_region_map import region_map
    regions = region_map()
    sols = solve(regions)
    print('%d solution(s)' % len(sols))
    for s in sols:
        print(render(s, regions))
        print('bits:', ''.join(map(str, to_bits(s))))


if __name__ == '__main__':
    main()
