"""Wrap ``z3_utils.compare_netlists()`` results in the findings schema.

What the plugin actually returns
--------------------------------
``hal_plugins.z3_utils.compare_netlists(netlist_a, netlist_b, fail_on_unknown,
solver_timeout)`` returns ``Optional[bool]`` -- see
``plugins/z3_utils/python/python_bindings.cpp`` -- and ``None`` means the C++
``Result`` carried an error (already logged by HAL).

Reading ``plugins/z3_utils/src/netlist_comparison.cpp`` is what makes an honest
mapping possible; the bool is far weaker than it looks:

* ``compare_nets_internal`` turns a solver ``unknown`` into ``!fail_on_unknown``.
  So with ``fail_on_unknown=True`` an unknown is reported as *not equivalent*,
  and with ``fail_on_unknown=False`` it is reported as *equivalent*.
* ``compare_netlists`` also returns ``false`` for purely structural reasons: a
  sequential gate without a same-named counterpart, or a matched pair whose gate
  types differ.  Those are not functional counterexamples.
* Only gates with the ``combinational`` property are traversed when building the
  subgraph functions; every other gate's output becomes a free Boolean variable.
* Top-module output pins that exist in only one of the netlists are skipped with
  a warning instead of failing the comparison.

Hence the status mapping below, which never upgrades an ambiguous ``bool`` into
a proof:

===========================  ==================  ==========================
``fail_on_unknown``          returned value      status
===========================  ==================  ==========================
``True``                     ``True``            proven_under_assumptions
``True``                     ``False``           unknown
``False``                    ``True``            unknown
``False``                    ``False``           counterexample
any                          ``None``            error
===========================  ==================  ==========================
"""

import time

from .. import model
from .. import __version__
from . import common

__all__ = [
    "PLUGIN_NAME",
    "ENTRY_POINT",
    "classify",
    "assumptions_for",
    "build_document",
    "run_compare_netlists",
]

PLUGIN_NAME = "z3_utils"
ENTRY_POINT = "z3_utils.compare_netlists"

_METHOD_DESCRIPTION = (
    "SAT equivalence checking with Z3: for every pair of same-named sequential gates the "
    "subgraph functions of their input nets are compared, together with the top-module "
    "output pins present in both netlists. Only combinational gates are traversed; the "
    "outputs of all other gates are free variables."
)

_ASSUMPTIONS = (
    (
        "sequential-gate-name-correspondence",
        "naming",
        "Sequential gates of the two netlists are matched by name (plus the flip-flop "
        "replacement aliases stored in the netlist). A renamed or missing sequential "
        "gate is reported as inequivalence, not as a naming problem.",
    ),
    (
        "combinational-frontier",
        "structural",
        "Only gates carrying the 'combinational' gate type property are traversed when "
        "building subgraph functions. The output of every other gate is an unconstrained "
        "free variable, so differences hidden behind such a gate are invisible.",
    ),
    (
        "matched-state-correspondence",
        "initial_state",
        "Matched sequential gates are assumed to hold the same state, which makes this a "
        "next-state and output function equivalence rather than a temporal one.",
    ),
    (
        "shared-top-level-output-pins",
        "structural",
        "Only top-module output pins present in both netlists are compared; a pin that "
        "exists in one netlist only is skipped with a warning.",
    ),
)


def assumptions_for(fail_on_unknown, solver_timeout):
    """The assumption list every ``compare_netlists`` verdict rests on."""
    assumptions = [
        model.assumption(identifier, description, kind=kind)
        for identifier, kind, description in _ASSUMPTIONS
    ]
    assumptions.append(
        model.assumption(
            "solver-answers-decisive",
            "Each SAT query was given {} s and fail_on_unknown={}; an 'unknown' answer is "
            "folded into the returned boolean by the plugin, so the boolean alone cannot "
            "distinguish an undecided query from a decided one.".format(
                solver_timeout, bool(fail_on_unknown)
            ),
            kind="tool",
        )
    )
    return assumptions


def classify(equivalent, fail_on_unknown):
    """Map ``(returned value, fail_on_unknown)`` to a status and its rationale."""
    if equivalent is None:
        return (
            model.STATUS_ERROR,
            "compare_netlists returned None: the underlying Result carried an error "
            "(details are in the HAL log).",
        )
    if fail_on_unknown:
        if equivalent:
            return (
                model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
                "With fail_on_unknown=True every undecided query would have produced "
                "False, so a True result means all equivalence queries were discharged.",
            )
        return (
            model.STATUS_UNKNOWN,
            "With fail_on_unknown=True a False result conflates three causes -- a real "
            "functional difference, a structural mismatch (missing or differently typed "
            "sequential gate), and an undecided solver query -- so no verdict can be "
            "reported.",
        )
    if equivalent:
        return (
            model.STATUS_UNKNOWN,
            "With fail_on_unknown=False an undecided query is reported as equivalence, so "
            "a True result cannot be distinguished from a proof and must not be presented "
            "as one. Rerun with fail_on_unknown=True to obtain a proof.",
        )
    return (
        model.STATUS_COUNTEREXAMPLE,
        "With fail_on_unknown=False undecided queries are reported as equivalence, so a "
        "False result is a decided inequivalence (functional or structural).",
    )


def _sequential_names(netlist):
    names = {}
    for gate in common.call(netlist, "get_gates", default=[]) or []:
        gate_type = common.call(gate, "get_type")
        if gate_type is None:
            continue
        if "sequential" not in common.gate_type_properties(gate_type):
            continue
        names[common.call(gate, "get_name", default="")] = gate
    return names


def _unmodelled_gate_types(netlist, artifact_id):
    """Gate types that are neither traversed nor matched by the comparison."""
    primitives = {}
    for gate in common.call(netlist, "get_gates", default=[]) or []:
        gate_type = common.call(gate, "get_type")
        if gate_type is None:
            continue
        properties = common.gate_type_properties(gate_type)
        if "combinational" in properties or "sequential" in properties:
            continue
        name = common.call(gate_type, "get_name", default="<unnamed>")
        entry = primitives.setdefault(
            name, {"count": 0, "properties": properties, "gates": []}
        )
        entry["count"] += 1
        if len(entry["gates"]) < 3:
            entry["gates"].append(common.gate_reference(gate, artifact_id))
    return primitives


def _precondition_findings(netlist_a, netlist_b, id_a, id_b, method):
    """Findings for preconditions of compare_netlists that this run can check itself."""
    findings = []

    names_a = _sequential_names(netlist_a)
    names_b = _sequential_names(netlist_b)
    only_a = sorted(set(names_a) - set(names_b))
    only_b = sorted(set(names_b) - set(names_a))
    if only_a or only_b:
        findings.append(
            model.finding(
                "z3_utils/compare_netlists/precondition/sequential-gate-names",
                "Sequential gate names do not correspond between the two netlists",
                model.STATUS_UNSUPPORTED,
                method,
                model.scope(
                    [id_a, id_b],
                    description="sequential gates without a same-named counterpart",
                    gates=(
                        [common.gate_reference(names_a[name], id_a) for name in only_a[:20]]
                        + [common.gate_reference(names_b[name], id_b) for name in only_b[:20]]
                    ),
                ),
                summary=(
                    "{} sequential gate(s) of {!r} and {} of {!r} have no same-named "
                    "counterpart. compare_netlists reports this as inequivalence, so any "
                    "False verdict from this run is a naming mismatch first and foremost."
                    .format(len(only_a), id_a, len(only_b), id_b)
                ),
                severity="medium",
                unsupported_dict=model.unsupported(
                    "construct",
                    "compare_netlists matches sequential gates by name and has no "
                    "renaming interface (the C++ implementation carries a TODO for a "
                    "user-provided name mapping); unmatched names cannot be compared",
                ),
                data={
                    "unmatched_in_a": only_a[:100],
                    "unmatched_in_b": only_b[:100],
                    "unmatched_count_a": len(only_a),
                    "unmatched_count_b": len(only_b),
                },
                tags=["precondition", "equivalence"],
            )
        )

    primitives = {}
    for netlist, artifact_id in ((netlist_a, id_a), (netlist_b, id_b)):
        for name, entry in _unmodelled_gate_types(netlist, artifact_id).items():
            merged = primitives.setdefault(
                name, {"count": 0, "properties": entry["properties"], "gates": []}
            )
            merged["count"] += entry["count"]
            merged["gates"].extend(entry["gates"])
    if primitives:
        findings.append(
            model.finding(
                "z3_utils/compare_netlists/coverage/unmodelled-primitives",
                "Gate types that the equivalence check neither traverses nor matches",
                model.STATUS_UNSUPPORTED,
                method,
                model.scope(
                    [id_a, id_b],
                    description="gate types without the combinational or sequential property",
                    gate_types=sorted(primitives),
                ),
                summary=(
                    "{} gate type(s) carry neither the 'combinational' nor the "
                    "'sequential' property. Their outputs enter the queries as free "
                    "variables and they are not matched between the netlists, so "
                    "differences involving them are outside this check.".format(
                        len(primitives)
                    )
                ),
                severity="medium",
                unsupported_dict=model.unsupported(
                    "primitive",
                    "compare_netlists traverses combinational gates and matches sequential "
                    "gates by name; any other gate type is modelled as a free variable",
                    [
                        model.unsupported_primitive(
                            name,
                            "gate type has neither the 'combinational' nor the "
                            "'sequential' property, so its output is an unconstrained "
                            "free variable in every query",
                            count=entry["count"],
                            properties=entry["properties"],
                            example_gates=entry["gates"][:6],
                        )
                        for name, entry in sorted(primitives.items())
                    ],
                ),
                tags=["coverage", "equivalence"],
            )
        )

    return findings


def build_document(
    netlist_a,
    netlist_b,
    equivalent,
    fail_on_unknown=True,
    solver_timeout=10,
    artifact_id_a="netlist_a",
    artifact_id_b="netlist_b",
    plugin_version="unknown",
    hal_version=None,
    wall_time_s=None,
    timed_out=False,
    error_message=None,
    netlist_path_a=None,
    netlist_path_b=None,
    generated_at=None,
    producer_command=None,
    evidence_list=None,
):
    """Build a findings document from a ``z3_utils.compare_netlists`` verdict.

    :param equivalent: exactly what the binding returned (``True``/``False``/``None``).
    :param timed_out: set by the caller when it aborted the run at a wall-clock
        budget; produces a ``timeout`` finding instead of interpreting the value.
    """
    artifacts = [
        common.netlist_artifact(netlist_a, artifact_id_a, path=netlist_path_a),
        common.netlist_artifact(netlist_b, artifact_id_b, path=netlist_path_b),
    ]

    method = model.method(
        "SAT equivalence check (z3_utils.compare_netlists)",
        "formal",
        False,
        description=_METHOD_DESCRIPTION,
        parameters={
            "fail_on_unknown": bool(fail_on_unknown),
            "solver_timeout_s": int(solver_timeout),
        },
    )
    structural_method = model.method(
        "netlist precondition check",
        "structural",
        False,
        description=(
            "Direct inspection of the two netlists for the preconditions "
            "compare_netlists relies on; no solver involved."
        ),
    )

    solver = model.solver("z3", wall_time_s=wall_time_s)
    limits = model.limits(
        timeout_s=float(solver_timeout),
        wall_time_s=wall_time_s,
        hit=bool(timed_out),
        description="per-query SAT solver timeout passed to compare_netlists",
    )
    assumptions = assumptions_for(fail_on_unknown, solver_timeout)
    scope = model.scope(
        [artifact_id_a, artifact_id_b],
        description="functional equivalence of the two netlists",
    )

    findings = _precondition_findings(
        netlist_a, netlist_b, artifact_id_a, artifact_id_b, structural_method
    )

    finding_id = "z3_utils/compare_netlists/equivalence"
    title = "Functional equivalence of {} and {}".format(artifact_id_a, artifact_id_b)

    if timed_out:
        findings.append(
            model.finding(
                finding_id,
                title,
                model.STATUS_TIMEOUT,
                method,
                scope,
                summary=(
                    "The equivalence check was aborted at a resource limit; no verdict "
                    "was reached."
                ),
                severity="info",
                assumptions=assumptions,
                solver_dict=solver,
                limits_dict=limits,
                evidence_list=evidence_list,
                tags=["equivalence"],
            )
        )
        return _document(
            artifacts, findings, fail_on_unknown, solver_timeout, plugin_version,
            hal_version, wall_time_s, generated_at, producer_command
        )

    status, rationale = classify(equivalent, fail_on_unknown)

    kwargs = {
        "summary": rationale,
        "assumptions": assumptions,
        "solver_dict": solver,
        "limits_dict": limits,
        "evidence_list": evidence_list,
        "tags": ["equivalence"],
    }

    if status == model.STATUS_PROVEN_UNDER_ASSUMPTIONS:
        kwargs["bounds_dict"] = model.unbounded(
            description=(
                "The check compares next-state and output functions with sequential gate "
                "outputs as free variables, so the claim holds for any number of cycles "
                "given the matched-state assumption."
            )
        )
        kwargs["severity"] = "info"
    elif status == model.STATUS_COUNTEREXAMPLE:
        kwargs["bounds_dict"] = model.unbounded(
            description="Inequivalence of the compared functions is not cycle bounded."
        )
        kwargs["counterexample_dict"] = model.counterexample(
            "compare_netlists reported inequivalence. The binding returns only a boolean, "
            "so no satisfying assignment is available; rerun the underlying compare_nets "
            "on the individual net pairs to obtain a witness.",
            witness_available=False,
        )
        kwargs["severity"] = "high"
    elif status == model.STATUS_ERROR:
        kwargs["error_dict"] = model.error(
            "plugin_error",
            error_message
            or "z3_utils.compare_netlists returned None; see the HAL log for the "
            "underlying error",
        )
        kwargs["severity"] = "medium"
    else:
        kwargs["severity"] = "info"

    findings.append(model.finding(finding_id, title, status, method, scope, **kwargs))

    return _document(
        artifacts, findings, fail_on_unknown, solver_timeout, plugin_version, hal_version,
        wall_time_s, generated_at, producer_command
    )


def _document(
    artifacts, findings, fail_on_unknown, solver_timeout, plugin_version, hal_version,
    wall_time_s, generated_at, producer_command
):
    analysis = {
        "plugin": {
            "name": PLUGIN_NAME,
            "version": str(plugin_version),
            "description": "Z3 based netlist utilities",
        },
        "entry_point": ENTRY_POINT,
        "configuration": {
            "fail_on_unknown": bool(fail_on_unknown),
            "solver_timeout_s": int(solver_timeout),
        },
    }
    if hal_version:
        analysis["hal"] = {"version": str(hal_version)}
    if wall_time_s is not None:
        analysis["duration_s"] = float(wall_time_s)

    producer = {"name": "hal_findings.adapters.netlist_comparison", "version": __version__}
    if producer_command:
        producer["command"] = list(producer_command)

    return model.document(
        producer,
        artifacts,
        analysis,
        findings,
        generated_at=generated_at if generated_at is not None else common.utc_now(),
        notes=[
            "gate and net IDs are scoped to the artifact they are declared under; the two "
            "netlists have independent ID spaces",
            "a boolean from compare_netlists is only a proof when fail_on_unknown=True",
        ],
    )


def run_compare_netlists(
    z3_utils_module, netlist_a, netlist_b, fail_on_unknown=True, solver_timeout=10, **kwargs
):
    """Run the comparison and wrap the outcome, turning exceptions into findings."""
    started = time.time()
    try:
        equivalent = z3_utils_module.compare_netlists(
            netlist_a, netlist_b, fail_on_unknown, solver_timeout
        )
        error_message = None
    except Exception as exc:  # the plugin raised instead of returning
        equivalent = None
        error_message = "{}: {}".format(type(exc).__name__, exc)
    wall_time_s = round(time.time() - started, 3)

    kwargs.setdefault("wall_time_s", wall_time_s)
    return build_document(
        netlist_a,
        netlist_b,
        equivalent,
        fail_on_unknown=fail_on_unknown,
        solver_timeout=solver_timeout,
        error_message=error_message,
        **kwargs
    )
