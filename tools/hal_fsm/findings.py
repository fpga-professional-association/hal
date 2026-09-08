"""Turning a run into a findings document.

The separation the issue asks for -- "candidate-selection confidence" apart
from "solver-verified transitions" -- is the whole reason this module exists as
its own layer:

* a candidate is a ``heuristic`` finding carrying a ``confidence`` and the
  features that produced it.  It never becomes a proof, no matter how well it
  scores;
* the transition relation is a *separate* finding with status
  ``proven_under_assumptions`` -- and one of its assumptions is the candidate.
  A reader who does not believe the state register does not have to believe the
  transitions either, and the document says so in a machine-readable way rather
  than in prose;
* every limit that was hit downgrades a claim rather than shrinking it: after a
  timeout there is a ``timeout`` finding and *no* transition relation, because
  ``solve_fsm`` returns nothing partial to report.

Nothing here imports ``hal_py``; it works on the plain values produced by
:mod:`hal_fsm.candidates`, :mod:`hal_fsm.transitions` and
:mod:`hal_fsm.solve`, which is what lets the unit tests build entire documents
and validate them without a HAL build.
"""

import os
import sys

if __package__ in (None, ""):  # pragma: no cover - direct execution
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_findings import model
from hal_findings.adapters.common import utc_now

from . import __version__
from .candidates import WEIGHTS
from .transitions import format_state

__all__ = ["MachineResult", "PLUGIN_NAME", "ENTRY_POINT", "build_document", "assumption"]

PLUGIN_NAME = "solve_fsm"
ENTRY_POINT = "solve_fsm.solve_fsm"

_CANDIDATE_METHOD = model.method(
    "state-register candidate proposal",
    "structural",
    False,
    description=(
        "Flip-flops are grouped by the feedback structure of their next-state "
        "functions: strongly connected components of the flip-flop dependency graph, "
        "clusters of self-dependent flip-flops, and -- when available -- DANA register "
        "groups. The score is a weighted sum of published structural features. It is "
        "evidence for a guess, never a proof that these flip-flops are the state "
        "register."
    ),
    parameters={"weights": dict(sorted(WEIGHTS.items()))},
)


def _state_label(value, width):
    """``'10 (2)'`` -- the bit vector a reader needs plus the number the data uses."""
    return "{} ({})".format(format_state(value, width, 2), value)


def _method(kind_name, kind, bounded, description, parameters=None):
    return model.method(kind_name, kind, bounded, description=description, parameters=parameters)


def assumption(assumption_id, description, kind, discharged=None, evidence_list=None):
    """Shorthand with the ``hal_fsm.`` id prefix every assumption here carries."""
    return model.assumption(
        assumption_id if assumption_id.startswith("hal_fsm.") else "hal_fsm." + assumption_id,
        description,
        kind=kind,
        discharged=discharged,
        evidence_list=evidence_list,
    )


class MachineResult(object):
    """Everything one candidate produced, solved or not."""

    def __init__(
        self,
        candidate,
        machine_id,
        outcome="skipped",
        table=None,
        assumptions=(),
        solver=None,
        limits=None,
        error=None,
        unsupported=None,
        cone=None,
        determinism=None,
        reachability=None,
        witnesses=(),
        comparison=None,
        evidence=(),
        metrics=None,
        notes=(),
    ):
        self.candidate = candidate
        self.machine_id = machine_id
        #: ``solved`` | ``error`` | ``timeout`` | ``unsupported`` | ``skipped``
        self.outcome = outcome
        self.table = table
        self.assumptions = list(assumptions)
        self.solver = solver
        self.limits = limits
        self.error = error
        self.unsupported = unsupported
        self.cone = dict(cone or {})
        self.determinism = determinism
        self.reachability = dict(reachability or {})
        self.witnesses = list(witnesses)
        self.comparison = comparison
        self.evidence = list(evidence)
        self.metrics = dict(metrics or {})
        self.notes = list(notes)


def _gate_ref(gate, artifact_id):
    return model.gate_ref(
        artifact_id,
        gate.id,
        gate.name,
        gate_type=gate.type,
        module=dict(gate.module) if gate.module else None,
    )


def _candidate_gate_refs(candidate, graph, artifact_id):
    return [_gate_ref(graph.gates[gate_id], artifact_id) for gate_id in candidate.gate_ids]


def _candidate_finding(index, candidate, graph, artifact_id, ambiguous_with=()):
    gate_refs = _candidate_gate_refs(candidate, graph, artifact_id)
    names = [ref["name"] for ref in gate_refs]
    data = candidate.to_json(graph)
    data["weights"] = dict(sorted(WEIGHTS.items()))
    if ambiguous_with:
        data["ambiguous_with"] = list(ambiguous_with)

    assumptions = None
    confidence = round(candidate.score, 4)
    if candidate.origin == "user_override":
        confidence = None
        assumptions = [
            assumption(
                "state-register-user-selected",
                "the state register was named in the configuration file; hal_fsm did "
                "not derive or check it",
                "user_provided",
                discharged=False,
            )
        ]

    return model.finding(
        "fsm/candidate/{:03d}".format(index),
        "Candidate state register: {} flip-flop(s) [{}]".format(
            len(names), ", ".join(names[:6]) + (" ..." if len(names) > 6 else "")
        ),
        model.STATUS_HEURISTIC,
        _CANDIDATE_METHOD,
        model.scope(
            [artifact_id],
            description="flip-flops proposed as one state register",
            gates=gate_refs,
            gate_types=sorted({ref["type"] for ref in gate_refs if ref.get("type")}) or None,
        ),
        summary=(
            "Proposed by {}. {}".format(
                ", ".join(candidate.sources) if candidate.sources else "the configuration file",
                " ".join(candidate.reasons[:3]),
            )
            if candidate.origin != "user_override"
            else "Selected by the user in the configuration file; not scored."
        ),
        severity="info",
        confidence=confidence,
        assumptions=assumptions,
        metrics={"state_bits": candidate.size},
        data=data,
        tags=["fsm", "candidate", "register-candidate"],
    )


def _ambiguity_finding(tied, graph, artifact_id, margin):
    gates = []
    entries = []
    for candidate in tied:
        gates.extend(_candidate_gate_refs(candidate, graph, artifact_id))
        entries.append(
            {
                "gate_names": candidate.names(graph),
                "gate_ids": list(candidate.gate_ids),
                "score": round(candidate.score, 4),
            }
        )
    return model.finding(
        "fsm/candidates/ambiguous",
        "Candidate selection is ambiguous: {} registers score within {}".format(
            len(tied), margin
        ),
        model.STATUS_HEURISTIC,
        _CANDIDATE_METHOD,
        model.scope(
            [artifact_id],
            description="flip-flops of every candidate that ties for the best score",
            gates=gates,
        ),
        summary=(
            "{} candidate state registers score within the ambiguity margin of {}. The "
            "highest-scoring one was used; the choice between them is not supported by "
            "the structural evidence and should be made in the configuration file "
            "(state_registers).".format(len(tied), margin)
        ),
        severity="medium",
        data={"candidates": entries, "ambiguity_margin": margin},
        tags=["fsm", "candidate", "ambiguous"],
    )


def _transition_data(result):
    table = result.table
    data = {
        "bit_order": list(table.bit_order),
        "bit_order_note": (
            "state bit i is the output of bit_order[i]; a state value is meaningless "
            "without this list"
        ),
        "initial_state": table.initial_state,
        "initial_state_bits": format_state(table.initial_state, table.width, 2),
        "solver_mode": table.solver,
        "states": table.states,
        "transitions": [
            {
                "source": transition.source,
                "target": transition.target,
                "condition": transition.condition,
            }
            for transition in table.transitions
        ],
        "signals": dict(table.signals),
    }
    if result.cone:
        data["transition_cone"] = result.cone
    if result.determinism:
        data["determinism_check"] = {
            key: value
            for key, value in result.determinism.items()
            if key
            in (
                "ok",
                "checked_states",
                "checked_assignments",
                "skipped_states",
                "nondeterministic",
                "incomplete",
                "undecided",
            )
        }
    return data


def _solver_dict(result):
    if not result.solver:
        return None
    solver = dict(result.solver)
    name = solver.pop("name", "solve_fsm")
    version = solver.pop("version", None)
    queries = solver.pop("queries", None)
    wall_time = solver.pop("wall_time_s", None)
    options = solver.pop("options", None) or {}
    options.update({key: value for key, value in solver.items() if value is not None})
    return model.solver(
        name,
        version=version,
        queries=queries,
        wall_time_s=wall_time,
        options=options or None,
    )


def _transitions_finding(result, graph, artifact_id):
    table = result.table
    gate_refs = _candidate_gate_refs(result.candidate, graph, artifact_id)
    method = _method(
        "solve_fsm ({})".format(table.solver),
        "symbolic" if table.solver == "smt" else "formal",
        False,
        description=(
            "solve_fsm builds the next-state function of every state flip-flop from the "
            "transition cone and enumerates successors with an SMT solver, then derives "
            "the condition of each transition symbolically."
            if table.solver == "smt"
            else "solve_fsm_brute_force evaluates the next-state functions for every "
            "state of the encoding and every assignment of their inputs; the result "
            "covers unreachable states as well."
        ),
    )
    return model.finding(
        "fsm/{}/transitions".format(result.machine_id),
        "Recovered state transition relation: {} states, {} transitions".format(
            len(table.states), len(table.transitions)
        ),
        model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
        method,
        model.scope(
            [artifact_id],
            description="the state register and its transition relation",
            gates=gate_refs,
        ),
        summary=(
            "Every transition below was derived from the netlist by {}. The relation is "
            "a statement about the design only as far as the listed assumptions hold -- "
            "in particular the choice of state register, which is a heuristic "
            "(finding fsm/candidate/*).".format(
                "solve_fsm's SMT exploration forward from the initial state"
                if table.solver == "smt"
                else "exhaustive evaluation over the whole state encoding"
            )
        ),
        severity="info",
        assumptions=result.assumptions,
        bounds_dict=model.unbounded(
            description="the transition relation holds in every clock cycle; no cycle "
            "bound is involved"
        ),
        solver_dict=_solver_dict(result),
        limits_dict=model.limits(**result.limits) if result.limits else None,
        evidence_list=result.evidence or None,
        metrics={
            "states": len(table.states),
            "transitions": len(table.transitions),
            "state_bits": table.width,
        },
        data=_transition_data(result),
        tags=["fsm", "transitions", table.solver],
    )


def _reachability_finding(result, graph, artifact_id):
    table = result.table
    reachable = result.reachability.get("reachable", [])
    mismatch = result.reachability.get("explored_mismatch")
    gate_refs = _candidate_gate_refs(result.candidate, graph, artifact_id)
    scope = model.scope(
        [artifact_id],
        description="the states the machine can occupy from its initial state",
        gates=gate_refs,
    )
    method = _method(
        "reachability over the recovered relation",
        "symbolic",
        False,
        description=(
            "Breadth-first closure of the transition relation returned by solve_fsm, "
            "computed by hal_fsm from the relation itself rather than taken from the "
            "solver's own exploration -- which is what makes the cross-check below "
            "meaningful."
        ),
    )
    data = dict(result.reachability)
    data["state_names"] = {
        str(state): format_state(state, table.width, 2) for state in reachable
    }

    if mismatch or not table.complete:
        return model.finding(
            "fsm/{}/reachable-states".format(result.machine_id),
            "Reachable state set could not be established",
            model.STATUS_UNKNOWN,
            method,
            scope,
            summary=(
                "The set of states solve_fsm explored is not the set reachable from the "
                "declared initial state in the relation it returned, or a limit stopped "
                "the run. No claim is made about which states the machine can occupy."
            ),
            severity="medium",
            bounds_dict=model.bounds(False, unroll_depth=0, description="no closure was established"),
            limits_dict=model.limits(**result.limits) if result.limits else None,
            data=data,
            tags=["fsm", "reachability"],
        )

    unreachable = result.reachability.get("unreachable_in_encoding")
    summary = (
        "{} of {} encodable states are reachable from the initial state {}.".format(
            len(reachable), 1 << table.width, _state_label(table.initial_state, table.width)
        )
    )
    if unreachable:
        summary += " {} state(s) are not reachable: {}.".format(
            len(unreachable),
            ", ".join(_state_label(state, table.width) for state in unreachable[:8]),
        )
    elif table.solver == "smt":
        summary += (
            " States outside this set were never visited by the solver; because the "
            "exploration ran to a fixpoint that is a statement about reachability, not "
            "about the encoding as a whole."
        )

    return model.finding(
        "fsm/{}/reachable-states".format(result.machine_id),
        "Reachable state set: {} state(s)".format(len(reachable)),
        model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
        method,
        scope,
        summary=summary,
        severity="info",
        assumptions=result.assumptions,
        bounds_dict=model.unbounded(
            description="the closure was computed to a fixpoint, so it covers every "
            "number of cycles"
        ),
        metrics={"reachable_states": len(reachable), "encodable_states": 1 << table.width},
        data=data,
        tags=["fsm", "reachability"],
    )


def _witness_finding(result, witness, graph, artifact_id):
    table = result.table
    target = witness["target"]
    finding_id = "fsm/{}/witness/{}".format(result.machine_id, target)
    gate_refs = _candidate_gate_refs(result.candidate, graph, artifact_id)
    # "unreachable" comes from the fixpoint closure of the relation, which
    # covers every number of cycles -- like a found witness, and unlike a search
    # that ran out of cycles, it is not a bounded claim.  Calling it one
    # contradicts the unbounded bounds of the same finding, and the schema
    # rejects the document rather than let the two disagree.
    method = _method(
        "bounded reachability witness",
        "symbolic",
        witness.get("status") not in ("found", "unreachable"),
        description=(
            "A breadth-first search over the recovered relation produces the shortest "
            "state path to the target; every step's condition is then solved for a "
            "concrete input assignment and re-evaluated under it, so a witness that is "
            "reported is a witness that was checked."
        ),
    )
    scope = model.scope(
        [artifact_id],
        description="a path from the initial state to state {}".format(target),
        gates=gate_refs,
    )

    if witness.get("status") == "found":
        entries = []
        for step in witness["steps"]:
            for signal, value in sorted(step["inputs"].items()):
                info = table.signals.get(signal, {})
                net = None
                if info.get("net_id"):
                    net = model.net_ref(
                        artifact_id,
                        info["net_id"],
                        info.get("name", signal),
                        role=info.get("role"),
                    )
                entries.append(
                    model.witness_entry(
                        info.get("name", signal), str(value), cycle=step["cycle"], net=net
                    )
                )
        data = {
            "target_state": target,
            "target_state_bits": format_state(target, table.width, 2),
            "initial_state": table.initial_state,
            "path": witness["path"],
            "path_bits": [format_state(state, table.width, 2) for state in witness["path"]],
            "cycles": len(witness["steps"]),
            "steps": witness["steps"],
            "external_state_inputs": witness.get("external_state_inputs", []),
        }
        return model.finding(
            finding_id,
            "State {} is reached in {} cycle(s)".format(
                _state_label(target, table.width), len(witness["steps"])
            ),
            model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
            method,
            scope,
            summary=(
                "Starting from the initial state {}, the recorded input sequence drives "
                "the machine to state {} in {} cycle(s). Each step's condition was "
                "evaluated under the inputs reported here.".format(
                    _state_label(table.initial_state, table.width),
                    _state_label(target, table.width),
                    len(witness["steps"]),
                )
            ),
            severity="info",
            assumptions=result.assumptions,
            bounds_dict=model.unbounded(
                description="a concrete finite path; nothing about it is limited to a "
                "cycle bound"
            ),
            evidence_list=[
                model.evidence(
                    "inline",
                    description="per-cycle input assignment of the witness",
                    inline=witness["steps"],
                )
            ],
            metrics={"cycles": len(witness["steps"])},
            data=data,
            tags=["fsm", "witness", "reachability"],
        )

    if witness.get("status") == "unreachable":
        return model.finding(
            finding_id,
            "State {} is not reachable from the initial state".format(
                _state_label(target, table.width)
            ),
            model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
            method,
            scope,
            summary=(
                "The reachable set was closed to a fixpoint and does not contain state "
                "{}. No input sequence drives the machine there from its initial "
                "state.".format(_state_label(target, table.width))
            ),
            severity="info",
            assumptions=result.assumptions,
            bounds_dict=model.unbounded(
                description="the closure covers every number of cycles"
            ),
            data={"target_state": target, "reachable": result.reachability.get("reachable", [])},
            tags=["fsm", "witness", "reachability"],
        )

    return model.finding(
        finding_id,
        "No witness for state {} within the search limits".format(
            _state_label(target, table.width)
        ),
        model.STATUS_UNKNOWN,
        method,
        scope,
        summary=(
            "{} This is not evidence that the state is unreachable.".format(
                witness.get("reason", "the witness search did not conclude.")
            )
        ),
        severity="low",
        bounds_dict=model.bounds(
            False,
            cycle_bound=witness.get("max_cycles"),
            description="the search covered paths of at most this many transitions",
        )
        if witness.get("max_cycles")
        else None,
        limits_dict=model.limits(
            cycle_limit=witness.get("max_cycles"),
            hit=True,
            description=witness.get("reason"),
        ),
        data={"target_state": target, "reason": witness.get("reason")},
        tags=["fsm", "witness"],
    )


def _determinism_finding(result, graph, artifact_id):
    report = result.determinism
    table = result.table
    method = _method(
        "exhaustive evaluation of the recovered conditions",
        "symbolic",
        False,
        description=(
            "For every state, every assignment of the variables its outgoing conditions "
            "mention is enumerated and the conditions are evaluated. Exactly one "
            "successor must be enabled per assignment: two mean the recovery is "
            "ambiguous, none mean it is incomplete."
        ),
    )
    scope = model.scope(
        [artifact_id],
        description="the outgoing conditions of every recovered state",
        gates=_candidate_gate_refs(result.candidate, graph, artifact_id),
    )
    data = {
        key: report[key]
        for key in ("checked_states", "checked_assignments", "skipped_states")
        if key in report
    }

    if report.get("nondeterministic") or report.get("incomplete"):
        first = (report.get("nondeterministic") or report.get("incomplete"))[0]
        witness = [
            model.witness_entry(
                table.signals.get(name, {}).get("name", name), str(value), cycle=0
            )
            for name, value in sorted(first["assignment"].items())
        ]
        data["nondeterministic"] = report.get("nondeterministic", [])[:16]
        data["incomplete"] = report.get("incomplete", [])[:16]
        return model.finding(
            "fsm/{}/relation-consistency".format(result.machine_id),
            "The recovered transition relation is not a deterministic total function",
            model.STATUS_COUNTEREXAMPLE,
            method,
            scope,
            summary=(
                "In state {} the recorded input assignment enables {} successor(s). A "
                "recovered relation that is not deterministic and total does not "
                "describe the machine; treat the transition finding as incomplete."
                .format(first["state"], len(first.get("targets", [])))
            ),
            severity="high",
            bounds_dict=model.unbounded(
                description="the counterexample is a single-cycle assignment; no cycle "
                "bound is involved"
            ),
            counterexample_dict=model.counterexample(
                "state {} with the recorded inputs enables successors {}".format(
                    first["state"], first.get("targets", [])
                ),
                witness=witness,
                witness_available=True,
            ),
            data=data,
            tags=["fsm", "consistency"],
        )

    if report.get("skipped_states") or report.get("undecided"):
        return model.finding(
            "fsm/{}/relation-consistency".format(result.machine_id),
            "Transition relation consistency could not be checked exhaustively",
            model.STATUS_UNKNOWN,
            method,
            scope,
            summary=(
                "{} state(s) have more condition variables than max_condition_vars "
                "allows, so the relation was not checked for determinism and "
                "totality there.".format(len(report.get("skipped_states", [])))
            ),
            severity="low",
            bounds_dict=model.bounds(
                False, unroll_depth=1, description="a single-cycle check, partially run"
            ),
            limits_dict=model.limits(hit=True, description="max_condition_vars"),
            data=data,
            tags=["fsm", "consistency"],
        )

    return model.finding(
        "fsm/{}/relation-consistency".format(result.machine_id),
        "The recovered transition relation is deterministic and total",
        model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
        method,
        scope,
        summary=(
            "Every one of the {} input assignments enumerated across {} state(s) enables "
            "exactly one successor.".format(
                report.get("checked_assignments", 0), report.get("checked_states", 0)
            )
        ),
        severity="info",
        assumptions=result.assumptions,
        bounds_dict=model.unbounded(
            description="a property of the one-cycle relation, and therefore of every "
            "cycle"
        ),
        metrics={
            "checked_states": report.get("checked_states", 0),
            "checked_assignments": report.get("checked_assignments", 0),
        },
        data=data,
        tags=["fsm", "consistency"],
    )


def _comparison_finding(result, graph, artifact_id):
    report = result.comparison
    method = _method(
        "comparison against a recorded reference",
        "structural",
        False,
        description=(
            "The recovered relation is permuted into the reference's state bit order "
            "(so renamed state bits do not change the comparison) and the two edge sets "
            "are compared exactly."
        ),
    )
    scope = model.scope(
        [artifact_id],
        description="the recovered relation against the reference transition table",
        gates=_candidate_gate_refs(result.candidate, graph, artifact_id),
    )
    if report.get("matches"):
        return model.finding(
            "fsm/{}/reference-comparison".format(result.machine_id),
            "Recovered relation matches the reference ({} transitions)".format(
                report.get("expected")
            ),
            model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
            method,
            scope,
            summary=(
                "After permuting the recovered table into the reference bit order {}, "
                "all {} reference transitions are present and no others.".format(
                    report.get("bit_order"), report.get("expected")
                )
            ),
            severity="info",
            assumptions=result.assumptions,
            bounds_dict=model.unbounded(description="an exact set comparison"),
            metrics={"expected": report.get("expected"), "matched": report.get("matched")},
            data=report,
            tags=["fsm", "reference"],
        )
    return model.finding(
        "fsm/{}/reference-comparison".format(result.machine_id),
        "Recovered relation differs from the reference",
        model.STATUS_COUNTEREXAMPLE,
        method,
        scope,
        summary=(
            "{} reference transition(s) are missing and {} were recovered that the "
            "reference does not contain.".format(
                len(report.get("missing", [])), len(report.get("unexpected", []))
            )
        ),
        severity="high",
        bounds_dict=model.unbounded(description="an exact set comparison"),
        counterexample_dict=model.counterexample(
            "missing {}; unexpected {}".format(
                report.get("missing", [])[:8], report.get("unexpected", [])[:8]
            ),
            witness_available=False,
        ),
        data=report,
        tags=["fsm", "reference"],
    )


def _failure_finding(result, graph, artifact_id):
    gate_refs = _candidate_gate_refs(result.candidate, graph, artifact_id)
    scope = model.scope(
        [artifact_id],
        description="the candidate state register that was handed to the solver",
        gates=gate_refs,
    )
    method = _method(
        "solve_fsm",
        "symbolic",
        result.outcome == "timeout",
        description="the SMT-based state transition graph reconstruction of the "
        "solve_fsm plugin",
    )

    if result.outcome == "timeout":
        return model.finding(
            "fsm/{}/transitions".format(result.machine_id),
            "FSM recovery hit a limit; no transition relation was produced",
            model.STATUS_TIMEOUT,
            method,
            scope,
            summary=(
                "solve_fsm did not finish within the limit. It has no partial result to "
                "return -- a solver timeout makes it fail the whole call -- so nothing "
                "is claimed about this machine's transitions. The structural findings "
                "above stand; the state machine does not."
            ),
            severity="medium",
            bounds_dict=model.bounds(
                False,
                unroll_depth=0,
                description="no state was fully explored",
            ),
            limits_dict=model.limits(**result.limits) if result.limits else model.limits(timeout_s=0),
            evidence_list=result.evidence or None,
            data={"candidate": result.candidate.to_json(graph)},
            tags=["fsm", "timeout"],
        )

    if result.outcome == "unsupported":
        return model.finding(
            "fsm/{}/transitions".format(result.machine_id),
            "FSM recovery does not cover this candidate",
            model.STATUS_UNSUPPORTED,
            method,
            scope,
            summary=result.unsupported.get("reason") if result.unsupported else None,
            severity="low",
            unsupported_dict=model.unsupported(
                (result.unsupported or {}).get("kind", "scale"),
                (result.unsupported or {}).get("reason", "outside the analysis' coverage"),
                (result.unsupported or {}).get("primitives"),
            ),
            data={"candidate": result.candidate.to_json(graph)},
            tags=["fsm", "coverage"],
        )

    return model.finding(
        "fsm/{}/transitions".format(result.machine_id),
        "FSM recovery failed for this candidate",
        model.STATUS_ERROR,
        method,
        scope,
        summary=(
            "solve_fsm returned no result for this candidate. This says nothing about "
            "the design: a wrong candidate, a flip-flop type solve_fsm cannot model, an "
            "unavailable SMT backend and a real solver failure all look like this from "
            "the Python side, and the reason is in HAL's own log."
        ),
        severity="medium",
        error_dict=model.error(
            (result.error or {}).get("kind", "plugin_error"),
            (result.error or {}).get("message", "solve_fsm returned None"),
            detail=(result.error or {}).get("detail"),
        ),
        evidence_list=result.evidence or None,
        data={"candidate": result.candidate.to_json(graph)},
        tags=["fsm", "error"],
    )


def _cone_finding(result, graph, artifact_id):
    holes = result.cone.get("holes") or []
    if not holes:
        return None
    return model.finding(
        "fsm/{}/transition-cone".format(result.machine_id),
        "The transition cone handed to the solver is incomplete",
        model.STATUS_UNSUPPORTED,
        _method(
            "transition cone check",
            "structural",
            False,
            description=(
                "Every variable left in a recovered condition is resolved back to the "
                "net it names. A variable driven by a combinational gate that was not "
                "part of the transition logic is a hole: solve_fsm treated that gate's "
                "output as a free input, so the conditions are wrong."
            ),
        ),
        model.scope(
            [artifact_id],
            description="nets that entered the conditions as free variables although "
            "combinational logic drives them",
            gates=_candidate_gate_refs(result.candidate, graph, artifact_id),
        ),
        summary=(
            "{} net(s) appear as free variables in the recovered conditions although a "
            "combinational gate drives them. The transition logic given to solve_fsm "
            "does not cover the whole cone, so the conditions -- and possibly the "
            "successors -- are not the design's.".format(len(holes))
        ),
        severity="high",
        unsupported_dict=model.unsupported(
            "construct",
            "the transition logic passed to solve_fsm did not close the combinational "
            "cone between the state flip-flops",
        ),
        data={"holes": holes[:32]},
        tags=["fsm", "coverage"],
    )


def _external_state_finding(result, graph, artifact_id):
    """Conditions that read another register's output -- not closed, not wrong."""
    external = result.cone.get("external_state") or []
    if not external:
        return None
    return model.finding(
        "fsm/{}/external-state-dependence".format(result.machine_id),
        "The recovered conditions depend on {} signal(s) from outside the state "
        "register".format(len(external)),
        model.STATUS_UNSUPPORTED,
        _method(
            "condition variable classification",
            "structural",
            False,
            description=(
                "Every variable left in a recovered condition is resolved back to its "
                "net and classified: a primary input, or the output of a flip-flop "
                "outside the state register."
            ),
        ),
        model.scope(
            [artifact_id],
            description="nets driven by flip-flops outside the state register that the "
            "transition conditions read",
            gates=_candidate_gate_refs(result.candidate, graph, artifact_id),
            nets=[
                model.net_ref(
                    artifact_id,
                    entry["net_id"],
                    entry.get("net_name") or "",
                    role="external_state",
                )
                for entry in external
                if entry.get("net_id")
            ]
            or None,
        ),
        summary=(
            "The transition relation is conditioned on {}, which {} drive{}. This is not "
            "necessarily wrong -- a controller may react to a counter -- but the "
            "recovered graph describes this register's behaviour *given* those signals, "
            "not a closed machine driven by inputs alone. If they were meant to be part "
            "of the machine, the state register selection is too small.".format(
                ", ".join(
                    sorted({entry.get("net_name") or "?" for entry in external})[:6]
                ),
                ", ".join(
                    sorted({entry.get("driver_gate_name") or "?" for entry in external})[:6]
                ),
                "s" if len({entry.get("driver_gate_name") for entry in external}) == 1 else "",
            )
        ),
        severity="medium",
        unsupported_dict=model.unsupported(
            "construct",
            "hal_fsm solves one state register at a time; a machine whose transitions "
            "depend on another register is only described relative to that register's "
            "outputs",
        ),
        data={"external_state": external},
        tags=["fsm", "coverage", "scope"],
    )


def _reset_finding(result, graph, artifact_id):
    gaps = result.cone.get("unmodelled_control") or []
    if not gaps:
        return None
    primitives = []
    seen = {}
    for entry in gaps:
        seen.setdefault(entry.get("gate_type") or "<unknown>", []).append(entry)
    for gate_type, entries in sorted(seen.items()):
        primitives.append(
            model.unsupported_primitive(
                gate_type,
                "the {} pin(s) of this flip-flop type are not part of the library's "
                "next-state function, and solve_fsm models only that function; an "
                "asynchronous clear or preset driven by real logic is therefore invisible "
                "in the recovered graph".format(
                    ", ".join(sorted({entry["pin_type"] for entry in entries}))
                ),
                count=len(entries),
            )
        )
    return model.finding(
        "fsm/{}/asynchronous-control".format(result.machine_id),
        "Asynchronous set/reset inputs are driven by logic and are not modelled",
        model.STATUS_UNSUPPORTED,
        _method(
            "control-pin inspection",
            "structural",
            False,
            description="the fan-in of every set/reset pin of the state register is "
            "resolved and classified as constant (inactive) or driven",
        ),
        model.scope(
            [artifact_id],
            description="state flip-flops whose asynchronous control inputs are driven",
            gates=_candidate_gate_refs(result.candidate, graph, artifact_id),
            nets=[
                model.net_ref(
                    artifact_id, entry["net_id"], entry.get("net_name", ""), role=entry["pin_type"]
                )
                for entry in gaps
                if entry.get("net_id")
            ]
            or None,
        ),
        summary=(
            "{} asynchronous control input(s) of the state register are driven by logic "
            "rather than tied to their inactive value. solve_fsm builds its next-state "
            "function from the gate library's next_state expression only, so these "
            "inputs do not appear in any recovered condition. Every transition below is "
            "conditional on them staying inactive.".format(len(gaps))
        ),
        severity="high",
        unsupported_dict=model.unsupported(
            "primitive",
            "asynchronous set/reset behaviour of the state flip-flops is outside the "
            "model solve_fsm uses",
            primitives,
        ),
        data={"control_inputs": gaps},
        tags=["fsm", "reset", "coverage"],
    )


def _uncovered_sequential_finding(graph, covered_ids, artifact_id):
    """Sequential gates no candidate covers -- absence of a finding is not absence."""
    uncovered = {}
    for gate_id in graph.ids():
        if gate_id in covered_ids:
            continue
        gate = graph.gates[gate_id]
        uncovered.setdefault(gate.type or "<unknown>", []).append(gate)
    for gate_id, reason in sorted(graph.unusable.items()):
        gate = graph.gates.get(int(gate_id))
        if gate is not None:
            uncovered.setdefault(gate.type or "<unknown>", [])
    if not uncovered:
        return None
    primitives = []
    for gate_type, gates in sorted(uncovered.items()):
        primitives.append(
            model.unsupported_primitive(
                gate_type,
                "sequential gates of this type are present but are not part of any "
                "state register that was solved; no statement is made about them",
                count=len(gates) or 1,
                properties=list(gates[0].properties) if gates else None,
                example_gates=[_gate_ref(gate, artifact_id) for gate in gates[:3]] or None,
            )
        )
    return model.finding(
        "fsm/coverage/uncovered-sequential-gates",
        "Sequential gates outside every solved state register",
        model.STATUS_UNSUPPORTED,
        _CANDIDATE_METHOD,
        model.scope(
            [artifact_id],
            description="sequential gates that no solved machine covers",
            gate_types=sorted(uncovered),
        ),
        summary=(
            "{} sequential gate type(s) are present in gates that no solved state "
            "register contains. That they produced no FSM is not evidence that they "
            "implement none.".format(len(uncovered))
        ),
        severity="info",
        unsupported_dict=model.unsupported(
            "primitive",
            "hal_fsm only reports the machines it solved; the gates below were either "
            "not proposed as a state register, not selected, or not solvable",
            primitives,
        ),
        tags=["fsm", "coverage"],
    )


def build_document(
    artifact,
    graph,
    candidates,
    results,
    configuration=None,
    ambiguous=(),
    plugin_version="unknown",
    hal_version=None,
    notes=(),
    producer_command=None,
    generated_at=None,
    duration_s=None,
):
    """Assemble the whole findings document for one hal_fsm run."""
    artifact_id = artifact["artifact_id"]
    findings = []

    ambiguous_keys = {candidate.key for candidate in ambiguous}
    for index, candidate in enumerate(candidates, start=1):
        tied_with = (
            [
                other.names(graph)
                for other in ambiguous
                if other.key != candidate.key
            ]
            if candidate.key in ambiguous_keys
            else []
        )
        findings.append(
            _candidate_finding(index, candidate, graph, artifact_id, ambiguous_with=tied_with)
        )
    if len(ambiguous) > 1:
        findings.append(
            _ambiguity_finding(
                ambiguous,
                graph,
                artifact_id,
                (configuration.ambiguity_margin if configuration else 0.05),
            )
        )

    covered = set()
    for result in results:
        covered |= set(result.candidate.gate_ids)
        reset_finding = _reset_finding(result, graph, artifact_id)
        if reset_finding is not None:
            findings.append(reset_finding)
        if result.outcome != "solved" or result.table is None:
            findings.append(_failure_finding(result, graph, artifact_id))
            continue
        cone_finding = _cone_finding(result, graph, artifact_id)
        if cone_finding is not None:
            findings.append(cone_finding)
        external_finding = _external_state_finding(result, graph, artifact_id)
        if external_finding is not None:
            findings.append(external_finding)
        findings.append(_transitions_finding(result, graph, artifact_id))
        if result.determinism is not None:
            findings.append(_determinism_finding(result, graph, artifact_id))
        findings.append(_reachability_finding(result, graph, artifact_id))
        for witness in result.witnesses:
            findings.append(_witness_finding(result, witness, graph, artifact_id))
        if result.comparison is not None:
            findings.append(_comparison_finding(result, graph, artifact_id))

    uncovered = _uncovered_sequential_finding(graph, covered, artifact_id)
    if uncovered is not None:
        findings.append(uncovered)

    analysis = {
        "plugin": {
            "name": PLUGIN_NAME,
            "version": str(plugin_version),
            "description": "state transition graph reconstruction (solve_fsm)",
        },
        "entry_point": ENTRY_POINT,
    }
    if configuration is not None:
        analysis["configuration"] = configuration.to_json()
    if hal_version:
        analysis["hal"] = {"version": str(hal_version)}
    if duration_s is not None:
        analysis["duration_s"] = float(duration_s)

    producer = {"name": "hal_fsm", "version": __version__}
    if producer_command:
        producer["command"] = [str(part) for part in producer_command]

    document_notes = list(notes)
    document_notes.append(
        "state values are relative to the bit order recorded on each transitions "
        "finding; gate references are scoped to artifact {!r}".format(artifact_id)
    )

    return model.document(
        producer,
        [artifact],
        analysis,
        findings,
        generated_at=generated_at if generated_at is not None else utc_now(),
        notes=document_notes,
    )
