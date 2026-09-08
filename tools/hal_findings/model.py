"""Builders for findings documents.

Every builder returns a plain ``dict`` -- the schema is the contract, not any
Python class -- but the builders drop ``None`` fields, keep key order
irrelevant (serialization sorts keys), and refuse the contradictions the schema
also rejects, so mistakes surface at the call site instead of at validation
time.

Status vocabulary (see the schema for the normative wording)::

    proven_under_assumptions  holds for every execution, given `assumptions`
    proven_bounded            holds only up to `bounds.cycle_bound`
    counterexample            refuted; no cycle bound involved
    bounded_counterexample    refuted within `bounds.cycle_bound`
    heuristic                 evidence-based guess, never a proof
    unknown                   no verdict (e.g. a solver returned unknown)
    timeout                   aborted at a resource limit
    error                     the analysis itself failed
    unsupported               outside what the analysis covers
"""

from .schema import SCHEMA_VERSION

__all__ = [
    "STATUS_PROVEN_UNDER_ASSUMPTIONS",
    "STATUS_PROVEN_BOUNDED",
    "STATUS_COUNTEREXAMPLE",
    "STATUS_BOUNDED_COUNTEREXAMPLE",
    "STATUS_HEURISTIC",
    "STATUS_UNKNOWN",
    "STATUS_TIMEOUT",
    "STATUS_ERROR",
    "STATUS_UNSUPPORTED",
    "STATUSES",
    "PROOF_STATUSES",
    "REFUTATION_STATUSES",
    "INCONCLUSIVE_STATUSES",
    "BOUNDED_STATUSES",
    "METHOD_KINDS",
    "is_unbounded_proof",
    "is_bounded_claim",
    "artifact",
    "gate_ref",
    "net_ref",
    "module_ref",
    "scope",
    "method",
    "assumption",
    "bounds",
    "unbounded",
    "bounded",
    "solver",
    "limits",
    "evidence",
    "witness_entry",
    "counterexample",
    "unsupported_primitive",
    "unsupported",
    "error",
    "finding",
    "document",
]

STATUS_PROVEN_UNDER_ASSUMPTIONS = "proven_under_assumptions"
STATUS_PROVEN_BOUNDED = "proven_bounded"
STATUS_COUNTEREXAMPLE = "counterexample"
STATUS_BOUNDED_COUNTEREXAMPLE = "bounded_counterexample"
STATUS_HEURISTIC = "heuristic"
STATUS_UNKNOWN = "unknown"
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"
STATUS_UNSUPPORTED = "unsupported"

STATUSES = (
    STATUS_PROVEN_UNDER_ASSUMPTIONS,
    STATUS_PROVEN_BOUNDED,
    STATUS_COUNTEREXAMPLE,
    STATUS_BOUNDED_COUNTEREXAMPLE,
    STATUS_HEURISTIC,
    STATUS_UNKNOWN,
    STATUS_TIMEOUT,
    STATUS_ERROR,
    STATUS_UNSUPPORTED,
)

#: Statuses that assert a property holds.
PROOF_STATUSES = (STATUS_PROVEN_UNDER_ASSUMPTIONS, STATUS_PROVEN_BOUNDED)
#: Statuses that assert a property does not hold.
REFUTATION_STATUSES = (STATUS_COUNTEREXAMPLE, STATUS_BOUNDED_COUNTEREXAMPLE)
#: Statuses that assert nothing about the property itself.
INCONCLUSIVE_STATUSES = (
    STATUS_HEURISTIC,
    STATUS_UNKNOWN,
    STATUS_TIMEOUT,
    STATUS_ERROR,
    STATUS_UNSUPPORTED,
)
#: Statuses whose claim is only valid up to a cycle bound.
BOUNDED_STATUSES = (STATUS_PROVEN_BOUNDED, STATUS_BOUNDED_COUNTEREXAMPLE)

METHOD_KINDS = (
    "formal",
    "bounded_formal",
    "symbolic",
    "structural",
    "heuristic",
    "simulation",
    "statistical",
)

_ARTIFACT_KINDS = ("netlist", "hal_project", "gate_library", "other")
_EVIDENCE_KINDS = (
    "file",
    "directory",
    "log",
    "dot",
    "waveform",
    "trace",
    "smt2",
    "report",
    "inline",
    "command",
)
_UNSUPPORTED_KINDS = ("primitive", "construct", "configuration", "format", "scale")
_ERROR_KINDS = ("exception", "plugin_error", "io", "invalid_input", "internal", "resource")
_ASSUMPTION_KINDS = (
    "structural",
    "naming",
    "environment",
    "library",
    "initial_state",
    "user_provided",
    "tool",
)


def is_unbounded_proof(finding_dict):
    """True only for a proof that is *not* limited to a cycle bound.

    Consumers that must not mistake a bounded result for a full proof should
    gate on this helper rather than on the status string alone.
    """
    if finding_dict.get("status") != STATUS_PROVEN_UNDER_ASSUMPTIONS:
        return False
    return bool(finding_dict.get("bounds", {}).get("unbounded"))


def is_bounded_claim(finding_dict):
    """True if the finding's claim is only valid up to ``bounds.cycle_bound``."""
    finding_bounds = finding_dict.get("bounds")
    if not finding_bounds:
        return False
    return not finding_bounds.get("unbounded", False)


def _drop_none(mapping):
    return {key: value for key, value in mapping.items() if value is not None}


def _require(value, name):
    if value is None or (isinstance(value, str) and not value):
        raise ValueError("{} is required".format(name))
    return value


def _check_choice(value, choices, name):
    if value not in choices:
        raise ValueError("{} must be one of {}, got {!r}".format(name, list(choices), value))
    return value


def artifact(
    artifact_id,
    kind="netlist",
    path=None,
    sha256=None,
    unhashed_reason=None,
    size_bytes=None,
    design_name=None,
    device_name=None,
    netlist_id=None,
    gate_count=None,
    net_count=None,
    gate_library=None,
    description=None,
):
    """Describe an exact input artifact that gate/net IDs are scoped to."""
    _require(artifact_id, "artifact_id")
    _check_choice(kind, _ARTIFACT_KINDS, "kind")
    if not sha256 and not unhashed_reason:
        raise ValueError(
            "artifact {!r} needs either a sha256 or an explicit unhashed_reason so "
            "that unreproducible inputs stay visible".format(artifact_id)
        )
    return _drop_none(
        {
            "artifact_id": artifact_id,
            "kind": kind,
            "path": path,
            "sha256": sha256,
            "unhashed_reason": unhashed_reason,
            "size_bytes": size_bytes,
            "design_name": design_name,
            "device_name": device_name,
            "netlist_id": netlist_id,
            "gate_count": gate_count,
            "net_count": net_count,
            "gate_library": gate_library,
            "description": description,
        }
    )


def gate_ref(artifact_id, gate_id, name, gate_type=None, module=None):
    """Reference a gate *within a specific artifact*.

    HAL gate IDs are only unique inside one netlist, so a reference without an
    ``artifact_id`` is meaningless as soon as two netlists are involved (as in
    an equivalence check).  The schema therefore makes it mandatory.
    """
    return _drop_none(
        {
            "artifact_id": _require(artifact_id, "artifact_id"),
            "id": _require(gate_id, "id"),
            "name": name if name is not None else "",
            "type": gate_type,
            "module": module,
        }
    )


def net_ref(artifact_id, net_id, name, role=None):
    """Reference a net within a specific artifact."""
    return _drop_none(
        {
            "artifact_id": _require(artifact_id, "artifact_id"),
            "id": _require(net_id, "id"),
            "name": name if name is not None else "",
            "role": role,
        }
    )


def module_ref(artifact_id, module_id, name, module_type=None):
    """Reference a module within a specific artifact."""
    return _drop_none(
        {
            "artifact_id": _require(artifact_id, "artifact_id"),
            "id": _require(module_id, "id"),
            "name": name if name is not None else "",
            "type": module_type,
        }
    )


def scope(artifact_ids, description=None, gates=None, nets=None, modules=None, gate_types=None):
    """Bind a finding to the artifacts (and optionally objects) it is about."""
    ids = list(artifact_ids or [])
    if not ids:
        raise ValueError("a finding scope must name at least one artifact_id")
    return _drop_none(
        {
            "artifact_ids": ids,
            "description": description,
            "gates": list(gates) if gates else None,
            "nets": list(nets) if nets else None,
            "modules": list(modules) if modules else None,
            "gate_types": sorted(gate_types) if gate_types else None,
        }
    )


def method(name, kind, is_bounded, description=None, parameters=None):
    """Describe how the claim was obtained; ``is_bounded`` is mandatory."""
    _check_choice(kind, METHOD_KINDS, "kind")
    if not isinstance(is_bounded, bool):
        raise ValueError("method 'bounded' must be a bool, got {!r}".format(is_bounded))
    return _drop_none(
        {
            "name": _require(name, "name"),
            "kind": kind,
            "bounded": is_bounded,
            "description": description,
            "parameters": parameters,
        }
    )


def assumption(assumption_id, description, kind=None, discharged=None, evidence_list=None):
    """An assumption the claim rests on."""
    if kind is not None:
        _check_choice(kind, _ASSUMPTION_KINDS, "kind")
    return _drop_none(
        {
            "id": _require(assumption_id, "id"),
            "description": _require(description, "description"),
            "kind": kind,
            "discharged": discharged,
            "evidence": list(evidence_list) if evidence_list else None,
        }
    )


def bounds(is_unbounded, cycle_bound=None, unroll_depth=None, input_bound=None, description=None):
    """State whether the claim is unbounded, or up to which cycle bound it holds."""
    if not isinstance(is_unbounded, bool):
        raise ValueError("bounds 'unbounded' must be a bool, got {!r}".format(is_unbounded))
    if is_unbounded and cycle_bound is not None:
        raise ValueError("an unbounded claim must not carry a cycle_bound")
    if not is_unbounded and cycle_bound is None and unroll_depth is None:
        raise ValueError(
            "a bounded claim must record the bound it holds under "
            "(cycle_bound and/or unroll_depth)"
        )
    return _drop_none(
        {
            "unbounded": is_unbounded,
            "cycle_bound": cycle_bound,
            "unroll_depth": unroll_depth,
            "input_bound": input_bound,
            "description": description,
        }
    )


def unbounded(description=None):
    """Shorthand for ``bounds(True, ...)``."""
    return bounds(True, description=description)


def bounded(cycle_bound, unroll_depth=None, description=None):
    """Shorthand for ``bounds(False, cycle_bound=...)``."""
    return bounds(False, cycle_bound=cycle_bound, unroll_depth=unroll_depth, description=description)


def solver(
    name,
    version=None,
    queries=None,
    sat=None,
    unsat=None,
    unknown=None,
    wall_time_s=None,
    options=None,
):
    """Describe the decision procedure that produced the claim."""
    return _drop_none(
        {
            "name": _require(name, "name"),
            "version": version,
            "queries": queries,
            "sat": sat,
            "unsat": unsat,
            "unknown": unknown,
            "wall_time_s": wall_time_s,
            "options": options,
        }
    )


def limits(
    timeout_s=None,
    wall_time_s=None,
    memory_mb=None,
    query_limit=None,
    cycle_limit=None,
    hit=None,
    description=None,
):
    """Resource limits the run was subject to (mandatory for ``timeout``)."""
    return _drop_none(
        {
            "timeout_s": timeout_s,
            "wall_time_s": wall_time_s,
            "memory_mb": memory_mb,
            "query_limit": query_limit,
            "cycle_limit": cycle_limit,
            "hit": hit,
            "description": description,
        }
    )


def evidence(kind, description=None, path=None, sha256=None, media_type=None, inline=None,
             command=None):
    """A pointer to material backing the finding."""
    _check_choice(kind, _EVIDENCE_KINDS, "kind")
    if path is None and inline is None and command is None:
        raise ValueError("evidence needs one of path, inline or command")
    return _drop_none(
        {
            "kind": kind,
            "description": description,
            "path": path,
            "sha256": sha256,
            "media_type": media_type,
            "inline": inline,
            "command": list(command) if command else None,
        }
    )


def witness_entry(signal, value, cycle=None, net=None):
    """One assignment of a counterexample witness."""
    return _drop_none(
        {
            "signal": _require(signal, "signal"),
            "value": value if value is not None else "",
            "cycle": cycle,
            "net": net,
        }
    )


def counterexample(description, cycle_bound=None, witness=None, witness_available=None,
                   evidence_list=None):
    """A refutation. ``cycle_bound`` is set exactly for bounded refutations."""
    return _drop_none(
        {
            "description": _require(description, "description"),
            "cycle_bound": cycle_bound,
            "witness": list(witness) if witness else None,
            "witness_available": witness_available,
            "evidence": list(evidence_list) if evidence_list else None,
        }
    )


def unsupported_primitive(gate_type, reason, count=None, properties=None, gate_library=None,
                          example_gates=None):
    """A gate type the analysis did not cover."""
    return _drop_none(
        {
            "gate_type": _require(gate_type, "gate_type"),
            "reason": _require(reason, "reason"),
            "count": count,
            "properties": sorted(properties) if properties else None,
            "gate_library": gate_library,
            "example_gates": list(example_gates) if example_gates else None,
        }
    )


def unsupported(kind, reason, primitives=None):
    """Explicit coverage gap. ``kind='primitive'`` requires at least one entry."""
    _check_choice(kind, _UNSUPPORTED_KINDS, "kind")
    primitive_list = list(primitives or [])
    if kind == "primitive" and not primitive_list:
        raise ValueError("unsupported(kind='primitive') requires at least one primitive")
    return {
        "kind": kind,
        "reason": _require(reason, "reason"),
        "primitives": primitive_list,
    }


def error(kind, message, detail=None):
    """A failure of the analysis itself (never a statement about the design)."""
    _check_choice(kind, _ERROR_KINDS, "kind")
    return _drop_none(
        {
            "kind": kind,
            "message": _require(message, "message"),
            "detail": detail,
        }
    )


def finding(
    finding_id,
    title,
    status,
    method_dict,
    scope_dict,
    summary=None,
    severity=None,
    confidence=None,
    assumptions=None,
    bounds_dict=None,
    solver_dict=None,
    limits_dict=None,
    evidence_list=None,
    counterexample_dict=None,
    unsupported_dict=None,
    error_dict=None,
    metrics=None,
    data=None,
    tags=None,
):
    """Assemble one finding, rejecting the status/bounds contradictions early."""
    _check_choice(status, STATUSES, "status")

    if status in BOUNDED_STATUSES:
        if not bounds_dict or bounds_dict.get("unbounded") is not False:
            raise ValueError(
                "status {!r} requires bounds with unbounded=False".format(status)
            )
        if bounds_dict.get("cycle_bound") is None:
            raise ValueError("status {!r} requires bounds.cycle_bound".format(status))
    if status == STATUS_PROVEN_UNDER_ASSUMPTIONS:
        if not bounds_dict or bounds_dict.get("unbounded") is not True:
            raise ValueError(
                "proven_under_assumptions requires bounds with unbounded=True; use "
                "proven_bounded for a claim that only holds up to a cycle bound"
            )
        if assumptions is None:
            raise ValueError(
                "proven_under_assumptions requires an explicit assumptions list "
                "(use [] only if the proof genuinely rests on nothing)"
            )
    if status == STATUS_COUNTEREXAMPLE:
        if not bounds_dict or bounds_dict.get("unbounded") is not True:
            raise ValueError(
                "counterexample requires bounds with unbounded=True; use "
                "bounded_counterexample when the witness is only valid up to a bound"
            )
        if counterexample_dict and counterexample_dict.get("cycle_bound") is not None:
            raise ValueError(
                "an unbounded counterexample must not carry a cycle_bound; use "
                "bounded_counterexample instead"
            )
    if status == STATUS_BOUNDED_COUNTEREXAMPLE:
        if not counterexample_dict or counterexample_dict.get("cycle_bound") is None:
            raise ValueError("bounded_counterexample requires counterexample.cycle_bound")
        if counterexample_dict["cycle_bound"] > bounds_dict["cycle_bound"]:
            raise ValueError(
                "counterexample.cycle_bound ({}) exceeds bounds.cycle_bound ({})".format(
                    counterexample_dict["cycle_bound"], bounds_dict["cycle_bound"]
                )
            )
    if status in (STATUS_HEURISTIC, STATUS_UNKNOWN, STATUS_TIMEOUT) and bounds_dict:
        if bounds_dict.get("unbounded") is True:
            raise ValueError(
                "status {!r} must not claim unbounded validity".format(status)
            )
    if status == STATUS_TIMEOUT and (not limits_dict or limits_dict.get("timeout_s") is None):
        raise ValueError("status 'timeout' requires limits with timeout_s")
    if status == STATUS_ERROR and not error_dict:
        raise ValueError("status 'error' requires an error object")
    if status == STATUS_UNSUPPORTED and not unsupported_dict:
        raise ValueError("status 'unsupported' requires an unsupported object")
    if status not in REFUTATION_STATUSES and counterexample_dict:
        raise ValueError(
            "only counterexample statuses may carry a counterexample object"
        )

    return _drop_none(
        {
            "id": _require(finding_id, "id"),
            "title": _require(title, "title"),
            "summary": summary,
            "status": status,
            "severity": severity,
            "confidence": confidence,
            "method": _require(method_dict, "method"),
            "assumptions": list(assumptions) if assumptions is not None else None,
            "bounds": bounds_dict,
            "scope": _require(scope_dict, "scope"),
            "solver": solver_dict,
            "limits": limits_dict,
            "evidence": list(evidence_list) if evidence_list else None,
            "counterexample": counterexample_dict,
            "unsupported": unsupported_dict,
            "error": error_dict,
            "metrics": metrics,
            "data": data,
            "tags": sorted(tags) if tags else None,
        }
    )


def document(producer, artifacts, analysis, findings, generated_at=None, notes=None):
    """Assemble a complete findings document."""
    return _drop_none(
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "producer": _require(producer, "producer"),
            "artifacts": list(artifacts),
            "analysis": _require(analysis, "analysis"),
            "findings": list(findings),
            "notes": list(notes) if notes else None,
        }
    )
