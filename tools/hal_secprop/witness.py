"""Witnesses that replay, and read as transactions rather than as bit vectors.

A security counterexample is only worth something if somebody else can run it.
The bundle written here therefore carries everything the run depended on -- the
whole policy document, the reset schedule, the environment assumptions that were
in force, the initial register state and one value per input per cycle -- and
:func:`replay` re-derives the trace from exactly those inputs before re-checking
the obligation *and* every assumption on the concrete result.

The format follows ``hal_apb_check``'s replay conventions deliberately
(``replay_version``, ``inputs``, ``trace``, ``initial_state``,
``environment_assumptions``, one VCD time step per clock edge), so the two tools'
evidence directories are readable with the same habits and the same viewers.
What is added is the **transaction sequence**: the policy already declares which
signals are the external write controls and what combination constitutes an
access, so the raw trace is decoded back into one line per cycle naming the
accesses that are asserted, whether reset is active, whether the lock is engaged,
and the address and write data as hex. That is the artefact a reviewer actually
wants -- "assert the lock, then drive this one debug write" -- and it is derived
from the same trace that replays, not typed alongside it.

``replay`` refuses a bundle whose stimulus breaks one of the assumptions the
original run relied on: a counterexample produced under an environment
assumption is worthless if the stimulus quietly violates it, because then the
bug is in the testbench. ``check`` replays every counterexample *before* writing
it into the report, so a witness that cannot be reproduced is never published.
"""

import json

from hal_apb_check.witness import to_vcd  # noqa: F401 - re-exported, same VCD conventions

from .context import TraceContext

__all__ = [
    "REPLAY_VERSION",
    "BUNDLE_KIND",
    "ReplayError",
    "build_bundle",
    "dumps_bundle",
    "read_bundle",
    "replay",
    "transactions_from_trace",
    "format_transactions",
    "to_vcd",
]

REPLAY_VERSION = "1.0"
BUNDLE_KIND = "hal_secprop.security-witness"


class ReplayError(RuntimeError):
    """A replayed witness did not reproduce, or broke its own assumptions."""


# ---------------------------------------------------------------------------
# transaction decoding
# ---------------------------------------------------------------------------


def _vector_value(values, signals):
    """Signals are least significant bit first."""
    number = 0
    for index, signal in enumerate(signals):
        if values.get(signal):
            number |= 1 << index
    return number


def transactions_from_trace(policy, trace):
    """Decode the external write controls of every cycle into readable records."""
    records = []
    for cycle, values in enumerate(trace):
        entry = {"cycle": cycle}
        reset = values.get(policy.reset_signal)
        entry["reset_active"] = bool(
            (not reset) if policy.reset_active_low else reset
        )
        if policy.lock_signal in values:
            entry["locked"] = bool(values[policy.lock_signal]) == bool(
                policy.lock_locked_value
            )
        controls = {}
        for role in sorted(policy.interface_signals):
            signals = policy.interface_signals[role]
            if not all(signal in values for signal in signals):
                continue
            if len(signals) == 1:
                controls[role] = 1 if values[signals[0]] else 0
            else:
                controls[role] = "0x{:x}".format(_vector_value(values, signals))
        entry["controls"] = controls
        entry["accesses"] = [
            access.id
            for access in policy.accesses
            if all(
                bool(values.get(signal)) == bool(wanted)
                for signal, wanted in access.condition.items()
            )
        ]
        entry["observations"] = {
            point["name"]: (1 if values.get(point["signal"]) else 0)
            for point in policy.observation_points
            if point["signal"] in values
        }
        records.append(entry)
    return records


def format_transactions(records, highlight=None):
    """Render the decoded transactions as aligned text for a terminal."""
    lines = []
    for entry in records:
        marker = ">>" if highlight is not None and entry["cycle"] == highlight else "  "
        flags = []
        if entry.get("reset_active"):
            flags.append("RESET")
        if entry.get("locked"):
            flags.append("LOCKED")
        if entry["accesses"]:
            flags.append("+".join(entry["accesses"]))
        controls = " ".join(
            "{}={}".format(role, entry["controls"][role])
            for role in sorted(entry["controls"])
        )
        lines.append(
            "{} cycle {:>2}  {:<24} {}".format(
                marker, entry["cycle"], " ".join(flags) or "-", controls
            )
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# bundles
# ---------------------------------------------------------------------------


def assumption_ids(policy):
    """The environment assumptions a run of ``policy`` is scoped to."""
    ids = ["secprop/env/reset-sequence", "secprop/env/single-clock"]
    for signal in sorted(policy.quiescent_inputs):
        ids.append("secprop/env/quiescent:{}".format(signal))
    return ids


def build_bundle(policy, system, prop, result, bound, producer=None, design_source=None):
    """Assemble the replayable witness bundle for one violated obligation.

    ``design_source`` records the *resolved* design paths and the netlist's
    content hash. Without it a bundle is only replayable from the directory it
    was written next to, and a bundle replayed against different bytes would
    silently confirm or deny a finding about a design nobody looked at.
    """
    trace = result.detail["trace"]
    inputs = {
        name: [1 if cycle[name] else 0 for cycle in trace] for name in system.inputs
    }
    return {
        "replay_version": REPLAY_VERSION,
        "bundle_kind": BUNDLE_KIND,
        "producer": producer or "hal_secprop",
        "design": system.name,
        "design_source": dict(design_source or {}),
        "property": prop.id,
        "property_title": prop.title,
        "register": prop.register,
        "bound": bound,
        "violation_cycle": result.detail["violation_cycle"],
        "failing_cycles": list(result.detail.get("failing_cycles") or []),
        "failing_bits": list(result.detail.get("failing_bits") or []),
        "clock": {"signal": policy.clock_signal, "edge": policy.clock_edge},
        "reset": {
            "signal": policy.reset_signal,
            "active_low": policy.reset_active_low,
            "cycles": policy.reset_cycles,
        },
        "environment_assumptions": assumption_ids(policy),
        "policy": policy.to_dict(),
        "initial_state": {
            name: bool(value) for name, value in sorted(result.detail["initial_state"].items())
        },
        "inputs": {name: inputs[name] for name in sorted(inputs)},
        "signals": sorted(trace[0]) if trace else [],
        "trace": [
            {name: (1 if values[name] else 0) for name in sorted(values)} for values in trace
        ],
        "transactions": transactions_from_trace(policy, trace),
        "unconstrained_variables": list(result.detail.get("unconstrained") or []),
    }


def dumps_bundle(bundle):
    """Deterministic JSON for a bundle: sorted keys, one trailing newline.

    Two runs on the same inputs must produce byte-identical evidence, so that a
    results directory can be diffed and cached.
    """
    return json.dumps(bundle, indent=2, sort_keys=True) + "\n"


def read_bundle(path):
    with open(path, "r", encoding="utf-8") as handle:
        bundle = json.load(handle)
    if bundle.get("replay_version") != REPLAY_VERSION:
        raise ReplayError(
            "unsupported replay_version {!r}; this build reads {}".format(
                bundle.get("replay_version"), REPLAY_VERSION
            )
        )
    if bundle.get("bundle_kind") != BUNDLE_KIND:
        raise ReplayError(
            "{!r} is not a hal_secprop witness (bundle_kind {!r}); a bundle from another "
            "tool carries different assumptions and must not be replayed here".format(
                path, bundle.get("bundle_kind")
            )
        )
    return bundle


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def _check_environment(policy, trace):
    """Re-check the declared environment on a concrete stimulus."""
    broken = []
    for cycle, values in enumerate(trace):
        raw = values.get(policy.reset_signal)
        if raw is None:
            broken.append(
                {
                    "assumption": "secprop/env/reset-sequence",
                    "cycle": cycle,
                    "detail": "the trace has no reset signal",
                }
            )
            break
        active = (not raw) if policy.reset_active_low else bool(raw)
        expected = cycle < policy.reset_cycles
        if active != expected:
            broken.append(
                {
                    "assumption": "secprop/env/reset-sequence",
                    "cycle": cycle,
                    "detail": "reset is {} at cycle {}, the declared schedule holds it "
                    "{} for the first {} cycle(s)".format(
                        "active" if active else "inactive",
                        cycle,
                        "active",
                        policy.reset_cycles,
                    ),
                }
            )
            break
    for signal, value in sorted(policy.quiescent_inputs.items()):
        for cycle, values in enumerate(trace):
            if signal not in values:
                continue
            if bool(values[signal]) != bool(value):
                broken.append(
                    {
                        "assumption": "secprop/env/quiescent:{}".format(signal),
                        "cycle": cycle,
                        "detail": "{} is {} at cycle {}, the policy pins it at {}".format(
                            signal, int(bool(values[signal])), cycle, value
                        ),
                    }
                )
                break
    return broken


def replay(bundle, system, policy, properties_by_id, check_from=1):
    """Re-run a witness bundle and confirm what it claims.

    Returns a report dict. Raises :class:`ReplayError` when the trace does not
    reproduce, when the obligation does *not* fail where the bundle says it
    does, or when the replayed stimulus violates an environment assumption the
    original run relied on.
    """
    prop = properties_by_id.get(bundle["property"])
    if prop is None:
        raise ReplayError(
            "the bundle refers to obligation {!r}, which this policy does not "
            "define".format(bundle["property"])
        )

    cycles = bundle["bound"] + 1
    inputs = {}
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
            "replay produced {} cycles, the bundle recorded {}".format(
                len(trace), len(recorded)
            )
        )
    for cycle, (values, stored) in enumerate(zip(trace, recorded)):
        for name in sorted(values):
            if name not in stored:
                continue
            if bool(stored[name]) != bool(values[name]):
                raise ReplayError(
                    "replay diverges at cycle {}: {} is {} but the bundle recorded "
                    "{}".format(cycle, name, int(values[name]), int(stored[name]))
                )

    broken = _check_environment(policy, trace)
    if broken:
        raise ReplayError(
            "the replayed stimulus violates environment assumption(s) {}; the witness is "
            "an artefact of the testbench, not a policy violation".format(
                ", ".join(
                    "{} at cycle {} ({})".format(
                        entry["assumption"], entry["cycle"], entry["detail"]
                    )
                    for entry in broken
                )
            )
        )

    context = TraceContext(policy, trace)
    violation_cycle = bundle["violation_cycle"]
    if violation_cycle + prop.horizon > context.bound:
        raise ReplayError(
            "the recorded violation at cycle {} needs {} more cycle(s) than the replayed "
            "trace has".format(violation_cycle, prop.horizon)
        )
    if context.holds(prop, violation_cycle):
        raise ReplayError(
            "obligation {} holds at cycle {} on the replayed trace; the witness did not "
            "reproduce".format(prop.id, violation_cycle)
        )
    if not context.exercised(prop, violation_cycle):
        raise ReplayError(
            "obligation {} fails at cycle {} but its exercise condition is not met there: "
            "no declared external write is being attempted, so this trace is not evidence "
            "that the interface can do it".format(prop.id, violation_cycle)
        )

    failing = [
        cycle
        for cycle in range(check_from, context.bound - prop.horizon + 1)
        if not context.holds(prop, cycle)
    ]
    return {
        "property": prop.id,
        "design": system.name,
        "bound": bundle["bound"],
        "violation_cycle": violation_cycle,
        "failing_cycles": failing,
        "failing_bits": context.failing_bits(prop, violation_cycle),
        "environment_assumptions_checked": list(bundle.get("environment_assumptions") or []),
        "environment_assumptions_hold": True,
        "exercised": True,
        "cycles": len(trace),
        "transactions": transactions_from_trace(policy, trace),
    }


def witness_entries(model, policy, prop, trace, net_refs=None):
    """Schema witness entries: the interface, the lock and the protected bits."""
    interesting = []
    for role in sorted(policy.interface_signals):
        for index, signal in enumerate(policy.interface_signals[role]):
            label = role if len(policy.interface_signals[role]) == 1 else "{}[{}]".format(
                role, index
            )
            interesting.append((label, signal))
    if policy.lock_signal:
        interesting.append(("lock", policy.lock_signal))
    interesting.append(("reset", policy.reset_signal))
    register = policy.register_by_name(prop.register) if prop.register else None
    if register is not None:
        for bit in register.bits:
            interesting.append((bit.name, bit.signal))
    for point in policy.observation_points:
        interesting.append((point["name"], point["signal"]))

    entries = []
    seen = set()
    for label, signal in interesting:
        if (label, signal) in seen:
            continue
        seen.add((label, signal))
        for cycle, values in enumerate(trace):
            if signal not in values:
                continue
            entries.append(
                model.witness_entry(
                    label,
                    "1" if values[signal] else "0",
                    cycle=cycle,
                    net=(net_refs or {}).get(signal),
                )
            )
    return entries
