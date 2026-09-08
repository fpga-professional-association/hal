"""Deterministic serialization for recovered-block documents.

Same contract as :mod:`hal_findings.serialize`, for the same reason: two runs of
the composer over the same inputs must produce byte-identical output, or the
model cannot be diffed in review or cached in CI.  Ordering is fixed here rather
than trusted from the composer, so a change of iteration order somewhere
upstream cannot silently change the file.
"""

import hashlib
import json

__all__ = [
    "VOLATILE_FIELDS",
    "normalize_document",
    "canonical_json",
    "dumps",
    "loads",
    "write_document",
    "read_document",
    "document_digest",
    "write_json",
    "read_json",
]

#: Fields excluded from :func:`document_digest` because they change every run.
VOLATILE_FIELDS = (("generated_at",), ("producer", "command"))


def _sort_gate_refs(refs):
    return sorted(refs, key=lambda ref: (ref.get("artifact_id", ""), ref.get("id", 0)))


def _normalize_claim(entry):
    entry = dict(entry)
    entry["gates"] = sorted(set(entry.get("gates", [])))
    entry["evidence"] = sorted(
        (dict(ref) for ref in entry.get("evidence", [])),
        key=lambda ref: (ref.get("source_id", ""), ref.get("finding_id", "")),
    )
    return entry


def _normalize_block(entry):
    entry = dict(entry)
    entry["gates"] = _sort_gate_refs(entry.get("gates", []))
    entry["claims"] = sorted(
        (_normalize_claim(claim) for claim in entry.get("claims", [])),
        key=lambda claim: claim.get("claim_id", ""),
    )
    ports = entry.get("ports")
    if ports:
        entry["ports"] = {
            key: _sort_gate_refs(value) for key, value in sorted(ports.items())
        }
    return entry


def _normalize_region(entry):
    entry = dict(entry)
    entry["gates"] = _sort_gate_refs(entry.get("gates", []))
    return entry


def normalize_document(document):
    """Return a copy of ``document`` with every order-insensitive list sorted."""
    normalized = dict(document)
    if "sources" in normalized:
        normalized["sources"] = sorted(
            (dict(entry) for entry in normalized["sources"]),
            key=lambda entry: entry.get("source_id", ""),
        )
    if "blocks" in normalized:
        normalized["blocks"] = sorted(
            (_normalize_block(entry) for entry in normalized["blocks"]),
            key=lambda entry: entry.get("block_id", ""),
        )
    if "unknown_regions" in normalized:
        normalized["unknown_regions"] = sorted(
            (_normalize_region(entry) for entry in normalized["unknown_regions"]),
            key=lambda entry: entry.get("region_id", ""),
        )
    if "ports" in normalized:
        normalized["ports"] = sorted(
            (dict(entry) for entry in normalized["ports"]),
            key=lambda entry: entry.get("port_id", ""),
        )
    if "edges" in normalized:
        normalized["edges"] = sorted(
            (dict(entry) for entry in normalized["edges"]),
            key=lambda entry: (entry.get("source", ""), entry.get("target", "")),
        )
    if "overlaps" in normalized:
        normalized["overlaps"] = sorted(
            (dict(entry) for entry in normalized["overlaps"]),
            key=lambda entry: entry.get("gate", {}).get("id", 0),
        )
    return normalized


def canonical_json(document, normalize=True):
    """The compact, sorted-key JSON string used for hashing."""
    if normalize:
        document = normalize_document(document)
    return json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def dumps(document, indent=2, normalize=True):
    """Serialize ``document`` deterministically, with a trailing newline."""
    if normalize:
        document = normalize_document(document)
    return json.dumps(document, sort_keys=True, ensure_ascii=False, indent=indent) + "\n"


def loads(text):
    return json.loads(text)


def write_document(document, path, indent=2, normalize=True):
    """Write ``document`` to ``path`` (UTF-8, LF endings) and return the path."""
    text = dumps(document, indent=indent, normalize=normalize)
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return str(path)


def read_document(path):
    with open(str(path), "r", encoding="utf-8") as handle:
        return json.load(handle)


#: Plain JSON helpers used for the inventory, which needs no normalization.
def write_json(payload, path, indent=2):
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=indent, sort_keys=True) + "\n")
    return str(path)


def read_json(path):
    with open(str(path), "r", encoding="utf-8") as handle:
        return json.load(handle)


def _without(document, fields=VOLATILE_FIELDS):
    stripped = json.loads(canonical_json(document))
    for path in fields:
        node = stripped
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return stripped


def document_digest(document, include_volatile=False):
    """SHA-256 over the canonical form, excluding the wall-clock fields."""
    payload = document if include_volatile else _without(document)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
