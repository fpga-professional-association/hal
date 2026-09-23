#!/usr/bin/env python3
"""
run_attempt.py -- drive the recovered puzzle netlist through one full attempt
and print whatever the chip answers.

Protocol (decoded in phase A, see artifacts/recon_notes.md):

    rst_n = 0 for 3 rising edges
    rst_n = 1, one idle rising edge with enable = 0
    enable = 1 for exactly 121 rising edges, one payload bit on ``I`` each
    enable = 0; the chip then emits one byte on ``O[7:0]`` per rising edge

Usage::

    python run_attempt.py --bits 0101...        # 121 bits, MSB = first bit
    python run_attempt.py --ascii "The night s" # 8 data bits LSB-first + 3 pad
    python run_attempt.py --solve               # solve the Star Battle first
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sc_sim

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NETLIST = os.path.join(ROOT, 'artifacts', 'puzzle_netlist.json')

NBITS = 121
NOUT = 16


def ascii_bits(text, nchar=11, nbit=11, data=8):
    text = (text + ' ' * nchar)[:nchar]
    bits = []
    for ch in text:
        v = ord(ch)
        bits.extend((v >> i) & 1 for i in range(data))
        bits.extend([0] * (nbit - data))
    return bits


def run(bits, netlist_path=NETLIST, nout=NOUT):
    nl = sc_sim.load_netlist(netlist_path)
    sim = sc_sim.Simulator(nl)
    for _ in range(3):
        sim.step({'rst_n': 0, 'enable': 0, 'I': 0})
    sim.step({'rst_n': 1, 'enable': 0, 'I': 0})
    for b in bits:
        sim.step({'rst_n': 1, 'enable': 1, 'I': b})
    out = []
    for _ in range(nout):
        sim.step({'rst_n': 1, 'enable': 0, 'I': 0})
        out.append((sim.get_bus('O', 8), sim.get('success')))
    return out


def decode(out):
    msg = ''.join(chr(b) if 32 <= b < 127 else ('' if b == 0 else '\\x%02x' % b)
                  for b, _ in out)
    return msg, max(s for _, s in out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bits')
    ap.add_argument('--ascii')
    ap.add_argument('--solve', action='store_true')
    a = ap.parse_args()
    if a.solve:
        from extract_region_map import region_map
        from starbattle import solve, to_bits, render
        regions = region_map()
        sols = solve(regions)
        print('star battle solutions: %d' % len(sols))
        print(render(sols[0], regions))
        bits = to_bits(sols[0])
    elif a.bits:
        bits = [int(c) for c in a.bits.strip() if c in '01']
    elif a.ascii is not None:
        bits = ascii_bits(a.ascii)
    else:
        ap.error('need --bits, --ascii or --solve')
    assert len(bits) == NBITS, len(bits)
    print('payload bits (%d): %s' % (len(bits), ''.join(map(str, bits))))
    out = run(bits)
    msg, succ = decode(out)
    print('raw bytes : %s' % ' '.join('%02x' % b for b, _ in out))
    print('success   : %d' % succ)
    print('message   : %r' % msg)


if __name__ == '__main__':
    main()
