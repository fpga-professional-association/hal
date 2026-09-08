"""Turn an engine report into a schema-valid ``hal_findings`` document.

The mapping from engine outcome to finding status is written out once, here,
and nowhere else:

===========================  ==========================  ===================
engine outcome               finding status              why
===========================  ==========================  ===================
``holds_bounded``            ``proven_bounded``          no violating run of ≤ *k* cycles exists, and the obligation *was* exercised
``violated``                 ``bounded_counterexample``  a witness exists inside the bound, and it replays
``vacuous``                  ``unknown``                 the obligation never fired: a pass, but not evidence
``not_instantiable``         ``unknown``                 the bound is smaller than the obligation's lookahead
``unsupported``              ``unsupported``             the design does not provide the signals the policy names
``timeout``                  ``timeout``                 the search budget ran out
``error``                    ``error``                   the analysis itself broke
===========================  ==========================  ===================

``proven_under_assumptions`` never appears. This is bounded model checking, not
induction: there is no way to earn an unbounded claim here, and the schema would
happily let the code pretend otherwise.

Structural cone results are a separate class of finding with status
``heuristic`` and *candidate* in the title. They are never merged with an
obligation's verdict, because "the interface can structurally reach this
register" and "the interface can write this register while it is locked" are
different statements and only the second one is a vulnerability.
"""

import importlib
import os
import sys

from . import VERSION, PRODUCER_NAME
from .engine import CheckOutcome
from . import properties as properties_module
from . import witness as witness_module

__all__ = [
    "FINDINGS_IMPORT_ERROR",
    "findings_package",
    "findings_model",
    "build_document",
    "build_unsupported_document",
]

FINDINGS_IMPORT_ERROR = (
    "tools/hal_findings is not importable. Run the checker from the repository root, or add "
    "the 'tools' directory to PYTHONPATH: the findings schema is a hard dependency, not an "
    "optional formatter."
)


def _import(module_name):
    try:
        return importlib.import_module(module_name)
    except ImportError:
        tools_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
    try:
        return importlib.import_module(module_name)
    except ImportError as error:
        raise ImportError("{} ({})".format(FINDINGS_IMPORT_ERROR, error))


def findings_package():
    return _import("hal_findings")


def findings_model():
    return _import("hal_findings.model")


#: Assumptions true of *every* result this analysis produces.
TOOL_ASSUMPTIONS = (
    (
        "secprop/tool/cycle-abstraction",
        "tool",
        "One step of the model is one active edge of the single declared clock. Clock "
        "trees, gated or divided clocks, multi-clock crossings, setup/hold timing, "
        "glitches and X-propagation are outside the model, so no result here says "
        "anything about them.",
    ),
    (
        "secprop/tool/async-reset-sampled",
        "tool",
        "Asynchronous set/clear inputs of flip-flops are sampled in the current cycle and "
        "applied at the edge, which is a synchronous over-approximation of the real "
        "asynchronous behaviour.",
    ),
    (
        "secprop/tool/policy-is-trusted",
        "user_provided",
        "The sensitive registers, the lock state and the external write controls are "
        "whatever the policy file says they are. Nothing is inferred from net names, so a "
        "wrong policy produces a confidently wrong answer.",
    ),
    (
        "secprop/tool/no-initial-state",
        "initial_state",
        "Registers start unconstrained; the declared reset sequence is what establishes a "
        "known state, and obligations are only instantiated once it has settled.",
    ),
    (
        "secprop/env/single-clock",
        "environment",
        "The design is treated as a single synchronous clock domain, as the policy "
        "declares. A second domain would need a different abstraction, not a larger bound.",
    ),
)


def _assumptions(model, report):
    policy = report.policy
    entries = [
        model.assumption(identifier, description, kind=kind)
        for identifier, kind, description in TOOL_ASSUMPTIONS
    ]
    entries.append(
        model.assumption(
            "secprop/env/reset-sequence",
            "{} is held {} for the first {} cycle(s) of every run and released "
            "afterwards.".format(
                policy.reset_signal,
                "low" if policy.reset_active_low else "high",
                policy.reset_cycles,
            ),
            kind="environment",
        )
    )
    for signal in sorted(report.diagnostics.get("quiescent_inputs_applied") or {}):
        value = report.diagnostics["quiescent_inputs_applied"][signal]
        entries.append(
            model.assumption(
                "secprop/env/quiescent:{}".format(signal),
                "Input {} is pinned at {} for every cycle of every run, as the policy's "
                "environment declares. A run in which it moves is outside what these "
                "results cover.".format(signal, value),
                kind="environment",
                discharged=False,
            )
        )
    if report.diagnostics.get("unresolved_signals"):
        entries.append(
            model.assumption(
                "secprop/env/unresolved-signals",
                "The policy names design signal(s) {} that the extracted model does not "
                "contain; every obligation that needs them was reported as "
                "unsupported.".format(", ".join(report.diagnostics["unresolved_signals"])),
                kind="structural",
            )
        )
    return entries


def _method(model, report, prop=None, kind="bounded_formal", name=None, description=None):
    parameters = {
        "bound": report.bound,
        "policy": report.policy.name,
        "clock": report.policy.clock_signal,
        "reset_cycles": report.policy.reset_cycles,
        "lock": report.policy.lock_signal,
    }
    if prop is not None:
        parameters["obligation"] = prop.id
        parameters["horizon_cycles"] = prop.horizon
    return model.method(
        name or "secprop_bounded_model_checking",
        kind,
        kind != "structural",
        description=description
        or (
            "Bounded model checking of one policy obligation over a {}-cycle unrolling of "
            "the design's transition relation, under the environment declared by the "
            "policy.".format(report.bound)
        ),
        parameters=parameters,
    )


def _solver(model, report):
    if report.solver is None:
        return None
    return model.solver(
        "hal_apb_check.sat (CDCL)",
        version=VERSION,
        queries=report.solver.queries,
        sat=report.solver.sat_count,
        unsat=report.solver.unsat_count,
        unknown=report.solver.budget_count,
        options={
            "decision_limit": report.solver.decision_limit,
            "conflict_limit": report.solver.conflict_limit,
            "timeout_s": report.solver.timeout_s,
        },
    )


def _limits(model, report, hit=False, description=None):
    return model.limits(
        timeout_s=report.solver.timeout_s if report.solver else None,
        query_limit=report.solver.conflict_limit if report.solver else None,
        cycle_limit=report.bound,
        hit=hit,
        description=description,
    )


def _coverage(result, report):
    cycles = result.detail.get("cycles")
    metrics = {
        "cycle_bound": report.bound,
        "horizon_cycles": result.detail.get("horizon"),
        "instantiated_from_cycle": cycles[0] if cycles else None,
        "instantiated_to_cycle": cycles[1] if cycles else None,
        "instantiated_cycle_count": (cycles[1] - cycles[0] + 1) if cycles else 0,
        "uncovered_tail_cycles": (report.bound - cycles[1]) if cycles else report.bound,
    }
    return {key: value for key, value in metrics.items() if value is not None}


_STATUS_FOR = {
    CheckOutcome.HOLDS_BOUNDED: "proven_bounded",
    CheckOutcome.VIOLATED: "bounded_counterexample",
    CheckOutcome.VACUOUS: "unknown",
    CheckOutcome.NOT_INSTANTIABLE: "unknown",
    CheckOutcome.UNSUPPORTED: "unsupported",
    CheckOutcome.TIMEOUT: "timeout",
    CheckOutcome.ERROR: "error",
}


def _cone_findings(model, report, scope_for):
    """Candidate reachability -- heuristic, and labelled as such everywhere."""
    findings = []
    if report.cones is None:
        return findings
    policy = report.policy
    controls = sorted(
        {signal for signals in policy.interface_signals.values() for signal in signals}
    )
    method = _method(
        model,
        report,
        kind="structural",
        name="secprop_structural_cone",
        description=(
            "Transitive fan-in cone of the target registers over the flattened "
            "next-state functions. This reports which external controls *can* influence "
            "a register and through how many register stages. It is candidate "
            "reachability: it is not evidence that a policy can be violated, and the "
            "bounded checks in this same document are what decide that."
        ),
    )
    for target in sorted(report.cones.cones):
        cone = report.cones.cones[target]
        data = cone.to_dict(external_controls=controls, lock_signal=policy.lock_signal)
        reachable = data["external_controls_in_cone"]
        findings.append(
            model.finding(
                "secprop/candidate-reachability/{}".format(target),
                "candidate path from the external interface to {}".format(target),
                "heuristic",
                method,
                scope_for("structural fan-in cone of {}".format(target)),
                summary=(
                    "{} external write control(s) appear in the fan-in cone of {}: {}. "
                    "This is a candidate path only. Whether any of them can actually "
                    "change {} while the lock is engaged is decided by "
                    "secprop/locked-write-blocked/{}, not here.".format(
                        len(reachable),
                        target,
                        ", ".join(reachable) or "none",
                        target,
                        target,
                    )
                    if reachable
                    else (
                        "No declared external write control appears in the fan-in cone of "
                        "{}. That is still not a safety verdict: it is a statement about "
                        "this extracted model under the policy's own signal list, and the "
                        "bounded checks in this document are what carry the claim.".format(
                            target
                        )
                    )
                ),
                severity="info",
                # Structural reachability is a filter, not a measurement. The
                # number is deliberately low and deliberately constant: it says
                # "treat this as a lead", not "this is 30% likely to be a bug".
                confidence=0.3,
                metrics={
                    "cone_inputs": data["input_count"],
                    "cone_states": data["state_count"],
                    "external_controls_in_cone": len(reachable),
                },
                data=data,
                tags=["secprop", "structural", "candidate", "reachability"],
            )
        )
    return findings


def build_document(
    report,
    design_artifact,
    policy_artifact,
    evidence_by_property=None,
    net_refs=None,
    witness_entries_by_property=None,
    command=None,
    generated_at=None,
):
    """Build the findings document for ``report``."""
    model = findings_model()
    policy = report.policy
    evidence_by_property = evidence_by_property or {}
    witness_entries_by_property = witness_entries_by_property or {}
    artifact_ids = [design_artifact["artifact_id"], policy_artifact["artifact_id"]]
    shared_evidence = list(evidence_by_property.get("*", []))

    net_refs = net_refs or {}

    def scope_for(description, signals=()):
        """Bind a finding to the artifacts and, where known, to the exact nets.

        ``net_refs`` is populated by the ``hal_py`` front end, where a net id is
        meaningful and scoped to the design artifact. The offline reader has
        only reader-local ids, so it contributes none and the scope stays at
        artifact granularity rather than pointing at numbers that mean nothing
        outside this process.
        """
        nets = [net_refs[name] for name in signals if name in net_refs]
        return model.scope(artifact_ids, description=description, nets=nets or None)

    findings = []

    for result in report.results:
        prop = result.property
        status = _STATUS_FOR[result.outcome]
        detail = result.detail
        evidence = shared_evidence + list(evidence_by_property.get(prop.id, []))
        common = {
            "summary": prop.description,
            "method_dict": _method(model, report, prop),
            "scope_dict": scope_for(
                "{} on design {} under policy {}".format(
                    prop.register or "the lock state", report.system.name, policy.name
                ),
                signals=prop.signals,
            ),
            "solver_dict": _solver(model, report),
            "evidence_list": evidence or None,
            "metrics": _coverage(result, report),
            "tags": [
                "secprop",
                "kind:" + prop.kind,
                "policy:" + policy.name,
            ]
            + (["register:" + prop.register] if prop.register else []),
        }

        if status == "proven_bounded":
            findings.append(
                model.finding(
                    prop.id,
                    prop.title,
                    status,
                    severity="info",
                    assumptions=_assumptions(model, report),
                    bounds_dict=model.bounded(
                        report.bound,
                        description=(
                            "No run of at most {} cycles violates this obligation, and the "
                            "obligation was exercised inside that bound. Nothing is "
                            "claimed about longer runs.".format(report.bound)
                        ),
                    ),
                    data={
                        "instantiated_cycles": detail.get("cycles"),
                        "exercised": bool(detail.get("exercised")),
                    },
                    **common
                )
            )
        elif status == "bounded_counterexample":
            witness_depth = min(
                report.bound, detail["violation_cycle"] + max(1, detail.get("horizon", 0))
            )
            # A refutation without a witness is an accusation. The entries are
            # derived from the trace that already replayed, so they are always
            # available here; the caller may pass richer ones (with net refs).
            entries = witness_entries_by_property.get(prop.id)
            if entries is None:
                entries = witness_module.witness_entries(
                    model, policy, prop, detail.get("trace") or [], net_refs
                )
            findings.append(
                model.finding(
                    prop.id,
                    prop.title,
                    status,
                    severity=prop.severity,
                    assumptions=_assumptions(model, report),
                    bounds_dict=model.bounded(report.bound),
                    counterexample_dict=model.counterexample(
                        "{} is violated at cycle {} of a {}-cycle run that satisfies the "
                        "declared reset schedule and environment. Bits affected: {}. The "
                        "witness is exported as a replayable transaction sequence and was "
                        "re-simulated before this finding was written.".format(
                            prop.id,
                            detail["violation_cycle"],
                            report.bound,
                            ", ".join(detail.get("failing_bits") or []) or "see the trace",
                        ),
                        cycle_bound=max(1, witness_depth),
                        witness=entries,
                        witness_available=bool(entries),
                        evidence_list=evidence or None,
                    ),
                    data={
                        "violation_cycle": detail["violation_cycle"],
                        "failing_cycles": detail.get("failing_cycles"),
                        "failing_bits": detail.get("failing_bits"),
                        "exercised": bool(detail.get("exercised")),
                        "unconstrained_variables": detail.get("unconstrained") or [],
                    },
                    **common
                )
            )
        elif status == "unknown":
            vacuous = result.outcome == CheckOutcome.VACUOUS
            findings.append(
                model.finding(
                    prop.id,
                    prop.title,
                    status,
                    severity="medium" if vacuous else "low",
                    assumptions=_assumptions(model, report),
                    bounds_dict=model.bounded(
                        report.bound,
                        description=(
                            "Vacuous: within {} cycles the obligation is never exercised, "
                            "so the absence of a violation is not evidence.".format(
                                report.bound
                            )
                            if vacuous
                            else "The obligation could not be instantiated inside this bound."
                        ),
                    ),
                    data={
                        "vacuous": vacuous,
                        "overconstrained": bool(detail.get("overconstrained")),
                        "reason": detail.get("reason"),
                        "instantiated_cycles": detail.get("cycles"),
                    },
                    **common
                )
            )
        elif status == "unsupported":
            findings.append(
                model.finding(
                    prop.id,
                    prop.title,
                    status,
                    severity="medium",
                    unsupported_dict=model.unsupported(
                        "configuration", detail.get("reason", "not covered")
                    ),
                    data={"missing_signals": detail.get("missing_signals") or []},
                    **common
                )
            )
        elif status == "timeout":
            budget = detail.get("budget") or {}
            findings.append(
                model.finding(
                    prop.id,
                    prop.title,
                    status,
                    severity="medium",
                    assumptions=_assumptions(model, report),
                    bounds_dict=model.bounded(report.bound),
                    limits_dict=_limits(
                        model,
                        report,
                        hit=True,
                        description="the {} search hit its {} budget of {}".format(
                            detail.get("phase", "violation"),
                            budget.get("kind", "search"),
                            budget.get("limit"),
                        ),
                    ),
                    data={"budget": budget, "instantiated_cycles": detail.get("cycles")},
                    **common
                )
            )
        else:
            findings.append(
                model.finding(
                    prop.id,
                    prop.title,
                    status,
                    severity="high",
                    error_dict=model.error(
                        "internal",
                        detail.get("message", "the check failed"),
                        detail=detail.get("error_detail"),
                    ),
                    **common
                )
            )

    findings.extend(_cone_findings(model, report, scope_for))

    # ---- coverage gaps in the policy itself -------------------------------
    for register in policy.registers:
        unchecked = register.bits_without_reset_value()
        if not unchecked or not register.wants("reset_clears"):
            continue
        findings.append(
            model.finding(
                "secprop/coverage/reset-value-unknown/{}".format(register.name),
                "{} has bit(s) with no declared reset value".format(register.name),
                "unsupported",
                _method(model, report, kind="structural", name="secprop_policy_coverage"),
                scope_for("reset coverage of {}".format(register.name)),
                summary=(
                    "The policy declares no reset value for {}, so the reset-clearing "
                    "obligation was not checked for {}. A missing reset value is not "
                    "assumed to be zero.".format(
                        ", ".join(bit.name for bit in unchecked),
                        "them" if len(unchecked) > 1 else "it",
                    )
                ),
                severity="medium",
                unsupported_dict=model.unsupported(
                    "configuration",
                    "no reset_value in the policy for bit(s) {}".format(
                        ", ".join(bit.name for bit in unchecked)
                    ),
                ),
                data={"bits": [bit.name for bit in unchecked]},
                tags=["secprop", "coverage", "policy"],
            )
        )

    if report.diagnostics.get("overconstrained"):
        findings.append(
            model.finding(
                "secprop/diagnostics/overconstrained",
                "the declared environment admits no run at all",
                "unknown",
                _method(model, report),
                scope_for("environment declared by policy {}".format(policy.name)),
                summary=(
                    "Every obligation in this document was downgraded: within {} cycles "
                    "the reset schedule and the quiescent-input assumptions admit no run, "
                    "so a clean report would mean nothing.".format(report.bound)
                ),
                severity="high",
                assumptions=_assumptions(model, report),
                bounds_dict=model.bounded(report.bound),
                solver_dict=_solver(model, report),
                data={
                    "environment_satisfiable": report.diagnostics.get(
                        "environment_satisfiable"
                    ),
                    "quiescent_inputs_applied": report.diagnostics.get(
                        "quiescent_inputs_applied"
                    ),
                },
                tags=["secprop", "diagnostics", "overconstrained"],
            )
        )

    for exclusion in properties_module.EXCLUSIONS:
        findings.append(
            model.finding(
                exclusion["id"],
                exclusion["title"],
                "unsupported",
                model.method(
                    "secprop_coverage_declaration",
                    "structural",
                    False,
                    description="A declared limit of this analysis, not a result about the "
                    "design.",
                ),
                scope_for("coverage of the security property checker itself"),
                summary=exclusion["reason"],
                severity="info",
                unsupported_dict=model.unsupported(exclusion["kind"], exclusion["reason"]),
                tags=["secprop", "coverage", "exclusion"],
            )
        )

    notes = [
        "Bounded coverage: obligations were instantiated from cycle {} up to (bound - "
        "their lookahead); nothing beyond cycle {} was examined. Per-finding 'metrics' "
        "give the exact cycle range and the uncovered tail.".format(
            report.diagnostics.get("check_from_cycle"), report.bound
        ),
        "Structural cone results are candidate reachability with status 'heuristic'. A "
        "candidate path is not a vulnerability and the absence of one is not a proof; the "
        "bounded checks carry every claim in this document.",
    ]
    if report.cones is not None:
        summary = report.cones.summary()
        notes.append(
            "Cone selection: {} of the design's {} register bit(s) are in the fan-in of a "
            "policy target.".format(
                summary["states_in_selected_cones"], summary["design_states"]
            )
        )
    if report.diagnostics.get("quiescent_inputs_unresolved"):
        notes.append(
            "Quiescent inputs the design does not have (assumption NOT applied): {}.".format(
                ", ".join(report.diagnostics["quiescent_inputs_unresolved"])
            )
        )
    if report.diagnostics.get("system_problems"):
        notes.append(
            "Transition system problems: {}.".format(
                "; ".join(report.diagnostics["system_problems"])
            )
        )

    producer = {"name": PRODUCER_NAME, "version": VERSION}
    if command:
        producer["command"] = [str(part) for part in command]

    return model.document(
        producer,
        [design_artifact, policy_artifact],
        {
            "plugin": {
                "name": PRODUCER_NAME,
                "version": VERSION,
                "description": "bounded interface-to-sensitive-state security property "
                "checks with replayable witnesses",
            },
            "entry_point": "hal_secprop.engine.check",
            "configuration": {
                "policy": policy.name,
                "bound": report.bound,
                "reset_cycles": policy.reset_cycles,
                "clock": policy.clock_signal,
                "lock": policy.lock_signal,
                "options": dict(policy.options),
                "front_end": design_artifact.get("description"),
            },
            "duration_s": report.duration_s,
        },
        findings,
        generated_at=generated_at,
        notes=notes,
    )


def build_unsupported_document(
    policy,
    primitives,
    message,
    design_artifact,
    policy_artifact,
    command=None,
    generated_at=None,
):
    """The document written when the design uses primitives we do not model.

    Nothing about the design is claimed. One finding names the primitives, and
    one finding per requested obligation says explicitly that it was *not*
    checked -- an empty report would read like a clean one.
    """
    model = findings_model()
    artifact_ids = [design_artifact["artifact_id"], policy_artifact["artifact_id"]]

    def scope_for(description):
        return model.scope(artifact_ids, description=description)

    method = model.method(
        "secprop_netlist_front_end",
        "structural",
        False,
        description="Extraction of a cycle-accurate transition system from the netlist.",
        parameters={"policy": policy.name},
    )

    artifact_id = design_artifact["artifact_id"]
    entries = [
        model.unsupported_primitive(
            entry["gate_type"],
            entry["reason"],
            count=entry.get("count"),
            example_gates=[
                model.gate_ref(
                    artifact_id,
                    gate.get("id"),
                    gate.get("name"),
                    gate_type=entry["gate_type"],
                )
                for gate in entry.get("example_gates") or []
                if gate.get("id") is not None
            ]
            or None,
        )
        for entry in primitives
    ]
    findings = [
        model.finding(
            "secprop/unsupported/primitives",
            "the design uses primitives this analysis does not model",
            "unsupported",
            method,
            scope_for("primitive coverage of {}".format(policy.name)),
            summary=(
                "{} No obligation was checked. A design whose state includes a primitive "
                "the model cannot represent produces an incomplete result, and an "
                "incomplete result is reported as such rather than as a pass.".format(
                    message
                )
            ),
            severity="high",
            unsupported_dict=model.unsupported(
                "primitive",
                "the transition system could not be extracted",
                primitives=entries,
            ),
            data={"primitives": list(primitives)},
            tags=["secprop", "unsupported", "primitive"],
        )
    ]

    for prop in properties_module.build(policy):
        findings.append(
            model.finding(
                prop.id,
                prop.title,
                "unsupported",
                method,
                scope_for(
                    "{} on the design named by policy {}".format(
                        prop.register or "the lock state", policy.name
                    )
                ),
                summary=(
                    "Not checked: the design could not be turned into a transition system "
                    "because it uses primitives this analysis does not model."
                ),
                severity="medium",
                unsupported_dict=model.unsupported("primitive", message, primitives=entries),
                tags=["secprop", "unsupported", "kind:" + prop.kind],
            )
        )

    producer = {"name": PRODUCER_NAME, "version": VERSION}
    if command:
        producer["command"] = [str(part) for part in command]
    return model.document(
        producer,
        [design_artifact, policy_artifact],
        {
            "plugin": {
                "name": PRODUCER_NAME,
                "version": VERSION,
                "description": "bounded interface-to-sensitive-state security property "
                "checks with replayable witnesses",
            },
            "entry_point": "hal_secprop.engine.check",
            "configuration": {"policy": policy.name, "checked": False},
        },
        findings,
        generated_at=generated_at,
        notes=[
            "This run checked nothing. Every obligation is reported as unsupported so that "
            "the document can never be read as a clean result.",
        ],
    )
