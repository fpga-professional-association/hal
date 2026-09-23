#!/usr/bin/env python3
"""
flop_map.py -- name all 92 flip-flops of the puzzle chip and emit the flop map
table used by ``artifacts/structure_report.md``.

The grouping is the result of reading the lifted next-state equations
(``tools/symlift.py`` -> ``artifacts/structured_eqs.txt``); this file just
records the naming and joins it with the physical placement from the GDS
extraction.

Writes ``artifacts/flop_map.md``.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from symlift import Lift

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (group, [(Q net, role)])
GROUPS = [
    ('bit counter (column, 0..10)', [
        ('n86', 'col[0]'), ('n6', 'col[1]'), ('n93', 'col[2]'), ('n182', 'col[3]')]),
    ('character counter (row, 0..10)', [
        ('n87', 'row[0]'), ('n89', 'row[1]'), ('n88', 'row[2]'), ('n102', 'row[3]')]),
    ('population counter (total stars)', [
        ('n213', 'pop[0]'), ('n146', 'pop[1]'), ('n147', 'pop[2]'), ('n173', 'pop[3]'),
        ('n130', 'pop[4]'), ('n142', 'pop[5]'), ('n189', 'pop[6]'), ('n204', 'pop[7]')]),
    ('per-row star checker', [
        ('n327', 'row_prev (row count low bit)'),
        ('n312', 'row_two  (row count high bit)'),
        ('n284', 'row_bad  (sticky: some row != 2)')]),
    ('input delay line (adjacency window)', [
        ('n468', 'sr[0]  = I one bit ago (left neighbour)'),
        ('n466', 'sr[1]'), ('n475', 'sr[2]'), ('n444', 'sr[3]'), ('n415', 'sr[4]'),
        ('n398', 'sr[5]'), ('n402', 'sr[6]'), ('n386', 'sr[7]'), ('n393', 'sr[8]'),
        ('n405', 'sr[9]  = up-right neighbour'),
        ('n419', 'sr[10] = up neighbour'),
        ('n456', 'sr[11] = up-left neighbour')]),
    ('adjacency violation flag', [('n418', 'adj_bad (sticky)')]),
    ('message LFSR / input digest', [
        ('n509', 'lfsr[0] (dfstp -> 1)'), ('n562', 'lfsr[1]'),
        ('n524', 'lfsr[2] (dfstp -> 1)'), ('n500', 'lfsr[3]'),
        ('n513', 'lfsr[4]'), ('n550', 'lfsr[5] (dfstp -> 1)'),
        ('n522', 'lfsr[6]'), ('n499', 'lfsr[7] (dfstp -> 1)')]),
    ('phase control', [
        ('n575', 'done (121 bits consumed)'),
        ('n665', 'outphase (message streaming)')]),
    ('output byte counter', [
        ('n252', 'outcnt[0]'), ('n245', 'outcnt[1]'),
        ('n246', 'outcnt[2]'), ('n291', 'outcnt[3]')]),
    ('verdict', [('success', 'success'), ('n680', 'near (all counts right, stars touch)')]),
]

REGION_PAIRS = [('n205', 'n176'), ('n223', 'n218'), ('n225', 'n240'),
                ('n242', 'n231'), ('n288', 'n264'), ('n271', 'n301'),
                ('n331', 'n334'), ('n355', 'n352'), ('n365', 'n363'),
                ('n376', 'n378'), ('n407', 'n422')]
COLUMN_PAIRS = [('n547', 'n533'), ('n583', 'n584'), ('n599', 'n604'),
                ('n627', 'n619'), ('n652', 'n654'), ('n661', 'n660'),
                ('n678', 'n672'), ('n695', 'n693'), ('n700', 'n704'),
                ('n710', 'n706'), ('n720', 'n721')]

for i, (lo, hi) in enumerate(REGION_PAIRS):
    GROUPS.append(('region star counter %d' % i if i == 0 else None,
                   [(lo, 'reg_cnt[%d][0]' % i), (hi, 'reg_cnt[%d][1]' % i)]))
for i, (lo, hi) in enumerate(COLUMN_PAIRS):
    GROUPS.append((None, [(lo, 'col_cnt[%d][0]' % i), (hi, 'col_cnt[%d][1]' % i)]))


def main():
    lift = Lift(os.path.join(ROOT, 'artifacts', 'puzzle_netlist.json'))
    seen = set()
    lines = ['| group | role | Q net | cell | instance | x (um) | y (um) |',
             '|---|---|---|---|---|---|---|']
    group = None
    for g, members in GROUPS:
        if g is not None:
            group = g
        elif members[0][1].startswith('reg_cnt'):
            group = 'region star counters'
        else:
            group = 'column star counters'
        for q, role in members:
            assert q not in seen, q
            seen.add(q)
            f = lift.flops[lift.qnets[q]]
            lines.append('| %s | `%s` | `%s` | %s | `%s` | %.2f | %.2f |'
                         % (group, role, q, f['kind'], f['inst'],
                            f['x'] / 1000.0, f['y'] / 1000.0))
    assert len(seen) == 92, len(seen)
    text = '# Flop map (92 flip-flops)\n\n' + '\n'.join(lines) + '\n'
    open(os.path.join(ROOT, 'artifacts', 'flop_map.md'), 'w').write(text)
    print(text)


if __name__ == '__main__':
    main()
