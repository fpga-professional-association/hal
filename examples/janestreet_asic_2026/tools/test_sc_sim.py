#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Validation suite for :mod:`sc_sim`.

The load-bearing test is :class:`TestWarmupAdderDemo`: it parses the shipped
``warmup/01_netlist.v`` (the gate-level result of synthesising
``warmup/00_source.v``) and simulates it against the behaviour the RTL
promises.  Since the design is an 8-bit serial-in adder compared to 496, a
correct simulation must reproduce ``sum == a + b`` for *every* input pair --
that single check exercises the whole ripple-carry cone, i.e. every
``xor2``/``xnor2``/``a31o``/``a21bo``/``a21boi``/``o21bai``/``nand2``/``and2``/
``or2``/``a21o``/``nor2`` cell in the netlist, plus the ``and3``/``and4bb``
comparator and all 16 ``dfrtp_2`` flip-flops behind ``mux2_1`` feedback muxes.

Shift direction (derived from ``00_source.v``)::

    parallel_out <= {parallel_out[6:0], serial_in};

so ``parallel_out[0]`` holds the most recently shifted bit and, after eight
shifts, the *first* bit presented ends up in ``parallel_out[7]`` (the MSB).
The stimulus therefore drives A and B **MSB first**.

Run::

    python test_sc_sim.py            # unittest, plus an artifacts/ report
    python -m unittest test_sc_sim   # plain unittest
"""

from __future__ import annotations

import itertools
import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sc_sim  # noqa: E402
from sc_sim import (  # noqa: E402
    CELLS,
    CombinationalLoopError,
    Netlist,
    Simulator,
    base_cell_name,
    load_json,
    lookup_cell,
    parse_verilog,
    parse_verilog_file,
)

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.dirname(HERE)
WARMUP = os.path.join(EXAMPLE, "warmup")
ARTIFACTS = os.path.join(EXAMPLE, "artifacts")
PUZZLE_GDS = os.path.join(EXAMPLE, "puzzle.gds")

#: Every ``sky130_fd_sc_hd__*`` cell instantiated in ``puzzle.gds`` (top cell
#: ``puzzle``), captured with gdstk.  :meth:`TestCellLibraryCoverage
#: .test_puzzle_gds_agrees` re-derives this from the GDS when gdstk is
#: importable, so the list cannot silently drift.
PUZZLE_CELL_TYPES = [
    "sky130_fd_sc_hd__a2111oi_2", "sky130_fd_sc_hd__a211o_2",
    "sky130_fd_sc_hd__a211oi_2", "sky130_fd_sc_hd__a21bo_2",
    "sky130_fd_sc_hd__a21boi_2", "sky130_fd_sc_hd__a21o_2",
    "sky130_fd_sc_hd__a21oi_2", "sky130_fd_sc_hd__a221o_2",
    "sky130_fd_sc_hd__a221oi_2", "sky130_fd_sc_hd__a22o_2",
    "sky130_fd_sc_hd__a22oi_2", "sky130_fd_sc_hd__a311o_2",
    "sky130_fd_sc_hd__a31o_2", "sky130_fd_sc_hd__a31oi_2",
    "sky130_fd_sc_hd__a32o_2", "sky130_fd_sc_hd__a41oi_2",
    "sky130_fd_sc_hd__and2_2", "sky130_fd_sc_hd__and2b_2",
    "sky130_fd_sc_hd__and3_2", "sky130_fd_sc_hd__and3b_2",
    "sky130_fd_sc_hd__and4_2", "sky130_fd_sc_hd__and4b_2",
    "sky130_fd_sc_hd__and4bb_2", "sky130_fd_sc_hd__buf_2",
    "sky130_fd_sc_hd__clkbuf_16", "sky130_fd_sc_hd__clkbuf_4",
    "sky130_fd_sc_hd__clkbuf_8", "sky130_fd_sc_hd__conb_1",
    "sky130_fd_sc_hd__decap_3", "sky130_fd_sc_hd__dfrtp_2",
    "sky130_fd_sc_hd__dfstp_2", "sky130_fd_sc_hd__dfxtp_2",
    "sky130_fd_sc_hd__diode_2", "sky130_fd_sc_hd__inv_2",
    "sky130_fd_sc_hd__mux2_1", "sky130_fd_sc_hd__nand2_2",
    "sky130_fd_sc_hd__nand2b_2", "sky130_fd_sc_hd__nand3_2",
    "sky130_fd_sc_hd__nand3b_2", "sky130_fd_sc_hd__nand4_2",
    "sky130_fd_sc_hd__nor2_2", "sky130_fd_sc_hd__nor3_2",
    "sky130_fd_sc_hd__nor3b_2", "sky130_fd_sc_hd__nor4_2",
    "sky130_fd_sc_hd__nor4b_2", "sky130_fd_sc_hd__o211a_2",
    "sky130_fd_sc_hd__o211ai_2", "sky130_fd_sc_hd__o21a_2",
    "sky130_fd_sc_hd__o21ai_2", "sky130_fd_sc_hd__o21ba_2",
    "sky130_fd_sc_hd__o21bai_2", "sky130_fd_sc_hd__o221a_2",
    "sky130_fd_sc_hd__o22a_2", "sky130_fd_sc_hd__o22ai_2",
    "sky130_fd_sc_hd__o2bb2a_2", "sky130_fd_sc_hd__o311a_2",
    "sky130_fd_sc_hd__o31a_2", "sky130_fd_sc_hd__o31ai_2",
    "sky130_fd_sc_hd__o32a_2", "sky130_fd_sc_hd__o32ai_2",
    "sky130_fd_sc_hd__or2_2", "sky130_fd_sc_hd__or3_2",
    "sky130_fd_sc_hd__or3b_2", "sky130_fd_sc_hd__or4_2",
    "sky130_fd_sc_hd__or4b_2", "sky130_fd_sc_hd__or4bb_2",
    "sky130_fd_sc_hd__tapvpwrvgnd_1", "sky130_fd_sc_hd__xnor2_2",
    "sky130_fd_sc_hd__xor2_2",
]

#: Collected by the adder test for the artifacts report.
REPORT: dict = {"cases": []}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _truth_pins(cell_base: str):
    cd = CELLS[cell_base]
    return tuple(cd.inputs)


def _eval(cell_base: str, **pins: int) -> dict:
    cd = CELLS[cell_base]
    missing = set(cd.inputs) - set(pins)
    if missing:
        raise AssertionError("%s: missing pins %s" % (cell_base, sorted(missing)))
    extra = set(pins) - set(cd.inputs)
    if extra:
        raise AssertionError("%s: unknown pins %s" % (cell_base, sorted(extra)))
    return {p: fn(pins) for p, fn in cd.outputs.items()}


def _single(cell_base: str, **pins: int) -> int:
    out = _eval(cell_base, **pins)
    assert len(out) == 1, "%s has %d outputs" % (cell_base, len(out))
    return next(iter(out.values()))


# ---------------------------------------------------------------------------
# 1. cell library
# ---------------------------------------------------------------------------


class TestCellLibraryCoverage(unittest.TestCase):
    def test_puzzle_cells_are_known(self):
        missing = [c for c in PUZZLE_CELL_TYPES if lookup_cell(c) is None]
        self.assertEqual(missing, [], "cells missing from the library")

    def test_warmup_cells_are_known(self):
        nl = parse_verilog_file(os.path.join(WARMUP, "01_netlist.v"))
        self.assertEqual(nl.unknown_cells(), [])

    def test_warmup_power_rail_variant_parses_identically(self):
        a = parse_verilog_file(os.path.join(WARMUP, "01_netlist.v"))
        b = parse_verilog_file(os.path.join(WARMUP, "02_netlist_with_power_rails.v"))
        self.assertEqual(b.unknown_cells(), [])
        # power pins are stripped, so the connectivity must be identical
        self.assertEqual(
            {i.name: (i.cell, i.pins) for i in a.instances},
            {i.name: (i.cell, i.pins) for i in b.instances},
        )
        self.assertEqual(a.inputs, b.inputs)
        self.assertEqual(a.outputs, b.outputs)

    def test_puzzle_gds_agrees(self):
        try:
            import gdstk  # type: ignore
        except ImportError:  # pragma: no cover
            self.skipTest("gdstk not installed")
        if not os.path.exists(PUZZLE_GDS):  # pragma: no cover
            self.skipTest("puzzle.gds not present")
        lib = gdstk.read_gds(PUZZLE_GDS)
        top = {c.name: c for c in lib.cells}["puzzle"]
        found = sorted(
            {r.cell.name for r in top.references if r.cell.name.startswith("sky130_")}
        )
        self.assertEqual(found, sorted(PUZZLE_CELL_TYPES))
        self.assertEqual([c for c in found if lookup_cell(c) is None], [])

    def test_compound_gate_roster_matches_sky130_fd_sc_hd(self):
        """No fictional AOI/OAI cells: the roster is the documented one.

        (Note the asymmetries in the real library: ``a222oi`` exists but
        ``a222o`` does not, and there is no ``a321``/``o321``/``o222``.)
        """
        expected = {
            "a2111o", "a2111oi", "a211o", "a211oi", "a21bo", "a21boi", "a21o",
            "a21oi", "a221o", "a221oi", "a222oi", "a22o", "a22oi", "a2bb2o",
            "a2bb2oi", "a311o", "a311oi", "a31o", "a31oi", "a32o", "a32oi",
            "a41o", "a41oi",
            "o2111a", "o2111ai", "o211a", "o211ai", "o21a", "o21ai", "o21ba",
            "o21bai", "o221a", "o221ai", "o22a", "o22ai", "o2bb2a", "o2bb2ai",
            "o311a", "o311ai", "o31a", "o31ai", "o32a", "o32ai", "o41a", "o41ai",
        }
        have = {
            c for c in CELLS
            if c[0] in "ao" and c[-1] in "aio" and any(ch.isdigit() for ch in c)
        }
        self.assertEqual(have, expected)

    def test_drive_strength_stripping(self):
        self.assertEqual(base_cell_name("sky130_fd_sc_hd__and4bb_2"), "and4bb")
        self.assertEqual(base_cell_name("sky130_fd_sc_hd__a2111oi_2"), "a2111oi")
        self.assertEqual(base_cell_name("sky130_fd_sc_hd__conb_1"), "conb")
        self.assertEqual(base_cell_name("sky130_fd_sc_hd__o2bb2a_2"), "o2bb2a")
        self.assertEqual(base_cell_name("sky130_fd_sc_hd__clkbuf_16"), "clkbuf")
        self.assertEqual(base_cell_name("nand2"), "nand2")


class TestCellFunctions(unittest.TestCase):
    """Independent restatement of the sky130 equations, checked exhaustively."""

    REFERENCE = {
        # simple gates
        "and2":   ("X", lambda A, B: A & B),
        "nand2":  ("Y", lambda A, B: 1 - (A & B)),
        "or2":    ("X", lambda A, B: A | B),
        "nor2":   ("Y", lambda A, B: 1 - (A | B)),
        "xor2":   ("X", lambda A, B: A ^ B),
        "xnor2":  ("Y", lambda A, B: 1 - (A ^ B)),
        "and3":   ("X", lambda A, B, C: A & B & C),
        "nand3":  ("Y", lambda A, B, C: 1 - (A & B & C)),
        "or3":    ("X", lambda A, B, C: A | B | C),
        "nor3":   ("Y", lambda A, B, C: 1 - (A | B | C)),
        "and4":   ("X", lambda A, B, C, D: A & B & C & D),
        "nand4":  ("Y", lambda A, B, C, D: 1 - (A & B & C & D)),
        "or4":    ("X", lambda A, B, C, D: A | B | C | D),
        "nor4":   ("Y", lambda A, B, C, D: 1 - (A | B | C | D)),
        "inv":    ("Y", lambda A: 1 - A),
        "buf":    ("X", lambda A: A),
        "clkbuf": ("X", lambda A: A),
        # inverted-input variants: AND/NAND invert from the front ...
        "and2b":  ("X", lambda A_N, B: (1 - A_N) & B),
        "nand2b": ("Y", lambda A_N, B: 1 - ((1 - A_N) & B)),
        "and3b":  ("X", lambda A_N, B, C: (1 - A_N) & B & C),
        "nand3b": ("Y", lambda A_N, B, C: 1 - ((1 - A_N) & B & C)),
        "and4b":  ("X", lambda A_N, B, C, D: (1 - A_N) & B & C & D),
        "and4bb": ("X", lambda A_N, B_N, C, D: (1 - A_N) & (1 - B_N) & C & D),
        # ... OR/NOR invert from the back
        "or3b":   ("X", lambda A, B, C_N: A | B | (1 - C_N)),
        "nor3b":  ("Y", lambda A, B, C_N: 1 - (A | B | (1 - C_N))),
        "or4b":   ("X", lambda A, B, C, D_N: A | B | C | (1 - D_N)),
        "nor4b":  ("Y", lambda A, B, C, D_N: 1 - (A | B | C | (1 - D_N))),
        "or4bb":  ("X", lambda A, B, C_N, D_N: A | B | (1 - C_N) | (1 - D_N)),
        # AND-into-OR
        "a21o":    ("X", lambda A1, A2, B1: (A1 & A2) | B1),
        "a21oi":   ("Y", lambda A1, A2, B1: 1 - ((A1 & A2) | B1)),
        "a22o":    ("X", lambda A1, A2, B1, B2: (A1 & A2) | (B1 & B2)),
        "a22oi":   ("Y", lambda A1, A2, B1, B2: 1 - ((A1 & A2) | (B1 & B2))),
        "a31o":    ("X", lambda A1, A2, A3, B1: (A1 & A2 & A3) | B1),
        "a31oi":   ("Y", lambda A1, A2, A3, B1: 1 - ((A1 & A2 & A3) | B1)),
        "a32o":    ("X", lambda A1, A2, A3, B1, B2: (A1 & A2 & A3) | (B1 & B2)),
        "a41oi":   ("Y", lambda A1, A2, A3, A4, B1:
                    1 - ((A1 & A2 & A3 & A4) | B1)),
        "a211o":   ("X", lambda A1, A2, B1, C1: (A1 & A2) | B1 | C1),
        "a211oi":  ("Y", lambda A1, A2, B1, C1: 1 - ((A1 & A2) | B1 | C1)),
        "a221o":   ("X", lambda A1, A2, B1, B2, C1: (A1 & A2) | (B1 & B2) | C1),
        "a221oi":  ("Y", lambda A1, A2, B1, B2, C1:
                    1 - ((A1 & A2) | (B1 & B2) | C1)),
        "a311o":   ("X", lambda A1, A2, A3, B1, C1: (A1 & A2 & A3) | B1 | C1),
        "a2111oi": ("Y", lambda A1, A2, B1, C1, D1:
                    1 - ((A1 & A2) | B1 | C1 | D1)),
        "a21bo":   ("X", lambda A1, A2, B1_N: (A1 & A2) | (1 - B1_N)),
        "a21boi":  ("Y", lambda A1, A2, B1_N: (1 - (A1 & A2)) & B1_N),
        "a2bb2o":  ("X", lambda A1_N, A2_N, B1, B2:
                    (1 - (A1_N | A2_N)) | (B1 & B2)),
        # OR-into-AND
        "o21a":    ("X", lambda A1, A2, B1: (A1 | A2) & B1),
        "o21ai":   ("Y", lambda A1, A2, B1: 1 - ((A1 | A2) & B1)),
        "o22a":    ("X", lambda A1, A2, B1, B2: (A1 | A2) & (B1 | B2)),
        "o22ai":   ("Y", lambda A1, A2, B1, B2: 1 - ((A1 | A2) & (B1 | B2))),
        "o31a":    ("X", lambda A1, A2, A3, B1: (A1 | A2 | A3) & B1),
        "o31ai":   ("Y", lambda A1, A2, A3, B1: 1 - ((A1 | A2 | A3) & B1)),
        "o32a":    ("X", lambda A1, A2, A3, B1, B2: (A1 | A2 | A3) & (B1 | B2)),
        "o32ai":   ("Y", lambda A1, A2, A3, B1, B2:
                    1 - ((A1 | A2 | A3) & (B1 | B2))),
        "o211a":   ("X", lambda A1, A2, B1, C1: (A1 | A2) & B1 & C1),
        "o211ai":  ("Y", lambda A1, A2, B1, C1: 1 - ((A1 | A2) & B1 & C1)),
        "o221a":   ("X", lambda A1, A2, B1, B2, C1:
                    (A1 | A2) & (B1 | B2) & C1),
        "o311a":   ("X", lambda A1, A2, A3, B1, C1: (A1 | A2 | A3) & B1 & C1),
        "o21ba":   ("X", lambda A1, A2, B1_N: (A1 | A2) & (1 - B1_N)),
        "o21bai":  ("Y", lambda A1, A2, B1_N: (1 - (A1 | A2)) | B1_N),
        "o2bb2a":  ("X", lambda A1_N, A2_N, B1, B2:
                    (1 - (A1_N & A2_N)) & (B1 | B2)),
        # muxes
        "mux2":    ("X", lambda A0, A1, S: A1 if S else A0),
        "mux2i":   ("Y", lambda A0, A1, S: 1 - (A1 if S else A0)),
    }

    def test_reference_equations(self):
        for base, (out_pin, fn) in self.REFERENCE.items():
            with self.subTest(cell=base):
                self.assertIn(base, CELLS, "cell %r missing" % base)
                pins = _truth_pins(base)
                self.assertEqual(
                    sorted(CELLS[base].outputs), [out_pin],
                    "%s should have exactly output %r" % (base, out_pin),
                )
                self.assertEqual(
                    sorted(pins), sorted(fn.__code__.co_varnames[: fn.__code__.co_argcount]),
                    "%s pin names disagree with the reference" % base,
                )
                for combo in itertools.product((0, 1), repeat=len(pins)):
                    kwargs = dict(zip(pins, combo))
                    self.assertEqual(
                        _single(base, **kwargs), fn(**kwargs),
                        "%s%s" % (base, kwargs),
                    )

    def test_inverting_siblings_are_complements(self):
        """Every ``*oi``/``*ai`` cell must be the exact complement of ``*o``/``*a``."""
        pairs = []
        for base in CELLS:
            if base.endswith("oi") and base[:-1] in CELLS:
                pairs.append((base[:-1], base))
            elif base.endswith("ai") and base[:-1] in CELLS:
                pairs.append((base[:-1], base))
        self.assertGreaterEqual(len(pairs), 20)
        for pos, neg in pairs:
            with self.subTest(cell=neg):
                self.assertEqual(_truth_pins(pos), _truth_pins(neg))
                self.assertEqual(sorted(CELLS[pos].outputs), ["X"])
                self.assertEqual(sorted(CELLS[neg].outputs), ["Y"])
                pins = _truth_pins(pos)
                for combo in itertools.product((0, 1), repeat=len(pins)):
                    kw = dict(zip(pins, combo))
                    self.assertEqual(_single(pos, **kw), 1 - _single(neg, **kw))

    def test_output_pin_polarity_convention(self):
        """Non-inverting cells drive X, inverting cells drive Y."""
        for base, cd in CELLS.items():
            if cd.physical or cd.is_seq or not cd.outputs:
                continue
            if base in ("conb", "ha", "fa", "fah", "fahcin", "fahcon"):
                continue
            with self.subTest(cell=base):
                self.assertEqual(len(cd.outputs), 1)
                pin = next(iter(cd.outputs))
                self.assertIn(pin, ("X", "Y"))

    def test_conb_constants(self):
        self.assertEqual(_eval("conb"), {"HI": 1, "LO": 0})

    def test_physical_cells_have_no_outputs(self):
        for base in ("decap", "tapvpwrvgnd", "diode", "fill"):
            self.assertTrue(CELLS[base].physical)
            self.assertEqual(CELLS[base].output_pins, ())

    def test_flipflop_shapes(self):
        self.assertEqual(CELLS["dfxtp"].seq.reset_b, None)
        self.assertEqual(CELLS["dfrtp"].seq.reset_b, "RESET_B")
        self.assertEqual(CELLS["dfrtp"].seq.set_b, None)
        self.assertEqual(CELLS["dfstp"].seq.set_b, "SET_B")
        self.assertEqual(CELLS["dfstp"].seq.reset_b, None)
        for base in ("dfxtp", "dfrtp", "dfstp"):
            self.assertEqual(CELLS[base].output_pins, ("Q",))


# ---------------------------------------------------------------------------
# 2. front ends and engine mechanics
# ---------------------------------------------------------------------------


def _json_ff_netlist(cell: str, async_pin: str) -> dict:
    """A one-flop design: D <- 'd', async pin <- 'ctl', Q -> 'q'."""
    pins = {"CLK": "clk", "D": "d", "Q": "q"}
    if async_pin:
        pins[async_pin] = "ctl"
    return {
        "module": "ff",
        "inputs": ["clk", "d"] + (["ctl"] if async_pin else []),
        "outputs": ["q"],
        "instances": [{"name": "ff0", "cell": cell, "pins": pins}],
    }


class TestFrontEnds(unittest.TestCase):
    def test_escaped_identifiers_and_named_ports(self):
        src = r"""
        // a comment with a / and a * in it
        module m (a, y);
         input a;
         output y;
         wire \foo/bar[3] ;
         sky130_fd_sc_hd__inv_2 \u/inv0  (.A(a), .Y(\foo/bar[3] ));
         sky130_fd_sc_hd__buf_2 u1 (.A(\foo/bar[3] ), .X(y));
        endmodule
        """
        nl = parse_verilog(src)
        self.assertEqual(nl.module, "m")
        self.assertEqual(nl.inputs, ["a"])
        self.assertEqual(nl.outputs, ["y"])
        self.assertEqual([i.name for i in nl.instances], ["u/inv0", "u1"])
        self.assertEqual(nl.instances[0].pins, {"A": "a", "Y": "foo/bar[3]"})
        sim = Simulator(nl)
        sim.set_inputs({"a": 1})
        sim.settle()
        self.assertEqual(sim.outputs(), {"y": 0})
        sim.set_inputs({"a": 0})
        sim.settle()
        self.assertEqual(sim.outputs(), {"y": 1})

    def test_vector_ports_and_power_pins_and_assign(self):
        src = r"""
        module m (a, o, VPWR, VGND);
         input [1:0] a;
         output [1:0] o;
         inout VPWR, VGND;
         wire t;
         supply1 hi;
         sky130_fd_sc_hd__nand2_2 g0 (.A(a[0]), .B(a[1]), .VPWR(VPWR),
             .VGND(VGND), .VPB(VPWR), .VNB(VGND), .Y(t));
         sky130_fd_sc_hd__and2_2 g1 (.A(t), .B(hi), .X(o[0]));
         assign o[1] = t;
        endmodule
        """
        nl = parse_verilog(src)
        self.assertEqual(nl.inputs, ["a[0]", "a[1]"])
        self.assertEqual(nl.outputs, ["o[0]", "o[1]"])
        self.assertEqual(nl.constants, {"hi": 1})
        self.assertNotIn("VPWR", nl.instances[0].pins)
        sim = Simulator(nl)
        for a0, a1 in itertools.product((0, 1), repeat=2):
            sim.set_inputs({"a[0]": a0, "a[1]": a1})
            sim.settle()
            want = 1 - (a0 & a1)
            self.assertEqual(sim.outputs(), {"o[0]": want, "o[1]": want})

    def test_json_roundtrip_matches_verilog(self):
        nl_v = parse_verilog_file(os.path.join(WARMUP, "01_netlist.v"))
        nl_j = load_json(json.loads(nl_v.to_json()))
        self.assertEqual(nl_v.to_dict(), nl_j.to_dict())
        a = _run_adder(Simulator(nl_v), 250, 246)
        b = _run_adder(Simulator(nl_j), 250, 246)
        self.assertEqual(a, b)

    def test_ports_dict_shape(self):
        """The GDS extractor emits ``ports: {name: {net, dir}}`` instead of lists."""
        nl = load_json({
            "top": "m",
            "ports": {"a": {"net": "a", "dir": "input"},
                      "y": {"net": "y", "dir": "output"}},
            "instances": [
                {"name": "u", "cell": "sky130_fd_sc_hd__inv_2",
                 "connections": {"A": "a", "Y": "y"}},
                {"name": "f", "cell": "sky130_fd_sc_hd__decap_3",
                 "physical_only": True, "connections": {}},
            ],
        })
        self.assertEqual(nl.module, "m")
        self.assertEqual(nl.inputs, ["a"])
        self.assertEqual(nl.outputs, ["y"])
        sim = Simulator(nl)
        sim.set_inputs({"a": 1})
        sim.settle()
        self.assertEqual(sim.outputs(), {"y": 0})

    def test_combinational_loop_is_reported(self):
        nl = load_json({
            "module": "loop", "inputs": ["a"], "outputs": ["y"],
            "instances": [
                {"name": "g0", "cell": "sky130_fd_sc_hd__nand2_2",
                 "pins": {"A": "a", "B": "y", "Y": "t"}},
                {"name": "g1", "cell": "sky130_fd_sc_hd__buf_2",
                 "pins": {"A": "t", "X": "y"}},
            ],
        })
        with self.assertRaises(CombinationalLoopError) as ctx:
            Simulator(nl)
        self.assertEqual(sorted(ctx.exception.instances), ["g0", "g1"])

    def test_unknown_cell_is_reported(self):
        nl = load_json({
            "module": "x", "inputs": ["a"], "outputs": ["y"],
            "instances": [{"name": "u", "cell": "sky130_fd_sc_hd__frobnicate_4",
                           "pins": {"A": "a", "X": "y"}}],
        })
        with self.assertRaises(sc_sim.UnknownCellError):
            Simulator(nl)


class TestFlipFlops(unittest.TestCase):
    def test_dfxtp(self):
        sim = Simulator(load_json(_json_ff_netlist("sky130_fd_sc_hd__dfxtp_2", "")))
        self.assertEqual(sim.get("q"), 0)
        self.assertEqual(sim.step({"d": 1})["q"], 1)
        self.assertEqual(sim.step({"d": 0})["q"], 0)
        self.assertEqual(sim.step({"d": 1})["q"], 1)
        self.assertEqual(sim.step({"d": 1})["q"], 1)

    def test_dfrtp_async_active_low_reset(self):
        sim = Simulator(load_json(
            _json_ff_netlist("sky130_fd_sc_hd__dfrtp_2", "RESET_B")))
        sim.set_inputs({"ctl": 1})
        self.assertEqual(sim.step({"d": 1})["q"], 1)
        # reset asserted (low) forces Q to 0 without waiting for a clock edge
        sim.set_inputs({"ctl": 0})
        sim.settle()
        self.assertEqual(sim.get("q"), 0)
        # and it keeps winning over D across an edge
        self.assertEqual(sim.step({"d": 1})["q"], 0)
        self.assertEqual(sim.step({"d": 1, "ctl": 1})["q"], 1)

    def test_dfstp_async_active_low_set(self):
        sim = Simulator(load_json(
            _json_ff_netlist("sky130_fd_sc_hd__dfstp_2", "SET_B")))
        sim.set_inputs({"ctl": 1})
        self.assertEqual(sim.step({"d": 0})["q"], 0)
        sim.set_inputs({"ctl": 0})
        sim.settle()
        self.assertEqual(sim.get("q"), 1)
        self.assertEqual(sim.step({"d": 0})["q"], 1)
        self.assertEqual(sim.step({"d": 0, "ctl": 1})["q"], 0)

    def test_inverted_clock_path_becomes_negedge(self):
        nl = load_json({
            "module": "m", "inputs": ["clk", "d"], "outputs": ["q"],
            "instances": [
                {"name": "inv", "cell": "sky130_fd_sc_hd__clkinv_2",
                 "pins": {"A": "clk", "Y": "clkb"}},
                {"name": "ff", "cell": "sky130_fd_sc_hd__dfxtp_2",
                 "pins": {"CLK": "clkb", "D": "d", "Q": "q"}},
            ],
        })
        sim = Simulator(nl)
        self.assertEqual(sim.clock, "clk")
        self.assertEqual(sim.info()["negedge_flops"], ["ff"])
        # it still advances once per cycle (mid-cycle falling edge)
        self.assertEqual(sim.step({"d": 1})["q"], 1)


# ---------------------------------------------------------------------------
# 3. the warmup design -- the real proof
# ---------------------------------------------------------------------------

_WARMUP_NETLIST = None


def _warmup_netlist() -> Netlist:
    global _WARMUP_NETLIST
    if _WARMUP_NETLIST is None:
        _WARMUP_NETLIST = parse_verilog_file(os.path.join(WARMUP, "01_netlist.v"))
    return _WARMUP_NETLIST


def _run_adder(sim: Simulator, a: int, b: int, settle_cycles: int = 1) -> dict:
    """Reset, shift A and B in MSB-first for 8 cycles, then report the result.

    Returns ``{"S":..., "sum":..., "a_reg":..., "b_reg":...}``.
    """
    sim.reset(0)
    # one cycle of asynchronous reset (rst_n low)
    sim.step({"clk": 0, "rst_n": 0, "en": 0, "A": 0, "B": 0})
    assert sim.get_bus("a_reg", 8) == 0
    assert sim.get_bus("b_reg", 8) == 0
    for i in range(7, -1, -1):            # MSB first
        sim.step({"rst_n": 1, "en": 1,
                  "A": (a >> i) & 1, "B": (b >> i) & 1})
    for _ in range(settle_cycles):        # hold: en low, registers must not move
        sim.step({"rst_n": 1, "en": 0, "A": 1, "B": 1})
    return {
        "S": sim.get("S"),
        "sum": sim.get_bus("sum", 9),
        "a_reg": sim.get_bus("a_reg", 8),
        "b_reg": sim.get_bus("b_reg", 8),
    }


class TestWarmupAdderDemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.netlist = _warmup_netlist()
        cls.sim = Simulator(cls.netlist)

    def test_structure(self):
        info = self.sim.info()
        self.assertEqual(info["clock"], "clk")
        self.assertEqual(info["flip_flops"], 16)
        self.assertEqual(sorted(info["inputs"]), ["A", "B", "clk", "en", "rst_n"])
        self.assertEqual(info["outputs"], ["S"])
        self.assertEqual(info["undriven_nets"], [])
        self.assertEqual(info["multiply_driven_nets"], [])
        self.assertEqual(info["negedge_flops"], [])
        self.assertEqual(info["unclocked_flops"], [])

    def test_reset_clears_the_shift_registers(self):
        sim = Simulator(self.netlist)
        sim.reset(1)                       # start from an all-ones state
        sim.set_inputs({"rst_n": 1, "en": 0, "A": 0, "B": 0})
        sim.settle()
        self.assertEqual(sim.get_bus("a_reg", 8), 0xFF)
        sim.set_inputs({"rst_n": 0})
        sim.settle()                       # asynchronous: no clock edge needed
        self.assertEqual(sim.get_bus("a_reg", 8), 0)
        self.assertEqual(sim.get_bus("b_reg", 8), 0)

    def test_shift_order_is_msb_first(self):
        """00_source.v: parallel_out <= {parallel_out[6:0], serial_in}."""
        sim = Simulator(self.netlist)
        sim.reset(0)
        sim.step({"rst_n": 0, "en": 0, "A": 0, "B": 0})
        sim.step({"rst_n": 1, "en": 1, "A": 1, "B": 0})   # first bit
        self.assertEqual(sim.get_bus("a_reg", 8), 0b00000001)
        for _ in range(7):
            sim.step({"rst_n": 1, "en": 1, "A": 0, "B": 0})
        # the first bit has walked up to the MSB
        self.assertEqual(sim.get_bus("a_reg", 8), 0b10000000)

    def test_enable_low_holds(self):
        sim = Simulator(self.netlist)
        res = _run_adder(sim, 0b10110011, 0b01001100, settle_cycles=5)
        self.assertEqual(res["a_reg"], 0b10110011)
        self.assertEqual(res["b_reg"], 0b01001100)

    def test_named_cases(self):
        cases = [
            # (a, b, expected S)   -- 250+246 == 496 and 1+2 == 3
            (250, 246, 1),
            (241, 255, 1),
            (255, 241, 1),
            (248, 248, 1),
            (1, 2, 0),
            (0, 0, 0),
            (255, 255, 0),
            (248, 247, 0),      # 495, one below
            (248, 249, 0),      # 497, one above
            (240, 255, 0),      # 495
        ]
        sim = Simulator(self.netlist)
        for a, b, want in cases:
            with self.subTest(a=a, b=b):
                res = _run_adder(sim, a, b)
                self.assertEqual(res["a_reg"], a, "A did not shift in correctly")
                self.assertEqual(res["b_reg"], b, "B did not shift in correctly")
                self.assertEqual(res["sum"], a + b, "gate-level sum wrong")
                self.assertEqual(res["S"], want)
                self.assertEqual(res["S"], 1 if a + b == 496 else 0)
                REPORT["cases"].append(
                    {"a": a, "b": b, "sum": res["sum"], "S": res["S"],
                     "expected_S": want, "group": "named"}
                )

    def test_all_pairs_that_sum_to_496(self):
        sim = Simulator(self.netlist)
        pairs = [(a, 496 - a) for a in range(241, 256)]
        self.assertEqual(len(pairs), 15)
        for a, b in pairs:
            with self.subTest(a=a, b=b):
                res = _run_adder(sim, a, b)
                self.assertEqual(res["sum"], 496)
                self.assertEqual(res["S"], 1)
        REPORT["exhaustive_496_pairs"] = len(pairs)

    def test_random_pairs_reproduce_a_plus_b(self):
        """The strongest check: the 9-bit gate-level sum must equal a+b."""
        rng = random.Random(0xA51C)
        sim = Simulator(self.netlist)
        checked = 0
        for _ in range(80):
            a = rng.randrange(256)
            b = rng.randrange(256)
            res = _run_adder(sim, a, b)
            with self.subTest(a=a, b=b):
                self.assertEqual(res["a_reg"], a)
                self.assertEqual(res["b_reg"], b)
                self.assertEqual(res["sum"], a + b)
                self.assertEqual(res["S"], 1 if a + b == 496 else 0)
            checked += 1
            REPORT["cases"].append(
                {"a": a, "b": b, "sum": res["sum"], "S": res["S"],
                 "expected_S": 1 if a + b == 496 else 0, "group": "random"}
            )
        REPORT["random_pairs"] = checked

    def test_boundary_sums_sweep_carry_chain(self):
        """Walk a single 1 up each operand: exercises every carry position."""
        sim = Simulator(self.netlist)
        for i in range(8):
            for j in range(8):
                a, b = 1 << i, (1 << j) - 1 + (1 << j)
                b &= 0xFF
                res = _run_adder(sim, a, b)
                with self.subTest(a=a, b=b):
                    self.assertEqual(res["sum"], a + b)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


class _ReportingResult(unittest.TextTestResult):
    pass


def _write_report(success: bool) -> str:
    os.makedirs(ARTIFACTS, exist_ok=True)
    path = os.path.join(ARTIFACTS, "sc_sim_warmup_validation.json")
    nl = _warmup_netlist()
    sim = Simulator(nl)
    info = sim.info()
    info.pop("cell_histogram", None)
    payload = {
        "design": "warmup/01_netlist.v (adder_demo)",
        "simulator": "tools/sc_sim.py",
        "passed": success,
        "shift_direction": "MSB first (parallel_out <= {parallel_out[6:0], serial_in})",
        "structure": info,
        "cell_histogram": nl.cell_histogram(),
        "library_cells": sorted(CELLS),
        "puzzle_cell_types": PUZZLE_CELL_TYPES,
        "puzzle_cells_covered": all(lookup_cell(c) is not None for c in PUZZLE_CELL_TYPES),
        "exhaustive_496_pairs": REPORT.get("exhaustive_496_pairs"),
        "random_pairs": REPORT.get("random_pairs"),
        "cases": REPORT["cases"],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    return path


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    path = _write_report(result.wasSuccessful())
    print("\nvalidation report: %s" % path)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
