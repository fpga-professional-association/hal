#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
hal_sim_replay.py -- replay the puzzle protocol through HAL's own simulator and
cross-check it, edge by edge, against this example's Python simulator.

``tools/sc_sim.py`` is the validated reference: it reproduces the recorded
``example_inputs.vcd`` at all 312 clock edges (``tools/replay_vcd.py``).  This
script drives the *same* stimulus through the ``hal_simulator`` engine of HAL's
``netlist_simulator_controller`` plugin and compares ``O[7:0]`` and ``success``
at every clock edge.  A divergence is therefore a finding about HAL, not about
the netlist -- the netlist is ground truth here.

Four payloads are replayed:

==================  ============================================================
``recorded``        ``"The night s"``, the failing attempt from the puzzle's own
                    example VCD -- expects the ``TRY AGAIN`` byte stream.
``solution``        the unique Star Battle key from ``artifacts/solution.txt``
                    -- expects ``success`` high and ``(* TWO STARS *)``.
``zeros``           121 zero bits -- expects ``EMPTY SKY``.
``ones``            121 one bits -- expects ``BIG BANG``.
==================  ============================================================

Protocol (see ``tools/run_attempt.py``): ``rst_n`` low for 3 rising edges, one
idle edge, 121 ``enable``-high edges with one payload bit on ``I`` each, then
``enable`` low while the chip streams one byte per edge on ``O``.

Clock grid, matching ``tools/hal_fault_campaign``: ``add_clock_period`` starts
the clock low at t=0, so the rising edge of cycle *k* is at ``k*P + P/2``;
stimulus for cycle *k* is applied at ``k*P`` and cycle *k* is sampled at
``k*P + 3P/4``, i.e. after its edge and before the next falling edge.  That is
exactly ``sc_sim.Simulator.step()``'s "apply inputs, settle, rising edge,
settle" ordering, so cycle *k* means the same thing on both sides.

Two things have to be done to the simulation set before HAL matches a plain
gate-level simulator on this netlist.  Pass ``--no-workarounds`` to see what
happens without them -- ``O`` reads ``X`` at every message edge of three of the
four payloads, which is why the first of the two is written up as a HAL finding
in ``artifacts/hal_sim_issue_draft.md``:

* **``conb`` tie cells.**  ``sky130_fd_sc_hd__conb_1`` has no input pins and
  *two* constant outputs, ``HI`` = 1 and ``LO`` = 0.  HAL only ever assigns a
  start value to a gate it has marked as a GND or a VCC gate, and
  ``GateLibrary::mark_gnd_gate_type``/``mark_vcc_gate_type`` only accept a type
  with exactly one output pin -- so ``conb`` is marked as neither, and
  ``hal_simulator`` never evaluates it either, because a combinational gate is
  only evaluated when one of its *input* nets sees an event and ``conb`` has
  none.  Its tie nets would stay ``X`` for the whole run.  The workaround keeps
  the ``conb`` gates out of the simulation set, which turns their tie nets into
  simulation inputs, and drives them to their constant values.
* **``n278``.**  The one net that is genuinely unrouted in the puzzle GDS (see
  the README).  It has no driver at all; ``sc_sim`` reads an undriven net as 0,
  so the harness drives it to 0 rather than leaving HAL to propagate ``X``.

Run it in the ``halbuild`` bench container::

    docker exec -e HAL_BASE_PATH=/work/build -e HAL_PY_PATH=/work/build/lib \\
        -e PYTHONPATH=/work/build/lib -w /work halbuild bash -c \\
        'python3 examples/janestreet_asic_2026/tools/hal_sim_replay.py \\
             --output examples/janestreet_asic_2026/artifacts/hal_sim_replay.txt'

Exit code is 0 only if HAL agreed with ``sc_sim`` on every payload at every
clock edge.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Dict, List, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXAMPLE = os.path.dirname(_HERE)
_REPO = os.path.dirname(os.path.dirname(_EXAMPLE))

if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import sc_sim  # noqa: E402  (deliberately after the sys.path fix-up)

#: Payload length in ``enable``-high clock edges.
NBITS = 121
#: Reset edges, idle edges and output edges around the payload.
NRESET = 3
NIDLE = 1
NOUT = 16
NCYCLES = NRESET + NIDLE + NBITS + NOUT

#: Engine states published by ``SimulationEngine::get_state()``.
ENGINE_DONE, ENGINE_RUNNING, ENGINE_PREPARING, ENGINE_FAILED = 0, 1, 2, -1
ENGINE_TIMEOUT_S = 900.0
_POLL_S = 0.02

#: The net that the puzzle GDS leaves unrouted; ``sc_sim`` reads it as 0.
UNDRIVEN_NETS = ("n278",)

#: What each payload is expected to make the chip say, for the report.  These
#: are assertions about the *chip*, taken from the README, and are checked
#: against HAL's own answer -- ``sc_sim`` agreeing is checked separately.
EXPECTED: Dict[str, Tuple[str, int]] = {
    "recorded": ("TRY AGAIN", 0),
    "solution": ("(* TWO STARS *)", 1),
    "zeros": ("EMPTY SKY", 0),
    "ones": ("BIG BANG", 0),
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


# ---------------------------------------------------------------------------
# stimulus
# ---------------------------------------------------------------------------


def ascii_bits(text: str, nchar: int = 11, nbit: int = 11, data: int = 8) -> List[int]:
    """The example VCD's framing: 8 data bits LSB-first plus 3 pad bits."""
    text = (text + " " * nchar)[:nchar]
    bits: List[int] = []
    for ch in text:
        value = ord(ch)
        bits.extend((value >> i) & 1 for i in range(data))
        bits.extend([0] * (nbit - data))
    return bits


def solution_bits(artifacts: str) -> List[int]:
    """The 121-bit row-major payload recorded in ``artifacts/solution.txt``."""
    path = os.path.join(artifacts, "solution.txt")
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    for match in re.finditer(r"[01]{%d}" % NBITS, text):
        return [int(c) for c in match.group(0)]
    raise SystemExit("no %d-bit payload found in %s" % (NBITS, rel(path)))


def payloads(artifacts: str) -> List[Tuple[str, str, List[int]]]:
    """``(name, description, bits)`` for every payload this script replays."""
    return [
        ("recorded", 'the example VCD\'s failing attempt, ASCII "The night s"',
         ascii_bits("The night s")),
        ("solution", "the unique Star Battle key (artifacts/solution.txt)",
         solution_bits(artifacts)),
        ("zeros", "121 zero bits", [0] * NBITS),
        ("ones", "121 one bits", [1] * NBITS),
    ]


def stimulus(bits: Sequence[int]) -> List[Dict[str, int]]:
    """One ``{net: value}`` dict per clock cycle for the whole protocol."""
    assert len(bits) == NBITS, len(bits)
    cycles = [{"rst_n": 0, "enable": 0, "I": 0} for _ in range(NRESET)]
    cycles += [{"rst_n": 1, "enable": 0, "I": 0} for _ in range(NIDLE)]
    cycles += [{"rst_n": 1, "enable": 1, "I": int(b)} for b in bits]
    cycles += [{"rst_n": 1, "enable": 0, "I": 0} for _ in range(NOUT)]
    assert len(cycles) == NCYCLES, len(cycles)
    return cycles


def phase_of(cycle: int) -> str:
    """Human-readable name of the protocol phase a cycle belongs to."""
    if cycle < NRESET:
        return "reset"
    if cycle < NRESET + NIDLE:
        return "idle"
    if cycle < NRESET + NIDLE + NBITS:
        return "payload[%d]" % (cycle - NRESET - NIDLE)
    return "out[%d]" % (cycle - NRESET - NIDLE - NBITS)


def message(trace: Sequence[Tuple[int, int]]) -> str:
    """Decode the output-phase bytes the way ``run_attempt.py`` does."""
    def char(byte: int) -> str:
        if byte < 0:
            return "?"  # X somewhere in the byte
        if byte == 0:
            return ""
        return chr(byte) if 32 <= byte < 127 else "\\x%02x" % byte

    return "".join(char(b) for b, _ in trace[NRESET + NIDLE + NBITS:])


# ---------------------------------------------------------------------------
# the reference simulator
# ---------------------------------------------------------------------------


def run_sc_sim(netlist_json: str, bits: Sequence[int]) -> List[Tuple[int, int]]:
    """``(O, success)`` after every clock edge, from the validated simulator."""
    netlist = sc_sim.load_netlist(netlist_json)
    sim = sc_sim.Simulator(netlist)
    trace: List[Tuple[int, int]] = []
    for inputs in stimulus(bits):
        sim.step(inputs)
        trace.append((sim.get_bus("O", 8), sim.get("success")))
    return trace


# ---------------------------------------------------------------------------
# HAL
# ---------------------------------------------------------------------------


class HalError(RuntimeError):
    """Raised when HAL could not be set up, run, or read back."""


class HalReplay:
    """Drives one payload through ``hal_simulator`` and samples it per cycle."""

    def __init__(self, hal_py, plugin, netlist, period_ps: int, engine_name: str,
                 workdir: str, workarounds: bool = True):
        self.hal_py = hal_py
        self.plugin = plugin
        self.netlist = netlist
        self.period = int(period_ps)
        if self.period % 4:
            raise HalError(
                "clock period %d ps is not divisible by 4; the edge (P/2) and sample "
                "(3P/4) times would not be whole picoseconds" % self.period
            )
        self.engine_name = engine_name
        self.workdir = workdir
        self.workarounds = workarounds

        self.nets = {normalize_net(n.get_name()): n for n in netlist.get_nets()}
        self.conb = [
            g for g in netlist.get_gates()
            if g.get_type().get_name().startswith("sky130_fd_sc_hd__conb")
        ]
        #: tie net -> constant value, read out of the ``conb`` gates themselves.
        #: A tie net with no destination is left out: taking the ``conb`` gates
        #: out of the simulation set does not make such a net a simulation
        #: input (nothing reads it), and it cannot influence anything anyway.
        self.ties: Dict[str, int] = {}
        for gate in self.conb:
            for pin, value in (("HI", 1), ("LO", 0)):
                net = gate.get_fan_out_net(pin)
                if net is not None and net.get_destinations():
                    self.ties[normalize_net(net.get_name())] = value
        #: Filled in by :meth:`run` with what HAL made of those nets.
        self.last_tie_values: Dict[str, Optional[int]] = {}

    def net(self, name: str):
        net = self.nets.get(name)
        if net is None:
            raise HalError("the netlist has no net named %r" % name)
        return net

    def sim_gates(self):
        if not self.workarounds:
            return self.netlist.get_gates()
        skip = {g.get_id() for g in self.conb}
        return [g for g in self.netlist.get_gates() if g.get_id() not in skip]

    def constant_inputs(self) -> Dict[str, int]:
        """Nets the harness has to hold at a constant value, and at which."""
        if not self.workarounds:
            return {}
        forced = dict(self.ties)
        for name in UNDRIVEN_NETS:
            if name in self.nets:
                forced[name] = 0
        return forced

    def _value(self, integer: int):
        bf = self.hal_py.BooleanFunction.Value
        return bf.ONE if integer else bf.ZERO

    def run(self, run_name: str, bits: Sequence[int]) -> List[Tuple[int, int]]:
        if self.workdir:
            os.makedirs(self.workdir, exist_ok=True)
        controller = self.plugin.create_simulator_controller(run_name, self.workdir or "")
        if controller is None:
            raise HalError(
                "create_simulator_controller() refused the working directory %r "
                "(NetlistSimulatorController::is_legal_directory_name rejects paths "
                "with spaces)" % self.workdir
            )

        controller.add_gates(self.sim_gates())
        engine = controller.create_simulation_engine(self.engine_name)
        if engine is None:
            raise HalError(
                "no simulation engine named %r is registered; this build offers %s"
                % (self.engine_name, ", ".join(controller.get_engine_names()) or "none")
            )

        # The clock waveform is what the simulation thread replays, so it has to
        # cover the whole run or the trace silently freezes at its last edge.
        controller.add_clock_period(
            self.net("clk"), self.period, True, self.period * NCYCLES
        )

        forced = self.constant_inputs()
        input_nets = {normalize_net(n.get_name()) for n in controller.get_input_nets()}
        missing = sorted(n for n in forced if n not in input_nets)
        if missing:
            raise HalError(
                "the harness wants to drive %s, but HAL does not see %s as simulation "
                "input nets -- the simulation set is not what this script assumes"
                % (", ".join(missing), "them" if len(missing) > 1 else "it")
            )
        for name in sorted(forced):
            controller.set_input(self.net(name), self._value(forced[name]))

        for cycle_inputs in stimulus(bits):
            for name in sorted(cycle_inputs):
                controller.set_input(self.net(name), self._value(cycle_inputs[name]))
            controller.simulate(self.period)

        if not controller.run_simulation():
            raise HalError(
                "run_simulation() refused to start the %r engine; see the HAL log and %s"
                % (self.engine_name, controller.get_working_directory())
            )
        state = self._wait(engine)
        if state == ENGINE_FAILED:
            raise HalError(
                "the %r engine failed; its working directory is %s"
                % (self.engine_name, engine.get_working_directory())
            )
        if not controller.get_results():
            raise HalError(
                "get_results() could not read the simulation back from the %r engine"
                % self.engine_name
            )

        waves = {
            name: controller.get_waveform_by_net(self.net(name))
            for name in ["O[%d]" % i for i in range(8)] + ["success"]
        }
        for name, wave in waves.items():
            if wave is None:
                raise HalError("no waveform was recorded for %r" % name)

        trace: List[Tuple[int, int]] = []
        for cycle in range(NCYCLES):
            sample = cycle * self.period + (3 * self.period) // 4
            byte = 0
            for bit in range(8):
                value = int(waves["O[%d]" % bit].get_value_at(sample))
                if value == 1:
                    byte |= 1 << bit
                elif value != 0:
                    byte = -1  # X or Z anywhere in the byte makes the byte unknown
                    break
            success = int(waves["success"].get_value_at(sample))
            trace.append((byte, success))

        # What HAL made of the nets no gate in the simulation set drives.  With
        # the workarounds on these are the values the harness forced; with them
        # off they are what HAL left behind, which is the evidence for the note
        # in artifacts/hal_sim_issue_draft.md.
        self.last_tie_values = {}
        last_sample = (NCYCLES - 1) * self.period + (3 * self.period) // 4
        for name in sorted(list(self.ties) + [n for n in UNDRIVEN_NETS if n in self.nets]):
            wave = controller.get_waveform_by_net(self.net(name))
            self.last_tie_values[name] = (
                None if wave is None else int(wave.get_value_at(last_sample))
            )
        return trace

    @staticmethod
    def _wait(engine, timeout_s: float = ENGINE_TIMEOUT_S) -> int:
        deadline = time.time() + timeout_s
        while True:
            state = engine.get_state()
            if state in (ENGINE_DONE, ENGINE_FAILED):
                return state
            if time.time() > deadline:
                raise HalError(
                    "the simulation engine was still in state %d after %.0fs"
                    % (state, timeout_s)
                )
            time.sleep(_POLL_S)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def fmt_value(value: int) -> str:
    return "X" if value < 0 else "%02x" % value


def fmt_success(value: int) -> str:
    return "X" if value not in (0, 1) else str(value)


def first_success_edge(trace: Sequence[Tuple[int, int]]) -> Optional[int]:
    """Index of the first clock edge at which ``success`` reads 1."""
    for cycle, (_byte, success) in enumerate(trace):
        if success == 1:
            return cycle
    return None


def fmt_edge(cycle: Optional[int]) -> str:
    return "never" if cycle is None else "edge %d (%s)" % (cycle, phase_of(cycle))


def compare(hal_trace: Sequence[Tuple[int, int]],
            ref_trace: Sequence[Tuple[int, int]]) -> List[Tuple[int, Tuple[int, int], Tuple[int, int]]]:
    return [
        (cycle, hal_trace[cycle], ref_trace[cycle])
        for cycle in range(min(len(hal_trace), len(ref_trace)))
        if hal_trace[cycle] != ref_trace[cycle]
    ]


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument(
        "--gate-library",
        default=os.path.join(
            _REPO, "plugins", "gate_libraries", "definitions", "SKY130_FD_SC_HD.hgl"
        ),
    )
    ap.add_argument("--artifacts", default=os.path.join(_EXAMPLE, "artifacts"))
    ap.add_argument("--engine", default="hal_simulator")
    ap.add_argument("--period", type=int, default=1000, help="clock period in ps")
    ap.add_argument("--workdir", default="", help="controller working directory")
    ap.add_argument("--only", action="append", help="replay only these payloads")
    ap.add_argument(
        "--no-workarounds",
        action="store_true",
        help="do not hold the conb tie nets and the unrouted net at constants",
    )
    ap.add_argument(
        "--skip-tie-probe",
        action="store_true",
        help="do not replay each payload a second time with the workarounds off",
    )
    ap.add_argument("--output", help="also write the report to this file")
    args = ap.parse_args(argv)

    verilog = os.path.join(args.artifacts, "puzzle_netlist.v")
    sidecar = os.path.join(args.artifacts, "puzzle_netlist.json")
    for path in (verilog, sidecar, args.gate_library):
        if not os.path.exists(path):
            raise SystemExit("missing %s -- run tools/gds2netlist.py first" % rel(path))

    import hal_py

    hal_py.plugin_manager.load_all_plugins()
    # Importing the plugin's bindings is what registers the downcast from
    # BasePluginInterface: without it get_plugin_instance() hands back a plain
    # BasePluginInterface with none of the controller factory methods on it.
    from hal_plugins import netlist_simulator_controller  # noqa: F401

    plugin = hal_py.plugin_manager.get_plugin_instance("netlist_simulator_controller")
    if plugin is None:
        raise SystemExit("the netlist_simulator_controller plugin is not available")

    netlist = hal_py.NetlistFactory.load_netlist(verilog, args.gate_library)
    if netlist is None:
        raise SystemExit("hal_py could not parse %s" % rel(verilog))

    replay = HalReplay(
        hal_py, plugin, netlist, args.period, args.engine, args.workdir,
        workarounds=not args.no_workarounds,
    )
    # The same replay with the tie/undriven nets left to HAL, so the report can
    # say what the workarounds are worth instead of asserting it.
    tie_probe = None
    if not args.no_workarounds and not args.skip_tie_probe:
        tie_probe = HalReplay(
            hal_py, plugin, netlist, args.period, args.engine, args.workdir,
            workarounds=False,
        )
    tie_probe_results: List[Tuple[str, int, str]] = []

    lines: List[str] = []
    out = lines.append
    out("HAL netlist_simulator replay vs. sc_sim -- Jane Street ASIC puzzle")
    out("=" * 66)
    out("netlist      : %s (%d gates, %d nets)"
        % (rel(verilog), len(netlist.get_gates()), len(netlist.get_nets())))
    out("gate library : %s" % rel(args.gate_library))
    out("reference    : %s" % rel(os.path.join(_HERE, "sc_sim.py")))
    out("engine       : %s of %s"
        % (args.engine, "netlist_simulator_controller"))
    out("clock        : %d ps period, rising edge at k*P+P/2, sampled at k*P+3P/4"
        % args.period)
    out("protocol     : %d reset + %d idle + %d payload + %d output = %d edges"
        % (NRESET, NIDLE, NBITS, NOUT, NCYCLES))
    forced = replay.constant_inputs()
    out("harness      : %s"
        % ("conb tie nets and the unrouted net held at constants (%s)"
           % ", ".join("%s=%d" % (n, v) for n, v in sorted(forced.items()))
           if forced else "no workarounds -- conb ties and n278 left to HAL"))

    failures = 0
    checks = 0
    for name, description, bits in payloads(args.artifacts):
        if args.only and name not in args.only:
            continue
        title = "%s -- %s" % (name, description)
        lines.append("")
        lines.append(title)
        lines.append("-" * len(title))
        out("  payload    : %s" % "".join(map(str, bits)))

        ref_trace = run_sc_sim(sidecar, bits)
        hal_trace = replay.run("replay_%s" % name, bits)

        out("  sc_sim   O : %s" % " ".join(fmt_value(b) for b, _ in ref_trace[-NOUT:]))
        out("  HAL      O : %s" % " ".join(fmt_value(b) for b, _ in hal_trace[-NOUT:]))
        out("  sc_sim msg : %r" % message(ref_trace))
        out("  HAL    msg : %r" % message(hal_trace))
        out("  sc_sim success : %s (max over the run), first high at %s"
            % (fmt_success(max(s for _, s in ref_trace)),
               fmt_edge(first_success_edge(ref_trace))))
        out("  HAL    success : %s (max over the run), first high at %s"
            % (fmt_success(max(s for _, s in hal_trace)),
               fmt_edge(first_success_edge(hal_trace))))

        diffs = compare(hal_trace, ref_trace)
        checks += 1
        if diffs:
            failures += 1
            out("  [MISMATCH] %d of %d clock edges differ" % (len(diffs), NCYCLES))
            for cycle, got, want in diffs[:12]:
                out("      edge %3d (%-12s) HAL O=%s success=%s, sc_sim O=%s success=%s"
                    % (cycle, phase_of(cycle), fmt_value(got[0]), fmt_success(got[1]),
                       fmt_value(want[0]), fmt_success(want[1])))
            if len(diffs) > 12:
                out("      ... %d more" % (len(diffs) - 12))
        else:
            out("  [ok] HAL and sc_sim agree on O and success at all %d clock edges"
                % NCYCLES)

        expect_msg, expect_success = EXPECTED[name]
        checks += 2
        hal_msg = message(hal_trace)
        hal_success = max(s for _, s in hal_trace)
        if hal_msg == expect_msg:
            out("  [ok] HAL streams the expected message %r" % expect_msg)
        else:
            failures += 1
            out("  [FAIL] HAL streams %r, expected %r" % (hal_msg, expect_msg))
        if hal_success == expect_success:
            out("  [ok] HAL's success is %s, as expected" % fmt_success(hal_success))
        else:
            failures += 1
            out("  [FAIL] HAL's success is %s, expected %d"
                % (fmt_success(hal_success), expect_success))

        if tie_probe is not None:
            probe_trace = tie_probe.run("probe_%s" % name, bits)
            tie_probe_results.append(
                (name, len(compare(probe_trace, ref_trace)), message(probe_trace))
            )

    lines.append("")
    lines.append("the conb tie cells, and what the harness workaround is worth")
    lines.append("-" * 60)
    out("  Values HAL gave the nets that no gate in the simulation set drives,")
    out("  sampled from its waveforms at the last clock edge (-1 is X):")
    for net_name in sorted(replay.last_tie_values):
        value = replay.last_tie_values[net_name]
        out("    %-10s %s" % (net_name, "no waveform" if value is None else value))
    if tie_probe is not None:
        out("  Replaying the same stimulus with --no-workarounds instead:")
        for net_name in sorted(tie_probe.last_tie_values):
            value = tie_probe.last_tie_values[net_name]
            out("    %-10s %s" % (net_name, "no waveform" if value is None else value))
        out("  and the resulting disagreement with sc_sim, per payload:")
        for name, n_diff, msg in tie_probe_results:
            out("    %-9s %3d of %d edges differ, message %r"
                % (name, n_diff, NCYCLES, msg))
    out("  A sky130 conb tie cell has no input pins, so hal_simulator never")
    out("  evaluates it -- a combinational gate is only evaluated when one of its")
    out("  input nets sees an event -- and GateLibrary::mark_gnd_gate_type /")
    out("  mark_vcc_gate_type reject a gate type with two output pins, so HAL does")
    out("  not give it a start value either.  Its constant 1 and 0 therefore stay X")
    out("  for the whole run.  This is a HAL finding, not a netlist one; see")
    out("  artifacts/hal_sim_issue_draft.md.")

    lines.append("")
    lines.append("summary")
    lines.append("-------")
    out("  %d checks, %d failures -- %s" % (checks, failures, "PASS" if not failures else "FAIL"))

    text = "\n".join(lines) + "\n"
    print(text)
    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
