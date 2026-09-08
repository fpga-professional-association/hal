"""Access to the versioned findings JSON schema."""

import json
import os

__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SCHEMA_DIR",
    "schema_path",
    "load_schema",
]

#: The schema version this package writes.
SCHEMA_VERSION = "1.0.0"

#: Every schema version this package can still read.
SUPPORTED_SCHEMA_VERSIONS = ("1.0.0",)

#: Directory holding the shipped ``findings-<version>.schema.json`` files.
SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema")

_CACHE = {}


def schema_path(version=SCHEMA_VERSION):
    """Return the absolute path of the schema file for ``version``."""
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(
            "unsupported findings schema version {!r}; this build of hal_findings "
            "understands {}".format(version, ", ".join(SUPPORTED_SCHEMA_VERSIONS))
        )
    return os.path.join(SCHEMA_DIR, "findings-{}.schema.json".format(version))


def load_schema(version=SCHEMA_VERSION):
    """Load (and cache) the schema document for ``version``."""
    if version not in _CACHE:
        with open(schema_path(version), "r", encoding="utf-8") as handle:
            _CACHE[version] = json.load(handle)
    return _CACHE[version]
