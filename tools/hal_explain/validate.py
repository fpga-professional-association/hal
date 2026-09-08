"""Schema and cross-reference validation for recovered-block documents.

Two layers, the same split :mod:`hal_findings.validate` uses:

1. **Schema** -- structure and vocabularies, via
   :mod:`hal_findings.jsonschema_mini` (no third-party dependency).
2. **Semantics** -- the rules JSON Schema cannot see, and every one of them is a
   way this tool could quietly lie:

   * a claim's confidence must be the one its status maps to, so a heuristic
     can never be presented as verified;
   * every evidence reference must name a ``source_id`` the document declares,
     so "the finding says so" is checkable;
   * a gate may not be in a block *and* in an unknown region -- if it is, the
     coverage numbers are meaningless;
   * ``coverage`` must add up to the design's gate count;
   * every edge endpoint must be a node the document declares.
"""

from hal_findings import jsonschema_mini

from . import model
from .schema import SUPPORTED_SCHEMA_VERSIONS, load_schema

__all__ = [
    "BlockModelValidationError",
    "schema_errors",
    "semantic_errors",
    "collect_errors",
    "validate_document",
    "is_valid",
]


class BlockModelValidationError(ValueError):
    """Raised when a document violates the schema or its reference rules."""

    def __init__(self, errors):
        self.errors = list(errors)
        ValueError.__init__(
            self,
            "recovered-block document is invalid ({} problem{}):\n  - {}".format(
                len(self.errors),
                "" if len(self.errors) == 1 else "s",
                "\n  - ".join(self.errors),
            ),
        )


def _schema_for(document):
    version = document.get("schema_version")
    if version is None:
        raise BlockModelValidationError(["document has no 'schema_version'"])
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise BlockModelValidationError(
            [
                "unsupported schema_version {!r}; this build understands {}".format(
                    version, ", ".join(SUPPORTED_SCHEMA_VERSIONS)
                )
            ]
        )
    return load_schema(version)


def schema_errors(document):
    """Return schema violations as human readable strings."""
    schema = _schema_for(document)
    return [str(error) for error in jsonschema_mini.iter_errors(document, schema)]


def semantic_errors(document):
    """Return violations of the rules JSON Schema cannot express."""
    errors = []

    artifact_id = (document.get("design") or {}).get("artifact_id")
    source_ids = set()
    for source in document.get("sources", []):
        source_id = source.get("source_id")
        if source_id in source_ids:
            errors.append("duplicate source_id {!r}".format(source_id))
        source_ids.add(source_id)

    node_ids = set()
    block_gates = {}
    claim_ids = set()

    for block in document.get("blocks", []):
        block_id = block.get("block_id")
        if block_id in node_ids:
            errors.append("duplicate block_id {!r}".format(block_id))
        node_ids.add(block_id)

        for ref in block.get("gates", []):
            if ref.get("artifact_id") != artifact_id:
                errors.append(
                    "block {!r} references gate {!r} in artifact {!r}, but this model "
                    "is about {!r}".format(
                        block_id, ref.get("name"), ref.get("artifact_id"), artifact_id
                    )
                )
            block_gates.setdefault(int(ref.get("id", 0)), []).append(block_id)
        block_gate_ids = {int(ref.get("id", 0)) for ref in block.get("gates", [])}

        confidences = []
        for claim in block.get("claims", []):
            claim_id = claim.get("claim_id")
            if claim_id in claim_ids:
                errors.append("duplicate claim_id {!r}".format(claim_id))
            claim_ids.add(claim_id)

            expected = model.confidence_for_status(claim.get("status"))
            if claim.get("confidence") != expected:
                errors.append(
                    "claim {!r} has status {!r} but confidence {!r}; confidence is "
                    "derived from the status and must be {!r}".format(
                        claim_id, claim.get("status"), claim.get("confidence"), expected
                    )
                )
            confidences.append(claim.get("confidence"))

            missing = sorted(set(claim.get("gates", [])) - block_gate_ids)
            if missing:
                errors.append(
                    "claim {!r} names gate(s) {} that are not in block {!r}; a claim "
                    "must be about the block it is attached to".format(
                        claim_id, missing, block_id
                    )
                )
            for evidence in claim.get("evidence", []):
                if evidence.get("source_id") not in source_ids:
                    errors.append(
                        "claim {!r} cites source {!r}, which the document does not "
                        "declare; the evidence link cannot be followed".format(
                            claim_id, evidence.get("source_id")
                        )
                    )

        expected_block = model.strongest_confidence(confidences)
        if block.get("confidence") != expected_block:
            errors.append(
                "block {!r} declares confidence {!r} but its strongest claim is "
                "{!r}".format(block_id, block.get("confidence"), expected_block)
            )

    region_gates = {}
    for region in document.get("unknown_regions", []):
        region_id = region.get("region_id")
        if region_id in node_ids:
            errors.append("duplicate node id {!r}".format(region_id))
        node_ids.add(region_id)
        for ref in region.get("gates", []):
            gate_id = int(ref.get("id", 0))
            region_gates.setdefault(gate_id, []).append(region_id)
            if gate_id in block_gates:
                errors.append(
                    "gate {!r} (id {}) is in block(s) {} and in unknown region {!r}; "
                    "a gate is either classified or not".format(
                        ref.get("name"), gate_id, block_gates[gate_id], region_id
                    )
                )
            if len(region_gates[gate_id]) > 1:
                errors.append(
                    "gate id {} appears in unknown regions {}".format(
                        gate_id, region_gates[gate_id]
                    )
                )

    for port in document.get("ports", []):
        port_id = port.get("port_id")
        if port_id in node_ids:
            errors.append("duplicate node id {!r}".format(port_id))
        node_ids.add(port_id)

    for edge in document.get("edges", []):
        for end in ("source", "target"):
            if edge.get(end) not in node_ids:
                errors.append(
                    "edge {} -> {} names {} {!r}, which is not a block, unknown region "
                    "or port in this document".format(
                        edge.get("source"), edge.get("target"), end, edge.get(end)
                    )
                )
        if edge.get("nets") and len(edge["nets"]) > edge.get("net_count", 0):
            errors.append(
                "edge {} -> {} lists more nets than its net_count".format(
                    edge.get("source"), edge.get("target")
                )
            )

    for overlap in document.get("overlaps", []):
        gate_id = int((overlap.get("gate") or {}).get("id", 0))
        declared = sorted(block_gates.get(gate_id, []))
        if declared != sorted(overlap.get("block_ids", [])):
            errors.append(
                "overlap for gate id {} lists blocks {} but the blocks that actually "
                "contain it are {}".format(gate_id, overlap.get("block_ids"), declared)
            )

    coverage = document.get("coverage") or {}
    design = document.get("design") or {}
    total = coverage.get("gates_total")
    if total is not None and design.get("gate_count") is not None:
        if total != design["gate_count"]:
            errors.append(
                "coverage.gates_total ({}) does not match design.gate_count ({})".format(
                    total, design["gate_count"]
                )
            )
    if (
        coverage.get("gates_in_blocks") is not None
        and coverage.get("gates_unclassified") is not None
        and total is not None
        and coverage["gates_in_blocks"] + coverage["gates_unclassified"] != total
    ):
        errors.append(
            "coverage does not add up: {} classified + {} unclassified != {} "
            "total".format(
                coverage["gates_in_blocks"], coverage["gates_unclassified"], total
            )
        )
    if coverage.get("gates_in_blocks") is not None and len(block_gates) != coverage[
        "gates_in_blocks"
    ]:
        errors.append(
            "coverage.gates_in_blocks is {} but {} distinct gates appear in "
            "blocks".format(coverage["gates_in_blocks"], len(block_gates))
        )
    if coverage.get("gates_unclassified") is not None and len(region_gates) != coverage[
        "gates_unclassified"
    ]:
        errors.append(
            "coverage.gates_unclassified is {} but {} distinct gates appear in unknown "
            "regions".format(coverage["gates_unclassified"], len(region_gates))
        )

    return errors


def collect_errors(document):
    """Return all schema and semantic problems, schema problems first."""
    errors = schema_errors(document)
    if errors:
        return errors
    return semantic_errors(document)


def validate_document(document):
    """Raise :class:`BlockModelValidationError` if ``document`` is invalid."""
    errors = collect_errors(document)
    if errors:
        raise BlockModelValidationError(errors)
    return document


def is_valid(document):
    try:
        return not collect_errors(document)
    except BlockModelValidationError:
        return False
