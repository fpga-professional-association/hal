#!/usr/bin/env python3
"""
check_recovered.py -- equivalence-check the behavioural model
(``tools/reference.py`` / ``recovered.v``) against the gate-level simulation
of the netlist extracted from ``puzzle.gds``.

For each stimulus the two models are stepped in lockstep through a complete
attempt (reset, 121 payload bits, 16 read-out cycles) and ``O[7:0]`` and
``success`` are compared on every rising edge.

Stimuli: the shipped example payload, the Star Battle solution, all-zeros,
all-ones, near misses, and N random 121-bit vectors.
"""

from __future__ import annotations

import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sc_sim
import reference
from run_attempt import ascii_bits
from starbattle import solve, to_bits

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NETLIST = os.path.join(ROOT, 'artifacts', 'puzzle_netlist.json')
TAIL = 20


def gate_trace(bits, sim):
    tr = []
    for _ in range(3):
        sim.step({'rst_n': 0, 'enable': 0, 'I': 0})
        tr.append((sim.get_bus('O', 8), sim.get('success')))
    sim.step({'rst_n': 1, 'enable': 0, 'I': 0})
    tr.append((sim.get_bus('O', 8), sim.get('success')))
    for b in bits:
        sim.step({'rst_n': 1, 'enable': 1, 'I': b})
        tr.append((sim.get_bus('O', 8), sim.get('success')))
    for _ in range(TAIL):
        sim.step({'rst_n': 1, 'enable': 0, 'I': 0})
        tr.append((sim.get_bus('O', 8), sim.get('success')))
    return tr


def model_trace(bits):
    p = reference.Puzzle()
    tr = []
    for _ in range(3):
        tr.append(p.tick(rst_n=0))
    tr.append(p.tick(rst_n=1))
    for b in bits:
        tr.append(p.tick(rst_n=1, enable=1, I=b))
    for _ in range(TAIL):
        tr.append(p.tick(rst_n=1, enable=0, I=0))
    return tr


def msg(tr):
    return ''.join(chr(b) for b, _ in tr if 32 <= b < 127)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-n', '--random', type=int, default=40)
    ap.add_argument('--seed', type=int, default=20260923)
    a = ap.parse_args()

    regions = reference.REGION
    sol = solve(regions)[0]
    cases = [
        ('example "The night s"', ascii_bits('The night s')),
        ('star battle solution', to_bits(sol)),
        ('all zeros', [0] * 121),
        ('all ones', [1] * 121),
    ]
    # near misses around the solution
    base = to_bits(sol)
    for k in (0, 17, 60, 120):
        v = list(base)
        v[k] ^= 1
        cases.append(('solution with bit %d flipped' % k, v))
    rng = random.Random(a.seed)
    for i in range(a.random):
        cases.append(('random #%d' % i, [rng.randint(0, 1) for _ in range(121)]))
    # random weight-22 vectors (hit the popcount==22 path more often)
    for i in range(a.random // 2):
        v = [0] * 121
        for j in rng.sample(range(121), 22):
            v[j] = 1
        cases.append(('random weight-22 #%d' % i, v))

    nl = sc_sim.load_netlist(NETLIST)
    bad = 0
    for name, bits in cases:
        sim = sc_sim.Simulator(nl)
        g = gate_trace(bits, sim)
        m = model_trace(bits)
        if g != m:
            bad += 1
            for i, (x, y) in enumerate(zip(g, m)):
                if x != y:
                    print('MISMATCH %-32s cycle %3d gate=%s model=%s'
                          % (name, i, x, y))
                    break
        else:
            print('ok  %-34s -> success=%d %r'
                  % (name, max(s for _, s in g), msg(g)))
    print('\n%d/%d cases match' % (len(cases) - bad, len(cases)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
