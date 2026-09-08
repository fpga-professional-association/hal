"""Module identification: verified arithmetic -> blocks (and the findings adapter).

Unlike dataflow and ``hal_fsm``, ``module_identification`` has no adapter in
``tools/hal_findings`` yet, so this module does both jobs:

``build_document(result, ...)``
    wraps a ``module_identification.Result`` in a findings document.  It is
    written against the plugin's Python bindings -- the accessors declared in
    ``plugins/module_identification/python/python_bindings.cpp`` --

        Result.get_netlist()                 -> hal_py.Netlist
        Result.get_candidates()              -> dict[int, VerifiedCandidate]
        Result.get_verified_candidates()     -> dict[int, VerifiedCandidate]
        Result.get_candidate_gates()         -> dict[int, list[hal_py.Gate]]
        Result.get_verified_candidate_gates()-> dict[int, list[hal_py.Gate]]
        Result.get_timing_stats()            -> str (JSON)
        VerifiedCandidate.is_verified()      -> bool
        VerifiedCandidate.get_name()         -> str
        VerifiedCandidate.types              -> set[CandidateType]
        VerifiedCandidate.gates / base_gates -> list[hal_py.Gate]
        VerifiedCandidate.operands           -> list[list[hal_py.Net]]
        VerifiedCandidate.output_nets        -> list[hal_py.Net]
        VerifiedCandidate.control_signals    -> list[hal_py.Net]
        VerifiedCandidate.total_input_nets / total_output_nets -> list[hal_py.Net]

    -- but it imports nothing from HAL: every accessor goes through
    ``hal_findings.adapters.common.call``, so the module is unit-testable
    against stubs and a renamed binding degrades into a missing field.

``contributions(source, inventory)``
    turns that document (or one written by any equivalent adapter) into blocks.

The status split is the reason this adapter exists at all.  A candidate the
plugin *verified* is the only thing in this whole tool that gets
``proven_under_assumptions``: the plugin proved, with an SMT solver, that the
gate cone implements the word-level operation it names.  A candidate that was
checked and not verified is ``unknown`` -- **not** "no arithmetic here".  The
two must never be rendered the same way, and the block model carries the
distinction as ``confidence: verified`` vs ``confidence: unknown``.
"""

import json

from hal_findings import model as findings_model
from hal_findings.adapters import common as findings_common

from .. import model
from . import common

__all__ = [
    "PLUGIN_NAMES",
    "FINDING_PREFIXES",
    "ADAPTER_NAME",
    "PLUGIN_NAME",
    "ENTRY_POINT",
    "CANDIDATE_KIND",
    "candidate_type_names",
    "block_kind_for_types",
    "build_document",
    "contributions",
]

ADAPTER_NAME = "module_identification"
PLUGIN_NAME = "module_identification"
ENTRY_POINT = "module_identification.execute"
PLUGIN_NAMES = ("module_identification",)
FINDING_PREFIXES = ("module_identification/",)

_CANDIDATE_PREFIX = "module_identification/candidate/"

#: ``module_identification.CandidateType`` -> recovered-block kind.
CANDIDATE_KIND = {
    "addition": "arithmetic",
    "addition_offset": "arithmetic",
    "subtraction": "arithmetic",
    "negation": "arithmetic",
    "absolute": "arithmetic",
    "constant_multiplication": "arithmetic",
    "constant_multiplication_offset": "arithmetic",
    "counter": "counter",
    "equal": "comparator",
    "less_than": "comparator",
    "less_equal": "comparator",
    "signed_less_than": "comparator",
    "signed_less_equal": "comparator",
    "value_check": "comparator",
    "mixed": "datapath",
    "none": "other",
}

_METHOD_DESCRIPTION = (
    "module_identification enumerates structural candidates around known registers, "
    "builds the word-level function of each candidate's gate cone, and checks it "
    "against a library of reference operations with an SMT solver. A verified "
    "candidate is an equivalence result about the cone, not a naming convention."
)

_VERIFIED_ASSUMPTIONS = (
    (
        "candidate-boundary",
        "the gate set of the candidate comes from a structural search around the "
        "known registers; the verification says what this cone computes, not that "
        "the cone is the whole functional unit a designer would have drawn",
        "structural",
    ),
    (
        "library-semantics",
        "the Boolean function of every gate type is taken from the gate library "
        "without further checking",
        "library",
    ),
    (
        "control-mapping-coverage",
        "the operation was verified for the control-signal mappings the run "
        "enumerated (bounded by max_control_signals); mappings outside that set "
        "were not checked",
        "tool",
    ),
)


def _name_of(value):
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value).rsplit(".", 1)[-1]


def candidate_type_names(candidate):
    """Sorted ``CandidateType`` names of a candidate, as plain strings."""
    types = getattr(candidate, "types", None)
    if types is None:
        types = findings_common.call(candidate, "get_types", default=[])
    return sorted({_name_of(entry) for entry in types or []})


def block_kind_for_types(type_names):
    """The block kind for a set of candidate types.

    A candidate that mixes kinds becomes ``datapath`` rather than being filed
    under whichever type happened to sort first.
    """
    kinds = {CANDIDATE_KIND.get(name, "other") for name in type_names or []}
    kinds.discard("other")
    if not kinds:
        return "other"
    if len(kinds) == 1:
        return kinds.pop()
    return "datapath"


# ---------------------------------------------------------------------------
# module_identification.Result -> findings document
# ---------------------------------------------------------------------------


def _net_names(nets, artifact_id):
    refs = []
    for net in nets or []:
        refs.append(findings_common.net_reference(net, artifact_id))
    return refs


def _operand_widths(candidate):
    widths = []
    for operand in getattr(candidate, "operands", None) or []:
        try:
            widths.append(len(operand))
        except TypeError:  # pragma: no cover - defensive
            continue
    return widths


def _candidate_finding(candidate_id, candidate, gates, artifact_id, method, verified):
    gate_refs = [findings_common.gate_reference(gate, artifact_id) for gate in gates]
    if not gate_refs:
        return None
    type_names = candidate_type_names(candidate)
    name = findings_common.call(candidate, "get_name", default="") or ""
    info = findings_common.call(candidate, "get_candidate_info", default="") or ""
    operand_widths = _operand_widths(candidate)
    output_nets = _net_names(getattr(candidate, "output_nets", None), artifact_id)
    control_nets = _net_names(getattr(candidate, "control_signals", None), artifact_id)

    data = {
        "candidate_id": int(candidate_id),
        "candidate_id_note": (
            "module_identification candidate IDs are internal to this run and are not "
            "HAL object IDs"
        ),
        "candidate_types": type_names,
        "operand_widths": operand_widths,
        "output_width": len(output_nets),
    }
    if name:
        data["operation"] = name
    if info:
        data["candidate_info"] = info

    if verified:
        title = "Verified {}: {}".format(
            "/".join(type_names) or "word-level operation", name or "unnamed operation"
        )
        summary = (
            "module_identification verified that the {} gate(s) of this candidate "
            "implement {}. The equivalence was decided by the plugin's SMT backend; "
            "the listed assumptions are what it rests on.".format(
                len(gate_refs), name or "the named word-level operation"
            )
        )
        return findings_model.finding(
            "{}{:04d}".format(_CANDIDATE_PREFIX, int(candidate_id)),
            title,
            findings_model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
            method,
            findings_model.scope(
                [artifact_id],
                description="the gate cone of one verified word-level operation",
                gates=gate_refs,
                nets=(output_nets + control_nets) or None,
            ),
            summary=summary,
            severity="info",
            assumptions=[
                findings_model.assumption(key, description, kind=kind, discharged=False)
                for key, description, kind in _VERIFIED_ASSUMPTIONS
            ],
            bounds_dict=findings_model.unbounded(
                description="a combinational equivalence result; no cycle bound is "
                "involved"
            ),
            metrics={"gate_count": len(gate_refs), "output_width": len(output_nets)},
            data=data,
            tags=["module-identification", "verified"] + type_names,
        )

    return findings_model.finding(
        "{}{:04d}".format(_CANDIDATE_PREFIX, int(candidate_id)),
        "Unverified candidate: {} gate(s), no word-level operation established".format(
            len(gate_refs)
        ),
        findings_model.STATUS_UNKNOWN,
        method,
        findings_model.scope(
            [artifact_id],
            description="a checked candidate cone with no verified operation",
            gates=gate_refs,
        ),
        summary=(
            "This cone was checked and no reference operation could be verified for "
            "it. That is not evidence that the cone computes nothing: the operation "
            "may simply be outside the plugin's reference library, or the run's "
            "control-signal budget may have cut the search short."
        ),
        severity="info",
        data=data,
        tags=["module-identification", "unverified"],
    )


def build_document(
    result,
    artifact_id="netlist",
    configuration=None,
    plugin_version="unknown",
    hal_version=None,
    netlist_path=None,
    duration_s=None,
    generated_at=None,
    producer_command=None,
):
    """Wrap a ``module_identification.Result`` in a findings document."""
    from .. import __version__

    netlist = findings_common.call(result, "get_netlist")
    artifact = findings_common.netlist_artifact(netlist, artifact_id, path=netlist_path)

    method = findings_model.method(
        "module identification",
        "formal",
        False,
        description=_METHOD_DESCRIPTION,
        parameters=configuration,
    )

    verified_map = findings_common.call(result, "get_verified_candidates", default={}) or {}
    verified_gate_map = (
        findings_common.call(result, "get_verified_candidate_gates", default={}) or {}
    )
    all_map = findings_common.call(result, "get_candidates", default={}) or {}
    all_gate_map = findings_common.call(result, "get_candidate_gates", default={}) or {}

    verified_ids = {int(key) for key in verified_map}
    findings = []
    for candidate_id in sorted({int(key) for key in all_map} | verified_ids):
        verified = candidate_id in verified_ids
        candidate = verified_map.get(candidate_id) if verified else None
        if candidate is None:
            candidate = all_map.get(candidate_id)
        gates = (
            verified_gate_map.get(candidate_id)
            if verified
            else all_gate_map.get(candidate_id)
        )
        if gates is None:
            gates = getattr(candidate, "gates", None) or []
        entry = _candidate_finding(
            candidate_id, candidate, gates, artifact_id, method, verified
        )
        if entry is not None:
            findings.append(entry)

    notes = [
        "module_identification candidate IDs are local to this run; gate references "
        "are scoped to artifact {!r}".format(artifact_id),
        "a candidate with status 'unknown' was checked and not verified; absence of a "
        "verified operation is never evidence that the cone computes nothing",
    ]

    analysis = {
        "plugin": {
            "name": PLUGIN_NAME,
            "version": str(plugin_version),
            "description": "verified word-level operation recovery",
        },
        "entry_point": ENTRY_POINT,
    }
    if configuration:
        analysis["configuration"] = configuration
    if hal_version:
        analysis["hal"] = {"version": str(hal_version)}
    if duration_s is not None:
        analysis["duration_s"] = float(duration_s)
    timing = findings_common.call(result, "get_timing_stats", default="")
    if timing:
        # The findings schema has no slot for per-plugin statistics; ``environment``
        # is the free-form one, and dropping the numbers would lose the only record
        # of how long the proofs took.
        try:
            analysis["environment"] = {"module_identification_timing_stats": json.loads(timing)}
        except (TypeError, ValueError):
            notes.append("the plugin's timing statistics were not valid JSON and were dropped")

    producer = {"name": "hal_explain.adapters.module_identification", "version": __version__}
    if producer_command:
        producer["command"] = [str(part) for part in producer_command]

    return findings_model.document(
        producer,
        [artifact],
        analysis,
        findings,
        generated_at=generated_at
        if generated_at is not None
        else findings_common.utc_now(),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# findings document -> block contributions
# ---------------------------------------------------------------------------


def _claim_text(finding, gate_ids, inventory):
    data = finding.get("data") or {}
    operation = data.get("operation")
    type_names = data.get("candidate_types") or []
    widths = data.get("operand_widths") or []
    if finding.get("status") == "proven_under_assumptions":
        text = (
            "The {} gate(s) {} implement {}, verified by module_identification "
            "against its reference operations.".format(
                len(gate_ids),
                common.gate_name_list(inventory, gate_ids),
                operation or "/".join(type_names) or "a word-level operation",
            )
        )
        if widths:
            text += " Operand width(s): {}.".format(
                ", ".join(str(width) for width in widths)
            )
        if data.get("output_width"):
            text += " Output width: {}.".format(data["output_width"])
        return text
    return (
        "The {} gate(s) {} were checked by module_identification and no reference "
        "operation could be verified for them. This is an inconclusive result, not "
        "a statement that the cone computes nothing.".format(
            len(gate_ids), common.gate_name_list(inventory, gate_ids)
        )
    )


def contributions(source, inventory):
    """Return ``(contributions, notes)`` for one module_identification document."""
    results = []
    notes = []

    for order, finding in enumerate(
        sorted(source.by_prefix(_CANDIDATE_PREFIX), key=lambda entry: entry.get("id", ""))
    ):
        scope = finding.get("scope") or {}
        gate_ids = common.resolve_gates(
            scope.get("gates"), inventory, source, finding.get("id", "?")
        )
        if not gate_ids:
            notes.append(
                "module_identification finding {!r} claims gates that are not in this "
                "netlist; it was skipped".format(finding.get("id"))
            )
            continue
        data = finding.get("data") or {}
        type_names = data.get("candidate_types") or []
        kind = block_kind_for_types(type_names)
        verified = finding.get("status") == "proven_under_assumptions"
        operation = data.get("operation")
        label = (
            "{}".format(operation or "/".join(type_names) or "word-level operation")
            if verified
            else "unverified cone [{} gates]".format(len(gate_ids))
        )
        attributes = {
            "candidate_types": type_names or None,
            "operation": operation,
            "operand_widths": data.get("operand_widths") or None,
            "output_width": data.get("output_width"),
            "verified": verified,
        }
        attributes = {key: value for key, value in attributes.items() if value is not None}
        claim = model.claim(
            "{}/{}".format(source.source_id, finding["id"]),
            _claim_text(finding, gate_ids, inventory),
            finding.get("status", "unknown"),
            gate_ids,
            [common.evidence_from_finding(source, finding)],
            metrics=finding.get("metrics"),
        )
        results.append(
            common.Contribution(
                finding["id"],
                kind if verified else "other",
                label,
                gate_ids,
                [claim],
                source.source_id,
                attributes=attributes,
                order=order,
            )
        )

    if not results:
        notes.append(
            "the module_identification document {!r} contained no candidates".format(
                source.source_id
            )
        )
    return results, notes
