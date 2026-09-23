#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
gen_sky130_hgl.py -- generate ``SKY130_FD_SC_HD.hgl``, HAL's gate library for
the SkyWater ``sky130_fd_sc_hd`` (high-density) standard cells.

The cell roster and every Boolean function come from :mod:`sc_sim` -- the
dependency-free simulator in this example, whose cell table is validated
against the Jane Street warm-up design's DEF/VCD ground truth
(``tools/test_sc_sim.py``, ``tools/validate_warmup.py``).  Nothing here is
typed by hand twice: the simulator's Python lambdas are *evaluated
symbolically* (see :class:`Sym`) to produce HAL Boolean-function strings, and
every emitted string is then re-checked exhaustively against the very lambda it
came from.  So a divergence between the HAL library and the simulator is a
generator error, not a silent modelling difference.

Usage::

    python3 tools/gen_sky130_hgl.py                # writes into the HAL tree
    python3 tools/gen_sky130_hgl.py -o /tmp/x.hgl  # somewhere else
    python3 tools/gen_sky130_hgl.py --check        # verify only, write nothing

Output target (default): ``plugins/gate_libraries/definitions/SKY130_FD_SC_HD.hgl``.

Modelling notes
---------------
* **HGL format version 4** -- the version this fork's ``hgl_parser`` expects
  (``plugins/hgl_parser/include/hgl_parser/hgl_parser.h``).  Anything older
  makes the parser log "outdated HGL file format".
* **Drive strengths.**  HAL resolves gate types by *exact* name, so
  ``sky130_fd_sc_hd__nand2_2`` and ``..._4`` are two gate types.  The roster is
  therefore deliberately generous (``_1/_2/_4`` for every cell plus the wider
  buffer/inverter/decap ladders): a name sky130 does not actually ship becomes
  an unused gate type, while a *missing* name breaks a netlist parse outright.
  :data:`OBSERVED_CELLS` pins down the names the two example netlists use; the
  generator refuses to write a file that does not cover them.
* **Flip-flops.**  ``dfrtp``/``dfstp``/... are ``ff`` gate types with state
  ``IQ``/``IQN``; sky130's asynchronous pins are active low, hence
  ``clear_on: "(! RESET_B)"`` and ``preset_on: "(! SET_B)"``.  ``dfbbp``/
  ``dfbbn`` assert both at once, and sky130's UDP leaves that case undefined --
  modelled as ``X``/``X``.  ``dfbbn`` is the one negative-edge flop:
  ``clocked_on: "(! CLK_N)"``.  Enable (``edfxtp``) and scan (``sdf*``) flops
  fold their mux into ``next_state``, following the Nangate/ice40 precedent in
  ``plugins/gate_libraries/definitions/``.
* **conb** is a *two*-output constant cell (``HI`` = 1, ``LO`` = 0).  HAL's
  ``mark_vcc_gate_type``/``mark_gnd_gate_type`` only accept single-output types,
  so HAL will auto-generate its ``HAL_VDD``/``HAL_GND`` helpers on load; the
  ``conb`` outputs still carry constant Boolean functions and
  ``power``/``ground`` pin types, which is what constant propagation reads.
* **Physical-only cells** (tap/decap/fill/diode) get no logic: only their
  supply pins (and ``DIODE`` for the antenna diode).  They are instantiated in
  the extracted netlists with an empty port list, e.g.
  ``sky130_fd_sc_hd__decap_3 decap_3_10120_10880 ();``.
* **Supply pins** (``VPWR``/``VGND``/``VPB``/``VNB``, plus ``KAPWR`` on the
  ``lpflow``/``kapwr`` cells) are declared for every cell, as the real library
  has them and as ``NangateOpenCellLibrary.hgl`` does for ``VDD``/``VSS``.  The
  GDS-extracted netlists connect none of them, which is fine -- they are simply
  unconnected gate pins.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import sc_sim  # noqa: E402  (path juggling above is deliberate)
from sc_sim import LIB_PREFIX  # noqa: E402

LIBRARY_NAME = "SKY130_FD_SC_HD"
HGL_FORMAT_VERSION = 4

#: Every cell name (library prefix stripped) used by the two netlists in
#: ``artifacts/``.  Those files are gitignored, so the list is inlined here and
#: asserted against the generated roster -- see :func:`check_roster`.
OBSERVED_CELLS: Tuple[str, ...] = (
    "a2111oi_2", "a211o_2", "a211oi_2", "a21bo_2", "a21boi_2", "a21o_2",
    "a21oi_2", "a221o_2", "a221oi_2", "a22o_2", "a22oi_2", "a311o_2",
    "a31o_2", "a31oi_2", "a32o_2", "a41oi_2", "and2_2", "and2b_2", "and3_2",
    "and3b_2", "and4_2", "and4b_2", "and4bb_2", "buf_2", "clkbuf_16",
    "clkbuf_4", "clkbuf_8", "conb_1", "decap_3", "dfrtp_2", "dfstp_2",
    "dfxtp_2", "diode_2", "inv_2", "mux2_1", "nand2_2", "nand2b_2",
    "nand3_2", "nand3b_2", "nand4_2", "nor2_2", "nor3_2", "nor3b_2",
    "nor4_2", "nor4b_2", "o211a_2", "o211ai_2", "o21a_2", "o21ai_2",
    "o21ba_2", "o21bai_2", "o221a_2", "o22a_2", "o22ai_2", "o2bb2a_2",
    "o311a_2", "o31a_2", "o31ai_2", "o32a_2", "o32ai_2", "or2_2", "or3_2",
    "or3b_2", "or4_2", "or4b_2", "or4bb_2", "tapvpwrvgnd_1", "xnor2_2",
    "xor2_2",
)


# ---------------------------------------------------------------------------
# symbolic evaluation of the sc_sim cell lambdas
# ---------------------------------------------------------------------------


class Sym:
    """A tiny Boolean expression tree that the sc_sim lambdas can be run on.

    The lambdas in :data:`sc_sim.CELLS` are written with ``&``, ``|``, ``^``
    and ``1 - x``; feeding them :class:`Sym` instances instead of ints yields
    the expression rather than a value.  ``and``/``or``/``xor`` chains are
    flattened so ``A & B & C`` renders as one n-ary node.
    """

    __slots__ = ("op", "args")

    def __init__(self, op: str, args: Sequence["Sym | str | int"]):
        self.op = op
        self.args = tuple(args)

    # -- constructors -------------------------------------------------------
    @staticmethod
    def var(name: str) -> "Sym":
        return Sym("var", [name])

    @staticmethod
    def const(value: int) -> "Sym":
        return Sym("const", [1 if value else 0])

    @staticmethod
    def coerce(value: "Sym | int") -> "Sym":
        if isinstance(value, Sym):
            return value
        if value in (0, 1):
            return Sym.const(value)
        raise TypeError("cannot use %r in a Boolean expression" % (value,))

    # -- operators ----------------------------------------------------------
    def _binary(self, op: str, other: "Sym | int", reverse: bool = False) -> "Sym":
        lhs, rhs = (Sym.coerce(other), self) if reverse else (self, Sym.coerce(other))
        args: List[Sym] = []
        for side in (lhs, rhs):
            args.extend(side.args if side.op == op else (side,))  # type: ignore[arg-type]
        return Sym(op, args)

    def __and__(self, other): return self._binary("and", other)
    def __rand__(self, other): return self._binary("and", other, reverse=True)
    def __or__(self, other): return self._binary("or", other)
    def __ror__(self, other): return self._binary("or", other, reverse=True)
    def __xor__(self, other): return self._binary("xor", other)
    def __rxor__(self, other): return self._binary("xor", other, reverse=True)

    def __invert__(self) -> "Sym":
        return Sym("not", [self])

    def __rsub__(self, other: int) -> "Sym":
        # the only subtraction sc_sim performs is the idiom ``1 - x``
        if other != 1:
            raise TypeError("unsupported expression: %r - Sym" % (other,))
        return Sym("not", [self])

    def __bool__(self):  # pragma: no cover - guards against silent truthiness
        raise TypeError(
            "a Sym has no truth value; this cell needs an entry in OVERRIDES"
        )

    # -- consumers ----------------------------------------------------------
    def render(self) -> str:
        """Render in HAL's ``BooleanFunction::from_string`` syntax."""
        if self.op == "var":
            return str(self.args[0])
        if self.op == "const":
            return "0b1" if self.args[0] else "0b0"
        if self.op == "not":
            return "(! %s)" % self.args[0].render()  # type: ignore[union-attr]
        glue = {"and": " & ", "or": " | ", "xor": " ^ "}[self.op]
        return "(" + glue.join(a.render() for a in self.args) + ")"  # type: ignore[union-attr]

    def evaluate(self, values: Mapping[str, int]) -> int:
        if self.op == "var":
            return values[str(self.args[0])]
        if self.op == "const":
            return int(self.args[0])  # type: ignore[arg-type]
        if self.op == "not":
            return 1 - self.args[0].evaluate(values)  # type: ignore[union-attr]
        vals = [a.evaluate(values) for a in self.args]  # type: ignore[union-attr]
        if self.op == "and":
            out = 1
            for v in vals:
                out &= v
        elif self.op == "or":
            out = 0
            for v in vals:
                out |= v
        else:
            out = 0
            for v in vals:
                out ^= v
        return out

    def variables(self) -> List[str]:
        if self.op == "var":
            return [str(self.args[0])]
        if self.op == "const":
            return []
        out: List[str] = []
        for a in self.args:
            for v in a.variables():  # type: ignore[union-attr]
                if v not in out:
                    out.append(v)
        return out


def _lit(name: str) -> Sym:
    return Sym.var(name)


def _nand(*names: str) -> Sym:
    return ~Sym("and", [_lit(n) for n in names])


def _majority(a: str, b: str, c: str) -> Sym:
    return Sym("or", [_lit(a) & _lit(b), _lit(a) & _lit(c), _lit(b) & _lit(c)])


def _mux2(sel: str, lo: str, hi: str) -> Sym:
    return (_lit(lo) & ~_lit(sel)) | (_lit(hi) & _lit(sel))


#: Cells whose sc_sim lambda cannot be run symbolically -- it either branches on
#: a pin value (``x if s else y``) or does integer arithmetic on it.  Each entry
#: is re-verified against that very lambda by :func:`boolean_functions`, so an
#: override that disagrees with the simulator is a hard error.
OVERRIDES: Dict[str, Dict[str, Sym]] = {
    "mux2": {"X": _mux2("S", "A0", "A1")},
    "mux2i": {"Y": ~_mux2("S", "A0", "A1")},
    "mux4": {
        "X": Sym(
            "or",
            [
                Sym(
                    "and",
                    [
                        _lit("A%d" % i),
                        _lit("S0") if i & 1 else ~_lit("S0"),
                        _lit("S1") if i & 2 else ~_lit("S1"),
                    ],
                )
                for i in range(4)
            ],
        )
    },
    "maj3": {"X": _majority("A", "B", "C")},
    "ha": {"COUT": _lit("A") & _lit("B"), "SUM": _lit("A") ^ _lit("B")},
    "fa": {
        "COUT": _majority("A", "B", "CIN"),
        "SUM": Sym("xor", [_lit("A"), _lit("B"), _lit("CIN")]),
    },
    "fah": {
        "COUT": _majority("A", "B", "CI"),
        "SUM": Sym("xor", [_lit("A"), _lit("B"), _lit("CI")]),
    },
    "fahcin": {
        "COUT": _majority("A", "B", "CIN"),
        "SUM": Sym("xor", [_lit("A"), _lit("B"), _lit("CIN")]),
    },
    "fahcon": {
        "COUT_N": ~_majority("A", "B", "CI"),
        "SUM": Sym("xor", [_lit("A"), _lit("B"), _lit("CI")]),
    },
}


def boolean_functions(cell: "sc_sim.CellDef") -> Dict[str, Sym]:
    """Return ``{output pin: expression}`` for a combinational sc_sim cell.

    Every expression -- symbolically derived or taken from :data:`OVERRIDES` --
    is checked against ``cell``'s own Python function over the full input space.
    """
    override = OVERRIDES.get(cell.name)
    result: Dict[str, Sym] = {}
    for pin, fn in cell.outputs.items():
        if override is not None:
            if pin not in override:
                raise AssertionError("cell '%s': no override for pin '%s'" % (cell.name, pin))
            result[pin] = override[pin]
            continue
        env = {p: Sym.var(p) for p in cell.inputs}
        result[pin] = Sym.coerce(fn(env))

    # exhaustive cross-check against the simulator's own lambdas
    pins = list(cell.inputs)
    for combo in itertools.product((0, 1), repeat=len(pins)):
        values = dict(zip(pins, combo))
        for pin, expr in result.items():
            want = int(cell.outputs[pin](values))
            got = expr.evaluate(values)
            if want != got:
                raise AssertionError(
                    "cell '%s' pin '%s': %s evaluates to %d at %r, sc_sim says %d"
                    % (cell.name, pin, expr.render(), got, values, want)
                )
    for pin, expr in result.items():
        stray = [v for v in expr.variables() if v not in pins]
        if stray:
            raise AssertionError(
                "cell '%s' pin '%s': unknown variable(s) %s" % (cell.name, pin, stray)
            )
    return result


# ---------------------------------------------------------------------------
# gate type properties, pin types, drive strengths
# ---------------------------------------------------------------------------

_BUFFERS = frozenset(
    {"buf", "clkbuf", "clkdlybuf4s15", "clkdlybuf4s18", "clkdlybuf4s25",
     "clkdlybuf4s50", "dlygate4sd1", "dlygate4sd2", "dlygate4sd3",
     "dlymetal6s2s", "dlymetal6s4s", "dlymetal6s6s", "lpflow_clkbufkapwr",
     "probe_p"}
)
_INVERTERS = frozenset({"inv", "clkinv", "clkinvlp", "bufinv"})
_HALF_ADDERS = frozenset({"ha"})
_FULL_ADDERS = frozenset({"fa", "fah", "fahcin", "fahcon"})
_MUXES = frozenset({"mux2", "mux2i", "mux4"})

#: ``(regex on the base cell name, extra properties)``, first match wins.
_PROPERTY_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (r"^nand\d", ("c_nand",)),
    (r"^and\d", ("c_and",)),
    (r"^nor\d", ("c_nor",)),
    (r"^or\d", ("c_or",)),
    (r"^xnor\d", ("c_xnor",)),
    (r"^xor\d", ("c_xor",)),
    (r"^a[\d]\S*oi$", ("c_aoi",)),
    (r"^a2bb2oi$", ("c_aoi",)),
    (r"^o[\d]\S*ai$", ("c_oai",)),
    (r"^o2bb2ai$", ("c_oai",)),
)


def gate_properties(cell: "sc_sim.CellDef") -> List[str]:
    """The HAL ``GateTypeProperty`` list for one sc_sim cell."""
    name = cell.name
    if cell.is_seq:
        props = ["sequential", "ff"]
        if name.startswith("sdf"):
            props.append("scan")
        return props
    if cell.physical:
        # no logic at all; ``combinational`` matches how Nangate's FILLCELL_X1
        # and ANTENNA_X1 are declared.
        return ["combinational"]
    if name == "conb":
        return ["combinational", "power", "ground"]
    if name in _BUFFERS:
        return ["combinational", "c_buffer"]
    if name in _INVERTERS:
        return ["combinational", "c_inverter"]
    if name in _MUXES:
        return ["combinational", "c_mux"]
    if name in _HALF_ADDERS:
        return ["combinational", "c_half_adder"]
    if name in _FULL_ADDERS:
        return ["combinational", "c_full_adder"]
    for pattern, extra in _PROPERTY_RULES:
        if re.match(pattern, name):
            return ["combinational", *extra]
    return ["combinational"]


#: Supply pins every sky130 cell carries, plus the ``kapwr`` rail on the
#: low-power-flow cells.  ``(pin, PinType)``.
_SUPPLY_PINS: Tuple[Tuple[str, str], ...] = (
    ("VPWR", "power"),
    ("VGND", "ground"),
    ("VPB", "power"),
    ("VNB", "ground"),
)
_KAPWR_PIN = ("KAPWR", "power")

#: Adder pins that carry a carry bit, whatever the cell spells them.
_CARRY_PINS = frozenset({"CI", "CIN", "COUT", "COUT_N"})
_SELECT_PINS = frozenset({"S", "S0", "S1"})


def comb_pin_type(cell: "sc_sim.CellDef", pin: str) -> str:
    """The HAL ``PinType`` for a pin of a combinational cell."""
    if cell.name == "conb":
        return {"HI": "power", "LO": "ground"}.get(pin, "none")
    if cell.name in _HALF_ADDERS or cell.name in _FULL_ADDERS:
        if pin in _CARRY_PINS:
            return "carry"
        if pin == "SUM":
            return "sum"
    if cell.name in _MUXES and pin in _SELECT_PINS:
        return "select"
    return "none"


DEFAULT_STRENGTHS: Tuple[int, ...] = (1, 2, 4)

#: Cells that exist at exactly one drive strength.
EXACT_STRENGTHS: Dict[str, Tuple[int, ...]] = {
    "conb": (1,),
    "diode": (2,),
    "probe_p": (8,),
    "tap": (1,),
    "tapvgnd": (1,),
    "tapvgnd2": (1,),
    "tapvpwrvgnd": (1,),
}

#: Drive strengths *in addition* to :data:`DEFAULT_STRENGTHS`.
EXTRA_STRENGTHS: Dict[str, Tuple[int, ...]] = {
    "buf": (6, 8, 12, 16),
    "inv": (6, 8, 12, 16),
    "bufinv": (8, 16),
    "clkbuf": (8, 16),
    "clkinv": (8, 16),
    "lpflow_clkbufkapwr": (8, 16),
    "nand2": (8,),
    "nor2": (8,),
    "mux2": (8,),
    "decap": (3, 6, 8, 12),
    "decaphe": (3, 6, 8, 12),
    "decaphetap": (3, 6, 8, 12),
    "decapkapwr": (3, 6, 8, 12),
    "lpflow_decapkapwr": (3, 6, 8, 12),
    "fill": (8,),
    "fillcap": (8,),
    "fill_diode": (8,),
    "tapmet1": (2,),
}


def strengths(base: str) -> Tuple[int, ...]:
    if base in EXACT_STRENGTHS:
        return EXACT_STRENGTHS[base]
    return tuple(sorted(set(DEFAULT_STRENGTHS) | set(EXTRA_STRENGTHS.get(base, ()))))


# ---------------------------------------------------------------------------
# flip-flop configuration
# ---------------------------------------------------------------------------

STATE = "IQ"
NEG_STATE = "IQN"


def ff_config(seq: "sc_sim.SeqDef") -> Dict[str, str]:
    """Translate an sc_sim :class:`~sc_sim.SeqDef` into an HGL ``ff_config``."""
    cfg: Dict[str, str] = {
        "state": STATE,
        "neg_state": NEG_STATE,
        "next_state": seq.d,
        "clocked_on": "(! %s)" % seq.clk if seq.negedge else seq.clk,
    }
    if seq.scan_sel and seq.scan_in:
        cfg["next_state"] = "((%s & %s) | (%s & (! %s)))" % (
            seq.scan_sel, seq.scan_in, seq.d, seq.scan_sel
        )
    if seq.enable:
        cfg["next_state"] = "((%s & %s) | (%s & (! %s)))" % (
            cfg["next_state"], seq.enable, STATE, seq.enable
        )
    if seq.reset_b:
        cfg["clear_on"] = "(! %s)" % seq.reset_b
    if seq.set_b:
        cfg["preset_on"] = "(! %s)" % seq.set_b
    if seq.reset_b and seq.set_b:
        # sky130's dff$NSR primitive leaves simultaneous set+reset undefined
        cfg["state_clear_preset"] = "X"
        cfg["neg_state_clear_preset"] = "X"
    return cfg


def ff_pin_types(seq: "sc_sim.SeqDef") -> Dict[str, str]:
    types = {seq.clk: "clock", seq.d: "data"}
    if seq.reset_b:
        types[seq.reset_b] = "reset"
    if seq.set_b:
        types[seq.set_b] = "set"
    if seq.enable:
        types[seq.enable] = "enable"
    if seq.scan_sel:
        types[seq.scan_sel] = "enable"
    if seq.scan_in:
        types[seq.scan_in] = "data"
    return types


# ---------------------------------------------------------------------------
# HGL assembly
# ---------------------------------------------------------------------------


def _pin_group(name: str, direction: str, pin_type: str, **extra) -> Dict[str, object]:
    pin: Dict[str, object] = {"name": name, "direction": direction, "type": pin_type}
    pin.update(extra)
    return {
        "name": name,
        "direction": direction,
        "type": pin_type,
        "ascending": False,
        "start_index": 0,
        "ordered": False,
        "pins": [pin],
    }


def build_cell(cell: "sc_sim.CellDef", full_name: str) -> Dict[str, object]:
    """Build one HGL cell entry for ``cell`` published under ``full_name``."""
    entry: Dict[str, object] = {"name": full_name, "types": gate_properties(cell)}
    groups: List[Dict[str, object]] = []

    if cell.is_seq:
        seq = cell.seq
        assert seq is not None
        entry["ff_config"] = ff_config(seq)
        pin_types = ff_pin_types(seq)
        for pin in cell.inputs:
            groups.append(_pin_group(pin, "input", pin_types.get(pin, "none")))
        if seq.q:
            groups.append(_pin_group(seq.q, "output", "state", function=STATE))
        if seq.qn:
            groups.append(_pin_group(seq.qn, "output", "neg_state", function=NEG_STATE))
    else:
        functions = boolean_functions(cell) if cell.outputs else {}
        for pin in cell.inputs:
            groups.append(_pin_group(pin, "input", comb_pin_type(cell, pin)))
        for pin, expr in functions.items():
            groups.append(
                _pin_group(
                    pin, "output", comb_pin_type(cell, pin), function=expr.render()
                )
            )

    for supply, supply_type in _SUPPLY_PINS:
        groups.append(_pin_group(supply, "input", supply_type))
    if "kapwr" in cell.name:
        groups.append(_pin_group(_KAPWR_PIN[0], "input", _KAPWR_PIN[1]))

    entry["pin_groups"] = groups
    return entry


def _sort_key(base: str, strength: int) -> Tuple[str, int]:
    return (base, strength)


def build_library() -> Dict[str, object]:
    cells: List[Dict[str, object]] = []
    for base, strength in sorted(
        ((b, s) for b in sc_sim.CELLS for s in strengths(b)), key=lambda kv: _sort_key(*kv)
    ):
        full_name = "%s%s_%d" % (LIB_PREFIX, base, strength)
        cells.append(build_cell(sc_sim.CELLS[base], full_name))

    return {
        "version": HGL_FORMAT_VERSION,
        "library": LIBRARY_NAME,
        # HGL is strict JSON and cannot carry a comment, so provenance lives in
        # a member of its own; the HGL parser ignores members it does not know.
        "comment": (
            "SkyWater sky130_fd_sc_hd standard cells. Generated by "
            "examples/janestreet_asic_2026/tools/gen_sky130_hgl.py from that "
            "example's sc_sim.py cell table -- do not edit by hand, re-run the "
            "generator."
        ),
        "gate_locations": {
            "data_category": "generic",
            "data_x_identifier": "X_COORDINATE",
            "data_y_identifier": "Y_COORDINATE",
        },
        "cells": cells,
    }


def check_roster(library: Mapping[str, object]) -> None:
    """Fail if the library misses a cell the example netlists instantiate."""
    names = {c["name"] for c in library["cells"]}  # type: ignore[index,union-attr]
    missing = sorted(
        LIB_PREFIX + c for c in OBSERVED_CELLS if LIB_PREFIX + c not in names
    )
    if missing:
        raise AssertionError(
            "library does not cover cells used by the example netlists: %s"
            % ", ".join(missing)
        )
    if len(names) != len(library["cells"]):  # type: ignore[arg-type]
        raise AssertionError("duplicate gate type names in the generated library")


def default_output_path() -> str:
    repo_root = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir, os.pardir))
    return os.path.join(
        repo_root, "plugins", "gate_libraries", "definitions", LIBRARY_NAME + ".hgl"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("-o", "--output", help="destination .hgl path")
    ap.add_argument(
        "--check", action="store_true", help="verify and report, but write nothing"
    )
    args = ap.parse_args(argv)

    library = build_library()
    check_roster(library)

    cells = library["cells"]
    assert isinstance(cells, list)
    n_ff = sum(1 for c in cells if "ff" in c["types"])
    n_phys = sum(1 for c in cells if not any(
        p["direction"] == "output" for g in c["pin_groups"] for p in g["pins"]
    ))
    print(
        "%s: %d gate types (%d base cells), %d flip-flops, %d physical-only"
        % (LIBRARY_NAME, len(cells), len(sc_sim.CELLS), n_ff, n_phys)
    )
    print("all %d cell types used by the example netlists are covered" % len(OBSERVED_CELLS))

    if args.check:
        return 0

    path = args.output or default_output_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    body = json.dumps(library, indent=4)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
        fh.write("\n")
    print("wrote %s (%d bytes)" % (path, len(body) + 1))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
