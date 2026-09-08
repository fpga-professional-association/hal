"""Turn comparison outcomes into a validated ``hal_findings`` document.

Every status decision was already taken in :mod:`hal_semantic_diff.compare`;
this module only maps them onto the shared vocabulary, and it maps them
conservatively:

======================  ===============================================
comparison outcome      findings status
======================  ===============================================
``equivalent``          ``proven_under_assumptions`` (bounds: unbounded)
``different``           ``counterexample`` (bounds: unbounded)
``unknown``             ``unknown``
``timeout``             ``timeout`` (carrying the limit it hit)
``error``               ``error``
a gap in the mapping    ``unsupported``
======================  ===============================================

Two choices are worth defending explicitly.

*Why ``counterexample`` and not ``bounded_counterexample``.*  The check
compares next-state and output *functions* with register outputs as free
variables.  Nothing is unrolled, so there is no cycle bound to report, and
``bounded_counterexample`` would have to invent one.  The schema forbids that
(a bounded status requires a real ``bounds.cycle_bound``), and inventing a
bound would also misrepresent the claim: the difference shown here is a
difference of the combinational function, valid in *every* cycle in which the
matched registers hold matched state.

*Why a witness may still be absent.*  ``witness_available`` is ``False``
whenever the model could not be retrieved or could not be replayed.  That is a
weaker finding, and it is labelled as one rather than dressed up.
"""

from hal_findings import model
from hal_findings.adapters import common

from . import __version__, compare

__all__ = [
    "ANALYSIS_NAME",
    "ENTRY_POINT",
    "assumptions_for",
    "strip_timings",
    "build_document",
]

ANALYSIS_NAME = "hal_semantic_diff"
ENTRY_POINT = "hal_semantic_diff.compare_points"

_METHOD_DESCRIPTION = (
    "For every observation point -- a shared top-module output pin, or an input pin of a "
    "pair of corresponding sequential gates -- the combinational cone feeding it is "
    "extracted in both builds and cut at the same boundary z3_utils uses (top-level "
    "inputs become GLOBAL_IN_<pin>, sequential outputs become <gate>_<pin>). The two cone "
    "functions are then compared with a single SMT query per point, so an inequivalence "
    "is localized to the point and the cone that changed instead of collapsing into one "
    "boolean for the whole netlist."
)

_STRUCTURAL_DESCRIPTION = (
    "Canonical signature of the combinational cone: gate type, output pin and the "
    "signatures of the fan-in nets, recursively, down to the named boundary variables. "
    "Two cones with the same signature are the same circuit over the same variables."
)

_ASSUMPTIONS = (
    (
        "explicit-correspondence",
        "user_provided",
        "The correspondence between the two builds -- which sequential gate, which "
        "top-level pin corresponds to which -- is the one in the mapping file. Names "
        "that the mapping does not cover are reported as unsupported and are not part "
        "of any equivalence claim.",
    ),
    (
        "combinational-frontier",
        "structural",
        "Only gates whose type carries the 'combinational' property are traversed. The "
        "output of every other gate is a free boundary variable, so a difference hidden "
        "inside such a gate is invisible to this check. This is the same frontier "
        "z3_utils::compare_nets uses.",
    ),
    (
        "matched-state-correspondence",
        "initial_state",
        "Corresponding sequential gates are assumed to hold equal state, which makes "
        "this an equivalence of next-state and output functions rather than a temporal "
        "one. Retiming and state re-encoding violate this assumption and are outside "
        "the model.",
    ),
    (
        "gate-library-semantics",
        "library",
        "Gate behaviour is whatever the gate library says it is; the Boolean functions "
        "come from GateType, not from the primitive's name.",
    ),
    (
        "shared-boundary",
        "structural",
        "Both builds have the same top-level boundary. Pins present in only one build "
        "are reported, never compared, and never counted as agreement.",
    ),
)


def assumptions_for(options, mapping):
    """The assumptions every verdict of this run rests on."""
    assumptions = [
        model.assumption(identifier, description, kind=kind)
        for identifier, kind, description in _ASSUMPTIONS
    ]
    if mapping is not None and not mapping.is_identity:
        assumptions.append(
            model.assumption(
                "boundary-variable-renaming",
                "The correspondence renames {} boundary point(s); the cone functions of "
                "build A were rewritten into build B's variable namespace before "
                "comparison. z3_utils::compare_nets cannot do this, so its cross-check "
                "is not applicable to this run.".format(len(mapping.renamings())),
                kind="naming",
            )
        )
    if mapping is not None and mapping.path is None:
        assumptions.append(
            model.assumption(
                "inferred-correspondence",
                "No correspondence file was given: the mapping was inferred as the "
                "identity over the names both builds happen to share. Any register or "
                "pin whose name changed is therefore a gap, not a match, and the "
                "resulting gaps are reported as unsupported.",
                kind="naming",
            )
        )
    if options.structural_fast_path:
        assumptions.append(
            model.assumption(
                "structural-fast-path",
                "Observation points whose two cones have identical canonical signatures "
                "were not sent to the solver; their equivalence is a structural "
                "identity, reported with a 'structural' method.",
                kind="tool",
            )
        )
    return assumptions


_TIMING_KEYS = ("wall_time_s", "duration_s")


def strip_timings(value):
    """Remove wall-clock measurements from a document, in place.

    Timings are the only part of a comparison that changes between two runs on
    identical inputs.  ``hal_findings``' ``document_digest`` ignores the
    document-level ones but not the per-finding ones, so a run that wants a
    byte-reproducible artifact -- a checkpoint key, a CI baseline -- drops them
    and keeps everything that is actually a result.
    """
    if isinstance(value, dict):
        for key in _TIMING_KEYS:
            value.pop(key, None)
        value.pop("per_point_wall_time_s", None)
        for nested in value.values():
            strip_timings(nested)
    elif isinstance(value, list):
        for nested in value:
            strip_timings(nested)
    return value


def _identifier(text, limit=110):
    safe = "".join(
        character if (character.isalnum() or character in "_.:/-") else "_"
        for character in str(text)
    )
    safe = safe.lstrip("_.:/-") or "point"
    return safe[:limit]


def _gate_refs(gates, artifact_id, limit=40):
    return [common.gate_reference(gate, artifact_id) for gate in gates[:limit]]


def _point_scope(outcome, artifact_id_a, artifact_id_b, changed_a, changed_b):
    gates = _gate_refs(changed_a, artifact_id_a) + _gate_refs(changed_b, artifact_id_b)
    nets = []
    if outcome.point.net_a is not None:
        nets.append(
            common.net_reference(outcome.point.net_a, artifact_id_a, role="observation_point")
        )
    if outcome.point.net_b is not None:
        nets.append(
            common.net_reference(outcome.point.net_b, artifact_id_b, role="observation_point")
        )
    return model.scope(
        [artifact_id_a, artifact_id_b],
        description="{} ({})".format(outcome.point.label, outcome.point.kind),
        gates=gates or None,
        nets=nets or None,
    )


def _changed_gate_data(changed_a, changed_b, limit=40):
    def names(gates):
        return [
            {
                "id": common.call(gate, "get_id"),
                "name": common.call(gate, "get_name", default=""),
                "type": common.gate_type_name(gate),
            }
            for gate in gates[:limit]
        ]

    return {
        "only_in_a": names(changed_a),
        "only_in_b": names(changed_b),
        "only_in_a_count": len(changed_a),
        "only_in_b_count": len(changed_b),
        "truncated": len(changed_a) > limit or len(changed_b) > limit,
    }


def _problem_finding(problem, method, artifact_id_a, artifact_id_b):
    return model.finding(
        "semantic_diff/correspondence/" + _identifier(problem.identifier),
        "Correspondence gap: {}".format(problem.identifier),
        model.STATUS_UNSUPPORTED,
        method,
        model.scope(
            [artifact_id_a, artifact_id_b],
            description="a point the correspondence does not cover",
            gates=(
                _gate_refs(problem.gates_a, artifact_id_a)
                + _gate_refs(problem.gates_b, artifact_id_b)
            )
            or None,
        ),
        summary=problem.message,
        severity="medium",
        unsupported_dict=model.unsupported(
            "construct",
            "{}: {}. An unmapped or mismatched point cannot be compared, and is never "
            "counted towards equivalence.".format(problem.kind, problem.message),
        ),
        data=dict(problem.detail, kind=problem.kind),
        tags=["correspondence", "precondition", "semantic-diff"],
    )


def _unmodelled_primitives(netlist, artifact_id):
    primitives = {}
    for gate in common.call(netlist, "get_gates", default=[]) or []:
        gate_type = common.call(gate, "get_type")
        if gate_type is None:
            continue
        properties = common.gate_type_properties(gate_type)
        if "combinational" in properties or "sequential" in properties:
            continue
        name = common.call(gate_type, "get_name", default="<unnamed>")
        entry = primitives.setdefault(name, {"count": 0, "properties": properties, "gates": []})
        entry["count"] += 1
        if len(entry["gates"]) < 3:
            entry["gates"].append(common.gate_reference(gate, artifact_id))
    return primitives


def _primitive_finding(netlist_a, netlist_b, artifact_id_a, artifact_id_b, method):
    primitives = {}
    for netlist, artifact_id in ((netlist_a, artifact_id_a), (netlist_b, artifact_id_b)):
        for name, entry in _unmodelled_primitives(netlist, artifact_id).items():
            merged = primitives.setdefault(
                name, {"count": 0, "properties": entry["properties"], "gates": []}
            )
            merged["count"] += entry["count"]
            merged["gates"].extend(entry["gates"])
    if not primitives:
        return None
    return model.finding(
        "semantic_diff/coverage/unmodelled-primitives",
        "Gate types outside the comparison model",
        model.STATUS_UNSUPPORTED,
        method,
        model.scope(
            [artifact_id_a, artifact_id_b],
            description="gate types with neither the combinational nor the sequential property",
            gate_types=sorted(primitives),
        ),
        summary=(
            "{} gate type(s) are neither traversed as combinational logic nor matched as "
            "state; their outputs enter every query as free variables, so a difference "
            "inside them cannot be seen.".format(len(primitives))
        ),
        severity="medium",
        unsupported_dict=model.unsupported(
            "primitive",
            "the comparison traverses combinational gates and matches sequential gates; "
            "any other gate type is modelled as an unconstrained free variable",
            [
                model.unsupported_primitive(
                    name,
                    "gate type carries neither the 'combinational' nor the 'sequential' "
                    "property",
                    count=entry["count"],
                    properties=entry["properties"],
                    example_gates=entry["gates"][:6],
                )
                for name, entry in sorted(primitives.items())
            ],
        ),
        tags=["coverage", "semantic-diff"],
    )


def _point_finding(
    outcome,
    artifact_id_a,
    artifact_id_b,
    formal_method,
    structural_method,
    assumptions,
    options,
    evidence_for_point,
):
    changed_a, changed_b = outcome.changed_gates()
    scope = _point_scope(outcome, artifact_id_a, artifact_id_b, changed_a, changed_b)
    method = structural_method if outcome.method == "structural" else formal_method
    finding_id = "semantic_diff/point/" + _identifier(outcome.point.key)
    title = "{}: {}".format(outcome.point.label, outcome.status)

    data = outcome.summary()
    data["changed_gates"] = _changed_gate_data(changed_a, changed_b)
    data.update(outcome.point.detail)

    queried = outcome.solver_outcome is not None
    solver = model.solver(
        "hal_py.SMT",
        queries=1 if queried else 0,
        unsat=1 if outcome.solver_outcome == "unsat" else 0,
        sat=1 if outcome.solver_outcome == "sat" else 0,
        unknown=1 if outcome.solver_outcome == "unknown" else 0,
        wall_time_s=outcome.wall_time_s,
    )
    limits = model.limits(
        timeout_s=float(options.solver_timeout_s),
        wall_time_s=outcome.wall_time_s,
        hit=outcome.timed_out,
        description="per-observation-point SMT query timeout",
    )

    kwargs = {
        "summary": outcome.rationale,
        "assumptions": assumptions,
        "solver_dict": solver,
        "limits_dict": limits,
        "evidence_list": evidence_for_point or None,
        "data": data,
        "tags": ["semantic-diff", "observation-point", outcome.point.kind],
    }

    if outcome.status == compare.EQUIVALENT:
        kwargs["bounds_dict"] = model.unbounded(
            description=(
                "The claim is about the combinational function feeding this point, with "
                "matched register outputs as free variables, so it is not limited to a "
                "number of cycles."
            )
        )
        kwargs["severity"] = "info"
        status = model.STATUS_PROVEN_UNDER_ASSUMPTIONS
    elif outcome.status == compare.DIFFERENT:
        kwargs["bounds_dict"] = model.unbounded(
            description=(
                "A difference of the combinational functions is not cycle bounded; "
                "nothing was unrolled, so there is no cycle bound to report."
            )
        )
        kwargs["counterexample_dict"] = model.counterexample(
            outcome.rationale,
            witness=[
                model.witness_entry(entry["signal"], entry["value"])
                for entry in outcome.witness
            ]
            or None,
            witness_available=bool(outcome.witness_available),
        )
        kwargs["severity"] = "high"
        status = model.STATUS_COUNTEREXAMPLE
    elif outcome.status == compare.TIMEOUT:
        kwargs["severity"] = "medium"
        status = model.STATUS_TIMEOUT
    elif outcome.status == compare.ERROR:
        kwargs["error_dict"] = model.error(
            outcome.error_kind or "plugin_error", outcome.error_message or "unknown failure"
        )
        kwargs["severity"] = "medium"
        status = model.STATUS_ERROR
    elif outcome.status == compare.UNSUPPORTED:
        kwargs["unsupported_dict"] = model.unsupported(
            "construct",
            outcome.unsupported_reason
            or "this observation point is outside what the correspondence covers",
        )
        kwargs["severity"] = "medium"
        status = model.STATUS_UNSUPPORTED
    else:
        kwargs["severity"] = "medium"
        status = model.STATUS_UNKNOWN

    return model.finding(finding_id, title, status, method, scope, **kwargs)


def _coverage_finding(coverage, outcomes, problems, artifact_id_a, artifact_id_b, method):
    """State what the run did *not* cover, when there is something to state."""
    disposition = coverage.get("top_output_disposition", {})
    uncovered = sorted(
        pin for pin, state in disposition.items() if state != "compared"
    )
    registered = [
        outcome.point.detail.get("pin_a")
        for outcome in outcomes
        if outcome.point.kind == "top_output"
        and outcome.cone_a is not None
        and not outcome.cone_a.gates
    ]
    registered = sorted(pin for pin in registered if pin)
    if not uncovered and not registered and not problems:
        return None
    lines = []
    if uncovered:
        lines.append(
            "{} top-level output pin(s) were not compared ({})".format(
                len(uncovered), ", ".join(uncovered[:10])
            )
        )
    if registered:
        lines.append(
            "{} top-level output pin(s) are driven directly by a matched sequential gate "
            "({}); their cone is a single free variable, so the real comparison for them "
            "happens at that register's inputs".format(len(registered), ", ".join(registered[:10]))
        )
    if problems:
        lines.append("{} correspondence gap(s) were reported separately".format(len(problems)))
    return model.finding(
        "semantic_diff/coverage/observation-points",
        "Output coverage of this comparison",
        model.STATUS_UNSUPPORTED,
        method,
        model.scope(
            [artifact_id_a, artifact_id_b],
            description="what the comparison did and did not observe",
        ),
        summary=". ".join(lines) + ".",
        severity="low" if not uncovered else "medium",
        unsupported_dict=model.unsupported(
            "configuration",
            "the comparison observes shared top-module output pins and the inputs of "
            "corresponding sequential gates; everything outside that set is listed here "
            "rather than assumed to agree",
        ),
        data=coverage,
        tags=["coverage", "semantic-diff"],
    )


def _summary_finding(
    outcomes, problems, coverage, artifact_id_a, artifact_id_b, method, assumptions, options
):
    counts = {status: 0 for status in compare.STATUSES}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    differing = [outcome for outcome in outcomes if outcome.status == compare.DIFFERENT]
    inconclusive = [
        outcome for outcome in outcomes if outcome.status in compare.INCONCLUSIVE
    ]

    data = {
        "counts": counts,
        "coverage": coverage,
        "correspondence_gaps": len(problems),
        "changed_points": [outcome.point.key for outcome in differing][:100],
        "inconclusive_points": [outcome.point.key for outcome in inconclusive][:100],
        "options": options.as_dict(),
    }

    scope = model.scope(
        [artifact_id_a, artifact_id_b],
        description="behavioural equivalence of the two builds at every observation point",
    )
    solver = model.solver(
        "hal_py.SMT",
        queries=len([o for o in outcomes if o.solver_outcome is not None]),
        sat=len([o for o in outcomes if o.solver_outcome == "sat"]),
        unsat=len([o for o in outcomes if o.solver_outcome == "unsat"]),
        unknown=len([o for o in outcomes if o.solver_outcome == "unknown"]),
        wall_time_s=round(sum(o.wall_time_s or 0.0 for o in outcomes), 4),
    )

    if differing:
        witness_source = next(
            (outcome for outcome in differing if outcome.witness_available), differing[0]
        )
        return model.finding(
            "semantic_diff/summary",
            "The two builds differ behaviourally",
            model.STATUS_COUNTEREXAMPLE,
            method,
            scope,
            summary=(
                "{} of {} observation point(s) implement a different function; the first "
                "one with a witness is {!r}.".format(
                    len(differing), len(outcomes), witness_source.point.key
                )
            ),
            severity="high",
            assumptions=assumptions,
            bounds_dict=model.unbounded(
                description="a difference of the compared functions is not cycle bounded"
            ),
            counterexample_dict=model.counterexample(
                "Observation point {} ({}) differs between the two builds.".format(
                    witness_source.point.key, witness_source.point.label
                ),
                witness=[
                    model.witness_entry(entry["signal"], entry["value"])
                    for entry in witness_source.witness
                ]
                or None,
                witness_available=bool(witness_source.witness_available),
            ),
            solver_dict=solver,
            data=data,
            tags=["semantic-diff", "summary"],
        )

    if inconclusive or problems:
        return model.finding(
            "semantic_diff/summary",
            "No equivalence verdict for the two builds",
            model.STATUS_UNKNOWN,
            method,
            scope,
            summary=(
                "{} observation point(s) were proven equivalent, but {} were "
                "inconclusive and {} correspondence gap(s) were found, so the builds "
                "cannot be reported as equivalent.".format(
                    counts.get(compare.EQUIVALENT, 0), len(inconclusive), len(problems)
                )
            ),
            severity="medium",
            assumptions=assumptions,
            solver_dict=solver,
            data=data,
            tags=["semantic-diff", "summary"],
        )

    if not outcomes:
        return model.finding(
            "semantic_diff/summary",
            "Nothing was compared",
            model.STATUS_UNKNOWN,
            method,
            scope,
            summary=(
                "The correspondence produced no observation point at all, so this run "
                "says nothing about the two builds."
            ),
            severity="medium",
            assumptions=assumptions,
            data=data,
            tags=["semantic-diff", "summary"],
        )

    return model.finding(
        "semantic_diff/summary",
        "The two builds are equivalent under the documented model",
        model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
        method,
        scope,
        summary=(
            "All {} observation point(s) implement the same function in both builds, with "
            "no correspondence gaps and no inconclusive queries.".format(len(outcomes))
        ),
        severity="info",
        assumptions=assumptions,
        bounds_dict=model.unbounded(
            description=(
                "Next-state and output functions are compared with matched register "
                "outputs as free variables, so the claim is not limited to a number of "
                "cycles -- given the matched-state assumption."
            )
        ),
        solver_dict=solver,
        data=data,
        tags=["semantic-diff", "summary"],
    )


def build_document(
    netlist_a,
    netlist_b,
    outcomes,
    problems,
    coverage,
    mapping,
    options,
    artifact_id_a="build_a",
    artifact_id_b="build_b",
    netlist_path_a=None,
    netlist_path_b=None,
    correspondence_artifact=None,
    hal_version=None,
    solver_backend=None,
    plugin_versions=None,
    generated_at=None,
    producer_command=None,
    evidence_by_point=None,
    wall_time_s=None,
    record_timings=True,
):
    """Assemble the complete findings document for one comparison run.

    With ``record_timings=False`` every wall-clock measurement is dropped, so
    two runs on identical inputs produce byte-identical documents.  That covers
    the fields ``hal_findings`` itself calls volatile
    (``serialize.VOLATILE_FIELDS``): the ``generated_at`` stamp and the
    ``producer.command``, which records the output directory and so differs
    between two runs that write to different places.  An explicit
    ``generated_at`` is still honoured -- a caller that pins the stamp wants it
    in the document.
    """
    artifacts = [
        common.netlist_artifact(
            netlist_a, artifact_id_a, path=netlist_path_a, description="build A"
        ),
        common.netlist_artifact(
            netlist_b, artifact_id_b, path=netlist_path_b, description="build B"
        ),
    ]
    if correspondence_artifact is not None:
        artifacts.append(correspondence_artifact)

    formal_method = model.method(
        "per-cone SAT equivalence check (hal_py SMT.Solver)",
        "formal",
        False,
        description=_METHOD_DESCRIPTION,
        parameters=options.as_dict(),
    )
    structural_method = model.method(
        "canonical cone signature",
        "structural",
        False,
        description=_STRUCTURAL_DESCRIPTION,
    )
    inspection_method = model.method(
        "netlist and correspondence inspection",
        "structural",
        False,
        description=(
            "Direct inspection of the two netlists and the correspondence file; no "
            "solver involved."
        ),
    )

    assumptions = assumptions_for(options, mapping)

    findings = [
        _problem_finding(problem, inspection_method, artifact_id_a, artifact_id_b)
        for problem in problems
    ]

    primitive_finding = _primitive_finding(
        netlist_a, netlist_b, artifact_id_a, artifact_id_b, inspection_method
    )
    if primitive_finding is not None:
        findings.append(primitive_finding)

    evidence_by_point = evidence_by_point or {}
    for outcome in outcomes:
        findings.append(
            _point_finding(
                outcome,
                artifact_id_a,
                artifact_id_b,
                formal_method,
                structural_method,
                assumptions,
                options,
                evidence_by_point.get(outcome.point.key),
            )
        )

    coverage_finding = _coverage_finding(
        coverage, outcomes, problems, artifact_id_a, artifact_id_b, inspection_method
    )
    if coverage_finding is not None:
        findings.append(coverage_finding)

    findings.append(
        _summary_finding(
            outcomes,
            problems,
            coverage,
            artifact_id_a,
            artifact_id_b,
            formal_method,
            assumptions,
            options,
        )
    )

    analysis = {
        "plugin": {
            "name": ANALYSIS_NAME,
            "version": __version__,
            "description": (
                "Behavioural diff of two synthesized builds: per-observation-point "
                "equivalence checking with changed-cone localization"
            ),
        },
        "entry_point": ENTRY_POINT,
        "configuration": dict(
            options.as_dict(),
            correspondence=mapping.as_dict() if mapping is not None else None,
            solver_backend=solver_backend,
        ),
    }
    if hal_version:
        analysis["hal"] = {"version": str(hal_version)}
    if wall_time_s is not None:
        analysis["duration_s"] = float(wall_time_s)
    if plugin_versions:
        analysis["configuration"]["plugin_versions"] = dict(plugin_versions)

    producer = {"name": "hal_semantic_diff", "version": __version__}
    if producer_command and record_timings:
        producer["command"] = [str(entry) for entry in producer_command]

    if generated_at is None and record_timings:
        generated_at = common.utc_now()

    document = model.document(
        producer,
        artifacts,
        analysis,
        findings,
        generated_at=generated_at,
        notes=[
            "gate and net IDs are scoped to the artifact they are declared under; the "
            "two builds have independent ID spaces",
            "equivalence here is equivalence of next-state and output functions under "
            "the recorded correspondence, not temporal equivalence; retiming and state "
            "re-encoding are outside the model and are reported as differences",
            "an observation point that could not be decided is 'unknown' or 'timeout' "
            "and never contributes to an equivalence claim",
        ],
    )
    return document if record_timings else strip_timings(document)
