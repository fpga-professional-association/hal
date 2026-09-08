"""Transition tables: bit orders, reachability, witnesses and comparison.

Everything here is pure Python over plain values.  The only thing it needs from
HAL is an *evaluator*: a callable ``evaluate(source, target, assignment)`` that
returns ``True``/``False``/``None`` for one transition condition under one
assignment of its variables.  :mod:`hal_fsm.solve` builds one from
``hal_py.BooleanFunction.evaluate``; the unit tests build one from a Python
expression.  That split is what makes the reachability search, the determinism
check and the ground-truth comparison testable without a HAL build.

The state encoding, and why it needs saying
-------------------------------------------
``solve_fsm`` numbers a state by the position of a flip-flop in the
``state_reg`` list it was given: bit ``i`` of a state value is the output of
``state_reg[i]`` (``plugins/solve_fsm/src/solve_fsm.cpp``,
``generate_conditional_transitions``).  A state value is therefore meaningless
without the list that produced it, which is why every table here carries its
``bit_order`` and why :func:`permute_to` exists: a design whose state bits have
been renamed can only be compared against a reference after both are expressed
in the same bit order.

The ``initial_state`` argument of ``solve_fsm`` is encoded the *other way
round* (same file, ``initial_state_num = (initial_state_num << 1) + value``
while iterating ``state_reg`` in order, so ``state_reg[0]`` lands in the most
significant bit).  For an all-zero -- or any palindromic -- initial state the
two agree and there is nothing to decide.  For anything else there is, and
:func:`initial_state_argument` makes the decision explicit instead of guessing;
:func:`explored_set_matches` then checks afterwards which of the two the solver
actually used.
"""

from collections import deque

__all__ = [
    "Transition",
    "TransitionTable",
    "format_state",
    "state_bits",
    "bit_permutation",
    "permute_state",
    "permute_to",
    "initial_state_argument",
    "reachable_states",
    "shortest_path",
    "explored_set_matches",
    "WitnessError",
    "satisfy",
    "build_witness",
    "check_determinism",
    "compare_tables",
]


def state_bits(value, width):
    """``value`` as a list of bits, index ``i`` = bit ``i`` (LSB first)."""
    return [(int(value) >> index) & 1 for index in range(width)]


def format_state(value, width, base=2):
    """Render a state value for a label: binary is the useful default.

    Binary is printed most-significant bit first -- i.e. the *last* flip-flop of
    the bit order first -- which is what a reader expects from a bit vector.
    """
    value = int(value)
    if base == 2:
        return "".join(str(bit) for bit in reversed(state_bits(value, width)))
    if base == 16:
        return "0x{:x}".format(value)
    return str(value)


class Transition(object):
    """One edge of a transition relation."""

    def __init__(self, source, target, condition="", variables=()):
        self.source = int(source)
        self.target = int(target)
        #: the condition as ``solve_fsm`` printed it (a Boolean function string).
        self.condition = condition
        #: variable names appearing in the condition (``net_<id>`` from HAL).
        self.variables = tuple(variables)

    def to_json(self):
        return {
            "source": self.source,
            "target": self.target,
            "condition": self.condition,
            "variables": list(self.variables),
        }


class TransitionTable(object):
    """A recovered transition relation plus everything needed to read it."""

    def __init__(
        self,
        bit_order,
        transitions=(),
        initial_state=0,
        solver="smt",
        complete=True,
        signals=None,
        notes=(),
    ):
        #: gate names, index = state bit.  Never optional: see the module docstring.
        self.bit_order = list(bit_order)
        self.transitions = []
        self._by_source = {}
        for transition in transitions:
            self.add(transition)
        self.initial_state = int(initial_state)
        #: ``"smt"`` (states reachable from the initial state) or
        #: ``"brute_force"`` (every state of the encoding, reachable or not).
        self.solver = solver
        #: False when a limit stopped the run: no completeness claim may be made.
        self.complete = bool(complete)
        #: ``{variable name: {"name":..., "net_id":..., "role":...}}``
        self.signals = dict(signals or {})
        self.notes = list(notes)

    # -- construction ----------------------------------------------------

    def add(self, transition):
        self.transitions.append(transition)
        self._by_source.setdefault(transition.source, {})[transition.target] = transition
        return transition

    @classmethod
    def from_mapping(cls, bit_order, mapping, **kwargs):
        """Build from ``{source: {target: condition}}`` (what solve_fsm returns)."""
        table = cls(bit_order, **kwargs)
        for source in sorted(mapping):
            for target in sorted(mapping[source]):
                condition = mapping[source][target]
                table.add(Transition(source, target, condition=str(condition)))
        return table

    # -- views -----------------------------------------------------------

    @property
    def width(self):
        return len(self.bit_order)

    @property
    def states(self):
        states = set(self._by_source)
        for transition in self.transitions:
            states.add(transition.target)
        return sorted(states)

    def successors(self, state):
        return dict(self._by_source.get(int(state), {}))

    def successor_map(self):
        return {source: sorted(targets) for source, targets in self._by_source.items()}

    def to_json(self):
        return {
            "bit_order": list(self.bit_order),
            "initial_state": self.initial_state,
            "solver": self.solver,
            "complete": self.complete,
            "width": self.width,
            "states": self.states,
            "transitions": [t.to_json() for t in self.transitions],
            "signals": dict(self.signals),
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# bit orders
# ---------------------------------------------------------------------------


def bit_permutation(source_order, target_order):
    """Map a state value in ``source_order`` onto one in ``target_order``.

    Returns a list ``perm`` with ``perm[i]`` = the bit index in the *source*
    order that becomes bit ``i`` of the target.  Raises ``ValueError`` when the
    two orders are not permutations of one another, because a comparison
    between different registers is a mistake, not a difference.
    """
    if sorted(source_order) != sorted(target_order):
        missing = sorted(set(target_order) - set(source_order))
        extra = sorted(set(source_order) - set(target_order))
        raise ValueError(
            "bit orders describe different registers: {} missing from the recovered "
            "order, {} not in the reference order".format(missing or "nothing", extra or "nothing")
        )
    lookup = {name: index for index, name in enumerate(source_order)}
    return [lookup[name] for name in target_order]


def permute_state(value, permutation):
    """Re-express one state value under a :func:`bit_permutation`."""
    out = 0
    for target_index, source_index in enumerate(permutation):
        if (int(value) >> source_index) & 1:
            out |= 1 << target_index
    return out


def permute_to(table, target_order):
    """Return a copy of ``table`` expressed in ``target_order``."""
    permutation = bit_permutation(table.bit_order, target_order)
    permuted = TransitionTable(
        list(target_order),
        initial_state=permute_state(table.initial_state, permutation),
        solver=table.solver,
        complete=table.complete,
        signals=table.signals,
        notes=table.notes,
    )
    for transition in table.transitions:
        permuted.add(
            Transition(
                permute_state(transition.source, permutation),
                permute_state(transition.target, permutation),
                condition=transition.condition,
                variables=transition.variables,
            )
        )
    return permuted


def initial_state_argument(state_value, width, encoding="auto"):
    """Bits to hand ``solve_fsm`` as ``initial_state``, in ``state_reg`` order.

    Returns ``(bits, chosen_encoding, note)`` where ``bits[k]`` is the value for
    ``state_reg[k]``.

    ``transition_index``
        compensates for the reversed encoding of the ``initial_state`` argument
        so that the solver's breadth-first search really starts at
        ``state_value`` *as the returned transition table numbers states*.
    ``argument_order``
        passes the bits through as declared, which is what the binding's
        documentation says and what a future fix upstream would make correct.
    ``auto``
        ``transition_index``, but silent when both agree (an all-zero or
        palindromic initial state), which is the common case.
    """
    bits = state_bits(state_value, width)
    reversed_value = permute_state(state_value, list(range(width - 1, -1, -1)))
    symmetric = reversed_value == int(state_value)

    if encoding == "argument_order":
        chosen = "argument_order"
    elif encoding == "transition_index":
        chosen = "transition_index"
    else:
        chosen = "transition_index"

    if chosen == "transition_index":
        argument_bits = list(reversed(bits))
    else:
        argument_bits = list(bits)

    note = None
    if not symmetric:
        note = (
            "solve_fsm builds its initial state as (num << 1) + value while walking "
            "state_reg in order, so state_reg[0] becomes the most significant bit, "
            "while the transition table it returns numbers state bit i by state_reg[i] "
            "(plugins/solve_fsm/src/solve_fsm.cpp). The two disagree for the requested "
            "initial state {} of {} bits; hal_fsm used the {!r} encoding. The explored "
            "state set is cross-checked against the relation afterwards.".format(
                state_value, width, chosen
            )
        )
    return argument_bits, chosen, note


# ---------------------------------------------------------------------------
# reachability
# ---------------------------------------------------------------------------


def reachable_states(table, initial_state=None, max_states=None):
    """States reachable from ``initial_state`` in the relation as recovered.

    Returns ``(states, truncated)``.  This is computed here, from the returned
    relation, rather than taken from the solver's own exploration -- which is
    what makes :func:`explored_set_matches` a real check.
    """
    start = table.initial_state if initial_state is None else int(initial_state)
    seen = {start}
    queue = deque([start])
    truncated = False
    while queue:
        state = queue.popleft()
        for target in sorted(table.successors(state)):
            if target in seen:
                continue
            if max_states is not None and len(seen) >= max_states:
                truncated = True
                continue
            seen.add(target)
            queue.append(target)
    return sorted(seen), truncated


def shortest_path(table, target_state, initial_state=None, max_cycles=32):
    """Shortest transition path to ``target_state``, or ``None`` within the bound.

    Returns ``(path, depth_searched)``.  ``path`` is a list of states starting
    at the initial state.  ``None`` means "not found within ``max_cycles``",
    which is never the same statement as "unreachable".
    """
    start = table.initial_state if initial_state is None else int(initial_state)
    target_state = int(target_state)
    if start == target_state:
        return [start], 0
    previous = {start: None}
    frontier = [start]
    depth = 0
    while frontier and depth < max_cycles:
        depth += 1
        next_frontier = []
        for state in frontier:
            for successor in sorted(table.successors(state)):
                if successor in previous:
                    continue
                previous[successor] = state
                if successor == target_state:
                    path = [successor]
                    while previous[path[-1]] is not None:
                        path.append(previous[path[-1]])
                    return list(reversed(path)), depth
                next_frontier.append(successor)
        frontier = next_frontier
    return None, depth


def explored_set_matches(table, initial_state=None):
    """Did the solver explore exactly what the relation says is reachable?

    ``solve_fsm``'s SMT mode explores forward from its own initial state, so the
    keys of the relation it returns must be the states reachable from the
    initial state we asked for (minus dead ends, which have no outgoing edges
    and therefore no key).  A mismatch means the solver started somewhere else
    -- most likely because of the ``initial_state`` encoding described in
    :func:`initial_state_argument` -- and the reachability claim must not be
    made.  Returns ``(matches, only_in_relation, only_in_reachable)``.
    """
    reachable, _ = reachable_states(table, initial_state)
    reachable = set(reachable)
    explored = set(table.states)
    return (
        explored == reachable,
        sorted(explored - reachable),
        sorted(reachable - explored),
    )


# ---------------------------------------------------------------------------
# witnesses
# ---------------------------------------------------------------------------


class WitnessError(RuntimeError):
    """Raised when a witness cannot be produced *and the reason matters*."""

    def __init__(self, message, kind="unknown"):
        RuntimeError.__init__(self, message)
        self.kind = kind


def _assignments(variables, limit):
    if len(variables) > limit:
        raise WitnessError(
            "the condition depends on {} variables, more than the max_condition_vars "
            "limit of {}; no concrete input assignment was searched for".format(
                len(variables), limit
            ),
            kind="limit",
        )
    for value in range(1 << len(variables)):
        yield {name: (value >> index) & 1 for index, name in enumerate(variables)}


def satisfy(table, source, target, evaluate, max_condition_vars=16):
    """Find one input assignment that enables ``source -> target``.

    ``evaluate(source, target, assignment) -> True/False/None``.  ``None`` (the
    evaluator could not decide) is propagated as a :class:`WitnessError` rather
    than silently treated as ``False``.
    """
    transition = table.successors(source).get(target)
    if transition is None:
        raise WitnessError(
            "the recovered relation has no transition {} -> {}".format(source, target),
            kind="missing",
        )
    variables = sorted(transition.variables)
    if not variables:
        result = evaluate(source, target, {})
        if result is None:
            raise WitnessError(
                "could not evaluate the condition of {} -> {}".format(source, target),
                kind="unknown",
            )
        if not result:
            raise WitnessError(
                "the condition of {} -> {} is unsatisfiable as recovered".format(
                    source, target
                ),
                kind="unsatisfiable",
            )
        return {}
    for assignment in _assignments(variables, max_condition_vars):
        result = evaluate(source, target, assignment)
        if result is None:
            raise WitnessError(
                "could not evaluate the condition of {} -> {}".format(source, target),
                kind="unknown",
            )
        if result:
            return assignment
    raise WitnessError(
        "no assignment of {} variable(s) satisfies the condition of {} -> {}".format(
            len(variables), source, target
        ),
        kind="unsatisfiable",
    )


def build_witness(table, path, evaluate, max_condition_vars=16):
    """Turn a state path into a per-cycle input sequence, checking every step.

    Returns a list of ``{"cycle", "source", "target", "condition", "inputs"}``.
    Each step's condition is *evaluated* under the assignment that is reported,
    so a witness that is printed is a witness that was checked.
    """
    steps = []
    for cycle, (source, target) in enumerate(zip(path, path[1:])):
        assignment = satisfy(
            table, source, target, evaluate, max_condition_vars=max_condition_vars
        )
        confirmed = evaluate(source, target, assignment)
        if confirmed is not True:
            raise WitnessError(
                "the assignment found for {} -> {} does not satisfy its own condition; "
                "the recovered relation is not self-consistent".format(source, target),
                kind="inconsistent",
            )
        transition = table.successors(source)[target]
        steps.append(
            {
                "cycle": cycle,
                "source": source,
                "target": target,
                "condition": transition.condition,
                "inputs": dict(assignment),
            }
        )
    return steps


def check_determinism(table, evaluate, states=None, max_condition_vars=16):
    """Is the recovered relation deterministic and total over its inputs?

    For every state, every assignment of the variables its outgoing conditions
    mention is enumerated and exactly one successor must be enabled.  Two
    enabled successors mean the recovery is ambiguous; none means it is
    incomplete.  Either way the transition relation should not be presented as
    a complete description of the machine.

    Returns a dict with ``checked``, ``skipped``, ``nondeterministic`` and
    ``incomplete`` (lists of ``{state, assignment, targets}``).
    """
    report = {
        "checked_states": 0,
        "checked_assignments": 0,
        "skipped_states": [],
        "nondeterministic": [],
        "incomplete": [],
        "undecided": [],
    }
    for state in table.states if states is None else sorted(states):
        successors = table.successors(state)
        if not successors:
            continue
        variables = sorted(
            {name for transition in successors.values() for name in transition.variables}
        )
        if len(variables) > max_condition_vars:
            report["skipped_states"].append(
                {"state": state, "variables": len(variables), "limit": max_condition_vars}
            )
            continue
        report["checked_states"] += 1
        for assignment in _assignments(variables, max_condition_vars):
            report["checked_assignments"] += 1
            enabled = []
            undecided = False
            for target in sorted(successors):
                result = evaluate(state, target, assignment)
                if result is None:
                    undecided = True
                    continue
                if result:
                    enabled.append(target)
            if undecided:
                report["undecided"].append({"state": state, "assignment": dict(assignment)})
            elif len(enabled) > 1:
                report["nondeterministic"].append(
                    {"state": state, "assignment": dict(assignment), "targets": enabled}
                )
            elif not enabled:
                report["incomplete"].append(
                    {"state": state, "assignment": dict(assignment), "targets": []}
                )
    # "ok" means *checked and clean*.  A state that was skipped because it has
    # too many condition variables was not checked, so the relation has not been
    # shown to be deterministic and total -- that is an ``unknown``, not a pass.
    report["ok"] = not (
        report["nondeterministic"]
        or report["incomplete"]
        or report["undecided"]
        or report["skipped_states"]
    )
    return report


# ---------------------------------------------------------------------------
# comparison against a reference
# ---------------------------------------------------------------------------


def compare_tables(recovered, reference_bit_order, reference_edges, restrict_to=None):
    """Compare a recovered table against a reference edge set.

    ``reference_edges`` is an iterable of ``(source, target)`` pairs expressed in
    ``reference_bit_order``.  The recovered table is permuted into the reference
    order first, so a design whose state bits were renamed -- and therefore
    ordered differently -- still compares.  ``restrict_to`` limits the
    comparison to a set of reference states (used to compare a reachable-only
    result against a total reference).

    Returns a dict with ``matches``, ``missing``, ``unexpected`` and the
    permutation that was applied.
    """
    permuted = permute_to(recovered, list(reference_bit_order))
    recovered_edges = {(t.source, t.target) for t in permuted.transitions}
    expected = {(int(a), int(b)) for a, b in reference_edges}
    if restrict_to is not None:
        allowed = {int(state) for state in restrict_to}
        expected = {edge for edge in expected if edge[0] in allowed}
        recovered_edges = {edge for edge in recovered_edges if edge[0] in allowed}
    missing = sorted(expected - recovered_edges)
    unexpected = sorted(recovered_edges - expected)
    return {
        "bit_order": list(reference_bit_order),
        "permutation": bit_permutation(recovered.bit_order, list(reference_bit_order)),
        "expected": len(expected),
        "recovered": len(recovered_edges),
        "matched": len(expected & recovered_edges),
        "missing": [list(edge) for edge in missing],
        "unexpected": [list(edge) for edge in unexpected],
        "matches": not missing and not unexpected,
    }
