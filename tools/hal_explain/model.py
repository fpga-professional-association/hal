"""Builders for the recovered-block intermediate representation.

Like :mod:`hal_findings.model`, every builder returns a plain ``dict`` -- the
schema is the contract -- but the builders drop ``None`` fields and refuse the
contradictions the schema also rejects, so a mistake surfaces at the call site.

The one rule worth stating in code rather than in prose: **confidence is a
function of the finding's status, not a judgement**.  :func:`confidence_for_status`
is the only place the mapping exists, so a composer cannot promote a heuristic
register grouping to a verified one by being enthusiastic about it.
"""

from .schema import SCHEMA_VERSION

__all__ = [
    "CONFIDENCE_VERIFIED",
    "CONFIDENCE_HEURISTIC",
    "CONFIDENCE_REFUTED",
    "CONFIDENCE_UNKNOWN",
    "CONFIDENCES",
    "CONFIDENCE_RANK",
    "BLOCK_KINDS",
    "STATUS_CONFIDENCE",
    "confidence_for_status",
    "strongest_confidence",
    "is_verified",
    "gate_ref",
    "net_ref",
    "evidence_ref",
    "claim",
    "block",
    "unknown_region",
    "port",
    "edge",
    "overlap",
    "coverage",
    "design",
    "source",
    "document",
]

#: The claim rests on a proof (bounded or not) produced by a decision procedure.
CONFIDENCE_VERIFIED = "verified"
#: The claim rests on structural evidence only.  It is a guess, and stays one.
CONFIDENCE_HEURISTIC = "heuristic"
#: An analysis actively refuted this claim.
CONFIDENCE_REFUTED = "refuted"
#: No verdict: unknown, timed out, errored, or outside what the analysis covers.
CONFIDENCE_UNKNOWN = "unknown"

CONFIDENCES = (
    CONFIDENCE_VERIFIED,
    CONFIDENCE_HEURISTIC,
    CONFIDENCE_REFUTED,
    CONFIDENCE_UNKNOWN,
)

#: Ordering used to pick a block's headline confidence and its diagram owner.
CONFIDENCE_RANK = {
    CONFIDENCE_VERIFIED: 3,
    CONFIDENCE_HEURISTIC: 2,
    CONFIDENCE_UNKNOWN: 1,
    CONFIDENCE_REFUTED: 0,
}

BLOCK_KINDS = (
    "register",
    "counter",
    "arithmetic",
    "comparator",
    "multiplexer",
    "state_machine",
    "datapath",
    "other",
)

#: findings status -> confidence.  The single place the mapping lives.
STATUS_CONFIDENCE = {
    "proven_under_assumptions": CONFIDENCE_VERIFIED,
    "proven_bounded": CONFIDENCE_VERIFIED,
    "counterexample": CONFIDENCE_REFUTED,
    "bounded_counterexample": CONFIDENCE_REFUTED,
    "heuristic": CONFIDENCE_HEURISTIC,
    "unknown": CONFIDENCE_UNKNOWN,
    "timeout": CONFIDENCE_UNKNOWN,
    "error": CONFIDENCE_UNKNOWN,
    "unsupported": CONFIDENCE_UNKNOWN,
}


def confidence_for_status(status):
    """Map a findings status onto a block-model confidence.

    An unrecognised status is ``unknown``, never ``verified``: a document from a
    future schema version must not be able to talk this tool into presenting an
    unread status as a proof.
    """
    return STATUS_CONFIDENCE.get(status, CONFIDENCE_UNKNOWN)


def strongest_confidence(confidences):
    """The headline confidence of a set of claims (``unknown`` when empty).

    ``refuted`` ranks *below* ``unknown`` so that one refutation next to an
    inconclusive result does not make the block look merely undecided -- but a
    block whose only claims are refutations is ``refuted``, not ``unknown``,
    which is why the empty case is handled separately instead of seeding the
    fold with ``unknown``.
    """
    values = list(confidences)
    if not values:
        return CONFIDENCE_UNKNOWN
    best = values[0]
    for value in values[1:]:
        if CONFIDENCE_RANK.get(value, 0) > CONFIDENCE_RANK.get(best, 0):
            best = value
    return best


def is_verified(entry):
    """True only for a block/claim whose confidence is ``verified``."""
    return entry.get("confidence") == CONFIDENCE_VERIFIED


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


# ---------------------------------------------------------------------------
# references
# ---------------------------------------------------------------------------


def gate_ref(artifact_id, gate_id, name, gate_type=None):
    """Reference a gate within the design this model is about."""
    return _drop_none(
        {
            "artifact_id": _require(artifact_id, "artifact_id"),
            "id": int(_require(gate_id, "id")),
            "name": name if name is not None else "",
            "type": gate_type,
        }
    )


def net_ref(artifact_id, net_id, name, role=None):
    """Reference a net within the design this model is about."""
    return _drop_none(
        {
            "artifact_id": _require(artifact_id, "artifact_id"),
            "id": int(_require(net_id, "id")),
            "name": name if name is not None else "",
            "role": role,
        }
    )


def evidence_ref(
    source_id,
    finding_id,
    status,
    document=None,
    title=None,
    method=None,
    open_assumptions=None,
    cycle_bound=None,
    artifacts=None,
):
    """Point at one finding in one source document.

    ``open_assumptions`` carries the assumptions the finding did *not*
    discharge.  A ``verified`` claim that rests on an undischarged assumption is
    still conditional, and dropping that here would be the exact mistake the
    findings schema was built to prevent.
    """
    return _drop_none(
        {
            "source_id": _require(source_id, "source_id"),
            "finding_id": _require(finding_id, "finding_id"),
            "status": _require(status, "status"),
            "document": document,
            "title": title,
            "method": method,
            "open_assumptions": sorted(open_assumptions) if open_assumptions else None,
            "cycle_bound": cycle_bound,
            "artifacts": sorted(artifacts) if artifacts else None,
        }
    )


# ---------------------------------------------------------------------------
# claims and blocks
# ---------------------------------------------------------------------------


def claim(
    claim_id,
    text,
    status,
    gates,
    evidence,
    confidence=None,
    bounded=None,
    cycle_bound=None,
    metrics=None,
):
    """One statement about one gate set, with the finding that produced it.

    ``gates`` may not be empty and ``evidence`` may not be empty: a claim that
    names no gates cannot be checked against the netlist, and a claim with no
    evidence is exactly the kind of assertion this whole tool exists to avoid.
    """
    gate_ids = sorted({int(entry) for entry in gates or ()})
    if not gate_ids:
        raise ValueError(
            "claim {!r} names no gates; a functional claim that cannot be tied to a "
            "gate set is not a finding, it is an opinion".format(claim_id)
        )
    evidence_list = list(evidence or ())
    if not evidence_list:
        raise ValueError(
            "claim {!r} has no evidence reference; every claim must name the finding "
            "it came from".format(claim_id)
        )

    derived = confidence_for_status(status)
    if confidence is None:
        confidence = derived
    elif confidence != derived:
        raise ValueError(
            "claim {!r} declares confidence {!r} but its status {!r} maps to {!r}; "
            "confidence is derived from the status, never chosen".format(
                claim_id, confidence, status, derived
            )
        )
    _check_choice(confidence, CONFIDENCES, "confidence")

    if bounded is None:
        bounded = cycle_bound is not None
    if bounded and cycle_bound is None and status in ("proven_bounded", "bounded_counterexample"):
        raise ValueError(
            "claim {!r} has status {!r} but no cycle_bound; a bounded claim that does "
            "not say what it is bounded by cannot be read".format(claim_id, status)
        )
    if not bounded and cycle_bound is not None:
        raise ValueError("claim {!r} carries a cycle_bound but is not bounded".format(claim_id))

    return _drop_none(
        {
            "claim_id": _require(claim_id, "claim_id"),
            "text": _require(text, "text"),
            "confidence": confidence,
            "status": status,
            "bounded": bool(bounded),
            "cycle_bound": cycle_bound,
            "gates": gate_ids,
            "evidence": evidence_list,
            "metrics": metrics,
        }
    )


def block(
    block_id,
    kind,
    label,
    gates,
    claims,
    confidence=None,
    bounded=None,
    contested=None,
    ports=None,
    attributes=None,
    notes=None,
):
    """One recovered block: a gate set plus the claims that describe it."""
    _check_choice(kind, BLOCK_KINDS, "kind")
    gate_list = list(gates or ())
    if not gate_list:
        raise ValueError("block {!r} has no gates".format(block_id))
    claim_list = list(claims or ())
    if not claim_list:
        raise ValueError(
            "block {!r} has no claims; a block nobody said anything about belongs in "
            "an unknown region, not in the block list".format(block_id)
        )

    derived = strongest_confidence(entry["confidence"] for entry in claim_list)
    if confidence is None:
        confidence = derived
    elif confidence != derived:
        raise ValueError(
            "block {!r} declares confidence {!r} but its strongest claim is {!r}".format(
                block_id, confidence, derived
            )
        )
    if contested is None:
        contested = any(entry["confidence"] == CONFIDENCE_REFUTED for entry in claim_list)
    if bounded is None:
        bounded = any(
            entry.get("bounded") and entry["confidence"] == confidence for entry in claim_list
        )

    return _drop_none(
        {
            "block_id": _require(block_id, "block_id"),
            "kind": kind,
            "label": _require(label, "label"),
            "confidence": confidence,
            "bounded": bool(bounded),
            "contested": bool(contested),
            "gates": gate_list,
            "ports": ports,
            "claims": claim_list,
            "attributes": attributes,
            "notes": list(notes) if notes else None,
        }
    )


def unknown_region(region_id, label, reason, gates, gate_types=None,
                   sequential_gate_count=None, notes=None):
    """A connected component of gates that no analysis claimed."""
    gate_list = list(gates or ())
    if not gate_list:
        raise ValueError("unknown region {!r} has no gates".format(region_id))
    return _drop_none(
        {
            "region_id": _require(region_id, "region_id"),
            "label": _require(label, "label"),
            "reason": _require(reason, "reason"),
            "gates": gate_list,
            "gate_types": gate_types or None,
            "sequential_gate_count": sequential_gate_count,
            "notes": list(notes) if notes else None,
        }
    )


def port(port_id, direction, nets, label=None):
    """A design-boundary endpoint for the diagram."""
    _check_choice(direction, ("input", "output"), "direction")
    net_list = list(nets or ())
    if not net_list:
        raise ValueError("port {!r} has no nets".format(port_id))
    return _drop_none(
        {
            "port_id": _require(port_id, "port_id"),
            "direction": direction,
            "label": label,
            "nets": net_list,
        }
    )


def edge(source, target, net_count, nets=None, kind=None, truncated=None):
    """A structural connection between two nodes of the model."""
    if int(net_count) < 1:
        raise ValueError("an edge must carry at least one net")
    if kind is not None:
        _check_choice(kind, ("data", "control"), "kind")
    return _drop_none(
        {
            "source": _require(source, "source"),
            "target": _require(target, "target"),
            "net_count": int(net_count),
            "kind": kind,
            "nets": list(nets) if nets else None,
            "truncated": truncated,
        }
    )


def overlap(gate, block_ids, owner):
    """A gate claimed by more than one block."""
    ids = sorted(set(block_ids or ()))
    if len(ids) < 2:
        raise ValueError("an overlap needs at least two block ids")
    if owner not in ids:
        raise ValueError("the overlap owner {!r} is not one of {}".format(owner, ids))
    return {"gate": gate, "block_ids": ids, "owner": owner}


def coverage(gates_total, gates_in_blocks, gates_unclassified, **extra):
    """The classification budget: what was covered, and what was not."""
    if gates_in_blocks + gates_unclassified != gates_total:
        raise ValueError(
            "coverage does not add up: {} classified + {} unclassified != {} total. "
            "Every gate of the netlist is either in a block or in an unknown "
            "region.".format(gates_in_blocks, gates_unclassified, gates_total)
        )
    entry = {
        "gates_total": int(gates_total),
        "gates_in_blocks": int(gates_in_blocks),
        "gates_unclassified": int(gates_unclassified),
    }
    entry.update({key: value for key, value in extra.items() if value is not None})
    if gates_total:
        entry.setdefault(
            "classified_fraction", round(float(gates_in_blocks) / float(gates_total), 4)
        )
    else:
        entry.setdefault("classified_fraction", 0.0)
    return entry


def design(
    artifact_id,
    gate_count,
    net_count,
    design_name=None,
    device_name=None,
    path=None,
    sha256=None,
    unhashed_reason=None,
    gate_library=None,
):
    """The netlist every reference in the document is scoped to."""
    return _drop_none(
        {
            "artifact_id": _require(artifact_id, "artifact_id"),
            "gate_count": int(gate_count),
            "net_count": int(net_count),
            "design_name": design_name,
            "device_name": device_name,
            "path": path,
            "sha256": sha256,
            "unhashed_reason": unhashed_reason,
            "gate_library": gate_library,
        }
    )


def source(
    source_id,
    adapter,
    finding_count,
    path=None,
    sha256=None,
    findings_schema_version=None,
    producer=None,
    analysis=None,
    status_counts=None,
    unresolved_gates=None,
    notes=None,
):
    """One findings document this model was composed from."""
    return _drop_none(
        {
            "source_id": _require(source_id, "source_id"),
            "adapter": _require(adapter, "adapter"),
            "finding_count": int(finding_count),
            "path": path,
            "sha256": sha256,
            "findings_schema_version": findings_schema_version,
            "producer": producer,
            "analysis": analysis,
            "status_counts": status_counts or None,
            "unresolved_gates": sorted(unresolved_gates) if unresolved_gates else None,
            "notes": list(notes) if notes else None,
        }
    )


def document(
    producer,
    design_dict,
    sources,
    blocks,
    unknown_regions,
    coverage_dict,
    ports=None,
    edges=None,
    overlaps=None,
    generated_at=None,
    notes=None,
):
    """Assemble a complete recovered-block document."""
    return _drop_none(
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "producer": _require(producer, "producer"),
            "design": _require(design_dict, "design"),
            "sources": list(sources),
            "blocks": list(blocks),
            "unknown_regions": list(unknown_regions),
            "ports": list(ports) if ports else None,
            "edges": list(edges) if edges else None,
            "overlaps": list(overlaps) if overlaps else None,
            "coverage": _require(coverage_dict, "coverage"),
            "notes": list(notes) if notes else None,
        }
    )
