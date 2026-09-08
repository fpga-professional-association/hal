"""Comparing one observation point, and turning the answer into a verdict.

The rules this module encodes are the whole point of the tool, so they are
stated once, here, and nowhere else:

* **UNSAT proves equivalence**, under the listed assumptions and only there.
* **SAT refutes it**, but the refutation is only reported as a counterexample
  once the witness has been *replayed* through both cone functions and the two
  results actually differ.  A model that does not reproduce the difference is
  reported as ``unknown``, never as a counterexample.
* **UNKNOWN is unknown.**  If the query hit its timeout it is a ``timeout``,
  otherwise an ``unknown``; in neither case is it equivalence.  This is exactly
  where ``compare_nets`` loses information: it folds an undecided query into
  ``!fail_on_unknown`` and hands back a bare bool.
* **A structural identity is a proof too**, but a different one: if the two
  cones have the same canonical signature they are the same circuit over the
  same boundary variables, and the finding says ``structural`` rather than
  claiming the solver did the work.
* **Anything else -- a missing net, a cone that cannot be built, a solver that
  errored -- is an error or an unsupported case.**  There is no code path in
  this module that turns any of them into "equivalent".

The solver is injected as an *engine*, so the classification above is unit
tested without HAL.  An engine implements::

    cone_function(netlist, cone)  -> opaque function object
    solve_difference(fn_a, fn_b, timeout_s) -> SolveResult
    evaluate(fn, assignment)      -> "0" / "1" / None
    function_text(fn)             -> str
"""

import time

from . import cones

__all__ = [
    "EQUIVALENT",
    "DIFFERENT",
    "UNKNOWN",
    "TIMEOUT",
    "ERROR",
    "UNSUPPORTED",
    "STATUSES",
    "INCONCLUSIVE",
    "SolveResult",
    "Options",
    "PointOutcome",
    "compare_point",
    "compare_points",
]

EQUIVALENT = "equivalent"
DIFFERENT = "different"
UNKNOWN = "unknown"
TIMEOUT = "timeout"
ERROR = "error"
#: The point cannot be compared at all -- e.g. its cone rests on a state element
#: the correspondence does not cover.  Emphatically not "different".
UNSUPPORTED = "unsupported"

STATUSES = (EQUIVALENT, DIFFERENT, UNKNOWN, TIMEOUT, ERROR, UNSUPPORTED)
#: Everything that is neither a proof nor a refutation.
INCONCLUSIVE = (UNKNOWN, TIMEOUT, ERROR, UNSUPPORTED)


class SolveResult(object):
    """What an engine returns for one query.

    ``outcome`` is ``"unsat"`` (the functions are equal), ``"sat"`` (they
    differ), ``"unknown"`` or ``"error"``.  ``model`` maps variable name to a
    string value; ``timed_out`` says whether the solver was killed by the
    timeout, which is the difference between ``timeout`` and ``unknown``.
    """

    def __init__(self, outcome, model=None, timed_out=False, message=None, wall_time_s=None):
        self.outcome = outcome
        self.model = dict(model) if model else None
        self.timed_out = bool(timed_out)
        self.message = message
        self.wall_time_s = wall_time_s


class Options(object):
    """Knobs of one comparison run."""

    def __init__(
        self,
        solver_timeout_s=10,
        max_cone_gates=4096,
        structural_fast_path=False,
        witness=True,
        max_witness_entries=64,
    ):
        self.solver_timeout_s = int(solver_timeout_s)
        self.max_cone_gates = int(max_cone_gates)
        self.structural_fast_path = bool(structural_fast_path)
        self.witness = bool(witness)
        self.max_witness_entries = int(max_witness_entries)

    def as_dict(self):
        return {
            "solver_timeout_s": self.solver_timeout_s,
            "max_cone_gates": self.max_cone_gates,
            "structural_fast_path": self.structural_fast_path,
            "witness": self.witness,
            "max_witness_entries": self.max_witness_entries,
        }


class PointOutcome(object):
    """The verdict for one observation point, with everything it rests on."""

    def __init__(self, point, status, **kwargs):
        self.point = point
        self.status = status
        self.cone_a = kwargs.get("cone_a")
        self.cone_b = kwargs.get("cone_b")
        self.method = kwargs.get("method", "formal")
        self.rationale = kwargs.get("rationale", "")
        self.structurally_identical = kwargs.get("structurally_identical")
        self.witness = list(kwargs.get("witness") or [])
        self.witness_available = kwargs.get("witness_available")
        self.replay = kwargs.get("replay")
        self.error_kind = kwargs.get("error_kind")
        self.error_message = kwargs.get("error_message")
        self.unsupported_reason = kwargs.get("unsupported_reason")
        self.wall_time_s = kwargs.get("wall_time_s")
        self.solver_outcome = kwargs.get("solver_outcome")
        self.timed_out = bool(kwargs.get("timed_out", False))
        self.cross_check = kwargs.get("cross_check")
        self.notes = list(kwargs.get("notes") or [])
        #: The two cone functions as text, when the engine could render them.
        self.function_text_a = kwargs.get("function_text_a")
        self.function_text_b = kwargs.get("function_text_b")

    @property
    def is_equivalent(self):
        return self.status == EQUIVALENT

    def changed_gates(self):
        """``(gates only in A's cone, gates only in B's cone)`` by sub-cone signature."""
        if self.cone_a is None or self.cone_b is None:
            return [], []
        return (
            self.cone_a.gates_with_signature_not_in(self.cone_b),
            self.cone_b.gates_with_signature_not_in(self.cone_a),
        )

    def summary(self):
        data = {
            "point": self.point.key,
            "kind": self.point.kind,
            "label": self.point.label,
            "status": self.status,
            "method": self.method,
        }
        if self.cone_a is not None:
            data["cone_a"] = self.cone_a.summary()
        if self.cone_b is not None:
            data["cone_b"] = self.cone_b.summary()
        if self.structurally_identical is not None:
            data["structurally_identical"] = self.structurally_identical
        if self.solver_outcome:
            data["solver_outcome"] = self.solver_outcome
        if self.replay is not None:
            data["replay"] = self.replay
        if self.cross_check is not None:
            data["cross_check"] = self.cross_check
        if self.wall_time_s is not None:
            data["wall_time_s"] = self.wall_time_s
        if self.notes:
            data["notes"] = list(self.notes)
        return data


def _text_of(engine, function, limit=20000):
    """Render a cone function as text, defensively: it is evidence, not a result."""
    renderer = getattr(engine, "function_text", None)
    if renderer is None:
        return None
    try:
        text = renderer(function)
    except Exception:  # noqa: BLE001 - a function that will not print is not a failure
        return None
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[:limit] + " ...[truncated]"


def _fill_assignment(model, function_variables):
    """Complete a solver model for one function; report what had to be filled.

    A solver only reports the variables it needed.  Evaluating a function needs
    all of them, so the missing ones are pinned to ``0`` -- and recorded, so a
    reader can see that the witness is one of many rather than the only one.
    """
    assignment = {}
    filled = []
    for name in sorted(function_variables):
        if name in model:
            assignment[name] = model[name]
        else:
            assignment[name] = "0"
            filled.append(name)
    return assignment, filled


def _witness_entries(model, limit):
    entries = [
        {"signal": name, "value": str(value)} for name, value in sorted(model.items())
    ]
    truncated = len(entries) > limit
    return entries[:limit], truncated


def _uncovered_state_boundaries(cone, unmatched_names):
    """Boundary variables that are state the correspondence does not cover.

    A cone that reads a register with no counterpart cannot be compared: the
    two functions then range over *different* free variables and a solver will
    happily report a difference that is a gap in the mapping, not a change in
    the design.  This is the check that keeps those two apart.
    """
    if cone is None or not unmatched_names:
        return []
    from hal_findings.adapters.common import call as _call

    uncovered = []
    for boundary in cone.boundaries:
        if boundary.kind != cones.BOUNDARY_SEQUENTIAL or boundary.gate is None:
            continue
        if _call(boundary.gate, "get_name", default="") in unmatched_names:
            uncovered.append(boundary)
    return uncovered


def compare_point(engine, netlist_a, netlist_b, point, mapping, options, unmatched=None):
    """Compare one observation point and return a :class:`PointOutcome`."""
    started = time.time()
    unmatched_a, unmatched_b = unmatched or (frozenset(), frozenset())

    rename = mapping.rename_boundary if mapping is not None else None
    try:
        cone_a = cones.extract_cone(
            netlist_a, point.net_a, max_gates=options.max_cone_gates, rename=rename
        )
    except cones.ConeError as exc:
        return PointOutcome(
            point,
            ERROR,
            error_kind=exc.kind,
            error_message="netlist A: {}".format(exc),
            rationale="the cone of this point could not be built in build A",
            wall_time_s=round(time.time() - started, 4),
        )
    try:
        cone_b = cones.extract_cone(
            netlist_b, point.net_b, max_gates=options.max_cone_gates
        )
    except cones.ConeError as exc:
        return PointOutcome(
            point,
            ERROR,
            cone_a=cone_a,
            error_kind=exc.kind,
            error_message="netlist B: {}".format(exc),
            rationale="the cone of this point could not be built in build B",
            wall_time_s=round(time.time() - started, 4),
        )

    structurally_identical = cone_a.signature == cone_b.signature
    notes = []

    uncovered = _uncovered_state_boundaries(cone_a, unmatched_a) + _uncovered_state_boundaries(
        cone_b, unmatched_b
    )
    if uncovered:
        return PointOutcome(
            point,
            UNSUPPORTED,
            cone_a=cone_a,
            cone_b=cone_b,
            structurally_identical=structurally_identical,
            unsupported_reason=(
                "the cone of this point reads {} state element(s) that the "
                "correspondence does not map ({}); the two functions would range over "
                "different free variables, so a solver verdict about them would be a "
                "statement about the mapping, not about the design".format(
                    len(uncovered), ", ".join(sorted({b.name for b in uncovered})[:6])
                )
            ),
            rationale=(
                "not compared: the point depends on unmapped state. This is a gap in "
                "the correspondence, not evidence of a behavioural change -- and not "
                "evidence of equivalence either."
            ),
            wall_time_s=round(time.time() - started, 4),
        )

    unmodelled = [
        boundary
        for cone in (cone_a, cone_b)
        for boundary in cone.boundaries
        if boundary.kind in (cones.BOUNDARY_UNMODELLED, cones.BOUNDARY_DANGLING)
    ]
    if unmodelled:
        notes.append(
            "{} boundary net(s) are neither a top-level input nor driven by a "
            "sequential gate; their correspondence rests on the net name alone: "
            "{}".format(len(unmodelled), ", ".join(sorted({b.name for b in unmodelled})[:8]))
        )

    if structurally_identical and options.structural_fast_path:
        return PointOutcome(
            point,
            EQUIVALENT,
            cone_a=cone_a,
            cone_b=cone_b,
            method="structural",
            structurally_identical=True,
            rationale=(
                "the two cones have the same canonical signature: identical gate "
                "types wired identically to identically named boundary variables, "
                "so they implement the same function without a solver query"
            ),
            wall_time_s=round(time.time() - started, 4),
            notes=notes,
        )

    try:
        function_a = engine.cone_function(netlist_a, cone_a)
        function_b = engine.cone_function(netlist_b, cone_b)
    except Exception as exc:  # noqa: BLE001 - a failed build is a failed point
        return PointOutcome(
            point,
            ERROR,
            cone_a=cone_a,
            cone_b=cone_b,
            structurally_identical=structurally_identical,
            error_kind="plugin_error",
            error_message="could not build the cone function: {}: {}".format(
                type(exc).__name__, exc
            ),
            rationale="the Boolean function of one of the cones could not be built",
            wall_time_s=round(time.time() - started, 4),
            notes=notes,
        )

    result = engine.solve_difference(function_a, function_b, options.solver_timeout_s)
    elapsed = round(time.time() - started, 4)

    common = {
        "cone_a": cone_a,
        "cone_b": cone_b,
        "structurally_identical": structurally_identical,
        "wall_time_s": elapsed,
        "solver_outcome": result.outcome,
        "notes": notes,
        "function_text_a": _text_of(engine, function_a),
        "function_text_b": _text_of(engine, function_b),
    }

    if result.outcome == "unsat":
        return PointOutcome(
            point,
            EQUIVALENT,
            method="formal",
            rationale=(
                "the solver proved that no assignment of the shared boundary "
                "variables makes the two cone functions differ"
            ),
            **common
        )

    if result.outcome == "unknown":
        status = TIMEOUT if result.timed_out else UNKNOWN
        return PointOutcome(
            point,
            status,
            method="formal",
            timed_out=result.timed_out,
            rationale=(
                "the solver returned 'unknown' after {} s; this is not evidence of "
                "equivalence and is not reported as such".format(options.solver_timeout_s)
                if result.timed_out
                else "the solver returned 'unknown'; no verdict can be derived from it"
            ),
            **common
        )

    if result.outcome == "error":
        return PointOutcome(
            point,
            ERROR,
            method="formal",
            error_kind="plugin_error",
            error_message=result.message or "the solver query failed",
            rationale="the solver query failed, which says nothing about the design",
            **common
        )

    # SAT: the functions differ. Now try to keep -- and verify -- a witness.
    model = result.model or {}
    if not options.witness or not model:
        return PointOutcome(
            point,
            DIFFERENT,
            method="formal",
            witness_available=False,
            rationale=(
                "the solver found the two cone functions to differ but no model was "
                "retained, so the difference is reported without an input witness"
            ),
            **common
        )

    variables_a = engine.function_variables(function_a)
    variables_b = engine.function_variables(function_b)
    assignment_a, filled_a = _fill_assignment(model, variables_a)
    assignment_b, filled_b = _fill_assignment(model, variables_b)

    value_a = engine.evaluate(function_a, assignment_a)
    value_b = engine.evaluate(function_b, assignment_b)
    replay = {
        "value_a": value_a,
        "value_b": value_b,
        "confirmed": bool(value_a is not None and value_b is not None and value_a != value_b),
        "defaulted_variables": sorted(set(filled_a) | set(filled_b)),
    }

    if not replay["confirmed"]:
        return PointOutcome(
            point,
            UNKNOWN,
            method="formal",
            witness_available=False,
            replay=replay,
            rationale=(
                "the solver reported a difference but replaying its model through both "
                "cone functions did not reproduce one ({!r} vs {!r}); the result is "
                "reported as unknown rather than as a counterexample".format(
                    value_a, value_b
                )
            ),
            **common
        )

    entries, truncated = _witness_entries(model, options.max_witness_entries)
    if truncated:
        notes.append(
            "the witness lists the first {} of {} assigned variables".format(
                options.max_witness_entries, len(model)
            )
        )
    return PointOutcome(
        point,
        DIFFERENT,
        method="formal",
        witness=entries,
        witness_available=True,
        replay=replay,
        rationale=(
            "the solver found an assignment of the shared boundary variables under "
            "which the two cones evaluate to {} and {}; the assignment was replayed "
            "through both functions to confirm it".format(value_a, value_b)
        ),
        **common
    )


def compare_points(
    engine, netlist_a, netlist_b, points, mapping, options, progress=None, unmatched=None
):
    """Compare every observation point, in order. Returns a list of outcomes."""
    outcomes = []
    for index, point in enumerate(points):
        if progress is not None:
            progress(index, len(points), point)
        outcomes.append(
            compare_point(
                engine, netlist_a, netlist_b, point, mapping, options, unmatched=unmatched
            )
        )
    return outcomes
