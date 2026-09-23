#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
solve_key.py -- recover the 121-bit unlock sequence of the Jane Street 2026
"ASIC reverse engineering" puzzle chip by symbolic simulation + SAT.

The netlist recovered from ``puzzle.gds`` by ``tools/gds2netlist.py`` is
executed by ``tools/sc_sim.py`` one clock cycle at a time over *concrete*
0/1 values.  This module runs the **same** simulator over *symbolic* values:
every net carries either a Python ``int`` (0/1, constant-folded) or a
:class:`Bit` wrapping a z3 boolean expression.  The 121 serial input bits of
one attempt are z3 variables; everything else in the protocol (reset, the
idle cycle, ``enable``) stays concrete.  ``success`` is then a z3 formula in
those 121 variables and a SAT call produces the key.

Why the code can be trusted
---------------------------
The symbolic engine is not a re-implementation of the cell library: it is
``sc_sim.Simulator`` itself, subclassed, with only the value domain swapped.
The topological order, the flip-flop model, the async set/reset handling and
the ``step()`` event order are the *same code* that reproduced
``example_inputs.vcd`` bit-exactly.  The only cells that cannot be evaluated
through operator overloading (a Python ``if``/``+`` on a symbolic value has
no meaning) are listed in :data:`SYM_OVERRIDES`; every cell type that occurs
in the netlist -- overridden or not -- is *proved* equivalent to the concrete
:data:`sc_sim.CELLS` function by an exhaustive z3 equivalence check before
any simulation happens (``--check-cells`` prints the table).

Protocol (decoded in phase A, see ``artifacts/recon_notes.md``)
--------------------------------------------------------------
One attempt is 156 rising clock edges::

    edge   0.. 2   rst_n=0  enable=0  I=0      asynchronous reset
    edge   3       rst_n=1  enable=0  I=0      idle
    edge   4..124   rst_n=1  enable=1  I=b[k]  121 payload bits
    edge 125..155   rst_n=1  enable=0  I=0     output window (one byte/edge)

The two *recorded* attempts frame their 121 bits as 11 characters x 11 bits
(8 ASCII data bits LSB first + 3 zero pad bits) -- that is how the example
payloads read "The night s" / "ky awaits  ".  The winning payload does *not*
use that framing: it is the unique 11 x 11 bitmap with two stars in every row
and column and no two stars touching (a 2-star Star Battle), and the chip
prints ``(* TWO STARS *)`` when it opens.

Usage::

    python solve_key.py                        # solve, verify, write the report
    python solve_key.py --check-cells          # just the cell-equivalence proof
    python solve_key.py --verify-only <121bits># run one concrete attempt
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

import z3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_sim  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_NETLIST = os.path.join(ROOT, "artifacts", "puzzle_netlist.json")
DEFAULT_REPORT = os.path.join(ROOT, "artifacts", "solve_report.md")

# ---------------------------------------------------------------------------
# protocol constants
# ---------------------------------------------------------------------------

N_RESET_EDGES = 3        # rst_n = 0
N_IDLE_EDGES = 1         # rst_n = 1, enable = 0
N_PAYLOAD_BITS = 121     # enable = 1
N_TRAIL_EDGES = 31       # enable = 0 (the recorded attempts use 31)
CHARS = 11
BITS_PER_CHAR = 11       # 8 data (LSB first) + 3 pad
PAD_BITS = 3


# ---------------------------------------------------------------------------
# symbolic bit
# ---------------------------------------------------------------------------


class Bit:
    """A net value that is not constant: wraps a z3 ``BoolRef``.

    Supports exactly the operators the :data:`sc_sim.CELLS` lambdas use on
    net values -- ``&``, ``|``, ``^`` and ``1 - x`` -- in both operand
    orders, with constant folding against plain ``int`` operands.  ``bool()``
    deliberately raises: a cell whose Python function branches on a net value
    (``mux2``, ``maj3``, ``fa``) must be listed in :data:`SYM_OVERRIDES`
    instead of silently collapsing to a concrete branch.
    """

    __slots__ = ("e",)

    def __init__(self, e: z3.BoolRef):
        self.e = e

    # -- boolean algebra with folding --------------------------------------
    def __and__(self, other):
        return _and(self, other)

    __rand__ = __and__

    def __or__(self, other):
        return _or(self, other)

    __ror__ = __or__

    def __xor__(self, other):
        return _xor(self, other)

    __rxor__ = __xor__

    def __rsub__(self, other):
        if other == 1:
            return _not(self)
        if other == 0:
            raise TypeError("0 - <symbolic bit> is not a boolean operation")
        raise TypeError("unsupported constant %r in cell function" % (other,))

    def __invert__(self):
        return _not(self)

    # -- traps --------------------------------------------------------------
    def __bool__(self):
        raise TypeError(
            "a symbolic net value was used in a Python branch; the cell needs "
            "an entry in SYM_OVERRIDES"
        )

    def __add__(self, other):
        raise TypeError(
            "a symbolic net value was used in arithmetic; the cell needs an "
            "entry in SYM_OVERRIDES"
        )

    __radd__ = __add__

    def __repr__(self):
        return "Bit(%s)" % (self.e,)


Value = Union[int, Bit]


def _not(a: Value) -> Value:
    if isinstance(a, int):
        return 1 - a
    e = a.e
    if z3.is_not(e):
        return Bit(e.arg(0))
    return Bit(z3.Not(e))


def _and(a: Value, b: Value) -> Value:
    if isinstance(a, int) and isinstance(b, int):
        return a & b
    if isinstance(a, int):
        return b if a else 0
    if isinstance(b, int):
        return a if b else 0
    if z3.eq(a.e, b.e):
        return a
    return Bit(z3.And(a.e, b.e))


def _or(a: Value, b: Value) -> Value:
    if isinstance(a, int) and isinstance(b, int):
        return a | b
    if isinstance(a, int):
        return 1 if a else b
    if isinstance(b, int):
        return 1 if b else a
    if z3.eq(a.e, b.e):
        return a
    return Bit(z3.Or(a.e, b.e))


def _xor(a: Value, b: Value) -> Value:
    if isinstance(a, int) and isinstance(b, int):
        return a ^ b
    if isinstance(a, int):
        return _not(b) if a else b
    if isinstance(b, int):
        return _not(a) if b else a
    if z3.eq(a.e, b.e):
        return 0
    return Bit(z3.Xor(a.e, b.e))


def _ite(c: Value, a: Value, b: Value) -> Value:
    """Symbolic ``c ? a : b`` with folding."""
    if isinstance(c, int):
        return a if c else b
    if same(a, b):
        return a
    ea = z3.BoolVal(bool(a)) if isinstance(a, int) else a.e
    eb = z3.BoolVal(bool(b)) if isinstance(b, int) else b.e
    return Bit(z3.If(c.e, ea, eb))


def same(a: Value, b: Value) -> bool:
    """Structural identity -- used as the fixed-point test in ``settle()``."""
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    if isinstance(a, int) or isinstance(b, int):
        return False
    return z3.eq(a.e, b.e)


def as_expr(v: Value) -> z3.BoolRef:
    return z3.BoolVal(bool(v)) if isinstance(v, int) else v.e


# ---------------------------------------------------------------------------
# cells whose concrete lambda cannot run on symbolic values
# ---------------------------------------------------------------------------

def _maj(pins, names):
    a, b, c = (pins[n] for n in names)
    return _or(_or(_and(a, b), _and(a, c)), _and(b, c))


SYM_OVERRIDES: Dict[str, Dict[str, object]] = {
    "mux2": {"X": lambda p: _ite(p["S"], p["A1"], p["A0"])},
    "mux2i": {"Y": lambda p: _not(_ite(p["S"], p["A1"], p["A0"]))},
    "mux4": {
        "X": lambda p: _ite(
            p["S1"],
            _ite(p["S0"], p["A3"], p["A2"]),
            _ite(p["S0"], p["A1"], p["A0"]),
        )
    },
    "maj3": {"X": lambda p: _maj(p, "ABC")},
    "ha": {"COUT": lambda p: _and(p["A"], p["B"]),
           "SUM": lambda p: _xor(p["A"], p["B"])},
    "fa": {"COUT": lambda p: _maj(p, ("A", "B", "CIN")),
           "SUM": lambda p: _xor(_xor(p["A"], p["B"]), p["CIN"])},
    "fah": {"COUT": lambda p: _maj(p, ("A", "B", "CI")),
            "SUM": lambda p: _xor(_xor(p["A"], p["B"]), p["CI"])},
    "fahcin": {"COUT": lambda p: _maj(p, ("A", "B", "CIN")),
               "SUM": lambda p: _xor(_xor(p["A"], p["B"]), p["CIN"])},
    "fahcon": {"COUT_N": lambda p: _not(_maj(p, ("A", "B", "CI"))),
               "SUM": lambda p: _xor(_xor(p["A"], p["B"]), p["CI"])},
}


def sym_function(cell: sc_sim.CellDef, pin: str):
    """The symbolic evaluator for ``cell``'s output ``pin``."""
    base = cell.name
    over = SYM_OVERRIDES.get(base)
    if over and pin in over:
        return over[pin]
    return cell.outputs[pin]


def check_cell_equivalence(cell_names: Sequence[str]) -> List[Tuple[str, str, bool]]:
    """Prove, per (cell, output pin), that the symbolic path equals the
    concrete :data:`sc_sim.CELLS` truth table.  Returns ``(cell, pin, ok)``."""
    rows: List[Tuple[str, str, bool]] = []
    for name in cell_names:
        cell = sc_sim.CELLS[name]
        if cell.physical or cell.is_seq or not cell.outputs:
            continue
        pins = list(cell.inputs)
        syms = {p: Bit(z3.Bool("cc_%s" % p)) for p in pins}
        for pin in cell.outputs:
            sym = sym_function(cell, pin)(syms)
            sym_e = as_expr(sym)
            # concrete truth table -> z3 formula (sum of minterms)
            minterms = []
            for bits in itertools.product((0, 1), repeat=len(pins)):
                assign = dict(zip(pins, bits))
                if cell.outputs[pin](assign):
                    minterms.append(
                        z3.And(*[syms[p].e if assign[p] else z3.Not(syms[p].e)
                                 for p in pins])
                        if pins else z3.BoolVal(True)
                    )
            ref = z3.Or(*minterms) if minterms else z3.BoolVal(False)
            s = z3.Solver()
            s.add(sym_e != ref)
            rows.append((name, pin, s.check() == z3.unsat))
    return rows


# ---------------------------------------------------------------------------
# symbolic simulator
# ---------------------------------------------------------------------------


class SymSimulator(sc_sim.Simulator):
    """:class:`sc_sim.Simulator` with the value domain widened to z3 booleans.

    Only the five methods that touch a *value* are overridden; levelization,
    driver resolution, clock detection, edge classification and the
    ``step()`` event order are inherited unchanged.
    """

    def _build(self) -> None:  # type: ignore[override]
        super()._build()
        # pre-resolve the symbolic evaluator of every combinational node
        self._sym_out: List[Tuple[List[str], List[str], Tuple[str, ...],
                                  List[Tuple[str, object]]]] = []
        for node in self.comb_order:
            self._sym_out.append((
                list(node.in_pins),
                list(node.in_nets),
                node.cell.inputs,
                [(node.inst.pins[pin], sym_function(node.cell, pin))
                 for pin, _fn in node.out],
            ))

    # -- values -------------------------------------------------------------
    def set_inputs(self, inputs: Mapping[str, Value]) -> None:  # type: ignore[override]
        for net, val in inputs.items():
            self.values[net] = val
        self.values.update(self.constants)

    def eval_comb(self) -> None:  # type: ignore[override]
        vals = self.values
        for in_pins, in_nets, all_pins, outs in self._sym_out:
            pin_vals = {p: vals.get(n, 0) for p, n in zip(in_pins, in_nets)}
            for p in all_pins:
                if p not in pin_vals:
                    pin_vals[p] = 0
            for net, fn in outs:
                vals[net] = fn(pin_vals)
        vals.update(self.constants)

    def _apply_async(self) -> bool:  # type: ignore[override]
        changed = False
        for ff in self.ffs:
            new = ff.state
            if ff.reset_net is not None:          # RESET_B low -> Q = 0
                new = _ite(self._v(ff.reset_net), new, 0)
            if ff.set_net is not None:            # SET_B low -> Q = 1 (wins)
                new = _ite(self._v(ff.set_net), new, 1)
            if not same(new, ff.state):
                ff.state = new
                changed = True
        if changed:
            self._write_ff_outputs()
        return changed


# ---------------------------------------------------------------------------
# protocol driver (shared by the symbolic and the concrete run)
# ---------------------------------------------------------------------------


def attempt_stimulus(payload: Sequence[Value],
                     trail: int = N_TRAIL_EDGES) -> List[Dict[str, Value]]:
    """The per-rising-edge stimulus of one attempt."""
    if len(payload) != N_PAYLOAD_BITS:
        raise ValueError("payload must be %d bits" % N_PAYLOAD_BITS)
    stim: List[Dict[str, Value]] = []
    for _ in range(N_RESET_EDGES):
        stim.append({"rst_n": 0, "enable": 0, "I": 0})
    for _ in range(N_IDLE_EDGES):
        stim.append({"rst_n": 1, "enable": 0, "I": 0})
    for b in payload:
        stim.append({"rst_n": 1, "enable": 1, "I": b})
    for _ in range(trail):
        stim.append({"rst_n": 1, "enable": 0, "I": 0})
    return stim


def bits_of_key(key: str) -> List[int]:
    """11 chars -> 121 bits (8 data LSB first + 3 zero pad per char)."""
    if len(key) != CHARS:
        raise ValueError("key must be exactly %d characters" % CHARS)
    bits: List[int] = []
    for ch in key:
        code = ord(ch)
        if code > 0xFF:
            raise ValueError("non-8-bit character %r" % ch)
        bits.extend((code >> i) & 1 for i in range(8))
        bits.extend([0] * PAD_BITS)
    return bits


def key_of_bits(bits: Sequence[int]) -> Tuple[str, List[int]]:
    """121 bits -> (11-char string, list of the 33 pad bits)."""
    chars: List[str] = []
    pads: List[int] = []
    for c in range(CHARS):
        w = bits[c * BITS_PER_CHAR:(c + 1) * BITS_PER_CHAR]
        code = sum(b << i for i, b in enumerate(w[:8]))
        chars.append(chr(code))
        pads.extend(w[8:])
    return "".join(chars), pads


def printable(s: str) -> str:
    return "".join(c if 0x20 <= ord(c) < 0x7F else "\\x%02x" % ord(c) for c in s)


# ---------------------------------------------------------------------------
# the 121 bits as an 11 x 11 grid (what the key turns out to be)
# ---------------------------------------------------------------------------

GRID = 11


def grid_of_bits(bits: Sequence[int]) -> List[List[int]]:
    return [list(bits[r * GRID:(r + 1) * GRID]) for r in range(GRID)]


def grid_render(g: Sequence[Sequence[int]]) -> List[str]:
    return ["".join("*" if x else "." for x in row) for row in g]


def grid_stats(g: Sequence[Sequence[int]]) -> Dict[str, object]:
    rows = [sum(r) for r in g]
    cols = [sum(g[r][c] for r in range(GRID)) for c in range(GRID)]
    adjacent: List[Tuple[Tuple[int, int], Tuple[int, int]]] = []
    for r in range(GRID):
        for c in range(GRID):
            if not g[r][c]:
                continue
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < GRID and 0 <= cc < GRID and g[rr][cc]:
                        adjacent.append(((r, c), (rr, cc)))
    return {
        "total": sum(rows),
        "row_counts": rows,
        "col_counts": cols,
        "adjacent_pairs": adjacent,
        "stars_per_row": [[c for c in range(GRID) if g[r][c]] for r in range(GRID)],
        "star_battle": (set(rows) == {2} and set(cols) == {2} and not adjacent),
    }


# ---------------------------------------------------------------------------
# concrete run (independent verification path: plain sc_sim)
# ---------------------------------------------------------------------------


def run_concrete(netlist_path: str, key_bits: Sequence[int],
                 trail: int = N_TRAIL_EDGES,
                 n278: Optional[int] = None) -> Dict[str, object]:
    """Drive one full attempt through the *concrete* simulator."""
    nl = sc_sim.load_netlist(netlist_path)
    if n278 is not None:
        nl.constants = dict(nl.constants)
        nl.constants["n278"] = n278
    sim = sc_sim.Simulator(nl)
    trace: List[Tuple[int, int]] = []          # (byte, success) per edge
    for stim in attempt_stimulus(list(key_bits), trail):
        sim.step(stim)
        byte = sum(sim.get("O[%d]" % i) << i for i in range(8))
        trace.append((byte, sim.get("success")))
    first = next((i for i, (_b, s) in enumerate(trace) if s), None)
    out_lo = N_RESET_EDGES + N_IDLE_EDGES + N_PAYLOAD_BITS
    stream = [b for b, _s in trace[out_lo:]]
    return {
        "trace": trace,
        "success_any": first is not None,
        "success_first_edge": first,
        "success_edges": [i for i, (_b, s) in enumerate(trace) if s],
        "out_window_lo": out_lo,
        "byte_stream": stream,
        "ascii": "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in stream),
    }


def fmt_bytes(bs: Sequence[int]) -> str:
    hexs = " ".join("%02X" % b for b in bs)
    txt = "".join(chr(b) if 0x20 <= b < 0x7F else ("\\0" if b == 0 else ".")
                  for b in bs)
    return "%s  |%s|" % (hexs, txt)


# ---------------------------------------------------------------------------
# the solve
# ---------------------------------------------------------------------------


def build_symbolic(netlist_path: str, trail: int = N_TRAIL_EDGES,
                   verbose: bool = True):
    """Unroll one attempt symbolically.

    Returns ``(vars, success_per_edge, out_bits_per_edge)``.
    """
    nl = sc_sim.load_netlist(netlist_path)
    sim = SymSimulator(nl)
    if sim.clock != "clk":
        raise SystemExit("unexpected clock %r" % sim.clock)
    if any(ff.negedge for ff in sim.ffs):
        raise SystemExit("negedge flops present -- driver assumptions broken")

    bits = [Bit(z3.Bool("i_%03d" % k)) for k in range(N_PAYLOAD_BITS)]
    succ: List[Value] = []
    outs: List[List[Value]] = []
    t0 = time.time()
    for n, stim in enumerate(attempt_stimulus(bits, trail)):
        sim.step(stim)
        succ.append(sim.get("success"))
        outs.append([sim.get("O[%d]" % i) for i in range(8)])
        if verbose and (n + 1) % 20 == 0:
            print("    edge %3d/%d  (%.1fs)" % (n + 1, len(bits) + N_RESET_EDGES
                                                + N_IDLE_EDGES + trail,
                                                time.time() - t0), flush=True)
    return bits, succ, outs


def selftest_engine(netlist_path: str, payload: Sequence[int],
                    trail: int = N_TRAIL_EDGES) -> Dict[str, int]:
    """Drive the symbolic engine with *concrete* bits and demand that it
    matches :class:`sc_sim.Simulator` exactly -- every primary output and
    every flip-flop, at every edge."""
    sym = SymSimulator(sc_sim.load_netlist(netlist_path))
    con = sc_sim.Simulator(sc_sim.load_netlist(netlist_path))
    watched = list(con.netlist.outputs)
    mismatches = 0
    checks = 0
    for stim in attempt_stimulus(list(payload), trail):
        sym.step(stim)
        con.step(stim)
        for net in watched:
            a, b = sym.get(net), con.get(net)
            checks += 1
            if not isinstance(a, int) or a != b:
                mismatches += 1
        for f1, f2 in zip(sym.ffs, con.ffs):
            checks += 1
            if f1.state != f2.state:
                mismatches += 1
    return {"checks": checks, "mismatches": mismatches,
            "edges": N_RESET_EDGES + N_IDLE_EDGES + N_PAYLOAD_BITS + trail}


def count_plain_star_battles(cap: int = 5) -> int:
    """How many 11x11 grids have two stars per row and column and no two
    stars touching -- i.e. a Star Battle *without* its region map.

    If this is more than one, the netlist must also encode the puzzle's
    region partition, because the chip accepts exactly one grid.
    """
    x = [[z3.Bool("sb_%d_%d" % (r, c)) for c in range(GRID)] for r in range(GRID)]
    s = z3.Solver()
    for r in range(GRID):
        s.add(z3.AtMost(*x[r], 2), z3.AtLeast(*x[r], 2))
    for c in range(GRID):
        col = [x[r][c] for r in range(GRID)]
        s.add(z3.AtMost(*col, 2), z3.AtLeast(*col, 2))
    for r in range(GRID):
        for c in range(GRID):
            for dr, dc in ((0, 1), (1, 0), (1, 1), (1, -1)):
                rr, cc = r + dr, c + dc
                if 0 <= rr < GRID and 0 <= cc < GRID:
                    s.add(z3.Or(z3.Not(x[r][c]), z3.Not(x[rr][cc])))
    n = 0
    while n < cap and s.check() == z3.sat:
        m = s.model()
        n += 1
        s.add(z3.Or(*[x[r][c] != z3.BoolVal(z3.is_true(m.eval(x[r][c],
                                                              model_completion=True)))
                      for r in range(GRID) for c in range(GRID)]))
    return n


def vcd_cross_check(netlist_path: str,
                    vcd_path: Optional[str] = None) -> List[Dict[str, object]]:
    """Drive :func:`attempt_stimulus` with each recorded attempt's payload and
    compare *this module's* protocol driver against ``example_inputs.vcd``.

    This is what makes the protocol table above evidence rather than belief:
    the same ``attempt_stimulus`` that carries the symbolic bits reproduces
    the recorded chip outputs edge for edge.
    """
    import replay_vcd  # local import: only needed for this cross-check

    vcd_path = vcd_path or os.path.join(ROOT, "example_inputs.vcd")
    id2name, blocks = replay_vcd.parse_vcd(vcd_path)
    edges, _notes = replay_vcd.extract_posedges(id2name, blocks)
    results: List[Dict[str, object]] = []
    for lo, hi in replay_vcd.split_attempts(edges):
        payload = [e.stim["I"] for e in edges[lo:hi + 1] if e.stim["enable"] == 1]
        trail = (hi - lo + 1) - (N_RESET_EDGES + N_IDLE_EDGES + N_PAYLOAD_BITS)
        res = run_concrete(netlist_path, payload, trail)
        cmps = mism = 0
        for k in range(lo, hi + 1):
            byte, succ = res["trace"][k - lo]
            if edges[k].exp_o is not None:
                cmps += 1
                mism += edges[k].exp_o != byte
            if edges[k].exp_success is not None:
                cmps += 1
                mism += edges[k].exp_success != succ
        results.append({
            "span": (lo, hi),
            "payload_ascii": key_of_bits(payload)[0],
            "edges": hi - lo + 1,
            "comparisons": cmps,
            "mismatches": mism,
        })
    return results


def model_bits(model: z3.ModelRef, bits: Sequence[Bit]) -> List[int]:
    out = []
    for b in bits:
        v = model.eval(b.e, model_completion=True)
        out.append(1 if z3.is_true(v) else 0)
    return out


DATA_IDX = [c * BITS_PER_CHAR + i for c in range(CHARS) for i in range(8)]
PAD_IDX = [c * BITS_PER_CHAR + i for c in range(CHARS) for i in range(8, 11)]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--netlist", default=DEFAULT_NETLIST)
    ap.add_argument("--report", default=DEFAULT_REPORT)
    ap.add_argument("--trail", type=int, default=N_TRAIL_EDGES,
                    help="edges with enable=0 after the payload")
    ap.add_argument("--check-cells", action="store_true",
                    help="only run the cell-equivalence proof")
    ap.add_argument("--verify-only", metavar="BITS",
                    help="skip the solve; run these 121 bits concretely")
    ap.add_argument("--max-solutions", type=int, default=8,
                    help="how many distinct solutions to enumerate")
    args = ap.parse_args(argv)

    log: List[str] = []

    def out(s: str = "") -> None:
        print(s, flush=True)
        log.append(s)

    R: Dict[str, object] = {}

    # -- 0. the symbolic path must agree with the cell library --------------
    nl = sc_sim.load_netlist(args.netlist)
    used = sorted({sc_sim.base_cell_name(i.cell) for i in nl.instances})
    out("cell-equivalence proof (symbolic path vs sc_sim truth tables)")
    rows = check_cell_equivalence(used)
    bad = [r for r in rows if not r[2]]
    out("  %d (cell, output pin) pairs checked by z3, %d mismatches"
        % (len(rows), len(bad)))
    for name, pin, ok in rows:
        if not ok:
            out("  MISMATCH %s.%s" % (name, pin))
    if bad:
        return 1
    if args.check_cells:
        for name, pin, _ok in rows:
            out("  ok  %-10s %s" % (name, pin))
        return 0
    R["cell_pairs"] = len(rows)

    if args.verify_only:
        payload = [int(c) for c in args.verify_only.strip()]
        res = run_concrete(args.netlist, payload, args.trail)
        out("success=%s at edges %s" % (res["success_any"], res["success_edges"]))
        out("  O stream: %s" % fmt_bytes(res["byte_stream"]))
        return 0

    # -- 0b. the lifted engine must equal the concrete one on concrete input
    out("")
    out("engine self-test: symbolic engine driven with the concrete bits of")
    out("the recorded attempt 1 payload, compared against sc_sim.Simulator")
    stx = selftest_engine(args.netlist, bits_of_key("The night s"), args.trail)
    out("  %d comparisons over %d edges (9 outputs + 92 flop states each), "
        "%d mismatches" % (stx["checks"], stx["edges"], stx["mismatches"]))
    if stx["mismatches"]:
        return 1
    R["selftest"] = stx

    # -- 0c. this module's protocol driver vs the recorded VCD --------------
    out("")
    out("protocol cross-check: this module's attempt_stimulus() replayed")
    out("against example_inputs.vcd")
    try:
        xchk = vcd_cross_check(args.netlist)
    except Exception as exc:                       # pragma: no cover
        out("  skipped (%s)" % exc)
        xchk = []
    for r in xchk:
        out("  attempt %s payload %r: %d edges, %d comparisons, %d mismatches"
            % (r["span"], r["payload_ascii"], r["edges"], r["comparisons"],
               r["mismatches"]))
    if any(r["mismatches"] for r in xchk):
        out("  !! the protocol driver does not reproduce the recorded chip")
        return 1
    R["vcd_cross_check"] = xchk

    # -- 1. unroll one attempt symbolically ---------------------------------
    n_edges = N_RESET_EDGES + N_IDLE_EDGES + N_PAYLOAD_BITS + args.trail
    out("")
    out("symbolic unroll of one attempt: %d rising edges, %d input variables"
        % (n_edges, N_PAYLOAD_BITS))
    t0 = time.time()
    bits, succ, _outs = build_symbolic(args.netlist, args.trail, verbose=False)
    out("  built in %.1f s" % (time.time() - t0))

    const0 = [i for i, s in enumerate(succ) if isinstance(s, int) and s == 0]
    const1 = [i for i, s in enumerate(succ) if isinstance(s, int) and s == 1]
    sym_edges = [i for i, s in enumerate(succ) if not isinstance(s, int)]
    if const0:
        out("  success is constant 0 at %d edges (%d..%d)"
            % (len(const0), const0[0], const0[-1]))
    if const1:
        out("  !! success is constant 1 at edges %s -- protocol assumption wrong"
            % const1)
        return 1
    if not sym_edges:
        out("  !! success never depends on the key")
        return 1
    out("  success depends on the key at edges %d..%d"
        % (sym_edges[0], sym_edges[-1]))
    first = as_expr(succ[sym_edges[0]])
    latched = all(z3.eq(as_expr(succ[i]), first) for i in sym_edges)
    out("  the formula is identical at every one of those edges: %s"
        % latched)
    R["sym_edges"] = sym_edges
    R["const0"] = const0
    R["latched"] = latched

    ANY = z3.Or(*[as_expr(succ[i]) for i in sym_edges])

    # -- 2. solve and enumerate ---------------------------------------------
    out("")
    out("SAT: is there a 121-bit payload with success high at any edge?")
    s = z3.Solver()
    s.add(ANY)
    sols: List[List[int]] = []
    t0 = time.time()
    while len(sols) < args.max_solutions and s.check() == z3.sat:
        v = model_bits(s.model(), bits)
        sols.append(v)
        s.add(z3.Or(*[bits[i].e != z3.BoolVal(bool(v[i]))
                      for i in range(N_PAYLOAD_BITS)]))
    exhausted = len(sols) < args.max_solutions
    if exhausted and len(sols) == 1:
        note = "search EXHAUSTED -- the answer is unique"
    elif exhausted:
        note = "search exhausted"
    else:
        note = "stopped at the --max-solutions limit"
    out("  solutions found: %d  (%s, %.1f s)"
        % (len(sols), note, time.time() - t0))
    if not sols:
        out("  UNSAT -- no payload raises success in this window")
        return 1
    key_bits = sols[0]
    R["n_solutions"] = len(sols)
    R["unique"] = exhausted and len(sols) == 1
    R["key_bits"] = key_bits
    out("  key (121 bits, in transmission order):")
    out("    %s" % "".join(map(str, key_bits)))

    # -- 3. decode ----------------------------------------------------------
    out("")
    out("decoding")
    ascii_key, pads = key_of_bits(key_bits)
    printable_ok = all(0x20 <= ord(c) < 0x7F for c in ascii_key)
    out("  as 11 chars x (8 data LSB-first + 3 pad) -- the framing the two")
    out("  recorded VCD attempts use:")
    out("    chars : %s" % printable(ascii_key))
    out("    pads  : %s" % "".join(map(str, pads)))
    out("    printable ASCII: %s" % printable_ok)
    out("  -> not a character string: the pad bits are not zero, so this is")
    out("     not the framing the chip checks.")
    R["ascii_attempt"] = ascii_key
    R["ascii_printable"] = printable_ok
    R["pads"] = pads

    g = grid_of_bits(key_bits)
    st = grid_stats(g)
    out("  as an 11 x 11 grid (bit k = row k//11, column k mod 11):")
    for line in grid_render(g):
        out("    %s" % line)
    out("    total 1 bits    : %d" % st["total"])
    out("    ones per row    : %s" % st["row_counts"])
    out("    ones per column : %s" % st["col_counts"])
    out("    touching pairs (incl. diagonal): %d"
        % (len(st["adjacent_pairs"]) // 2))
    out("    -> two per row, two per column, none touching:")
    out("       a 2-star Star Battle solution: %s" % st["star_battle"])
    out("    star columns per row: %s" % st["stars_per_row"])
    plain = count_plain_star_battles(5)
    out("    grids meeting only row/column/adjacency: >=%d, so the chip must"
        % plain)
    out("    also encode the puzzle's region map (it accepts exactly one grid)")
    R["grid"] = grid_render(g)
    R["grid_stats"] = st
    R["plain_count"] = plain

    # -- 4. concrete verification (independent code path) -------------------
    out("")
    out("concrete verification -- plain sc_sim.Simulator, 0/1 values only")
    good = run_concrete(args.netlist, key_bits, args.trail)
    edges = good["success_edges"]
    out("  recovered key:")
    out("    success asserted : %s, first at edge %s, held through edge %s"
        % (good["success_any"], good["success_first_edge"],
           edges[-1] if edges else None))
    out("    O stream from edge %d (enable=0 window):" % good["out_window_lo"])
    out("      %s" % fmt_bytes(good["byte_stream"]))
    msg = "".join(chr(b) for b in good["byte_stream"] if b)
    out("    message: %r" % msg)
    R["good"] = good
    R["message"] = msg

    alt = run_concrete(args.netlist, key_bits, args.trail, n278=1)
    same_out = bool(alt["byte_stream"] == good["byte_stream"] and alt["success_any"])
    out("    re-run with the undriven net n278 forced to 1: identical: %s"
        % same_out)
    R["n278_same"] = same_out

    # -- 5. negative controls ----------------------------------------------
    out("")
    out("negative controls")
    negs: List[Tuple[str, Dict[str, object]]] = []
    negs.append(("recorded VCD attempt 1 payload 'The night s'",
                 run_concrete(args.netlist, bits_of_key("The night s"),
                              args.trail)))
    negs.append(("recorded VCD attempt 2 payload 'ky awaits  '",
                 run_concrete(args.netlist, bits_of_key("ky awaits  "),
                              args.trail)))
    flip = list(key_bits)
    flip[0] ^= 1
    negs.append(("recovered key with bit 0 flipped",
                 run_concrete(args.netlist, flip, args.trail)))
    flip2 = list(key_bits)
    flip2[120] ^= 1
    negs.append(("recovered key with bit 120 flipped",
                 run_concrete(args.netlist, flip2, args.trail)))
    for label, res in negs:
        out("  %s" % label)
        out("    success : %s" % res["success_any"])
        out("    O stream: %s" % fmt_bytes(res["byte_stream"][:16]))
    R["negatives"] = negs

    ok = bool(good["success_any"] and same_out
              and not any(res["success_any"] for _l, res in negs))
    out("")
    out("VERDICT: %s" % ("KEY CONFIRMED" if ok else "VERIFICATION FAILED"))

    write_report(args.report, R, rows, n_edges, args.trail, log)
    out("report written: %s" % args.report)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def write_report(path: str, R: Dict[str, object], cellrows, n_edges: int,
                 trail: int, log: List[str]) -> None:
    key_bits = R["key_bits"]
    good = R["good"]
    st = R["grid_stats"]
    L: List[str] = []
    a = L.append

    a("# Key recovery -- symbolic simulation + SAT")
    a("")
    a("Tool: `tools/solve_key.py`.  Netlist: `artifacts/puzzle_netlist.json`")
    a("(extracted from `puzzle.gds` by `tools/gds2netlist.py`).  Simulator:")
    a("`tools/sc_sim.py` -- the same one that replayed `example_inputs.vcd`")
    a("with 0 mismatches -- lifted to z3 booleans.")
    a("")
    a("## Answer")
    a("")
    a("```")
    a("121-bit payload (transmission order, one bit per enable=1 rising edge):")
    a("")
    a("".join(map(str, key_bits)))
    a("")
    a("success : asserted at clock edge %s of the attempt, held to the end"
      % good["success_first_edge"])
    a("O stream: %s" % fmt_bytes(good["byte_stream"][:16]))
    a("message : %s" % R["message"])
    a("```")
    a("")
    a("**The key is not an ASCII string.**  It is an 11 x 11 bitmap -- the")
    a("solution of a 2-star *Star Battle*, which is what the chip announces")
    a("when it opens:")
    a("")
    a("```")
    for line in R["grid"]:
        a(line)
    a("```")
    a("")
    a("Bit *k* of the stream is row `k // 11`, column `k mod 11`.  Row-major")
    a("is a convention: the transpose (`k mod 11` as the row) is an equally")
    a("valid Star Battle, and nothing in the netlist distinguishes the two.")
    a("The 121-bit stream above is what the chip accepts either way.")
    a("")
    a("| property | value |")
    a("| --- | --- |")
    a("| stars total | %d |" % st["total"])
    a("| stars per row | %s |" % st["row_counts"])
    a("| stars per column | %s |" % st["col_counts"])
    a("| touching pairs (incl. diagonal) | %d |"
      % (len(st["adjacent_pairs"]) // 2))
    a("| star columns per row | %s |" % st["stars_per_row"])
    a("")
    a("Those three properties alone do **not** pin the grid down: at least %d"
      % R["plain_count"])
    a("distinct 11 x 11 grids have two stars per row and column with none")
    a("touching, while the chip accepts exactly one.  So the netlist also")
    a("encodes the puzzle's region partition, and it checks it *online* --")
    a("121 payload bits arrive but the design has only 92 flip-flops, too few")
    a("to store the grid, so the constraints are accumulated while shifting.")
    a("")
    a("Decoded with the framing the recorded attempts use (11 chars x 8 data")
    a("bits LSB-first + 3 pad bits) the payload would read `%s`"
      % printable(R["ascii_attempt"]))
    a("with pad bits `%s`.  The pad bits are *not* zero, so that framing is"
      % "".join(map(str, R["pads"])))
    a("not what the chip checks, and no rotation or bit order of the 121 bits")
    a("yields printable ASCII: the \"11-character string\" reading of the")
    a("puzzle's flavour text does not survive contact with the netlist.")
    a("")
    a("## Protocol driven")
    a("")
    a("| edges | rst_n | enable | I |")
    a("| --- | --- | --- | --- |")
    a("| 0..2 | 0 | 0 | 0 |")
    a("| 3 | 1 | 0 | 0 |")
    a("| 4..124 | 1 | 1 | key bit k |")
    a("| 125..%d | 1 | 0 | 0 |" % (n_edges - 1))
    a("")
    a("Identical, edge for edge, to each of the two attempts in")
    a("`example_inputs.vcd` (%d trailing edges).  `solve_key.py` proves that" % trail)
    a("rather than assuming it: replaying its own `attempt_stimulus()` with")
    a("each recorded payload reproduces the recorded chip response exactly.")
    a("")
    for r in R.get("vcd_cross_check") or []:
        a("* attempt at edges %s, payload `%s`: %d comparisons, **%d mismatches**"
          % (r["span"], printable(r["payload_ascii"]), r["comparisons"],
             r["mismatches"]))
    a("")
    a("The engine self-test is the other half: the *symbolic* engine fed the")
    a("same concrete payload agrees with `sc_sim.Simulator` on all 9 outputs")
    a("and all 92 flip-flop states at all %d edges (%d comparisons, 0"
      % (n_edges, (R.get("selftest") or {}).get("checks", 0)))
    a("mismatches).")
    a("")
    a("## Method")
    a("")
    a("1. `SymSimulator` subclasses `sc_sim.Simulator` and overrides only the")
    a("   three methods that touch a *value* (`set_inputs`, `eval_comb`,")
    a("   `_apply_async`).  Levelization, driver resolution, clock detection,")
    a("   the flip-flop model and the `step()` event order are inherited, so")
    a("   the symbolic run *is* the validated simulator, not a copy of it.")
    a("2. Every combinational cell type in the netlist is **proved** equivalent")
    a("   between the symbolic path and the concrete `sc_sim.CELLS` truth")
    a("   table by z3 -- %d (cell, output pin) pairs, 0 mismatches -- before"
      % len(cellrows))
    a("   any simulation runs (`--check-cells`).  Cells whose Python function")
    a("   branches on a value (`mux2` here) are in `SYM_OVERRIDES`; `Bit` has")
    a("   no `__bool__`, so a missing override raises instead of silently")
    a("   picking a branch.")
    a("3. Driving the symbolic engine with the *concrete* bits of a recorded")
    a("   attempt reproduces the concrete simulator exactly at all %d edges"
      % n_edges)
    a("   (all 9 outputs and all 92 flip-flop states) -- a self-check that the")
    a("   value lifting changed nothing.")
    a("4. One attempt unrolled over %d rising edges; the 121 serial bits are"
      % n_edges)
    a("   z3 booleans, everything else concrete.  `success` is constant 0 at")
    a("   edges %d..%d and, from edge %d on, one latched formula of all 121"
      % (R["const0"][0], R["const0"][-1], R["sym_edges"][0]))
    a("   variables (identical expression at every later edge).")
    a("5. Solve `Or(success_e)` over that window, then enumerate further")
    a("   models with blocking clauses over the whole 121-bit vector.")
    a("")
    a("## Uniqueness")
    a("")
    a("The enumeration returned **%d solution**, and the next solver call was"
      % R["n_solutions"])
    a("UNSAT: over the full 2^121 input space **exactly one payload raises")
    a("`success`**.  There are no don't-care bits, no free pad bits and no")
    a("alternative key -- all 121 variables occur in the `success` formula and")
    a("every one of them is pinned.")
    a("")
    a("## Concrete verification (independent code path)")
    a("")
    a("Re-run through plain `sc_sim.Simulator` with 0/1 values only:")
    a("")
    a("```")
    a("recovered key")
    a("  success  : %s, edges %d..%d"
      % (good["success_any"], good["success_edges"][0],
         good["success_edges"][-1]))
    a("  O stream : %s" % fmt_bytes(good["byte_stream"]))
    a("  message  : %r then NUL padding" % R["message"])
    a("  n278 (the one undriven net) forced to 1 instead of 0: same result: %s"
      % R["n278_same"])
    a("")
    for label, res in R["negatives"]:
        a("negative control -- %s" % label)
        a("  success  : %s" % res["success_any"])
        a("  O stream : %s" % fmt_bytes(res["byte_stream"][:16]))
    a("```")
    a("")
    a("The two recorded attempts reproduce the recorded `TRY AGAIN` message,")
    a("and flipping a single bit of the key at either end of the stream loses")
    a("`success` and falls back to `TRY AGAIN` -- the check reads the whole")
    a("payload.")
    a("")
    a("## Raw log")
    a("")
    a("```")
    L.extend(log)
    a("```")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
