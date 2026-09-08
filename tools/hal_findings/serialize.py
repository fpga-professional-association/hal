"""Deterministic serialization for findings documents.

Two byte-identical runs of an analysis must produce byte-identical documents,
otherwise findings cannot be diffed in review or cached in CI.  That means:

* every mapping is written with sorted keys;
* every list whose order carries no meaning is sorted by a stable key
  (:func:`normalize_document`);
* the document digest deliberately excludes the wall-clock fields, so that a
  rerun that only differs in timing hashes the same.
"""

import hashlib
import json

from .schema import SCHEMA_VERSION

__all__ = [
    "VOLATILE_FIELDS",
    "normalize_document",
    "canonical_json",
    "dumps",
    "loads",
    "write_document",
    "read_document",
    "document_digest",
    "sha256_file",
]

#: Fields ignored by :func:`document_digest` because they change on every run.
VOLATILE_FIELDS = (
    ("generated_at",),
    ("producer", "command"),
    ("analysis", "started_at"),
    ("analysis", "finished_at"),
    ("analysis", "duration_s"),
)


def _sort_refs(refs):
    return sorted(refs, key=lambda ref: (ref.get("artifact_id", ""), ref.get("id", 0)))


def _normalize_scope(scope):
    scope = dict(scope)
    if "artifact_ids" in scope:
        scope["artifact_ids"] = sorted(set(scope["artifact_ids"]))
    for key in ("gates", "nets", "modules"):
        if key in scope:
            scope[key] = _sort_refs(scope[key])
    if "gate_types" in scope:
        scope["gate_types"] = sorted(set(scope["gate_types"]))
    return scope


def _normalize_finding(finding):
    finding = dict(finding)
    if "scope" in finding:
        finding["scope"] = _normalize_scope(finding["scope"])
    if "assumptions" in finding:
        finding["assumptions"] = sorted(finding["assumptions"], key=lambda a: a.get("id", ""))
    if "tags" in finding:
        finding["tags"] = sorted(set(finding["tags"]))
    if "unsupported" in finding:
        unsupported = dict(finding["unsupported"])
        primitives = []
        for primitive in unsupported.get("primitives", []):
            primitive = dict(primitive)
            if "example_gates" in primitive:
                primitive["example_gates"] = _sort_refs(primitive["example_gates"])
            primitives.append(primitive)
        unsupported["primitives"] = sorted(primitives, key=lambda p: p.get("gate_type", ""))
        finding["unsupported"] = unsupported
    if "counterexample" in finding:
        counterexample = dict(finding["counterexample"])
        if "witness" in counterexample:
            counterexample["witness"] = sorted(
                counterexample["witness"],
                key=lambda entry: (entry.get("cycle", 0), entry.get("signal", "")),
            )
        finding["counterexample"] = counterexample
    return finding


def normalize_document(document):
    """Return a copy of ``document`` with order-insensitive lists sorted.

    Findings are sorted by ``id``; give findings zero-padded, meaningful IDs
    (``"dataflow/group/0007"``) so that the sorted order stays readable.
    """
    normalized = dict(document)
    if "artifacts" in normalized:
        normalized["artifacts"] = sorted(
            (dict(artifact) for artifact in normalized["artifacts"]),
            key=lambda artifact: artifact.get("artifact_id", ""),
        )
    if "findings" in normalized:
        normalized["findings"] = sorted(
            (_normalize_finding(finding) for finding in normalized["findings"]),
            key=lambda finding: finding.get("id", ""),
        )
    return normalized


def canonical_json(document, normalize=True):
    """Return the compact, sorted-key JSON string used for hashing."""
    if normalize:
        document = normalize_document(document)
    return json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def dumps(document, indent=2, normalize=True):
    """Serialize ``document`` deterministically, with a trailing newline."""
    if normalize:
        document = normalize_document(document)
    return (
        json.dumps(document, sort_keys=True, ensure_ascii=False, indent=indent) + "\n"
    )


def loads(text):
    """Parse a findings document from a JSON string."""
    return json.loads(text)


def write_document(document, path, indent=2, normalize=True):
    """Write ``document`` to ``path`` (UTF-8, LF endings) and return the path."""
    text = dumps(document, indent=indent, normalize=normalize)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def read_document(path):
    """Read a findings document from ``path``."""
    with open(path, "r", encoding="utf-8") as handle:
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
    """SHA-256 over the canonical form; volatile timing fields are excluded.

    Two runs of the same analysis on the same inputs therefore share a digest,
    which is what makes findings cacheable and diffable in CI.
    """
    payload = document if include_volatile else _without(document)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def sha256_file(path, chunk_size=1 << 20):
    """SHA-256 of a file's contents, used to pin artifacts."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def schema_version_of(document):
    """Return the ``schema_version`` of ``document`` (defaults to the current one)."""
    return document.get("schema_version", SCHEMA_VERSION)
