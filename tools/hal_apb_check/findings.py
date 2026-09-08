"""Turn an engine report into a schema-valid ``hal_findings`` document.

The translation is where the honesty of the whole plugin is decided, so the
mapping from engine outcome to finding status is written out once, here, and
nowhere else:

===========================  ==========================  ===================
engine outcome               finding status              why
===========================  ==========================  ===================
``holds_bounded``            ``proven_bounded``          no violating run exists *up to the bound*; nothing was proven beyond it
``violated``                 ``bounded_counterexample``  a witness exists inside the bound, and it replays
``vacuous``                  ``unknown``                 the property never fired: a pass, but not evidence
``not_instantiable``         ``unknown``                 the bound is smaller than the property's lookahead
``unsupported``              ``unsupported``             the mapping does not provide the signals
``timeout``                  ``timeout``                 the search budget ran out
``error``                    ``error``                   the analysis itself broke
===========================  ==========================  ===================

``proven_under_assumptions`` never appears: this analysis does bounded model
checking, not induction, so it has no way to earn an unbounded claim.
"""

import importlib
import os
import sys

from . import VERSION, PRODUCER_NAME
from .engine import CheckOutcome
from . import spec

__all__ = [
    "FINDINGS_IMPORT_ERROR",
    "build_document",
    "findings_model",
    "findings_package",
]

FINDINGS_IMPORT_ERROR = (
    "tools/hal_findings is not importable. Run the checker from the repository root, or add "
    "the 'tools' directory to PYTHONPATH: the findings schema is a hard dependency, not an "
    "optional formatter."
)


def _import(module_name):
    """Import ``module_name``, adding ``tools/`` to ``sys.path`` if needed."""
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
    """The ``hal_findings`` package (schema version, serializer, validator)."""
    return _import("hal_findings")


def findings_model():
    """The ``hal_findings.model`` builders."""
    return _import("hal_findings.model")


#: Assumptions that are true of *every* result this analysis produces.
TOOL_ASSUMPTIONS = (
    (
        "apb/tool/cycle-abstraction",
        "tool",
        "One step of the model is one active PCLK edge. Clock trees, gated or divided clocks, "
        "multi-clock crossings, setup/hold timing, glitches and X-propagation are outside the "
        "model, so no result here says anything about them.",
    ),
    (
        "apb/tool/async-reset-sampled",
        "tool",
        "Asynchronous set/clear inputs of flip-flops are sampled in the current cycle and "
        "applied at the edge. Reset behaviour is therefore modelled synchronously, which is an "
        "over-approximation of the real asynchronous behaviour.",
    ),
    (
        "apb/tool/mapping-is-trusted",
        "user_provided",
        "The APB signals are whatever the bus mapping file says they are. Nothing is inferred "
        "from net names, so a wrong mapping produces a confidently wrong answer.",
    ),
    (
        "apb/tool/no-initial-state",
        "initial_state",
        "Registers start unconstrained; the declared reset sequence is what establishes a known "
        "state, and properties are only instantiated once it has settled.",
    ),
)


def _reset_assumption(bus_mapping):
    return (
        "apb/env/reset-sequence",
        "environment",
        "{} is held {} for the first {} cycle(s) of every run and released afterwards.".format(
            bus_mapping.reset_signal,
            "low" if bus_mapping.reset_active_low else "high",
            bus_mapping.reset_cycles,
        ),
    )


def _assumptions(model, bus_mapping, report):
    """Environment assumptions plus the tool-level ones, as schema objects."""
    entries = []
    for assumption_id, kind, description in TOOL_ASSUMPTIONS + (_reset_assumption(bus_mapping),):
        entries.append(model.assumption(assumption_id, description, kind=kind))
    for entry in report.assumptions:
        assumed = _property_by_id(entry["assumption"])
        entries.append(
            model.assumption(
                entry["assumption"],
                "Environment assumption ({} obligation), asserted for cycles {}..{}: {}".format(
                    assumed.obligation_of if assumed else "bus",
                    entry["cycles"][0],
                    entry["cycles"][1],
                    assumed.description if assumed else "",
                ),
                kind="environment",
                discharged=False,
            )
        )
    if report.diagnostics.get("unresolved_signals"):
        entries.append(
            model.assumption(
                "apb/env/unresolved-signals",
                "The mapping names design signal(s) {} that the extracted model does not "
                "contain; every property that needs them was reported as unsupported.".format(
                    ", ".join(report.diagnostics["unresolved_signals"])
                ),
                kind="structural",
            )
        )
    return entries


_PROPERTY_INDEX = {prop.id: prop for prop in spec.PROPERTIES}


def _property_by_id(property_id):
    return _PROPERTY_INDEX.get(property_id)


def _method(model, prop, report, kind="bounded_formal"):
    return model.method(
        "apb_bounded_model_checking",
        kind,
        True,
        description=(
            "Bounded model checking of one APB property over a {}-cycle unrolling of the "
            "design's transition relation, under the environment assumptions listed on this "
            "finding.".format(report.bound)
        ),
        parameters={
            "bound": report.bound,
            "revision": report.mapping.revision,
            "dut_role": report.mapping.role,
            "property": prop.id if prop else None,
            "max_wait_states": report.mapping.option("max_wait_states"),
        },
    )


def _solver(model, report):
    """Describe the decision procedure.

    Wall-clock time is deliberately *not* recorded here: two runs on the same
    inputs must produce byte-identical documents so that findings can be diffed
    and cached, and ``serialize.document_digest`` only excludes the timing fields
    the schema declares volatile. Elapsed time is reported once, in
    ``analysis.duration_s``, which is one of them.
    """
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
        "uncovered_tail_cycles": (
            report.bound - cycles[1] if cycles else report.bound
        ),
    }
    return {key: value for key, value in metrics.items() if value is not None}


def _witness_entries(model, result, bus_mapping, net_refs=None):
    """One entry per mapped APB bit per cycle -- the readable part of the trace."""
    entries = []
    trace = result.detail.get("trace") or []
    for apb_signal in sorted(bus_mapping.mapped_signals()):
        bits = bus_mapping.bits(apb_signal)
        for index, design_signal in enumerate(bits):
            label = design_signal if len(bits) == 1 else "{}[{}]".format(apb_signal, index)
            for cycle, values in enumerate(trace):
                if design_signal not in values:
                    continue
                entry_net = (net_refs or {}).get(design_signal)
                entries.append(
                    model.witness_entry(
                        label,
                        "1" if values[design_signal] else "0",
                        cycle=cycle,
                        net=entry_net,
                    )
                )
    return entries


def _status_for(result):
    return {
        CheckOutcome.HOLDS_BOUNDED: "proven_bounded",
        CheckOutcome.VIOLATED: "bounded_counterexample",
        CheckOutcome.VACUOUS: "unknown",
        CheckOutcome.NOT_INSTANTIABLE: "unknown",
        CheckOutcome.UNSUPPORTED: "unsupported",
        CheckOutcome.TIMEOUT: "timeout",
        CheckOutcome.ERROR: "error",
    }[result.outcome]


def build_document(
    report,
    design_artifact,
    mapping_artifact,
    evidence_by_property=None,
    net_refs=None,
    command=None,
    generated_at=None,
):
    """Build the findings document for ``report``.

    ``design_artifact``/``mapping_artifact`` are already-built schema artifacts;
    ``evidence_by_property`` maps a property id (or ``"*"``) to a list of schema
    evidence objects.
    """
    model = findings_model()

    bus_mapping = report.mapping
    evidence_by_property = evidence_by_property or {}
    artifact_ids = [design_artifact["artifact_id"], mapping_artifact["artifact_id"]]
    shared_evidence = list(evidence_by_property.get("*", []))

    def scope_for(description):
        return model.scope(artifact_ids, description=description)

    findings = []

    for result in report.results:
        prop = result.property
        status = _status_for(result)
        detail = result.detail
        evidence = shared_evidence + list(evidence_by_property.get(prop.id, []))
        common = {
            "summary": prop.description,
            "method_dict": _method(model, prop, report),
            "scope_dict": scope_for(
                "{} {} on the {} interface mapped by {}".format(
                    bus_mapping.revision,
                    prop.obligation_of,
                    bus_mapping.role,
                    bus_mapping.name,
                )
            ),
            "solver_dict": _solver(model, report),
            "evidence_list": evidence or None,
            "metrics": _coverage(result, report),
            "tags": [
                "apb",
                bus_mapping.revision.lower(),
                "role:" + bus_mapping.role,
                "kind:" + prop.kind,
                "obligation:" + prop.obligation_of,
            ],
        }

        if status == "proven_bounded":
            findings.append(
                model.finding(
                    prop.id,
                    prop.title,
                    status,
                    severity="info",
                    assumptions=_assumptions(model, bus_mapping, report),
                    bounds_dict=model.bounded(
                        report.bound,
                        description=(
                            "No run of at most {} cycles violates this property. Nothing is "
                            "claimed about longer runs.".format(report.bound)
                        ),
                    ),
                    data={"instantiated_cycles": detail.get("cycles")},
                    **common
                )
            )
        elif status == "bounded_counterexample":
            witness_depth = min(
                report.bound, detail["violation_cycle"] + max(1, detail.get("horizon", 0))
            )
            findings.append(
                model.finding(
                    prop.id,
                    prop.title,
                    status,
                    severity=prop.severity,
                    assumptions=_assumptions(model, bus_mapping, report),
                    bounds_dict=model.bounded(report.bound),
                    counterexample_dict=model.counterexample(
                        "{} is violated at cycle {} of a {}-cycle run that satisfies every "
                        "environment assumption listed on this finding.".format(
                            prop.id, detail["violation_cycle"], report.bound
                        ),
                        cycle_bound=max(1, witness_depth),
                        witness=_witness_entries(model, result, bus_mapping, net_refs),
                        witness_available=True,
                        evidence_list=evidence or None,
                    ),
                    data={
                        "violation_cycle": detail["violation_cycle"],
                        "failing_cycles": detail.get("failing_cycles"),
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
                    assumptions=_assumptions(model, bus_mapping, report),
                    bounds_dict=model.bounded(
                        report.bound,
                        description=(
                            "Vacuous: no run within {} cycles activates the antecedent, so the "
                            "absence of a violation is not evidence.".format(report.bound)
                            if vacuous
                            else "The property could not be instantiated inside this bound."
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
                    assumptions=_assumptions(model, bus_mapping, report),
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

    # ---- run-level diagnostics -------------------------------------------
    if not report.results:
        # An APB2 completer, for instance: without PREADY and PSLVERR the
        # revision places no checkable obligation on that side of the bus. Say so
        # rather than emitting a document that reads like a clean run.
        findings.append(
            model.finding(
                "apb/diagnostics/no-obligations",
                "no checkable obligation exists for this revision and role",
                "unsupported",
                _method(model, None, report, kind="structural"),
                scope_for("{} {} interface {}".format(
                    bus_mapping.revision, bus_mapping.role, bus_mapping.name
                )),
                summary=(
                    "This run checked nothing: {} defines no obligation for a {} that this "
                    "analysis can state. An empty result here is a coverage gap, not a "
                    "pass.".format(bus_mapping.revision, bus_mapping.role)
                ),
                severity="medium",
                unsupported_dict=model.unsupported(
                    "configuration",
                    "{} has no {} obligation in this catalogue; APB2 in particular has no "
                    "PREADY or PSLVERR, so a completer has nothing to be checked "
                    "against.".format(bus_mapping.revision, bus_mapping.role),
                ),
                tags=["apb", "diagnostics", "coverage"],
            )
        )

    if report.diagnostics.get("overconstrained"):
        findings.append(
            model.finding(
                "apb/diagnostics/overconstrained",
                "the environment assumptions leave no APB transfer to check",
                "unknown",
                _method(model, None, report, kind="bounded_formal"),
                scope_for("environment assumptions for {}".format(bus_mapping.name)),
                summary=(
                    "Every obligation below was downgraded: within {} cycles the assumptions "
                    "admit either no run at all or no ACCESS phase, so a clean report would "
                    "mean nothing.".format(report.bound)
                ),
                severity="high",
                assumptions=_assumptions(model, bus_mapping, report),
                bounds_dict=model.bounded(report.bound),
                solver_dict=_solver(model, report),
                data={
                    "environment_satisfiable": report.diagnostics.get("environment_satisfiable"),
                    "transfer_reachable": report.diagnostics.get("transfer_reachable"),
                    "assumptions_applied": report.assumptions,
                    "assumptions_not_applied": report.diagnostics.get(
                        "assumptions_not_applied"
                    ),
                },
                tags=["apb", "diagnostics", "overconstrained"],
            )
        )

    for exclusion in spec.EXCLUSIONS:
        applies = exclusion["applies"]
        if applies != "always" and applies != bus_mapping.revision:
            continue
        findings.append(
            model.finding(
                exclusion["id"],
                exclusion["title"],
                "unsupported",
                model.method(
                    "apb_coverage_declaration",
                    "structural",
                    False,
                    description="A declared limit of this analysis, not a result about the design.",
                ),
                scope_for("coverage of the APB checker itself"),
                summary=exclusion["reason"],
                severity="info",
                unsupported_dict=model.unsupported(exclusion["kind"], exclusion["reason"]),
                tags=["apb", "coverage", "exclusion"],
            )
        )

    notes = [
        "Bounded coverage: obligations were instantiated from cycle {} onwards, each up to "
        "(bound - its own lookahead horizon); nothing beyond cycle {} was examined. Per-finding "
        "'metrics' give the exact cycle range and the uncovered tail.".format(
            report.diagnostics.get("check_from_cycle"), report.bound
        ),
        "Environment assumptions applied: {}.".format(
            ", ".join(entry["assumption"] for entry in report.assumptions) or "none"
        ),
    ]
    if report.diagnostics.get("assumptions_not_applied"):
        notes.append(
            "Environment assumptions NOT applied: {}.".format(
                ", ".join(
                    entry["assumption"]
                    for entry in report.diagnostics["assumptions_not_applied"]
                )
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
        [design_artifact, mapping_artifact],
        {
            "plugin": {
                "name": PRODUCER_NAME,
                "version": VERSION,
                "description": "bounded APB protocol checks with replayable counterexamples",
            },
            "entry_point": "hal_apb_check.engine.check",
            "configuration": {
                "revision": bus_mapping.revision,
                "dut_role": bus_mapping.role,
                "bound": report.bound,
                "reset_cycles": bus_mapping.reset_cycles,
                "options": dict(bus_mapping.options),
                "mapping": bus_mapping.name,
            },
            "duration_s": report.duration_s,
        },
        findings,
        generated_at=generated_at,
        notes=notes,
    )
