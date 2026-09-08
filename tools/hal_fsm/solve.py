"""Wrapping ``solve_fsm``: the second and last module that touches ``hal_py``.

The plugin's Python surface is exactly three functions
(``plugins/solve_fsm/python/python_bindings.cpp``)::

    solve_fsm(nl, state_reg, transition_logic, initial_state={}, graph_path="",
              timeout=600000) -> dict[int, dict[int, BooleanFunction]] | None
    solve_fsm_brute_force(nl, state_reg, transition_logic, graph_path="") -> ... | None
    generate_dot_graph(state_reg, transitions, graph_path="", max_condition_length=128,
                       base=10) -> str | None

Three properties of that interface drive everything in this module:

1. **It is all-or-nothing.**  Any failure -- an unsupported flip-flop, an SMT
   ``unknown``, a multi-driven net -- makes the whole call return ``None`` after
   logging into HAL's log.  There is no partial transition graph to salvage, so
   a limit that is hit produces a ``timeout`` finding and nothing else.
2. **A state value only means something together with the register order.**
   Bit ``i`` of a state is the output of ``state_reg[i]``.  The order used here
   is sorted by flip-flop name, which is stable across runs and independent of
   netlist IDs, and it is recorded on the table.
3. **The conditions come back as ``BooleanFunction`` objects over ``net_<id>``
   variables.**  Resolving those variables back to nets is what makes the cone
   check, the signal names in a witness, and the determinism check possible.
"""

import os
import sys
import time

if __package__ in (None, ""):  # pragma: no cover - direct execution
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_findings.adapters.common import call

from .transitions import Transition, TransitionTable, initial_state_argument

__all__ = [
    "SolveError",
    "SolveOutcome",
    "state_register_order",
    "condition_variables",
    "net_id_of",
    "make_evaluator",
    "initial_state_for",
    "cone_holes",
    "external_state_signals",
    "solve",
]

NET_VARIABLE_PREFIX = "net_"


def _invoke(module, name, *args):
    """Call a plugin function, returning ``(result, error message or None)``.

    The bindings return ``None`` on failure and log the reason into HAL's log,
    so ``None`` is expected rather than exceptional -- but an exception (a
    renamed binding, a wrong argument type) must not be mistaken for it.
    """
    function = getattr(module, name, None)
    if function is None:
        return None, "the solve_fsm plugin has no {!r} binding in this HAL build".format(name)
    try:
        return function(*args), None
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        return None, "{} raised {}: {}".format(name, type(exc).__name__, exc)


class SolveError(RuntimeError):
    """A failure of the solve step itself, with a typed kind for the findings."""

    def __init__(self, message, kind="plugin_error", detail=None):
        RuntimeError.__init__(self, message)
        self.kind = kind
        self.detail = detail


class SolveOutcome(object):
    """What one solver call produced."""

    def __init__(
        self,
        table=None,
        mode=None,
        wall_time_s=0.0,
        error=None,
        timed_out=False,
        dot_path=None,
        holes=(),
        attempts=(),
        notes=(),
    ):
        self.table = table
        self.mode = mode
        self.wall_time_s = wall_time_s
        self.error = error
        self.timed_out = timed_out
        self.dot_path = dot_path
        self.holes = list(holes)
        self.attempts = list(attempts)
        self.notes = list(notes)


def state_register_order(extraction, gate_ids):
    """The ``state_reg`` list, ordered so that a rerun numbers states the same.

    Sorted by flip-flop name (then ID as a tie-break): netlist IDs depend on
    parse order and would make state values change when the netlist is
    re-imported, whereas names are a property of the design.
    """
    entries = []
    for gate_id in gate_ids:
        gate = extraction.graph.gates[int(gate_id)]
        entries.append((gate.name, gate.id))
    entries.sort()
    return [gate_id for _, gate_id in entries]


def condition_variables(function):
    """Sorted variable names of a ``BooleanFunction`` (empty if unavailable)."""
    names = call(function, "get_variable_names", default=None)
    if names is None:
        return ()
    return tuple(sorted(str(name) for name in names))


def net_id_of(variable):
    """``'net_42'`` -> ``42``; ``None`` for anything else."""
    text = str(variable)
    if not text.startswith(NET_VARIABLE_PREFIX):
        return None
    try:
        return int(text[len(NET_VARIABLE_PREFIX) :])
    except ValueError:
        return None


def make_evaluator(hal_py, functions):
    """Build ``evaluate(source, target, assignment)`` over recovered conditions.

    ``functions`` maps ``(source, target)`` to the ``BooleanFunction`` the plugin
    returned.  A value the bindings cannot decide (``X``, or an evaluation
    error) becomes ``None``; the callers treat that as "undecided" and never as
    "false".
    """
    value_type = getattr(getattr(hal_py, "BooleanFunction", None), "Value", None)
    one = getattr(value_type, "ONE", 1)
    zero = getattr(value_type, "ZERO", 0)

    def evaluate(source, target, assignment):
        function = functions.get((int(source), int(target)))
        if function is None:
            return None
        inputs = {name: (one if value else zero) for name, value in assignment.items()}
        result = call(function, "evaluate", inputs, default=None)
        if result is None:
            return None
        if isinstance(result, (list, tuple)):
            if not result:
                return None
            result = result[0]
        if result == one:
            return True
        if result == zero:
            return False
        return None

    return evaluate


def initial_state_for(extraction, order, configuration):
    """Decide the initial state and how to express it for ``solve_fsm``.

    Returns ``(value, bits_in_register_order, encoding, source, discharged,
    notes)``.  ``discharged`` is True only when the value was read out of the
    design (the flip-flops' ``INIT`` attributes) rather than assumed.
    """
    notes = []
    width = len(order)
    requested = configuration.initial_state
    source = configuration.initial_state_source
    discharged = False

    if requested is None:
        values = [extraction.graph.gates[gate_id].init_value for gate_id in order]
        if all(value is not None for value in values):
            state_value = 0
            for index, value in enumerate(values):
                if value:
                    state_value |= 1 << index
            discharged = True
            notes.append(
                "the initial state was read from the INIT attribute of every state "
                "flip-flop"
            )
        else:
            state_value = 0
            source = "zero"
            missing = [
                extraction.graph.gates[gate_id].name
                for gate_id, value in zip(order, values)
                if value is None
            ]
            notes.append(
                "no INIT attribute could be read for {}; the initial state is *assumed* "
                "to be all-zero, which is also what solve_fsm defaults to".format(
                    ", ".join(missing[:6]) + (" ..." if len(missing) > 6 else "")
                )
            )
    elif requested == "zero":
        state_value = 0
    elif isinstance(requested, int):
        state_value = int(requested)
    else:
        state_value = 0
        by_name = {extraction.graph.gates[gate_id].name: index for index, gate_id in enumerate(order)}
        unknown = sorted(set(requested) - set(by_name))
        if unknown:
            raise SolveError(
                "initial_state names flip-flop(s) that are not in the state register: "
                "{}".format(", ".join(unknown)),
                kind="invalid_input",
            )
        missing = sorted(set(by_name) - set(requested))
        if missing:
            raise SolveError(
                "initial_state does not give a value for {}; solve_fsm rejects a partial "
                "initial state map".format(", ".join(missing)),
                kind="invalid_input",
            )
        for name, bit in requested.items():
            if bit:
                state_value |= 1 << by_name[name]

    bits, encoding, encoding_note = initial_state_argument(
        state_value, width, configuration.initial_state_encoding
    )
    if encoding_note:
        notes.append(encoding_note)
    return state_value, bits, encoding, source, discharged, notes


def _transitions_to_table(mapping, order, extraction, initial_state, mode, member_ids):
    """Convert what the plugin returned into a table plus its condition objects."""
    table = TransitionTable(
        [extraction.graph.gates[gate_id].name for gate_id in order],
        initial_state=initial_state,
        solver=mode,
        complete=True,
    )
    functions = {}
    signals = {}
    for source in sorted(mapping):
        for target in sorted(mapping[source]):
            function = mapping[source][target]
            variables = condition_variables(function)
            for variable in variables:
                if variable in signals:
                    continue
                net_id = net_id_of(variable)
                signals[variable] = {
                    "name": extraction.net_name(net_id) if net_id else variable,
                    "net_id": net_id,
                    "role": extraction.net_role(net_id, member_ids) if net_id else "unknown",
                }
            table.add(
                Transition(
                    source,
                    target,
                    condition=str(call(function, "to_string", default=function)),
                    variables=variables,
                )
            )
            functions[(int(source), int(target))] = function
    table.signals = signals
    return table, functions


def cone_holes(table, extraction, cone_gate_ids):
    """Condition variables that a combinational gate outside the cone drives.

    ``solve_fsm`` turns any net it cannot expand through the transition logic it
    was given into a free variable.  A free variable that is in fact driven by
    combinational logic therefore means the cone had a hole and the conditions
    are not the design's.
    """
    holes = []
    cone = set(int(gate_id) for gate_id in cone_gate_ids)
    for variable, info in sorted(table.signals.items()):
        net_id = info.get("net_id")
        if net_id is None:
            continue
        entry = extraction.nets.get(int(net_id))
        if entry is None:
            continue
        source = entry.get("source_gate")
        if source is None or entry.get("source_kind") != "combinational":
            continue
        if int(source) in cone:
            continue
        gate = extraction.graph.gates.get(int(source))
        holes.append(
            {
                "variable": variable,
                "net_id": int(net_id),
                "net_name": entry.get("name"),
                "driver_gate_id": int(source),
                "driver_gate_name": call(extraction.gate(source), "get_name", default=""),
                "driver_is_state_flip_flop": gate is not None,
            }
        )
    return holes


def external_state_signals(table, extraction):
    """Condition variables driven by flip-flops *outside* the state register.

    They are not an error -- a controller may legitimately react to a counter's
    terminal count -- but they change what the recovered graph is: it describes
    the machine's behaviour *given* those signals, not a closed machine driven by
    inputs alone.  A candidate that is really two registers glued together shows
    up here, which is one of the few structural signals that a selection was
    wrong.
    """
    external = []
    for variable, info in sorted(table.signals.items()):
        if info.get("role") != "external_state":
            continue
        net_id = info.get("net_id")
        entry = extraction.nets.get(int(net_id)) if net_id else None
        driver = entry.get("source_gate") if entry else None
        external.append(
            {
                "variable": variable,
                "net_id": int(net_id) if net_id else None,
                "net_name": info.get("name"),
                "driver_gate_id": int(driver) if driver else None,
                "driver_gate_name": (
                    extraction.graph.gates[int(driver)].name
                    if driver and int(driver) in extraction.graph.gates
                    else None
                ),
            }
        )
    return external


def solve(
    hal_py,
    solve_fsm_module,
    netlist,
    extraction,
    candidate,
    configuration,
    output_dir=None,
    transition_logic_ids=None,
):
    """Run ``solve_fsm`` for one candidate and return a :class:`SolveOutcome`."""
    limits = configuration.limits
    order = state_register_order(extraction, candidate.gate_ids)
    width = len(order)

    if width > limits.max_state_bits:
        raise SolveError(
            "the candidate has {} state flip-flops, more than the max_state_bits limit "
            "of {}; solve_fsm was not called".format(width, limits.max_state_bits),
            kind="resource",
        )
    unusable = [gate_id for gate_id in order if gate_id in extraction.graph.unusable]
    if unusable:
        raise SolveError(
            "; ".join(extraction.graph.unusable[gate_id] for gate_id in unusable),
            kind="invalid_input",
        )

    state_reg = [extraction.gate(gate_id) for gate_id in order]
    if any(gate is None for gate in state_reg):
        raise SolveError("a candidate flip-flop is not in the netlist", kind="internal")

    cone_ids = (
        set(int(gate_id) for gate_id in transition_logic_ids)
        if transition_logic_ids is not None
        else extraction.graph.cone_of(order)
    )
    transition_logic = [
        extraction.gate(gate_id) for gate_id in sorted(cone_ids) if extraction.gate(gate_id)
    ]
    if not transition_logic:
        raise SolveError(
            "the transition cone of this candidate contains no combinational gate; "
            "solve_fsm has nothing to build a next-state function from",
            kind="invalid_input",
        )

    state_value, bits, encoding, initial_source, discharged, notes = initial_state_for(
        extraction, order, configuration
    )

    dot_path = ""
    if output_dir:
        dot_path = os.path.join(output_dir, "solve_fsm-{}.dot".format(candidate.gate_ids[0]))

    modes = []
    if configuration.solver in ("auto", "smt"):
        modes.append("smt")
    if configuration.solver in ("auto", "brute_force"):
        modes.append("brute_force")

    attempts = []
    started = time.time()
    for mode in modes:
        attempt_started = time.time()
        mapping = None
        failure = None
        if mode == "smt":
            initial_state = {}
            if state_value or configuration.initial_state is not None:
                initial_state = {
                    state_reg[index]: bool(bits[index]) for index in range(width)
                }
            mapping, failure = _invoke(
                solve_fsm_module,
                "solve_fsm",
                netlist,
                state_reg,
                transition_logic,
                initial_state,
                dot_path,
                int(limits.smt_timeout_s * 1000),
            )
        else:
            budget = 1 << width
            if budget > limits.brute_force_max_states:
                attempts.append(
                    {
                        "mode": mode,
                        "skipped": "2**{} states exceed the brute_force_max_states limit "
                        "of {}".format(width, limits.brute_force_max_states),
                    }
                )
                continue
            mapping, failure = _invoke(
                solve_fsm_module,
                "solve_fsm_brute_force",
                netlist,
                state_reg,
                transition_logic,
                dot_path,
            )
        elapsed = time.time() - attempt_started
        attempt = {"mode": mode, "wall_time_s": round(elapsed, 3), "ok": mapping is not None}
        if failure:
            attempt["error"] = failure
        attempts.append(attempt)
        if mapping is None:
            continue

        table, functions = _transitions_to_table(
            mapping, order, extraction, state_value, mode, candidate.gate_ids
        )
        if len(table.states) > limits.max_states or len(table.transitions) > limits.max_transitions:
            table.complete = False
            notes.append(
                "the recovered relation has {} states and {} transitions, beyond the "
                "max_states/max_transitions limits ({}/{}); it is reported but not "
                "presented as a complete machine".format(
                    len(table.states),
                    len(table.transitions),
                    limits.max_states,
                    limits.max_transitions,
                )
            )
        outcome = SolveOutcome(
            table=table,
            mode=mode,
            wall_time_s=time.time() - started,
            dot_path=dot_path if dot_path and os.path.isfile(dot_path) else None,
            holes=cone_holes(table, extraction, cone_ids),
            attempts=attempts,
            notes=notes,
        )
        outcome.functions = functions
        outcome.state_register_order = order
        outcome.initial_state_encoding = encoding
        outcome.initial_state_source = initial_source
        outcome.initial_state_discharged = discharged
        outcome.transition_logic_ids = sorted(cone_ids)
        return outcome

    elapsed = time.time() - started
    timed_out = elapsed >= limits.wall_time_s
    raised = [attempt["error"] for attempt in attempts if attempt.get("error")]
    outcome = SolveOutcome(
        mode=modes[0] if modes else None,
        wall_time_s=elapsed,
        error={
            "kind": "resource" if timed_out else "plugin_error",
            "message": (
                "solve_fsm produced no result for this candidate after {:.1f}s{}"
                .format(elapsed, "; " + raised[0] if raised else "")
            ),
            "detail": "attempts: {}".format(attempts),
        },
        timed_out=timed_out,
        attempts=attempts,
        notes=notes,
    )
    outcome.functions = {}
    outcome.state_register_order = order
    outcome.initial_state_encoding = encoding
    outcome.initial_state_source = initial_source
    outcome.initial_state_discharged = discharged
    outcome.transition_logic_ids = sorted(cone_ids)
    return outcome
