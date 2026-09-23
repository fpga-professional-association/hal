#!/usr/bin/env python3
"""
reference.py -- behavioural reference model of the Jane Street 2026 ASIC
puzzle chip, recovered by structural reverse engineering of ``puzzle.gds``.

This is the Python twin of ``recovered.v``; both are written from the same
derivation and both are checked cycle-for-cycle against the gate-level
simulation of the extracted netlist (``tools/check_recovered.py``).

What the chip is
----------------
It is a **Star Battle judge**.  121 serial bits are an 11x11 grid read
row-major; a 1 is a star.  The chip accepts the grid iff

  * every row has exactly 2 stars,
  * every column has exactly 2 stars,
  * every one of 11 irregular regions has exactly 2 stars,
  * there are 22 stars in total,
  * no two stars touch, not even diagonally,

and the region map is hard-wired in ~130 gates of random logic (see
``extract_region_map.py``).  While the grid streams in, an 8-bit LFSR
(``x^8 + x^6 + x^5 + x^4 + 1`` in this shift direction, seeded 0xA5) absorbs
every bit; on acceptance the answer message is emitted as that LFSR XORed
with a per-byte mask, so the success text cannot be read out of the layout
without first solving the puzzle.

Usage::

    python reference.py --solve
    python reference.py --bits <121 bits>
"""

from __future__ import annotations

import argparse
import os
import sys

# --------------------------------------------------------------------------
# constants lifted from the netlist
# --------------------------------------------------------------------------

N = 11
NBITS = N * N            # 121
STARS = 2                # per row / column / region
TOTAL = N * STARS        # 22

#: region of each (row, column), read out of the {n165,n199,n207,n216} cone.
REGION = [
    [6, 6, 6, 6, 6, 8, 8, 5, 4, 4, 9],
    [6, 6, 0, 6, 6, 8, 5, 5, 4, 4, 9],
    [6, 6, 0, 8, 8, 8, 8, 5, 5, 4, 9],
    [6, 6, 0, 8, 1, 1, 1, 9, 5, 5, 9],
    [0, 6, 0, 8, 1, 9, 9, 9, 9, 9, 9],
    [0, 0, 0, 8, 1, 1, 1, 9, 2, 2, 2],
    [8, 8, 8, 8, 8, 8, 1, 9, 2, 10, 10],
    [8, 7, 7, 7, 1, 1, 1, 9, 2, 10, 10],
    [8, 7, 7, 3, 9, 9, 9, 9, 2, 10, 10],
    [8, 8, 7, 3, 3, 9, 9, 9, 2, 2, 2],
    [8, 7, 7, 3, 9, 9, 9, 9, 9, 9, 9],
]

LFSR_SEED = 0xA5                     # the four dfstp_2 cells
LFSR_TAPS = (7, 5, 4, 3)             # bits XORed into the incoming bit

#: canned messages; index = output byte counter 0..15
MSG_FAIL = b"TRY AGAIN\0\0\0\0\0\0\0"
MSG_TOUCH = b"TWO\"NOT TOUCH\0\0\0"
MSG_EMPTY = b"EMPTY SKY\0\0\0\0\0\0\0"
MSG_FULL = b"BIG BANG\0\0\0\0\0\0\0\0"

#: success message is LFSR ^ this mask, byte by byte
MSG_SUCCESS_MASK = [0x4D, 0xAD, 0xFB, 0x83, 0x13, 0x79, 0x1C, 0xB5,
                    0x79, 0x63, 0xC7, 0x68, 0x93, 0xF5, 0x8F, 0x00]


def _sat_inc(c, en):
    """The 2-bit saturating counters used for the column/region buckets."""
    return min(c + 1, 3) if en else c


def _lfsr_in(state, bit):
    fb = bit
    for t in LFSR_TAPS:
        fb ^= (state >> t) & 1
    return ((state << 1) | fb) & 0xFF


def _lfsr_out(p):
    """One keystream byte: the LFSR advances eight times per emitted byte.

    The synthesised logic is the collapsed 8-step transition matrix (the flat
    equations for n499/n500/n509/... during the read-out phase); running the
    one-bit step eight times is bit-identical for all 256 states.
    """
    for _ in range(8):
        p = _lfsr_in(p, 0)
    return p


class Puzzle:
    """Cycle-accurate model.  ``tick()`` == one rising clock edge."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.col = 0            # bit index inside the character  (n86,n6,n93,n182)
        self.row = 0            # character index                 (n87,n89,n88,n102)
        self.done = 0           # n575
        self.outphase = 0       # n665
        self.shift = 0          # 12-deep input delay line, bit0 = 1 cycle ago
        self.adj_bad = 0        # n418
        self.pop = 0            # 8-bit population counter
        self.row_prev = 0       # n327
        self.row_two = 0        # n312
        self.row_bad = 0        # n284
        self.col_cnt = [0] * N
        self.reg_cnt = [0] * N
        self.lfsr = LFSR_SEED
        self.outcnt = 0
        self.success = 0
        self.near = 0           # n680: everything right except the touching rule
        self.O = 0

    # -- combinational output ------------------------------------------------
    def _outputs(self):
        if not self.outphase or self.outcnt == 15:
            return 0
        k = self.outcnt
        if self.pop == 0:
            return MSG_EMPTY[k]
        if self.pop == NBITS:
            return MSG_FULL[k]
        if self.success:
            return self.lfsr ^ MSG_SUCCESS_MASK[k]
        if self.near:
            return MSG_TOUCH[k]
        return MSG_FAIL[k]

    # -- one rising edge -----------------------------------------------------
    def tick(self, rst_n=1, enable=0, I=0):
        if not rst_n:
            self.reset()
            self.O = self._outputs()
            return self.O, self.success

        was_done = self.done            # every flop samples the *old* state
        act = enable and not self.done
        last_col = (self.col == N - 1)
        step_out = self.outphase and self.outcnt != 15 and not act

        # --- checks that look at the *current* cycle's inputs ---------------
        # (row_two, row_prev) encodes "stars seen so far in this row":
        #   00 = 0, 01 = 1, 10 = 2, 11 = 3 or more
        if act and last_col:
            bad_row = ((not (I and self.row_prev)) and not self.row_two) or \
                      (self.row_two and (I or self.row_prev))
            new_row_bad = self.row_bad or bad_row
        else:
            new_row_bad = self.row_bad

        if act:
            touch = I and (
                (self.col != 0 and (self.shift >> 11) & 1) or       # up-left
                (self.col != 0 and (self.shift >> 0) & 1) or        # left
                ((self.shift >> 9) & 1 and self.col != N - 1) or    # up-right
                ((self.shift >> 10) & 1))                           # up
            new_adj = self.adj_bad or bool(touch)
        else:
            new_adj = self.adj_bad

        sel = REGION[self.row][self.col]
        cur_col = self.col

        # --- verdict registers (sampled from the *previous* cycle's state) --
        all_ok = (self.pop == TOTAL and not self.row_bad
                  and all(c == 2 for c in self.col_cnt)
                  and all(c == 2 for c in self.reg_cnt)
                  and self.done and not self.outphase)
        new_success = self.success
        new_near = self.near
        if self.done and not self.outphase:
            if all_ok and not self.adj_bad:
                new_success = 1
            if all_ok and self.adj_bad:
                new_near = 1

        # --- datapath -------------------------------------------------------
        if act:
            self.shift = ((self.shift << 1) | I) & 0xFFF
            self.pop = (self.pop + I) & 0xFF
            self.col_cnt[cur_col] = _sat_inc(self.col_cnt[cur_col], I)
            self.reg_cnt[sel] = _sat_inc(self.reg_cnt[sel], I)
            self.lfsr = _lfsr_in(self.lfsr, I)
            if last_col:
                self.row_prev = 0
                self.row_two = 0
                if self.row == N - 1:
                    self.done = 1
                    self.row = 0
                else:
                    self.row += 1
                self.col = 0
            else:
                prev, two = self.row_prev, self.row_two
                self.row_two = int(bool(two or (I and prev)))
                self.row_prev = int(bool((I or prev)
                                         and ((not (I and prev)) or two)))
                self.col += 1
        elif step_out:
            self.lfsr = _lfsr_out(self.lfsr)

        self.row_bad = int(new_row_bad)
        self.adj_bad = int(new_adj)
        self.success = new_success
        self.near = new_near

        was_outphase = self.outphase
        if was_outphase and self.outcnt != 15:
            self.outcnt += 1
        if was_done:
            self.outphase = 1

        self.O = self._outputs()
        return self.O, self.success


# --------------------------------------------------------------------------
# convenience
# --------------------------------------------------------------------------

def run(bits, tail=16):
    p = Puzzle()
    for _ in range(3):
        p.tick(rst_n=0)
    p.tick(rst_n=1)
    for b in bits:
        p.tick(rst_n=1, enable=1, I=b)
    out = []
    for _ in range(tail):
        out.append(p.tick(rst_n=1, enable=0, I=0))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bits')
    ap.add_argument('--solve', action='store_true')
    a = ap.parse_args()
    if a.solve:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from starbattle import solve, to_bits, render
        sols = solve(REGION)
        print('%d solution(s)' % len(sols))
        print(render(sols[0], REGION))
        bits = to_bits(sols[0])
    else:
        bits = [int(c) for c in a.bits if c in '01']
    out = run(bits)
    msg = ''.join(chr(b) for b, _ in out if b)
    print('success: %d' % max(s for _, s in out))
    print('message: %r' % msg)


if __name__ == '__main__':
    main()
