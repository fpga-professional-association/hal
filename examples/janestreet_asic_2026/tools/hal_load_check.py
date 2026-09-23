#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
hal_load_check.py -- prove that HAL's ``SKY130_FD_SC_HD`` gate library really
loads the netlists this example extracts from ``puzzle.gds``.

It loads ``plugins/gate_libraries/definitions/SKY130_FD_SC_HD.hgl`` (produced by
``tools/gen_sky130_hgl.py``) and parses both extracted designs through
``hal_py``, then checks the resulting :class:`hal_py.Netlist` against the JSON
sidecar the extractor wrote next to each Verilog file:

* gate count, per-cell-type census, and net count,
* top-level port names and directions,
* every instantiated cell type resolved to a real gate type (no HAL fallback),
* the flip-flops come back as ``ff`` gate types with the right asynchronous
  reset/set functions and clock pin,
* the ``conb`` constant cells drive nets HAL sees as constant 0/1.

HAL builds on Linux only, so run this in the ``halbuild`` bench container::

    docker exec -e HAL_BASE_PATH=/work/build -e HAL_PY_PATH=/work/build/lib \\
        -e PYTHONPATH=/work/build/lib -w /work halbuild bash -c \\
        'python3 examples/janestreet_asic_2026/tools/hal_load_check.py'

Exit code is 0 only if every check passed.  ``--output PATH`` additionally
writes the report (the checked-in copy lives at
``artifacts/hal_load_check.txt``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXAMPLE = os.path.dirname(_HERE)
_REPO = os.path.dirname(os.path.dirname(_EXAMPLE))

DESIGNS: Tuple[Tuple[str, str], ...] = (
    ("warmup", "warmup_netlist"),
    ("puzzle", "puzzle_netlist"),
)

#: sky130's edge-triggered flop families: ``df*``, ``edf*`` (enable), ``sdf*``
#: (scan).  Used to derive the expected flip-flop count from the sidecar without
#: asking the gate library (which is what we are testing).
FF_CELL_RE = re.compile(r"^sky130_fd_sc_hd__[es]?df[a-z0-9]*_\d+$")

#: Flip-flop cells and what their HGL ``ff_config`` must come back as.
FF_EXPECTATIONS: Dict[str, Dict[str, Optional[str]]] = {
    "sky130_fd_sc_hd__dfxtp_2": {"clock": "CLK", "reset": None, "set": None},
    "sky130_fd_sc_hd__dfrtp_2": {"clock": "CLK", "reset": "(! RESET_B)", "set": None},
    "sky130_fd_sc_hd__dfstp_2": {"clock": "CLK", "reset": None, "set": "(! SET_B)"},
}


def rel(path: str) -> str:
    """Repo-relative path, so the checked-in report is host-independent."""
    try:
        common = os.path.commonpath([os.path.abspath(path), _REPO])
    except ValueError:  # different drives on Windows
        return path
    return os.path.relpath(path, _REPO).replace(os.sep, "/") if common == _REPO else path


def normalize_net(name: str) -> str:
    """HAL renders vector bits as ``O(0)``; the extractor writes ``O[0]``."""
    return re.sub(r"\((\d+)\)$", r"[\1]", name)


class Report:
    """Collects check results; any failure makes :meth:`ok` False."""

    def __init__(self) -> None:
        self.lines: List[str] = []
        self.failures = 0
        self.checks = 0

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(title)
        self.lines.append("-" * len(title))

    def note(self, text: str) -> None:
        self.lines.append(text)

    def check(self, label: str, actual, expected) -> bool:
        self.checks += 1
        good = actual == expected
        if good:
            self.lines.append("  [ok]   %-46s %s" % (label, _fmt(actual)))
        else:
            self.failures += 1
            self.lines.append(
                "  [FAIL] %-46s got %s, expected %s"
                % (label, _fmt(actual), _fmt(expected))
            )
        return good

    def require(self, label: str, condition: bool, detail: str = "") -> bool:
        self.checks += 1
        if condition:
            self.lines.append("  [ok]   %-46s %s" % (label, detail))
        else:
            self.failures += 1
            self.lines.append("  [FAIL] %-46s %s" % (label, detail))
        return condition

    @property
    def ok(self) -> bool:
        return self.failures == 0

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


def _fmt(value) -> str:
    if isinstance(value, (list, tuple)) and len(value) > 12:
        return "[%s, ... %d items]" % (", ".join(map(str, value[:12])), len(value))
    return str(value)


def bf_str(bf) -> Optional[str]:
    """Render a Boolean function, or ``None`` when the gate type has none."""
    return None if bf is None or bf.is_empty() else str(bf)


def ff_component(gate_type):
    """Return the FFComponent of a gate type, or ``None``."""
    import hal_py

    component = gate_type.get_component(lambda c: hal_py.FFComponent.is_class_of(c))
    return component if component is not None else None


def check_library_semantics(rep: Report, library) -> None:
    """Re-derive every combinational cell's truth table *through HAL*.

    ``gen_sky130_hgl.py`` already proves its rendered strings agree with
    ``sc_sim``; this closes the loop on the other side by evaluating what HAL's
    Boolean-function parser actually built out of the shipped ``.hgl``.
    """
    import hal_py

    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import sc_sim

    zero, one = hal_py.BooleanFunction.Value.ZERO, hal_py.BooleanFunction.Value.ONE
    unknown: List[str] = []
    pin_problems: List[str] = []
    wrong: List[str] = []
    n_types = 0
    n_rows = 0

    for name, gt in sorted(library.get_gate_types().items()):
        if name in ("HAL_GND", "HAL_VDD"):
            continue
        cell = sc_sim.lookup_cell(name)
        if cell is None:
            unknown.append(name)
            continue
        n_types += 1
        signal_in = [
            p.get_name() for p in gt.get_input_pins()
            if p.get_name() not in sc_sim.POWER_PINS
        ]
        out_pins = [p.get_name() for p in gt.get_output_pins()]
        if sorted(signal_in) != sorted(cell.inputs):
            pin_problems.append("%s inputs %s != %s" % (name, signal_in, list(cell.inputs)))
        if sorted(out_pins) != sorted(cell.output_pins):
            pin_problems.append("%s outputs %s != %s" % (name, out_pins, list(cell.output_pins)))
        if cell.is_seq or not cell.outputs:
            continue
        for row in range(1 << len(cell.inputs)):
            values = {p: (row >> i) & 1 for i, p in enumerate(cell.inputs)}
            hal_values = {p: (one if v else zero) for p, v in values.items()}
            for pin in cell.outputs:
                bf = gt.get_boolean_function(pin)
                result = bf.evaluate(hal_values)
                n_rows += 1
                if isinstance(result, (list, tuple)):
                    result = result[0] if result else None
                got = None if result is None else (1 if result == one else 0)
                want = int(cell.outputs[pin](values))
                if got != want:
                    wrong.append("%s.%s at %r: HAL %s, sc_sim %d" % (name, pin, values, got, want))

    rep.section("library semantics vs. sc_sim")
    rep.require(
        "every gate type maps to an sc_sim cell",
        not unknown,
        "" if not unknown else _fmt(unknown),
    )
    rep.require("pin rosters match sc_sim", not pin_problems, "" if not pin_problems else _fmt(pin_problems))
    rep.require(
        "Boolean functions match sc_sim",
        not wrong,
        "%d gate types, %d truth-table rows evaluated through hal_py"
        % (n_types, n_rows) if not wrong else _fmt(wrong),
    )


def check_design(rep: Report, netlist, expected: Dict[str, object], name: str) -> None:
    rep.section("%s (%s)" % (name, expected["top"]))

    rep.check("design name", netlist.get_design_name(), expected["top"])

    instances = expected["instances"]
    rep.check("gate count", len(netlist.get_gates()), len(instances))

    # per-cell-type census
    census: Dict[str, int] = {}
    for gate in netlist.get_gates():
        census[gate.get_type().get_name()] = census.get(gate.get_type().get_name(), 0) + 1
    want_census: Dict[str, int] = {}
    for inst in instances:
        want_census[inst["cell"]] = want_census.get(inst["cell"], 0) + 1
    rep.check("distinct cell types", len(census), len(want_census))
    rep.require(
        "per-cell-type census matches the extractor",
        census == want_census,
        "%d instances over %d gate types" % (len(instances), len(want_census)),
    )
    mismatch = sorted(
        c for c in set(census) | set(want_census) if census.get(c) != want_census.get(c)
    )
    if mismatch:
        rep.note("       census mismatch on: %s" % ", ".join(mismatch))

    # every instantiated type resolved to a real library gate type
    fallback = sorted(t for t in census if t in ("HAL_GND", "HAL_VDD") or "__" not in t)
    rep.require(
        "no unresolved / fallback gate types",
        not fallback,
        "" if not fallback else "found %s" % ", ".join(fallback),
    )

    # nets
    hal_nets = {normalize_net(n.get_name()) for n in netlist.get_nets()}
    want_nets = {normalize_net(n) for n in expected["nets"]}
    rep.check("net count", len(netlist.get_nets()), len(want_nets))
    only_hal = sorted(hal_nets - want_nets)
    only_json = sorted(want_nets - hal_nets)
    rep.require(
        "net names identical to the extractor's",
        not only_hal and not only_json,
        ""
        if not (only_hal or only_json)
        else "HAL-only %s / JSON-only %s" % (_fmt(only_hal), _fmt(only_json)),
    )

    # top-level ports
    ports = expected["ports"]
    want_in = sorted(normalize_net(p["net"]) for p in ports.values() if p["dir"] == "input")
    want_out = sorted(normalize_net(p["net"]) for p in ports.values() if p["dir"] == "output")
    got_in = sorted(normalize_net(n.get_name()) for n in netlist.get_global_input_nets())
    got_out = sorted(normalize_net(n.get_name()) for n in netlist.get_global_output_nets())
    rep.check("global input nets", got_in, want_in)
    rep.check("global output nets", got_out, want_out)

    # flip-flops
    import hal_py

    ffs = [g for g in netlist.get_gates() if g.get_type().has_property(hal_py.GateTypeProperty.ff)]
    want_ffs = [i for i in instances if FF_CELL_RE.match(i["cell"])]
    rep.check("flip-flop gates", len(ffs), len(want_ffs))

    ff_census: Dict[str, int] = {}
    for gate in ffs:
        ff_census[gate.get_type().get_name()] = ff_census.get(gate.get_type().get_name(), 0) + 1
    for cell, count in sorted(ff_census.items()):
        rep.note("       %-32s %d" % (cell, count))
        spec = FF_EXPECTATIONS.get(cell)
        if spec is None:
            continue
        gt = netlist.get_gate_library().get_gate_type_by_name(cell)
        comp = ff_component(gt)
        if not rep.require("%s: has an FFComponent" % cell, comp is not None):
            continue
        rep.check("%s: clock function" % cell, bf_str(comp.get_clock_function()), spec["clock"])
        rep.check("%s: async reset" % cell, bf_str(comp.get_async_reset_function()), spec["reset"])
        rep.check("%s: async set" % cell, bf_str(comp.get_async_set_function()), spec["set"])
        rep.check("%s: next state" % cell, bf_str(comp.get_next_state_function()), "D")

    # constants driven by conb
    conb = [g for g in netlist.get_gates() if g.get_type().get_name().startswith("sky130_fd_sc_hd__conb")]
    want_conb = sum(1 for i in instances if i["cell"].startswith("sky130_fd_sc_hd__conb"))
    rep.check("conb constant cells", len(conb), want_conb)
    hi_nets: List[str] = []
    lo_nets: List[str] = []
    const_ok = True
    for gate in conb:
        for pin, value, bucket in (("HI", 1, hi_nets), ("LO", 0, lo_nets)):
            bf = gate.get_boolean_function(pin)
            if bf.is_empty() or not bf.has_constant_value(value):
                const_ok = False
                continue
            net = gate.get_fan_out_net(pin)
            if net is None:
                const_ok = False
                continue
            bucket.append(net.get_name())
    if conb:
        rep.require(
            "conb HI/LO are constant 1/0 and drive a net",
            const_ok,
            "HI -> %s, LO -> %s" % (_fmt(sorted(hi_nets)), _fmt(sorted(lo_nets))),
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument(
        "--gate-library",
        default=os.path.join(
            _REPO, "plugins", "gate_libraries", "definitions", "SKY130_FD_SC_HD.hgl"
        ),
    )
    ap.add_argument("--artifacts", default=os.path.join(_EXAMPLE, "artifacts"))
    ap.add_argument("--output", help="also write the report to this file")
    args = ap.parse_args(argv)

    import hal_py

    hal_py.plugin_manager.load_all_plugins()

    rep = Report()
    rep.note("HAL gate library load check -- sky130_fd_sc_hd")
    rep.note("=" * 46)
    rep.note("gate library : %s" % rel(args.gate_library))
    rep.note("artifacts    : %s" % rel(args.artifacts))

    library = hal_py.GateLibraryManager.load(args.gate_library)
    rep.section("gate library")
    if not rep.require("library parsed", library is not None, rel(args.gate_library)):
        print(rep.render())
        return 1
    rep.check("library name", library.get_name(), "SKY130_FD_SC_HD")
    gate_types = library.get_gate_types()
    rep.note("  gate types   : %d (incl. HAL's auto-generated GND/VDD helpers)" % len(gate_types))
    n_ff = sum(
        1 for gt in gate_types.values() if gt.has_property(hal_py.GateTypeProperty.ff)
    )
    rep.note("  ff types     : %d" % n_ff)

    check_library_semantics(rep, library)

    for name, stem in DESIGNS:
        verilog = os.path.join(args.artifacts, stem + ".v")
        sidecar = os.path.join(args.artifacts, stem + ".json")
        if not os.path.exists(verilog) or not os.path.exists(sidecar):
            rep.section(name)
            rep.require(
                "netlist present",
                False,
                "%s missing -- run tools/gds2netlist.py first" % rel(verilog),
            )
            continue
        with open(sidecar, "r", encoding="utf-8") as fh:
            expected = json.load(fh)
        netlist = hal_py.NetlistFactory.load_netlist(verilog, args.gate_library)
        if netlist is None:
            rep.section(name)
            rep.require("netlist parsed", False, rel(verilog))
            continue
        check_design(rep, netlist, expected, name)

    rep.section("summary")
    rep.note(
        "  %d checks, %d failures -- %s"
        % (rep.checks, rep.failures, "PASS" if rep.ok else "FAIL")
    )

    text = rep.render()
    print(text)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
