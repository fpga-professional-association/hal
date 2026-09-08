"""Versioned access to the two hal_migration formats and their validation.

Two documents are exchanged by this tool:

``source-inventory``
    what a netlist contains (:mod:`hal_migration.inventory`);
``target-catalogue``
    what a target technology offers, and which mapping a human reviewer is
    willing to propose for each source primitive (:mod:`hal_migration.catalogue`).

Both carry a mandatory format version.  A document whose version this build
does not know is rejected outright rather than parsed on a best-effort basis --
the same rule ``hal_findings`` applies to findings documents, and for the same
reason: silently reading an unknown format is how a stale mapping table ends up
in a report that looks current.

Validation reuses ``hal_findings.jsonschema_mini``, the dependency-free
validator that already ships with the findings contract, so this tool adds no
third-party requirement.  ``jsonschema`` is used instead when it is installed
and ``prefer_jsonschema`` is set.
"""

import json
import os

from hal_findings import jsonschema_mini

__all__ = [
    "INVENTORY_VERSION",
    "CATALOGUE_VERSION",
    "SUPPORTED_INVENTORY_VERSIONS",
    "SUPPORTED_CATALOGUE_VERSIONS",
    "SCHEMA_DIR",
    "FormatError",
    "schema_path",
    "load_schema",
    "document_kind",
    "version_of",
    "collect_errors",
    "validate",
    "is_valid",
    "read_json",
    "write_json",
    "dumps",
]

#: The inventory format version this package writes.
INVENTORY_VERSION = "1.0.0"
#: The catalogue format version this package writes.
CATALOGUE_VERSION = "1.0.0"

#: Every inventory format version this package can still read.
SUPPORTED_INVENTORY_VERSIONS = ("1.0.0",)
#: Every catalogue format version this package can still read.
SUPPORTED_CATALOGUE_VERSIONS = ("1.0.0",)

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema")

#: kind -> (version key, schema file stem, supported versions)
_KINDS = {
    "inventory": ("inventory_version", "source-inventory", SUPPORTED_INVENTORY_VERSIONS),
    "catalogue": ("catalogue_version", "target-catalogue", SUPPORTED_CATALOGUE_VERSIONS),
}

_CACHE = {}


class FormatError(ValueError):
    """A document is not a valid inventory/catalogue.

    Carries every problem found, not just the first, so a broken hand-written
    catalogue can be fixed in one pass.
    """

    def __init__(self, kind, errors):
        self.kind = kind
        self.errors = list(errors)
        ValueError.__init__(
            self,
            "invalid {} document:\n  - {}".format(kind, "\n  - ".join(self.errors)),
        )


def _kind_entry(kind):
    if kind not in _KINDS:
        raise ValueError(
            "unknown document kind {!r}; expected one of {}".format(
                kind, ", ".join(sorted(_KINDS))
            )
        )
    return _KINDS[kind]


def schema_path(kind, version=None):
    """Absolute path of the schema file for ``kind`` at ``version``."""
    version_key, stem, supported = _kind_entry(kind)
    version = version or supported[-1]
    if version not in supported:
        raise ValueError(
            "unsupported {} format version {!r}; this build understands {}".format(
                kind, version, ", ".join(supported)
            )
        )
    return os.path.join(SCHEMA_DIR, "{}-{}.schema.json".format(stem, version))


def load_schema(kind, version=None):
    """Load (and cache) the schema document for ``kind``/``version``."""
    path = schema_path(kind, version)
    if path not in _CACHE:
        with open(path, "r", encoding="utf-8") as handle:
            _CACHE[path] = json.load(handle)
    return _CACHE[path]


def document_kind(document):
    """Return ``'inventory'``/``'catalogue'``, or ``None`` if it is neither."""
    if not isinstance(document, dict):
        return None
    for kind, (version_key, _stem, _supported) in sorted(_KINDS.items()):
        if version_key in document:
            return kind
    return None


def version_of(document, kind=None):
    """Return the declared format version of ``document``."""
    kind = kind or document_kind(document)
    if kind is None:
        return None
    version_key = _kind_entry(kind)[0]
    return document.get(version_key)


def collect_errors(document, kind=None, prefer_jsonschema=False):
    """Return every problem with ``document``; an empty list means valid."""
    detected = document_kind(document)
    if kind is None:
        kind = detected
    if kind is None:
        return [
            "document declares neither 'inventory_version' nor 'catalogue_version', so "
            "its format cannot be established"
        ]
    version_key, _stem, supported = _kind_entry(kind)
    if detected is not None and detected != kind:
        return [
            "expected a {} document but it declares {!r}".format(kind, version_key)
        ]

    version = document.get(version_key)
    if version not in supported:
        return [
            "unsupported {} format version {!r}; this build understands {}".format(
                kind, version, ", ".join(supported)
            )
        ]

    schema = load_schema(kind, version)
    if prefer_jsonschema:
        try:
            import jsonschema  # noqa: WPS433 (optional dependency)
        except ImportError:
            prefer_jsonschema = False
        else:
            validator = jsonschema.Draft202012Validator(schema)
            return [
                "{}: {}".format(
                    "/".join(str(part) for part in error.absolute_path) or "#", error.message
                )
                for error in sorted(validator.iter_errors(document), key=str)
            ]
    return [str(error) for error in jsonschema_mini.iter_errors(document, schema)]


def validate(document, kind=None, prefer_jsonschema=False):
    """Raise :class:`FormatError` unless ``document`` is valid."""
    errors = collect_errors(document, kind=kind, prefer_jsonschema=prefer_jsonschema)
    if errors:
        raise FormatError(kind or document_kind(document) or "unknown", errors)
    return document


def is_valid(document, kind=None, prefer_jsonschema=False):
    """``True`` if ``document`` validates against its schema."""
    return not collect_errors(document, kind=kind, prefer_jsonschema=prefer_jsonschema)


def read_json(path):
    """Read a JSON document, with the path in the error message on failure."""
    with open(path, "r", encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except ValueError as exc:
            raise FormatError("json", ["{}: {}".format(path, exc)])


def dumps(document, indent=2):
    """Deterministic JSON: sorted keys, one trailing newline."""
    return json.dumps(document, indent=indent, sort_keys=True, ensure_ascii=False) + "\n"


def write_json(document, path, indent=2):
    """Write ``document`` deterministically so reruns are byte-identical."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps(document, indent=indent))
    return path
