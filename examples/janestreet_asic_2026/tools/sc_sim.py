#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
sc_sim.py -- a small, dependency-free cycle-based gate-level simulator for
SkyWater ``sky130_fd_sc_hd`` standard-cell netlists.

It exists to support the Jane Street "ASIC reverse engineering" puzzle in
``examples/janestreet_asic_2026``: once a netlist has been recovered from
``puzzle.gds`` it has to be *executed* to find the input sequence that raises
``success``.  The same code is validated against the shipped warmup design
(``warmup/01_netlist.v``), whose behaviour is known from ``warmup/00_source.v``.

Three pieces live here:

1. :data:`CELLS` -- the cell function library.  Every combinational cell is a
   pure Python function of its input pins; every flip-flop is described by a
   :class:`SeqDef` record.  See `Cell naming convention`_ below.
2. Two front ends -- :func:`parse_verilog` for structural Verilog (the format
   OpenLane/yosys emits, escaped identifiers and all) and :func:`load_json`
   for the simple JSON interchange format described under `JSON format`_.
3. :class:`Simulator` -- levelizes the combinational logic between flip-flops,
   reports combinational loops, and steps the design one clock cycle at a time.

Command line::

    python sc_sim.py warmup/01_netlist.v --info
    python sc_sim.py warmup/01_netlist.v --json out/netlist.json

Cell naming convention
----------------------
sky130 compound cells are named ``a<widths><o|oi>`` / ``o<widths><a|ai>``:

* leading ``a`` = AND terms feeding an OR;  leading ``o`` = OR terms feeding
  an AND.
* the digits give the width of each first-stage term, most-significant first;
  a ``1`` is a term that is just a single literal.
* the trailing letter(s) give the second stage: ``o``/``a`` = non-inverting,
  ``oi``/``ai`` = inverting output (pin ``Y`` instead of ``X``).
* pins of the first stage are ``A1..An`` for the first term, ``B1..Bn`` for the
  second, ``C1..`` for the third, ``D1..`` for the fourth.

So ``a31o``: ``X = (A1 & A2 & A3) | B1``;  ``o221ai``: ``Y = !((A1|A2) & (B1|B2) & C1)``.

Inverted inputs are marked with ``b`` / ``bb`` in the name and the pin gets an
``_N`` suffix.  **The side that is inverted differs between the AND-ish and the
OR-ish families** -- AND/NAND invert from the front, OR/NOR invert from the back:

======================  ==========================  ===========================
cell                    pins                        function
======================  ==========================  ===========================
``and2b``               ``A_N, B``                  ``X = !A_N & B``
``and4bb``              ``A_N, B_N, C, D``          ``X = !A_N & !B_N & C & D``
``nand3b``              ``A_N, B, C``               ``Y = !(!A_N & B & C)``
``or3b``                ``A, B, C_N``               ``X = A | B | !C_N``
``nor4b``               ``A, B, C, D_N``            ``Y = !(A | B | C | !D_N)``
``or4bb``               ``A, B, C_N, D_N``          ``X = A | B | !C_N | !D_N``
``a21bo``               ``A1, A2, B1_N``            ``X = (A1 & A2) | !B1_N``
``a21boi``              ``A1, A2, B1_N``            ``Y = !(A1 & A2) & B1_N``
``o21ba``               ``A1, A2, B1_N``            ``X = (A1 | A2) & !B1_N``
``o21bai``              ``A1, A2, B1_N``            ``Y = !(A1 | A2) | B1_N``
``o2bb2a``              ``A1_N, A2_N, B1, B2``      ``X = !(A1_N & A2_N) & (B1 | B2)``
``a2bb2o``              ``A1_N, A2_N, B1, B2``      ``X = !(A1_N | A2_N) | (B1 & B2)``
======================  ==========================  ===========================

Drive-strength suffixes (``_1``, ``_2``, ``_4``, ``_16``, ...) and the
``sky130_fd_sc_hd__`` prefix are stripped before lookup, so ``and4bb_2`` and
``and4bb_4`` resolve to the same function.

JSON format
-----------
:func:`load_json` reads a dict (or a file containing one)::

    {
      "module":    "puzzle",                 # optional, default "top"
      "inputs":    ["clk", "rst_n", "I"],    # primary inputs (scalar net names)
      "outputs":   ["success", "O[0]"],      # primary outputs
      "inouts":    [],                       # optional, ignored by the sim
      "constants": {"net_tie_hi": 1},        # optional forced net values
      "instances": [
        {"name": "g1", "cell": "sky130_fd_sc_hd__nand2_2",
         "pins": {"A": "n1", "B": "n2", "Y": "n3"}},
        ...
      ]
    }

Tolerated spellings: ``cell`` / ``type`` / ``cell_type``; ``pins`` /
``connections`` / ``conns``; ``instances`` may also be a ``{name: {...}}`` dict.
Instead of ``inputs``/``outputs`` the port directions may be given as
``"ports": {"I": {"net": "I", "dir": "input"}, ...}`` (the shape the GDS
extractor ``gds2netlist.py`` emits); ``dir`` may be ``input``/``in``,
``output``/``out`` or ``inout``.  Instances carrying ``"physical_only": true``
are accepted as-is -- the cell library already knows fillers/taps/decaps do
nothing.  Power pins (``VPWR``/``VGND``/``VPB``/``VNB``/...) may be present and
are dropped.  Bit-select net names are plain strings -- write ``"a_reg[0]"``,
the simulator never interprets the brackets.

Simulation model
----------------
Two-valued (0/1), zero-delay, cycle based.  One :meth:`Simulator.step`:

1. apply the caller's input values,
2. settle: evaluate all combinational cells in topological order, then apply
   asynchronous set/reset, repeating until the flip-flop outputs stop moving,
3. rising clock edge: every positive-edge flip-flop samples its (settled) data
   input; asynchronous set/reset still wins,
4. settle again,
5. mid-cycle falling edge for any negative-edge flip-flop, settle again,
6. return the primary-output values.

Undriven nets read as 0 and are listed in :attr:`Simulator.undriven`.
A combinational loop is reported by :class:`CombinationalLoopError`, which
carries the offending instances and nets.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

__all__ = [
    "CELLS",
    "CellDef",
    "SeqDef",
    "Instance",
    "Netlist",
    "Simulator",
    "UnknownCellError",
    "CombinationalLoopError",
    "NetlistError",
    "parse_verilog",
    "parse_verilog_file",
    "load_json",
    "load_json_file",
    "load_netlist",
    "base_cell_name",
]

LIB_PREFIX = "sky130_fd_sc_hd__"

#: Pins that carry power/ground/well bias and never take part in simulation.
POWER_PINS = frozenset(
    {"VPWR", "VGND", "VPB", "VNB", "VNW", "VPW", "KAPWR", "LOWHVPWR", "VPWRIN"}
)

#: Net names that are hard constants wherever they appear on a signal pin.
CONST_NETS: Dict[str, int] = {
    "1'b0": 0,
    "1'b1": 1,
    "1'h0": 0,
    "1'h1": 1,
    "1'bx": 0,
    "1'bz": 0,
    "VGND": 0,
    "VSS": 0,
    "VPWR": 1,
    "VDD": 1,
}


class NetlistError(Exception):
    """Raised for malformed netlist input."""


class UnknownCellError(NetlistError):
    """Raised when a netlist uses a cell that is not in :data:`CELLS`."""

    def __init__(self, cells: Iterable[str]):
        self.cells = sorted(set(cells))
        super().__init__(
            "unknown cell type(s): " + ", ".join(self.cells)
        )


class CombinationalLoopError(NetlistError):
    """Raised when the combinational logic between flip-flops is not acyclic."""

    def __init__(self, instances: Sequence[str], nets: Sequence[str]):
        self.instances = list(instances)
        self.nets = list(nets)
        super().__init__(
            "combinational loop through %d instance(s): %s (nets: %s)"
            % (
                len(self.instances),
                ", ".join(self.instances[:12]) + (" ..." if len(self.instances) > 12 else ""),
                ", ".join(self.nets[:12]) + (" ..." if len(self.nets) > 12 else ""),
            )
        )


# ---------------------------------------------------------------------------
# cell library
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CellDef:
    """A standard cell.

    ``inputs``  -- signal input pin names (power pins excluded).
    ``outputs`` -- ordered mapping ``pin -> f(pinvalues) -> 0/1`` for
                   combinational cells.  Empty for physical-only cells.
    ``seq``     -- set for flip-flops; ``outputs`` is then empty and the
                   output pins come from the :class:`SeqDef`.
    ``physical``-- True for fillers/taps/decaps/diodes that do nothing.
    """

    name: str
    inputs: Tuple[str, ...] = ()
    outputs: Dict[str, Callable[[Mapping[str, int]], int]] = field(default_factory=dict)
    seq: Optional["SeqDef"] = None
    physical: bool = False

    @property
    def is_seq(self) -> bool:
        return self.seq is not None

    @property
    def output_pins(self) -> Tuple[str, ...]:
        if self.seq is not None:
            return self.seq.output_pins
        return tuple(self.outputs)


@dataclass(frozen=True)
class SeqDef:
    """Description of an edge-triggered flip-flop.

    ``clk``       clock pin name.
    ``negedge``   True if the cell itself triggers on the falling edge
                  (``CLK_N`` pins); combined with inverters found on the clock
                  path to decide the effective active edge.
    ``d``         data pin.
    ``q`` / ``qn`` output pin names (either may be ``None``).
    ``reset_b``   active-low asynchronous reset pin (forces Q to 0).
    ``set_b``     active-low asynchronous set pin (forces Q to 1).
    ``enable``    active-high synchronous enable (``DE``); when 0 the flop holds.
    ``scan_sel`` / ``scan_in``  scan mux in front of D (``SCE`` selects ``SCD``).
    """

    clk: str = "CLK"
    negedge: bool = False
    d: str = "D"
    q: Optional[str] = "Q"
    qn: Optional[str] = None
    reset_b: Optional[str] = None
    set_b: Optional[str] = None
    enable: Optional[str] = None
    scan_sel: Optional[str] = None
    scan_in: Optional[str] = None

    @property
    def output_pins(self) -> Tuple[str, ...]:
        return tuple(p for p in (self.q, self.qn) if p)


CELLS: Dict[str, CellDef] = {}


def _comb(name: str, inputs: Sequence[str], **outs: Callable[[Mapping[str, int]], int]) -> None:
    CELLS[name] = CellDef(name=name, inputs=tuple(inputs), outputs=dict(outs))


def _seq(name: str, inputs: Sequence[str], seq: SeqDef) -> None:
    CELLS[name] = CellDef(name=name, inputs=tuple(inputs), seq=seq)


def _phys(name: str, inputs: Sequence[str] = ()) -> None:
    CELLS[name] = CellDef(name=name, inputs=tuple(inputs), physical=True)


# -- buffers / inverters ----------------------------------------------------
for _n in ("buf", "clkbuf", "clkdlybuf4s15", "clkdlybuf4s18", "clkdlybuf4s25",
           "clkdlybuf4s50", "dlygate4sd1", "dlygate4sd2", "dlygate4sd3",
           "dlymetal6s2s", "dlymetal6s4s", "dlymetal6s6s", "lpflow_clkbufkapwr",
           "probe_p"):
    _comb(_n, ["A"], X=lambda p: p["A"])
for _n in ("inv", "clkinv", "clkinvlp"):
    _comb(_n, ["A"], Y=lambda p: 1 - p["A"])
_comb("bufinv", ["A"], Y=lambda p: 1 - p["A"])

# -- constants --------------------------------------------------------------
_comb("conb", [], HI=lambda p: 1, LO=lambda p: 0)

# -- physical-only ----------------------------------------------------------
for _n in ("decap", "decaphe", "decaphetap", "decapkapwr", "fill", "fillcap",
           "fill_diode", "tap", "tapvgnd", "tapvgnd2", "tapvpwrvgnd",
           "tapmet1", "lpflow_decapkapwr"):
    _phys(_n)
_phys("diode", ["DIODE"])

# -- basic AND / NAND / OR / NOR -------------------------------------------
_comb("and2", "AB", X=lambda p: p["A"] & p["B"])
_comb("and3", "ABC", X=lambda p: p["A"] & p["B"] & p["C"])
_comb("and4", "ABCD", X=lambda p: p["A"] & p["B"] & p["C"] & p["D"])
_comb("nand2", "AB", Y=lambda p: 1 - (p["A"] & p["B"]))
_comb("nand3", "ABC", Y=lambda p: 1 - (p["A"] & p["B"] & p["C"]))
_comb("nand4", "ABCD", Y=lambda p: 1 - (p["A"] & p["B"] & p["C"] & p["D"]))
_comb("or2", "AB", X=lambda p: p["A"] | p["B"])
_comb("or3", "ABC", X=lambda p: p["A"] | p["B"] | p["C"])
_comb("or4", "ABCD", X=lambda p: p["A"] | p["B"] | p["C"] | p["D"])
_comb("nor2", "AB", Y=lambda p: 1 - (p["A"] | p["B"]))
_comb("nor3", "ABC", Y=lambda p: 1 - (p["A"] | p["B"] | p["C"]))
_comb("nor4", "ABCD", Y=lambda p: 1 - (p["A"] | p["B"] | p["C"] | p["D"]))

# -- AND/NAND with inverted inputs: inverted pins come from the FRONT (A_N, B_N)
_comb("and2b", ["A_N", "B"], X=lambda p: (1 - p["A_N"]) & p["B"])
_comb("and3b", ["A_N", "B", "C"], X=lambda p: (1 - p["A_N"]) & p["B"] & p["C"])
_comb("and4b", ["A_N", "B", "C", "D"],
      X=lambda p: (1 - p["A_N"]) & p["B"] & p["C"] & p["D"])
_comb("and4bb", ["A_N", "B_N", "C", "D"],
      X=lambda p: (1 - p["A_N"]) & (1 - p["B_N"]) & p["C"] & p["D"])
_comb("nand2b", ["A_N", "B"], Y=lambda p: 1 - ((1 - p["A_N"]) & p["B"]))
_comb("nand3b", ["A_N", "B", "C"],
      Y=lambda p: 1 - ((1 - p["A_N"]) & p["B"] & p["C"]))
_comb("nand4b", ["A_N", "B", "C", "D"],
      Y=lambda p: 1 - ((1 - p["A_N"]) & p["B"] & p["C"] & p["D"]))
_comb("nand4bb", ["A_N", "B_N", "C", "D"],
      Y=lambda p: 1 - ((1 - p["A_N"]) & (1 - p["B_N"]) & p["C"] & p["D"]))

# -- OR/NOR with inverted inputs: inverted pins come from the BACK (C_N, D_N)
_comb("or2b", ["A", "B_N"], X=lambda p: p["A"] | (1 - p["B_N"]))
_comb("or3b", ["A", "B", "C_N"], X=lambda p: p["A"] | p["B"] | (1 - p["C_N"]))
_comb("or4b", ["A", "B", "C", "D_N"],
      X=lambda p: p["A"] | p["B"] | p["C"] | (1 - p["D_N"]))
_comb("or4bb", ["A", "B", "C_N", "D_N"],
      X=lambda p: p["A"] | p["B"] | (1 - p["C_N"]) | (1 - p["D_N"]))
_comb("nor2b", ["A", "B_N"], Y=lambda p: 1 - (p["A"] | (1 - p["B_N"])))
_comb("nor3b", ["A", "B", "C_N"],
      Y=lambda p: 1 - (p["A"] | p["B"] | (1 - p["C_N"])))
_comb("nor4b", ["A", "B", "C", "D_N"],
      Y=lambda p: 1 - (p["A"] | p["B"] | p["C"] | (1 - p["D_N"])))
_comb("nor4bb", ["A", "B", "C_N", "D_N"],
      Y=lambda p: 1 - (p["A"] | p["B"] | (1 - p["C_N"]) | (1 - p["D_N"])))

# -- XOR / XNOR / majority / adders ----------------------------------------
_comb("xor2", "AB", X=lambda p: p["A"] ^ p["B"])
_comb("xor3", "ABC", X=lambda p: p["A"] ^ p["B"] ^ p["C"])
_comb("xnor2", "AB", Y=lambda p: 1 - (p["A"] ^ p["B"]))
_comb("xnor3", "ABC", Y=lambda p: 1 - (p["A"] ^ p["B"] ^ p["C"]))
_comb("maj3", "ABC",
      X=lambda p: 1 if (p["A"] + p["B"] + p["C"]) >= 2 else 0)
_comb("ha", ["A", "B"], COUT=lambda p: p["A"] & p["B"], SUM=lambda p: p["A"] ^ p["B"])
_comb("fa", ["A", "B", "CIN"],
      COUT=lambda p: 1 if (p["A"] + p["B"] + p["CIN"]) >= 2 else 0,
      SUM=lambda p: p["A"] ^ p["B"] ^ p["CIN"])
_comb("fah", ["A", "B", "CI"],
      COUT=lambda p: 1 if (p["A"] + p["B"] + p["CI"]) >= 2 else 0,
      SUM=lambda p: p["A"] ^ p["B"] ^ p["CI"])
_comb("fahcin", ["A", "B", "CIN"],
      COUT=lambda p: 1 if (p["A"] + p["B"] + p["CIN"]) >= 2 else 0,
      SUM=lambda p: p["A"] ^ p["B"] ^ p["CIN"])
_comb("fahcon", ["A", "B", "CI"],
      COUT_N=lambda p: 0 if (p["A"] + p["B"] + p["CI"]) >= 2 else 1,
      SUM=lambda p: p["A"] ^ p["B"] ^ p["CI"])

# -- muxes ------------------------------------------------------------------
_comb("mux2", ["A0", "A1", "S"], X=lambda p: p["A1"] if p["S"] else p["A0"])
_comb("mux2i", ["A0", "A1", "S"], Y=lambda p: 1 - (p["A1"] if p["S"] else p["A0"]))
_comb("mux4", ["A0", "A1", "A2", "A3", "S0", "S1"],
      X=lambda p: p["A%d" % (p["S0"] + 2 * p["S1"])])

# -- AND-into-OR family (aNM...o / ...oi) ----------------------------------
#    X = OR of the AND terms; the "i" variant is the complement on pin Y.
_A_FAMILY: Dict[str, Tuple[Tuple[str, ...], Callable[[Mapping[str, int]], int]]] = {
    "a21":  (("A1", "A2", "B1"),
             lambda p: (p["A1"] & p["A2"]) | p["B1"]),
    "a22":  (("A1", "A2", "B1", "B2"),
             lambda p: (p["A1"] & p["A2"]) | (p["B1"] & p["B2"])),
    "a31":  (("A1", "A2", "A3", "B1"),
             lambda p: (p["A1"] & p["A2"] & p["A3"]) | p["B1"]),
    "a32":  (("A1", "A2", "A3", "B1", "B2"),
             lambda p: (p["A1"] & p["A2"] & p["A3"]) | (p["B1"] & p["B2"])),
    "a41":  (("A1", "A2", "A3", "A4", "B1"),
             lambda p: (p["A1"] & p["A2"] & p["A3"] & p["A4"]) | p["B1"]),
    "a211": (("A1", "A2", "B1", "C1"),
             lambda p: (p["A1"] & p["A2"]) | p["B1"] | p["C1"]),
    "a221": (("A1", "A2", "B1", "B2", "C1"),
             lambda p: (p["A1"] & p["A2"]) | (p["B1"] & p["B2"]) | p["C1"]),
    "a222": (("A1", "A2", "B1", "B2", "C1", "C2"),
             lambda p: (p["A1"] & p["A2"]) | (p["B1"] & p["B2"]) | (p["C1"] & p["C2"])),
    "a311": (("A1", "A2", "A3", "B1", "C1"),
             lambda p: (p["A1"] & p["A2"] & p["A3"]) | p["B1"] | p["C1"]),
    "a2111": (("A1", "A2", "B1", "C1", "D1"),
              lambda p: (p["A1"] & p["A2"]) | p["B1"] | p["C1"] | p["D1"]),
    "a2bb2": (("A1_N", "A2_N", "B1", "B2"),
              lambda p: ((1 - p["A1_N"]) & (1 - p["A2_N"])) | (p["B1"] & p["B2"])),
    "a21b": (("A1", "A2", "B1_N"),
             lambda p: (p["A1"] & p["A2"]) | (1 - p["B1_N"])),
}
#: stems for which the library ships only the inverting member (``a222oi``
#: exists, ``a222o`` does not).
_A_ONLY_INVERTING = {"a222"}
for _stem, (_pins, _fn) in _A_FAMILY.items():
    if _stem not in _A_ONLY_INVERTING:
        _comb(_stem + "o", _pins, X=_fn)
    _comb(_stem + "oi", _pins, Y=(lambda f: (lambda p: 1 - f(p)))(_fn))

# -- OR-into-AND family (oNM...a / ...ai) ----------------------------------
_O_FAMILY: Dict[str, Tuple[Tuple[str, ...], Callable[[Mapping[str, int]], int]]] = {
    "o21":  (("A1", "A2", "B1"),
             lambda p: (p["A1"] | p["A2"]) & p["B1"]),
    "o22":  (("A1", "A2", "B1", "B2"),
             lambda p: (p["A1"] | p["A2"]) & (p["B1"] | p["B2"])),
    "o31":  (("A1", "A2", "A3", "B1"),
             lambda p: (p["A1"] | p["A2"] | p["A3"]) & p["B1"]),
    "o32":  (("A1", "A2", "A3", "B1", "B2"),
             lambda p: (p["A1"] | p["A2"] | p["A3"]) & (p["B1"] | p["B2"])),
    "o41":  (("A1", "A2", "A3", "A4", "B1"),
             lambda p: (p["A1"] | p["A2"] | p["A3"] | p["A4"]) & p["B1"]),
    "o211": (("A1", "A2", "B1", "C1"),
             lambda p: (p["A1"] | p["A2"]) & p["B1"] & p["C1"]),
    "o221": (("A1", "A2", "B1", "B2", "C1"),
             lambda p: (p["A1"] | p["A2"]) & (p["B1"] | p["B2"]) & p["C1"]),
    "o311": (("A1", "A2", "A3", "B1", "C1"),
             lambda p: (p["A1"] | p["A2"] | p["A3"]) & p["B1"] & p["C1"]),
    "o2111": (("A1", "A2", "B1", "C1", "D1"),
              lambda p: (p["A1"] | p["A2"]) & p["B1"] & p["C1"] & p["D1"]),
    "o2bb2": (("A1_N", "A2_N", "B1", "B2"),
              lambda p: ((1 - p["A1_N"]) | (1 - p["A2_N"])) & (p["B1"] | p["B2"])),
    "o21b": (("A1", "A2", "B1_N"),
             lambda p: (p["A1"] | p["A2"]) & (1 - p["B1_N"])),
}
for _stem, (_pins, _fn) in _O_FAMILY.items():
    _comb(_stem + "a", _pins, X=_fn)
    _comb(_stem + "ai", _pins, Y=(lambda f: (lambda p: 1 - f(p)))(_fn))

# -- flip-flops -------------------------------------------------------------
_seq("dfxtp", ["CLK", "D"], SeqDef(clk="CLK", d="D", q="Q"))
_seq("dfxbp", ["CLK", "D"], SeqDef(clk="CLK", d="D", q="Q", qn="Q_N"))
_seq("dfrtp", ["CLK", "D", "RESET_B"],
     SeqDef(clk="CLK", d="D", q="Q", reset_b="RESET_B"))
_seq("dfrbp", ["CLK", "D", "RESET_B"],
     SeqDef(clk="CLK", d="D", q="Q", qn="Q_N", reset_b="RESET_B"))
_seq("dfstp", ["CLK", "D", "SET_B"],
     SeqDef(clk="CLK", d="D", q="Q", set_b="SET_B"))
_seq("dfsbp", ["CLK", "D", "SET_B"],
     SeqDef(clk="CLK", d="D", q="Q", qn="Q_N", set_b="SET_B"))
_seq("dfbbp", ["CLK", "D", "SET_B", "RESET_B"],
     SeqDef(clk="CLK", d="D", q="Q", qn="Q_N", set_b="SET_B", reset_b="RESET_B"))
_seq("dfbbn", ["CLK_N", "D", "SET_B", "RESET_B"],
     SeqDef(clk="CLK_N", negedge=True, d="D", q="Q", qn="Q_N",
            set_b="SET_B", reset_b="RESET_B"))
_seq("edfxtp", ["CLK", "D", "DE"], SeqDef(clk="CLK", d="D", q="Q", enable="DE"))
_seq("sdfxtp", ["CLK", "D", "SCD", "SCE"],
     SeqDef(clk="CLK", d="D", q="Q", scan_sel="SCE", scan_in="SCD"))
_seq("sdfxbp", ["CLK", "D", "SCD", "SCE"],
     SeqDef(clk="CLK", d="D", q="Q", qn="Q_N", scan_sel="SCE", scan_in="SCD"))
_seq("sdfrtp", ["CLK", "D", "SCD", "SCE", "RESET_B"],
     SeqDef(clk="CLK", d="D", q="Q", reset_b="RESET_B",
            scan_sel="SCE", scan_in="SCD"))
_seq("sdfstp", ["CLK", "D", "SCD", "SCE", "SET_B"],
     SeqDef(clk="CLK", d="D", q="Q", set_b="SET_B",
            scan_sel="SCE", scan_in="SCD"))


def base_cell_name(cell: str) -> str:
    """Strip the library prefix and the drive-strength suffix.

    ``sky130_fd_sc_hd__and4bb_2`` -> ``and4bb``;  ``conb_1`` -> ``conb``.
    Names that are already bare are returned unchanged.
    """
    name = cell
    if name.startswith(LIB_PREFIX):
        name = name[len(LIB_PREFIX) :]
    elif "__" in name:  # some other sky130 flavour (hs/ms/lp/hvl)
        name = name.split("__", 1)[1]
    name = re.sub(r"_\d+$", "", name)
    return name


def lookup_cell(cell: str) -> Optional[CellDef]:
    """Return the :class:`CellDef` for a (possibly decorated) cell name."""
    return CELLS.get(base_cell_name(cell))


# ---------------------------------------------------------------------------
# netlist data model
# ---------------------------------------------------------------------------


@dataclass
class Instance:
    name: str
    cell: str
    pins: Dict[str, str]

    def __post_init__(self) -> None:
        self.pins = {k: v for k, v in self.pins.items() if k not in POWER_PINS}


@dataclass
class Netlist:
    module: str = "top"
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    inouts: List[str] = field(default_factory=list)
    instances: List[Instance] = field(default_factory=list)
    constants: Dict[str, int] = field(default_factory=dict)

    # -- helpers ------------------------------------------------------------
    def nets(self) -> Set[str]:
        nets: Set[str] = set(self.inputs) | set(self.outputs) | set(self.inouts)
        for inst in self.instances:
            nets.update(inst.pins.values())
        return nets

    def cell_histogram(self) -> Dict[str, int]:
        hist: Dict[str, int] = {}
        for inst in self.instances:
            hist[inst.cell] = hist.get(inst.cell, 0) + 1
        return dict(sorted(hist.items()))

    def unknown_cells(self) -> List[str]:
        return sorted({i.cell for i in self.instances if lookup_cell(i.cell) is None})

    def to_dict(self) -> Dict[str, object]:
        return {
            "module": self.module,
            "inputs": list(self.inputs),
            "outputs": list(self.outputs),
            "inouts": list(self.inouts),
            "constants": dict(self.constants),
            "instances": [
                {"name": i.name, "cell": i.cell, "pins": dict(i.pins)}
                for i in self.instances
            ],
        }

    def to_json(self, indent: int = 1) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ---------------------------------------------------------------------------
# JSON front end
# ---------------------------------------------------------------------------


def load_json(data: Mapping[str, object]) -> Netlist:
    """Build a :class:`Netlist` from the JSON interchange dict (see module docstring)."""
    raw_insts = data.get("instances", [])
    items: List[Tuple[Optional[str], Mapping[str, object]]]
    if isinstance(raw_insts, dict):
        items = [(k, v) for k, v in raw_insts.items()]  # type: ignore[misc]
    else:
        items = [(None, v) for v in raw_insts]  # type: ignore[misc]

    instances: List[Instance] = []
    for idx, (key, obj) in enumerate(items):
        if not isinstance(obj, dict):
            raise NetlistError("instance %r is not an object" % (key or idx))
        name = str(obj.get("name", key if key is not None else "u%d" % idx))
        cell = obj.get("cell", obj.get("type", obj.get("cell_type")))
        if cell is None:
            raise NetlistError("instance %r has no 'cell'" % name)
        pins = obj.get("pins", obj.get("connections", obj.get("conns", {})))
        if not isinstance(pins, dict):
            raise NetlistError("instance %r has non-dict pins" % name)
        instances.append(
            Instance(name=name, cell=str(cell), pins={str(k): str(v) for k, v in pins.items()})
        )

    inputs = [str(x) for x in data.get("inputs", [])]      # type: ignore[union-attr]
    outputs = [str(x) for x in data.get("outputs", [])]    # type: ignore[union-attr]
    inouts = [str(x) for x in data.get("inouts", [])]      # type: ignore[union-attr]

    ports = data.get("ports")
    if isinstance(ports, dict):
        bucket = {"input": inputs, "in": inputs, "output": outputs,
                  "out": outputs, "inout": inouts}
        for pname, pinfo in ports.items():
            if isinstance(pinfo, dict):
                direction = str(pinfo.get("dir", pinfo.get("direction", ""))).lower()
                net = str(pinfo.get("net", pname))
            else:
                direction, net = str(pinfo).lower(), str(pname)
            target = bucket.get(direction)
            if target is None:
                raise NetlistError("port %r has unknown direction %r" % (pname, direction))
            if net not in target:
                target.append(net)

    return Netlist(
        module=str(data.get("module", data.get("top", data.get("name", "top")))),
        inputs=inputs,
        outputs=outputs,
        inouts=inouts,
        instances=instances,
        constants={str(k): int(v) for k, v in dict(data.get("constants", {})).items()},
    )


def load_json_file(path: str) -> Netlist:
    with open(path, "r", encoding="utf-8") as fh:
        return load_json(json.load(fh))


# ---------------------------------------------------------------------------
# structural Verilog front end
# ---------------------------------------------------------------------------

_ESCAPED_RE = re.compile(r"\\\S+")
_TOKEN_RE = re.compile(
    r"""
      (?P<esc>\\\S+)                                  # escaped identifier
    | (?P<based>[0-9]*'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_]+)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_$]*)
    | (?P<int>[0-9]+)
    | (?P<punct>[(),;=\[\]{}.:@#])
    | (?P<other>\S)
    """,
    re.VERBOSE,
)

_DECL_KEYWORDS = {"input", "output", "inout", "wire", "reg", "supply0", "supply1",
                  "tri", "wand", "wor", "logic"}
_SKIP_KEYWORDS = {"parameter", "localparam", "specify", "endspecify", "timescale",
                  "default_nettype", "celldefine", "endcelldefine"}


def _strip_comments(text: str) -> str:
    """Remove // and /* */ comments without breaking escaped identifiers.

    An escaped identifier (``\\add0/_00_ ``) may legitimately contain ``/``,
    so the scanner consumes escaped identifiers atomically.
    """
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            m = _ESCAPED_RE.match(text, i)
            if m:
                out.append(m.group(0))
                i = m.end()
                continue
        if c == "/" and i + 1 < n:
            if text[i + 1] == "/":
                j = text.find("\n", i)
                i = n if j < 0 else j
                continue
            if text[i + 1] == "*":
                j = text.find("*/", i + 2)
                i = n if j < 0 else j + 2
                out.append(" ")
                continue
        if c == "`":  # compiler directive: drop the rest of the line
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        out.append(c)
        i += 1
    return "".join(out)


@dataclass
class _Tok:
    kind: str
    text: str


def _tokenize(text: str) -> List[_Tok]:
    toks: List[_Tok] = []
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup or "other"
        val = m.group()
        if kind == "esc":
            # strip the leading backslash; the trailing whitespace is already
            # excluded by the \S+ match
            toks.append(_Tok("esc", val[1:]))
        else:
            toks.append(_Tok(kind, val))
    return toks


class _VParser:
    def __init__(self, toks: List[_Tok]):
        self.t = toks
        self.i = 0

    # -- token helpers ------------------------------------------------------
    def peek(self, k: int = 0) -> Optional[_Tok]:
        j = self.i + k
        return self.t[j] if j < len(self.t) else None

    def next(self) -> _Tok:
        if self.i >= len(self.t):
            raise NetlistError("unexpected end of file")
        tok = self.t[self.i]
        self.i += 1
        return tok

    def expect(self, text: str) -> _Tok:
        tok = self.next()
        if tok.text != text:
            raise NetlistError("expected %r, got %r" % (text, tok.text))
        return tok

    def at(self, text: str) -> bool:
        tok = self.peek()
        return tok is not None and tok.text == text

    def skip_to(self, text: str) -> None:
        while self.i < len(self.t) and self.t[self.i].text != text:
            self.i += 1
        if self.i < len(self.t):
            self.i += 1

    # -- grammar ------------------------------------------------------------
    def _range(self) -> Optional[Tuple[int, int]]:
        """Parse an optional ``[msb:lsb]`` or ``[idx]`` range."""
        if not self.at("["):
            return None
        self.expect("[")
        hi = int(self.next().text)
        if self.at(":"):
            self.expect(":")
            lo = int(self.next().text)
        else:
            lo = hi
        self.expect("]")
        return (hi, lo)

    def _net_ref(self) -> str:
        """Parse one net reference; returns the canonical net name."""
        tok = self.next()
        if tok.kind == "esc":
            return tok.text  # already carries its own [i] if any
        if tok.kind == "based" or tok.kind == "int":
            return tok.text.lower()
        if tok.text == "{":
            raise NetlistError("concatenations in port connections are not supported")
        name = tok.text
        if self.at("["):
            self.expect("[")
            idx = self.next().text
            if self.at(":"):
                raise NetlistError("part selects in port connections are not supported")
            self.expect("]")
            name = "%s[%s]" % (name, idx)
        return name

    def parse(self) -> Netlist:
        # find 'module'
        while self.i < len(self.t) and self.t[self.i].text != "module":
            self.i += 1
        if self.i >= len(self.t):
            raise NetlistError("no module found")
        self.expect("module")
        nl = Netlist(module=self.next().text)
        if self.at("("):  # port list -- directions come from the declarations
            depth = 0
            while self.i < len(self.t):
                tok = self.next()
                if tok.text == "(":
                    depth += 1
                elif tok.text == ")":
                    depth -= 1
                    if depth == 0:
                        break
        self.expect(";")

        declared: Dict[str, str] = {}
        while self.i < len(self.t):
            tok = self.peek()
            assert tok is not None
            if tok.text == "endmodule":
                self.i += 1
                break
            if tok.text in _SKIP_KEYWORDS:
                self.skip_to(";")
                continue
            if tok.text in _DECL_KEYWORDS and tok.kind == "ident":
                self._decl(nl, declared)
                continue
            if tok.text == "assign" and tok.kind == "ident":
                self._assign(nl)
                continue
            if tok.text == ";":
                self.i += 1
                continue
            self._instance(nl)

        # nets declared as 'output' but never listed again are still outputs
        nl.outputs = [n for n in nl.outputs]
        return nl

    def _decl(self, nl: Netlist, declared: Dict[str, str]) -> None:
        kind = self.next().text
        # optional net type modifiers: 'output wire [7:0] x;'
        while self.peek() is not None and self.peek().text in ("wire", "reg", "logic", "signed"):  # type: ignore[union-attr]
            self.i += 1
        rng = self._range()
        names: List[str] = []
        while True:
            tok = self.next()
            if tok.text == ";":
                break
            if tok.text == ",":
                continue
            if tok.kind == "esc":
                names.append(tok.text)
            elif tok.kind == "ident":
                nm = tok.text
                if self.at("["):  # rare: 'wire x[3];'
                    self.expect("[")
                    idx = self.next().text
                    self.expect("]")
                    nm = "%s[%s]" % (nm, idx)
                names.append(nm)
            elif tok.text == "=":
                # 'wire x = y;' -- treat as an alias
                rhs = self._net_ref()
                if names:
                    nl.constants.pop(names[-1], None)
                    _add_alias(nl, names[-1], rhs)
            else:
                raise NetlistError("unexpected token %r in declaration" % tok.text)

        expanded: List[str] = []
        for nm in names:
            if rng is not None and "[" not in nm:
                hi, lo = rng
                step = 1 if hi >= lo else -1
                expanded.extend("%s[%d]" % (nm, i) for i in range(lo, hi + step, step))
            else:
                expanded.append(nm)

        for nm in expanded:
            if kind == "input":
                if nm not in nl.inputs:
                    nl.inputs.append(nm)
            elif kind == "output":
                if nm not in nl.outputs:
                    nl.outputs.append(nm)
            elif kind == "inout":
                if nm not in nl.inouts:
                    nl.inouts.append(nm)
            elif kind == "supply0":
                nl.constants[nm] = 0
            elif kind == "supply1":
                nl.constants[nm] = 1
            declared[nm] = kind

    def _assign(self, nl: Netlist) -> None:
        self.expect("assign")
        lhs = self._net_ref()
        self.expect("=")
        rhs = self._net_ref()
        self.expect(";")
        _add_alias(nl, lhs, rhs)

    def _instance(self, nl: Netlist) -> None:
        cell_tok = self.next()
        cell = cell_tok.text
        name_tok = self.next()
        inst_name = name_tok.text
        if name_tok.text == "(":
            raise NetlistError("instance of %r has no instance name" % cell)
        # optional parameter override '#(...)' -- skip
        if self.at("#"):
            self.expect("#")
            self.skip_to(")")
        self.expect("(")
        pins: Dict[str, str] = {}
        positional: List[str] = []
        while not self.at(")"):
            if self.at(","):
                self.i += 1
                continue
            if self.at("."):
                self.expect(".")
                pin_tok = self.next()
                pin = pin_tok.text
                self.expect("(")
                if self.at(")"):
                    self.expect(")")
                    continue  # unconnected: .PIN()
                net = self._net_ref()
                self.expect(")")
                pins[pin] = net
            else:
                positional.append(self._net_ref())
        self.expect(")")
        self.expect(";")

        if positional:
            cd = lookup_cell(cell)
            if cd is None:
                raise NetlistError(
                    "instance %r of unknown cell %r uses positional connections"
                    % (inst_name, cell)
                )
            order = tuple(cd.output_pins) + tuple(cd.inputs)
            if len(positional) != len(order):
                raise NetlistError(
                    "instance %r: %d positional connections for %d pins"
                    % (inst_name, len(positional), len(order))
                )
            pins.update(dict(zip(order, positional)))

        nl.instances.append(Instance(name=inst_name, cell=cell, pins=pins))


def _add_alias(nl: Netlist, lhs: str, rhs: str) -> None:
    """Model ``assign lhs = rhs`` as a buffer instance (or a constant)."""
    if rhs in CONST_NETS or re.fullmatch(r"[01]", rhs):
        nl.constants[lhs] = CONST_NETS.get(rhs, int(rhs))
        return
    nl.instances.append(
        Instance(
            name="__assign_%d" % len(nl.instances),
            cell=LIB_PREFIX + "buf_1",
            pins={"A": rhs, "X": lhs},
        )
    )


def parse_verilog(text: str) -> Netlist:
    """Parse a structural Verilog netlist (single module) into a :class:`Netlist`.

    Supported: escaped identifiers (``\\a_reg[0] ``), named and positional port
    connections, scalar and vector ``input``/``output``/``inout``/``wire``
    declarations, ``supply0``/``supply1``, and simple ``assign a = b;`` aliases.
    Not supported (and reported as errors): concatenations or part-selects in
    port connections, behavioural statements, multiple modules per file
    (only the first module is parsed).
    """
    return _VParser(_tokenize(_strip_comments(text))).parse()


def parse_verilog_file(path: str) -> Netlist:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return parse_verilog(fh.read())


def load_netlist(path: str) -> Netlist:
    """Dispatch on file extension: ``.json`` -> JSON, anything else -> Verilog."""
    if path.lower().endswith(".json"):
        return load_json_file(path)
    return parse_verilog_file(path)


# ---------------------------------------------------------------------------
# simulator
# ---------------------------------------------------------------------------


@dataclass
class _CombNode:
    inst: Instance
    cell: CellDef
    in_pins: Tuple[str, ...]
    in_nets: Tuple[str, ...]
    out: Tuple[Tuple[str, Callable[[Mapping[str, int]], int]], ...]


@dataclass
class _FFNode:
    inst: Instance
    cell: CellDef
    seq: SeqDef
    d_net: Optional[str]
    clk_net: Optional[str]
    reset_net: Optional[str]
    set_net: Optional[str]
    enable_net: Optional[str]
    scan_sel_net: Optional[str]
    scan_in_net: Optional[str]
    q_net: Optional[str]
    qn_net: Optional[str]
    negedge: bool = False
    clock_root: Optional[str] = None
    state: int = 0


class Simulator:
    """Cycle-based simulator over a :class:`Netlist`.

    ``clock``  -- name of the primary clock input.  Auto-detected by tracing
                  every flip-flop ``CLK`` pin back through buffers/inverters.
    ``reset_value`` -- power-up value of every flip-flop (default 0).
    """

    MAX_SETTLE_ITERATIONS = 64

    def __init__(
        self,
        netlist: Netlist,
        clock: Optional[str] = None,
        reset_value: int = 0,
        strict: bool = True,
    ):
        self.netlist = netlist
        self.strict = strict
        self.values: Dict[str, int] = {}
        self.undriven: List[str] = []
        self.multiply_driven: List[str] = []
        self.constants: Dict[str, int] = dict(netlist.constants)

        unknown = netlist.unknown_cells()
        if unknown:
            raise UnknownCellError(unknown)

        self._build()
        self.clock = clock if clock is not None else self._detect_clock()
        self._classify_edges()
        self.reset(reset_value)

    # -- construction -------------------------------------------------------
    def _build(self) -> None:
        self.comb_nodes: List[_CombNode] = []
        self.ffs: List[_FFNode] = []
        self.driver: Dict[str, Tuple[str, str]] = {}  # net -> (inst name, pin)
        self._driver_node: Dict[str, object] = {}     # net -> _CombNode | _FFNode

        primary_in = set(self.netlist.inputs) | set(self.netlist.inouts)

        for inst in self.netlist.instances:
            cell = lookup_cell(inst.cell)
            assert cell is not None
            if cell.physical:
                continue
            if cell.is_seq:
                seq = cell.seq
                assert seq is not None
                node = _FFNode(
                    inst=inst,
                    cell=cell,
                    seq=seq,
                    d_net=inst.pins.get(seq.d),
                    clk_net=inst.pins.get(seq.clk),
                    reset_net=inst.pins.get(seq.reset_b) if seq.reset_b else None,
                    set_net=inst.pins.get(seq.set_b) if seq.set_b else None,
                    enable_net=inst.pins.get(seq.enable) if seq.enable else None,
                    scan_sel_net=inst.pins.get(seq.scan_sel) if seq.scan_sel else None,
                    scan_in_net=inst.pins.get(seq.scan_in) if seq.scan_in else None,
                    q_net=inst.pins.get(seq.q) if seq.q else None,
                    qn_net=inst.pins.get(seq.qn) if seq.qn else None,
                    negedge=seq.negedge,
                )
                self.ffs.append(node)
                for pin, net in ((seq.q, node.q_net), (seq.qn, node.qn_net)):
                    if net:
                        self._register_driver(net, inst.name, pin or "?", node)
                continue

            if not cell.outputs:
                continue
            in_pins = tuple(p for p in cell.inputs if p in inst.pins)
            node = _CombNode(
                inst=inst,
                cell=cell,
                in_pins=in_pins,
                in_nets=tuple(inst.pins[p] for p in in_pins),
                out=tuple((p, f) for p, f in cell.outputs.items() if p in inst.pins),
            )
            missing = [p for p in cell.inputs if p not in inst.pins]
            if missing and self.strict:
                # unconnected inputs read as 0; record but do not fail
                self.undriven.append("%s.%s" % (inst.name, ",".join(missing)))
            if not node.out:
                continue
            self.comb_nodes.append(node)
            for pin, _fn in node.out:
                self._register_driver(inst.pins[pin], inst.name, pin, node)

        # nets that are read but never driven
        known = set(self.driver) | primary_in | set(self.constants) | set(CONST_NETS)
        seen: Set[str] = set()
        for node in self.comb_nodes:
            for net in node.in_nets:
                if net not in known and net not in seen:
                    seen.add(net)
                    self.undriven.append(net)
        for ff in self.ffs:
            for net in (ff.d_net, ff.clk_net, ff.reset_net, ff.set_net,
                        ff.enable_net, ff.scan_sel_net, ff.scan_in_net):
                if net and net not in known and net not in seen:
                    seen.add(net)
                    self.undriven.append(net)

        self._levelize()

    def _register_driver(self, net: str, inst: str, pin: str, node: object) -> None:
        if net in self.driver:
            self.multiply_driven.append(
                "%s (by %s.%s and %s.%s)"
                % (net, self.driver[net][0], self.driver[net][1], inst, pin)
            )
            return
        self.driver[net] = (inst, pin)
        self._driver_node[net] = node

    def _levelize(self) -> None:
        """Topologically order the combinational cells; raise on a loop."""
        ready: Set[str] = set(self.netlist.inputs) | set(self.netlist.inouts)
        ready |= set(self.constants) | set(CONST_NETS)
        for ff in self.ffs:
            for net in (ff.q_net, ff.qn_net):
                if net:
                    ready.add(net)
        # nets with no driver at all are constants-0 for levelization purposes
        all_driven = set(self.driver)
        for node in self.comb_nodes:
            for net in node.in_nets:
                if net not in all_driven:
                    ready.add(net)

        pending = list(self.comb_nodes)
        order: List[_CombNode] = []
        remaining = pending
        while remaining:
            progressed = False
            still: List[_CombNode] = []
            for node in remaining:
                if all(n in ready for n in node.in_nets):
                    order.append(node)
                    for pin, _fn in node.out:
                        ready.add(node.inst.pins[pin])
                    progressed = True
                else:
                    still.append(node)
            remaining = still
            if not progressed:
                nets = sorted(
                    {n for node in remaining for n in node.in_nets if n not in ready}
                )
                raise CombinationalLoopError(
                    [n.inst.name for n in remaining], nets
                )
        self.comb_order = order
        self.levels = self._compute_levels(order)

    @staticmethod
    def _compute_levels(order: Sequence[_CombNode]) -> Dict[str, int]:
        net_level: Dict[str, int] = {}
        levels: Dict[str, int] = {}
        for node in order:
            lvl = 1 + max((net_level.get(n, 0) for n in node.in_nets), default=0)
            levels[node.inst.name] = lvl
            for pin, _fn in node.out:
                net_level[node.inst.pins[pin]] = lvl
        return levels

    # -- clock handling -----------------------------------------------------
    def _trace_clock(self, net: Optional[str]) -> Tuple[Optional[str], bool]:
        """Walk a clock net back to a primary input through buffers/inverters.

        Returns ``(root_net, inverted)``.  ``root_net`` is None if the walk hits
        something that is not a simple buffer/inverter.
        """
        inverted = False
        seen: Set[str] = set()
        primary = set(self.netlist.inputs) | set(self.netlist.inouts)
        while net is not None and net not in primary:
            if net in seen:
                return (None, inverted)
            seen.add(net)
            node = self._driver_node.get(net)
            if not isinstance(node, _CombNode):
                return (None, inverted)
            base = base_cell_name(node.inst.cell)
            cd = node.cell
            if len(cd.inputs) != 1 or len(cd.outputs) != 1:
                return (None, inverted)
            if "inv" in base:
                inverted = not inverted
            net = node.inst.pins.get(cd.inputs[0])
        return (net, inverted)

    def _detect_clock(self) -> Optional[str]:
        roots: Dict[str, int] = {}
        for ff in self.ffs:
            root, _inv = self._trace_clock(ff.clk_net)
            if root:
                roots[root] = roots.get(root, 0) + 1
        if not roots:
            return None
        return max(roots.items(), key=lambda kv: kv[1])[0]

    def _classify_edges(self) -> None:
        self.unclocked_ffs: List[str] = []
        for ff in self.ffs:
            root, inverted = self._trace_clock(ff.clk_net)
            ff.clock_root = root
            if root is None:
                self.unclocked_ffs.append(ff.inst.name)
            elif self.clock is not None and root != self.clock:
                self.unclocked_ffs.append(ff.inst.name)
            ff.negedge = ff.seq.negedge != inverted

    # -- state --------------------------------------------------------------
    def reset(self, value: int = 0) -> None:
        """Set every flip-flop to ``value`` and clear all net values."""
        self.values = {}
        for net, val in CONST_NETS.items():
            self.values[net] = val
        self.values.update(self.constants)
        for ff in self.ffs:
            ff.state = value
        self._write_ff_outputs()
        self.eval_comb()

    def _write_ff_outputs(self) -> None:
        for ff in self.ffs:
            if ff.q_net:
                self.values[ff.q_net] = ff.state
            if ff.qn_net:
                self.values[ff.qn_net] = 1 - ff.state

    def _v(self, net: Optional[str]) -> int:
        if net is None:
            return 0
        return self.values.get(net, 0)

    # -- evaluation ---------------------------------------------------------
    def eval_comb(self) -> None:
        """Evaluate every combinational cell once, in topological order."""
        vals = self.values
        for node in self.comb_order:
            pin_vals = {p: vals.get(n, 0) for p, n in zip(node.in_pins, node.in_nets)}
            for p in node.cell.inputs:
                if p not in pin_vals:
                    pin_vals[p] = 0
            pins = node.inst.pins
            for pin, fn in node.out:
                vals[pins[pin]] = fn(pin_vals)
        # forced constants always win
        vals.update(self.constants)

    def _apply_async(self) -> bool:
        """Apply async set/reset; return True if any flip-flop changed."""
        changed = False
        for ff in self.ffs:
            new = ff.state
            if ff.reset_net is not None and self._v(ff.reset_net) == 0:
                new = 0
            if ff.set_net is not None and self._v(ff.set_net) == 0:
                new = 1
            if new != ff.state:
                ff.state = new
                changed = True
        if changed:
            self._write_ff_outputs()
        return changed

    def settle(self) -> None:
        """Evaluate combinational logic and async set/reset to a fixed point."""
        for _ in range(self.MAX_SETTLE_ITERATIONS):
            self.eval_comb()
            if not self._apply_async():
                return
        raise NetlistError(
            "design did not settle in %d iterations (asynchronous loop?)"
            % self.MAX_SETTLE_ITERATIONS
        )

    def _next_d(self, ff: _FFNode) -> int:
        d = self._v(ff.d_net)
        if ff.scan_sel_net is not None and self._v(ff.scan_sel_net) == 1:
            d = self._v(ff.scan_in_net)
        if ff.enable_net is not None and self._v(ff.enable_net) == 0:
            d = ff.state
        return d

    def _clock_edge(self, negedge: bool) -> None:
        nxt = [(ff, self._next_d(ff)) for ff in self.ffs if ff.negedge == negedge]
        for ff, d in nxt:
            ff.state = d
        self._write_ff_outputs()
        self.settle()

    # -- public API ---------------------------------------------------------
    def set_inputs(self, inputs: Mapping[str, int]) -> None:
        for net, val in inputs.items():
            self.values[net] = 1 if val else 0
        self.values.update(self.constants)

    def step(self, inputs: Optional[Mapping[str, int]] = None) -> Dict[str, int]:
        """Run one clock cycle and return the primary-output values.

        Order of events: apply ``inputs`` -> settle -> rising edge -> settle ->
        (mid-cycle) falling edge -> settle.
        """
        if inputs:
            self.set_inputs(inputs)
        self.settle()
        self._clock_edge(negedge=False)
        if any(ff.negedge for ff in self.ffs):
            self._clock_edge(negedge=True)
        return self.outputs()

    def run(self, stimulus: Iterable[Mapping[str, int]]) -> List[Dict[str, int]]:
        """Step once per stimulus dict; return the per-cycle output dicts."""
        return [self.step(inp) for inp in stimulus]

    def outputs(self) -> Dict[str, int]:
        return {net: self._v(net) for net in self.netlist.outputs}

    def get(self, net: str) -> int:
        """Current value of any net."""
        return self._v(net)

    def get_bus(self, base: str, width: int, msb_first: bool = False) -> int:
        """Assemble ``base[0..width-1]`` into an integer (bit i at position i)."""
        val = 0
        for i in range(width):
            if self._v("%s[%d]" % (base, i)):
                val |= 1 << i
        if msb_first:
            rev = 0
            for i in range(width):
                if val & (1 << i):
                    rev |= 1 << (width - 1 - i)
            return rev
        return val

    def state(self) -> Dict[str, int]:
        """Current flip-flop contents, keyed by instance name."""
        return {ff.inst.name: ff.state for ff in self.ffs}

    # -- reporting ----------------------------------------------------------
    def info(self) -> Dict[str, object]:
        return {
            "module": self.netlist.module,
            "inputs": list(self.netlist.inputs),
            "outputs": list(self.netlist.outputs),
            "instances": len(self.netlist.instances),
            "combinational_cells": len(self.comb_nodes),
            "flip_flops": len(self.ffs),
            "physical_cells": sum(
                1 for i in self.netlist.instances
                if (lookup_cell(i.cell) or CellDef("?")).physical
            ),
            "logic_depth": max(self.levels.values(), default=0),
            "clock": self.clock,
            "negedge_flops": [ff.inst.name for ff in self.ffs if ff.negedge],
            "unclocked_flops": list(self.unclocked_ffs),
            "undriven_nets": list(self.undriven),
            "multiply_driven_nets": list(self.multiply_driven),
            "cell_histogram": self.netlist.cell_histogram(),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("netlist", help="structural Verilog (.v) or JSON netlist")
    ap.add_argument("--info", action="store_true", help="print a design summary")
    ap.add_argument("--json", metavar="PATH", help="write the parsed netlist as JSON")
    ap.add_argument("--clock", help="name of the primary clock input")
    args = ap.parse_args(argv)

    nl = load_netlist(args.netlist)

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(nl.to_json())
        print("wrote %s" % args.json)

    unknown = nl.unknown_cells()
    if unknown:
        print("UNKNOWN CELLS: %s" % ", ".join(unknown), file=sys.stderr)
        return 2

    sim = Simulator(nl, clock=args.clock)
    if args.info or not args.json:
        info = sim.info()
        hist = info.pop("cell_histogram")
        for key, val in info.items():
            print("%-22s %s" % (key, val))
        print("cell histogram:")
        for cell, cnt in sorted(hist.items(), key=lambda kv: (-kv[1], kv[0])):  # type: ignore[union-attr]
            print("  %5d  %s" % (cnt, cell))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
