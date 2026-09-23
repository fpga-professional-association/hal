#!/usr/bin/env python3
"""
check_recovered_v.py -- compare ``recovered.v`` (compiled with Icarus Verilog)
against the gate-level simulation of the extracted netlist.

Complements ``check_recovered.py``, which checks the Python twin
(``tools/reference.py``).  Requires ``iverilog``/``vvp`` on PATH.
"""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sc_sim
from run_attempt import ascii_bits
from reference import REGION
from starbattle import solve, to_bits

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NETLIST = os.path.join(ROOT, 'artifacts', 'puzzle_netlist.json')
TAIL = 20


def gate(bits, nl):
    sim = sc_sim.Simulator(nl)
    for _ in range(3):
        sim.step({'rst_n': 0, 'enable': 0, 'I': 0})
    sim.step({'rst_n': 1, 'enable': 0, 'I': 0})
    for b in bits:
        sim.step({'rst_n': 1, 'enable': 1, 'I': b})
    out = []
    for _ in range(TAIL):
        sim.step({'rst_n': 1, 'enable': 0, 'I': 0})
        out.append((sim.get_bus('O', 8), sim.get('success')))
    return out


def rtl(bits, vvp, tmp):
    p = os.path.join(tmp, 'payload.txt')
    open(p, 'w').write(''.join(map(str, bits)))
    r = subprocess.run(['vvp', vvp, '+payload=%s' % p.replace('\\', '/')],
                       capture_output=True, text=True, check=True)
    out = []
    for line in r.stdout.splitlines():
        f = line.split()
        if len(f) == 2 and f[0].isdigit():
            out.append((int(f[0]), int(f[1])))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-n', '--random', type=int, default=20)
    ap.add_argument('--seed', type=int, default=20260923)
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix='recovered_v_')
    vvp = os.path.join(tmp, 'tb.vvp')
    subprocess.run(['iverilog', '-o', vvp, os.path.join(ROOT, 'recovered.v'),
                    os.path.join(HERE, 'tb_recovered.v')], check=True)

    sol = solve(REGION)[0]
    cases = [('example "The night s"', ascii_bits('The night s')),
             ('star battle solution', to_bits(sol)),
             ('all zeros', [0] * 121),
             ('all ones', [1] * 121)]
    rng = random.Random(a.seed)
    for i in range(a.random):
        cases.append(('random #%d' % i, [rng.randint(0, 1) for _ in range(121)]))

    nl = sc_sim.load_netlist(NETLIST)
    bad = 0
    for name, bits in cases:
        g, r = gate(bits, nl), rtl(bits, vvp, tmp)
        if g != r:
            bad += 1
            print('MISMATCH %-28s gate=%s rtl=%s' % (name, g[:6], r[:6]))
        else:
            txt = ''.join(chr(b) for b, _ in g if 32 <= b < 127)
            print('ok  %-30s success=%d %r'
                  % (name, max(s for _, s in g), txt))
    print('\n%d/%d cases match' % (len(cases) - bad, len(cases)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
