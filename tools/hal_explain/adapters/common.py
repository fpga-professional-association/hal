"""Shared machinery for the findings-document adapters.

Nothing here imports ``hal_py``.  An adapter's job is narrow on purpose:

1. resolve the gate references of a finding against the inventory, recording
   the ones that do not resolve instead of dropping them;
2. turn the finding into one :class:`Contribution` with a templated claim whose
   evidence points back at that finding;
3. never invent a status.  The claim's confidence comes from
   :func:`hal_explain.model.confidence_for_status`.
"""

import os

from .. import model

__all__ = [
    "AdapterError",
    "Contribution",
    "SourceDocument",
    "load_source",
    "resolve_gates",
    "evidence_from_finding",
    "open_assumptions",
    "evidence_artifacts",
    "status_counts",
    "gate_name_list",
    "plural",
]


class AdapterError(ValueError):
    """The findings document cannot be adapted."""


def plural(count, singular, plural_form=None):
    if count == 1:
        return "1 {}".format(singular)
    return "{} {}".format(count, plural_form or singular + "s")


class Contribution(object):
    """One proposed block, before it is placed in a model.

    ``gate_ids`` is already resolved against the inventory; ``claims`` are
    finished claim dicts.  ``compose`` decides the final block id, the ports and
    the connectivity -- the adapter decides only what was claimed and by whom.
    """

    __slots__ = ("key", "kind", "label", "gate_ids", "claims", "attributes", "notes",
                 "source_id", "order")

    def __init__(self, key, kind, label, gate_ids, claims, source_id, attributes=None,
                 notes=(), order=0):
        self.key = str(key)
        self.kind = str(kind)
        self.label = str(label)
        self.gate_ids = sorted({int(entry) for entry in gate_ids})
        self.claims = list(claims)
        self.source_id = str(source_id)
        self.attributes = dict(attributes or {})
        self.notes = list(notes)
        #: stable tie-break for deterministic block numbering
        self.order = int(order)

    def merge(self, other):
        """Fold another contribution over the same gate set into this one."""
        self.gate_ids = sorted(set(self.gate_ids) | set(other.gate_ids))
        self.claims.extend(other.claims)
        for key, value in other.attributes.items():
            self.attributes.setdefault(key, value)
        for note in other.notes:
            if note not in self.notes:
                self.notes.append(note)
        return self

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Contribution({!r}, {} gates, {} claims)".format(
            self.key, len(self.gate_ids), len(self.claims)
        )


class SourceDocument(object):
    """A loaded findings document plus the bookkeeping the model needs."""

    def __init__(self, source_id, document, path=None, sha256=None):
        self.source_id = str(source_id)
        self.document = document
        self.path = path
        self.sha256 = sha256
        #: gate reference strings that could not be resolved against the inventory
        self.unresolved = []
        self.notes = []

    def findings(self):
        return list(self.document.get("findings", []))

    def by_prefix(self, prefix):
        return [
            entry
            for entry in self.findings()
            if str(entry.get("id", "")).startswith(prefix)
        ]

    def to_source(self, adapter_name):
        return model.source(
            self.source_id,
            adapter_name,
            len(self.findings()),
            path=self.path,
            sha256=self.sha256,
            findings_schema_version=self.document.get("schema_version"),
            producer=self.document.get("producer"),
            analysis=self.document.get("analysis"),
            status_counts=status_counts(self.document),
            unresolved_gates=self.unresolved or None,
            notes=self.notes or None,
        )


def load_source(path, source_id=None, validate=True):
    """Read a findings document and wrap it in a :class:`SourceDocument`.

    The document is schema-validated by default.  A malformed document is a
    setup error, not a quiet degradation: composing a block model out of a
    document whose statuses may not mean what they say is exactly the failure
    mode this tool must not have.
    """
    from hal_findings import serialize as findings_serialize
    from hal_findings import validate as findings_validate

    path = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(path):
        raise AdapterError("findings document does not exist: {}".format(path))
    try:
        document = findings_serialize.read_document(path)
    except ValueError as exc:
        raise AdapterError("{} is not valid JSON: {}".format(path, exc))
    if validate:
        try:
            findings_validate.validate_document(document)
        except findings_validate.FindingsValidationError as exc:
            raise AdapterError("{} is not a valid findings document:\n{}".format(path, exc))
    if source_id is None:
        source_id = os.path.splitext(os.path.basename(path))[0]
    return SourceDocument(
        source_id, document, path=path, sha256=findings_serialize.sha256_file(path)
    )


def resolve_gates(gate_refs, inventory, source, where):
    """Map findings gate references onto inventory gate ids.

    Resolution is by ID first (IDs are only meaningful inside one artifact, and
    the inventory *is* that artifact) and by unique name second, which is what
    lets a findings document produced in an earlier HAL session still be
    composed.  Anything that resolves by neither is recorded on the source, so
    the report can say that this block is missing gates rather than presenting a
    silently smaller one.
    """
    resolved = []
    for ref in gate_refs or ():
        gate_id = ref.get("id")
        name = ref.get("name") or ""
        entry = inventory.gate(gate_id) if gate_id is not None else None
        if entry is not None and (not name or entry["name"] == name):
            resolved.append(int(gate_id))
            continue
        by_name = inventory.gate_id_by_name(name) if name else None
        if by_name is not None:
            resolved.append(int(by_name))
            continue
        marker = "{} (id {}, in {})".format(name or "<unnamed>", gate_id, where)
        if marker not in source.unresolved:
            source.unresolved.append(marker)
    return sorted(set(resolved))


def open_assumptions(finding):
    """IDs of the assumptions this finding did *not* discharge."""
    return [
        entry.get("id", "<unnamed>")
        for entry in finding.get("assumptions", []) or []
        if not entry.get("discharged")
    ]


def evidence_artifacts(finding):
    """Paths of the evidence files a finding points at."""
    paths = []
    for entry in finding.get("evidence", []) or []:
        path = entry.get("path")
        if path:
            paths.append(str(path))
    return paths


def evidence_from_finding(source, finding):
    """Build the evidence reference that links a claim back to its finding."""
    method = finding.get("method") or {}
    method_ref = {
        key: method[key] for key in ("name", "kind", "bounded") if key in method
    } or None
    bounds = finding.get("bounds") or {}
    cycle_bound = bounds.get("cycle_bound")
    return model.evidence_ref(
        source.source_id,
        finding.get("id"),
        finding.get("status"),
        document=source.path,
        title=finding.get("title"),
        method=method_ref,
        open_assumptions=open_assumptions(finding),
        cycle_bound=cycle_bound,
        artifacts=evidence_artifacts(finding),
    )


def status_counts(document):
    counts = {}
    for finding in document.get("findings", []):
        status = finding.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def gate_name_list(inventory, gate_ids, limit=6):
    """A readable, deterministic, truncation-honest gate list for a claim."""
    names = []
    for gate_id in sorted(gate_ids):
        gate = inventory.gate(gate_id)
        names.append(gate["name"] if gate else "<gate {}>".format(gate_id))
    if len(names) <= limit:
        return ", ".join(names)
    return "{}, ... (+{} more)".format(", ".join(names[:limit]), len(names) - limit)
