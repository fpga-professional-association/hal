"""Counterexamples that can be replayed, not just read.

A refutation is only worth something if somebody else can reproduce it. The
bundle written here therefore carries everything the run depended on -- the full
bus mapping, the reset sequence, the list of environment assumptions that were
in force, the initial register state and one value per input per cycle -- and
:func:`replay` re-derives the trace from exactly those inputs and re-checks the
property *and* every environment assumption on the concrete result.

That last part matters: a counterexample produced under environment assumptions
is worthless if the replayed stimulus quietly violates one of them, because then
the "bug" is the testbench. :func:`replay` fails loudly in that case instead of
confirming the finding.

The same trace is also written as VCD so it can be opened in a waveform viewer
or fed to a simulator.
"""

import json

from . import expr

__all__ = [
    "REPLAY_VERSION",
    "TraceContext",
    "trace_from_model",
    "build_bundle",
    "write_bundle",
    "read_bundle",
    "ReplayError",
    "replay",
    "to_vcd",
]

REPLAY_VERSION = "1.0"


class ReplayError(RuntimeError):
    """A replayed counterexample did not reproduce, or broke its own assumptions."""


class TraceContext(object):
    """A :class:`~.mapping.SignalContext` look-alike backed by a concrete trace.

    Feeding the *same* property lambdas a constant-valued context is what keeps
    the symbolic check and the concrete replay from drifting apart: there is one
    definition of every APB property in :mod:`hal_apb_check.spec`, and both
    paths evaluate it.
    """

    def __init__(self, bus_mapping, trace):
        self.mapping = bus_mapping
        self.trace = trace
        self.bound = len(trace) - 1

    def _value(self, name, cycle):
        if cycle < 0 or cycle >= len(self.trace):
            raise IndexError("cycle {} is outside the replayed trace".format(cycle))
        values = self.trace[cycle]
        if name not in values:
            raise KeyError("the trace has no signal {!r}".format(name))
        return expr.TRUE if values[name] else expr.FALSE

    def has(self, apb_signal):
        if not self.mapping.has(apb_signal):
            return False
        return all(bit in self.trace[0] for bit in self.mapping.bits(apb_signal))

    def bits(self, apb_signal, cycle):
        return [self._value(bit, cycle) for bit in self.mapping.bits(apb_signal)]

    def bit(self, apb_signal, cycle):
        names = self.mapping.bits(apb_signal)
        if len(names) != 1:
            raise ValueError("{} is {} bits wide".format(apb_signal, len(names)))
        return self._value(names[0], cycle)

    def stable(self, apb_signal, cycle):
        return expr.and_(
            *[
                expr.iff(now, later)
                for now, later in zip(
                    self.bits(apb_signal, cycle), self.bits(apb_signal, cycle + 1)
                )
            ]
        )

    def reset_active(self, cycle):
        term = self._value(self.mapping.reset_signal, cycle)
        return expr.not_(term) if self.mapping.reset_active_low else term

    def option(self, key, default=None):
        return self.mapping.option(key, default)

    # -- concrete evaluation ------------------------------------------------

    def holds(self, prop, cycle):
        """``True``/``False`` for ``prop`` at ``cycle`` on this trace."""
        antecedent = prop.antecedent(self, cycle)
        if not expr.const_value(antecedent):
            return True
        return expr.const_value(prop.consequent(self, cycle))

    def activated(self, prop, cycle):
        return expr.const_value(prop.antecedent(self, cycle))


def trace_from_model(system, unrolling, model, bound):
    """Turn a SAT model into a concrete trace by *re-simulating* the system.

    The model already assigns every unrolled variable, but re-simulating from
    the model's inputs is what proves the witness is executable rather than an
    artefact of the encoding -- and it is what a replay has to do anyway.
    Variables the solver left unconstrained default to ``0`` and are listed in
    ``unconstrained``.
    """
    unconstrained = []
    initial_state = {}
    for name in system.states:
        declared = system.initial.get(name)
        if declared is not None:
            initial_state[name] = bool(declared)
            continue
        key = unrolling.variable(name, 0)
        if key in model:
            initial_state[name] = bool(model[key])
        else:
            initial_state[name] = False
            unconstrained.append(key)

    inputs = {}
    for cycle in range(bound + 1):
        for name in system.inputs:
            key = unrolling.variable(name, cycle)
            if key in model:
                inputs[(name, cycle)] = bool(model[key])
            else:
                inputs[(name, cycle)] = False
                unconstrained.append(key)

    trace = system.simulate(inputs, initial_values=initial_state)
    return trace, initial_state, sorted(unconstrained)


def build_bundle(
    bus_mapping,
    system,
    prop,
    trace,
    initial_state,
    violation_cycle,
    bound,
    assumption_ids,
    unconstrained=(),
    producer=None,
):
    """Assemble the replayable counterexample bundle."""
    inputs = {
        name: [1 if cycle[name] else 0 for cycle in trace] for name in system.inputs
    }
    return {
        "replay_version": REPLAY_VERSION,
        "producer": producer or "hal_apb_check",
        "design": system.name,
        "property": prop.id,
        "property_title": prop.title,
        "bound": bound,
        "violation_cycle": violation_cycle,
        "reset": {
            "signal": bus_mapping.reset_signal,
            "active_low": bus_mapping.reset_active_low,
            "cycles": bus_mapping.reset_cycles,
        },
        "environment_assumptions": sorted(assumption_ids),
        "mapping": bus_mapping.to_dict(),
        "initial_state": {name: bool(value) for name, value in sorted(initial_state.items())},
        "inputs": {name: inputs[name] for name in sorted(inputs)},
        "signals": sorted(trace[0]) if trace else [],
        "trace": [
            {name: (1 if values[name] else 0) for name in sorted(values)} for values in trace
        ],
        "unconstrained_variables": list(unconstrained),
    }


def write_bundle(bundle, path):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    return path


def read_bundle(path):
    with open(path, "r", encoding="utf-8") as handle:
        bundle = json.load(handle)
    version = bundle.get("replay_version")
    if version not in (REPLAY_VERSION,):
        raise ReplayError(
            "unsupported replay_version {!r}; this build writes and reads {}".format(
                version, REPLAY_VERSION
            )
        )
    return bundle


def replay(bundle, system, bus_mapping, properties_by_id, assumption_properties, check_from=1):
    """Re-run a counterexample bundle and confirm what it claims.

    Returns a report dict. Raises :class:`ReplayError` when the trace does not
    reproduce, when the property does *not* fail where the bundle says it does,
    or when the replayed stimulus violates an environment assumption the
    original run relied on.
    """
    prop = properties_by_id.get(bundle["property"])
    if prop is None:
        raise ReplayError(
            "the bundle refers to property {!r}, which this build does not define".format(
                bundle["property"]
            )
        )

    inputs = {}
    cycles = bundle["bound"] + 1
    for name, values in bundle["inputs"].items():
        if name not in system.inputs:
            raise ReplayError(
                "the bundle drives {!r}, which is not an input of design {!r}".format(
                    name, system.name
                )
            )
        if len(values) != cycles:
            raise ReplayError(
                "input {!r} has {} values but the bundle covers {} cycles".format(
                    name, len(values), cycles
                )
            )
        for cycle, value in enumerate(values):
            inputs[(name, cycle)] = bool(value)
    missing = [name for name in system.inputs if name not in bundle["inputs"]]
    if missing:
        raise ReplayError("the bundle does not drive input(s) {}".format(sorted(missing)))

    initial_state = {name: bool(value) for name, value in bundle["initial_state"].items()}
    trace = system.simulate(inputs, initial_values=initial_state)

    recorded = bundle.get("trace") or []
    if len(recorded) != len(trace):
        raise ReplayError(
            "replay produced {} cycles, the bundle recorded {}".format(len(trace), len(recorded))
        )
    for cycle, (values, stored) in enumerate(zip(trace, recorded)):
        for name in sorted(values):
            if name not in stored:
                continue
            if bool(stored[name]) != bool(values[name]):
                raise ReplayError(
                    "replay diverges at cycle {}: {} is {} but the bundle recorded {}".format(
                        cycle, name, int(values[name]), int(stored[name])
                    )
                )

    context = TraceContext(bus_mapping, trace)

    violation_cycle = bundle["violation_cycle"]
    horizon = prop.horizon_for(context)
    if violation_cycle + horizon > context.bound:
        raise ReplayError(
            "the recorded violation at cycle {} needs {} more cycle(s) than the replayed "
            "trace has".format(violation_cycle, horizon)
        )
    if context.holds(prop, violation_cycle):
        raise ReplayError(
            "property {} holds at cycle {} on the replayed trace; the counterexample did not "
            "reproduce".format(prop.id, violation_cycle)
        )

    broken_assumptions = []
    for assumption in assumption_properties:
        assumption_horizon = assumption.horizon_for(context)
        for cycle in range(check_from, context.bound - assumption_horizon + 1):
            if not context.holds(assumption, cycle):
                broken_assumptions.append({"assumption": assumption.id, "cycle": cycle})
                break
    if broken_assumptions:
        raise ReplayError(
            "the replayed stimulus violates environment assumption(s) {}; the counterexample "
            "is an artefact of the testbench, not a design defect".format(
                ", ".join(
                    "{} at cycle {}".format(entry["assumption"], entry["cycle"])
                    for entry in broken_assumptions
                )
            )
        )

    failing_cycles = [
        cycle
        for cycle in range(check_from, context.bound - horizon + 1)
        if not context.holds(prop, cycle)
    ]
    return {
        "property": prop.id,
        "design": system.name,
        "bound": bundle["bound"],
        "violation_cycle": violation_cycle,
        "failing_cycles": failing_cycles,
        "environment_assumptions_checked": sorted(p.id for p in assumption_properties),
        "environment_assumptions_hold": True,
        "cycles": len(trace),
    }


def to_vcd(trace, timescale="1ns", module="apb"):
    """Render a trace as VCD, one time step per clock cycle."""
    if not trace:
        raise ValueError("cannot write an empty trace")
    names = sorted(trace[0])
    identifiers = {}
    for index, name in enumerate(names):
        # printable ASCII identifiers, base-94 starting at '!'
        code = ""
        value = index
        while True:
            code = chr(33 + (value % 94)) + code
            value = value // 94
            if value == 0:
                break
        identifiers[name] = code

    lines = ["$timescale {} $end".format(timescale), "$scope module {} $end".format(module)]
    for name in names:
        lines.append("$var wire 1 {} {} $end".format(identifiers[name], name))
    lines.append("$upscope $end")
    lines.append("$enddefinitions $end")

    previous = {}
    for cycle, values in enumerate(trace):
        lines.append("#{}".format(cycle))
        if cycle == 0:
            lines.append("$dumpvars")
        for name in names:
            value = 1 if values[name] else 0
            if cycle == 0 or previous.get(name) != value:
                lines.append("{}{}".format(value, identifiers[name]))
            previous[name] = value
        if cycle == 0:
            lines.append("$end")
    lines.append("#{}".format(len(trace)))
    return "\n".join(lines) + "\n"
