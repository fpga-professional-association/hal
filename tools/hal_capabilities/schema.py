"""Schema file lookup and version constants for plugin capability documents.

Mirrors :mod:`hal_findings.schema`: the schema files are data, they are versioned,
and a released version is never edited in place -- add a new
``schema/plugin-capabilities-<version>.schema.json`` and extend
:data:`SUPPORTED_CAPABILITIES_VERSIONS` instead.
"""

import json
import os

__all__ = [
    "CAPABILITIES_VERSION",
    "SUPPORTED_CAPABILITIES_VERSIONS",
    "SCHEMA_DIRECTORY",
    "schema_path",
    "load_schema",
]

#: Version this package writes.
CAPABILITIES_VERSION = "1.0.0"

#: Versions this package can read.  A document with any other version is
#: rejected outright rather than parsed on a best-effort basis.
SUPPORTED_CAPABILITIES_VERSIONS = ("1.0.0",)

SCHEMA_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema")

_CACHE = {}


def schema_path(version=CAPABILITIES_VERSION):
    """Absolute path of the schema file for ``version``."""
    if version not in SUPPORTED_CAPABILITIES_VERSIONS:
        raise ValueError(
            "unsupported capabilities_version {!r}; this build reads {}".format(
                version, ", ".join(SUPPORTED_CAPABILITIES_VERSIONS)
            )
        )
    return os.path.join(SCHEMA_DIRECTORY, "plugin-capabilities-{}.schema.json".format(version))


def load_schema(version=CAPABILITIES_VERSION):
    """Load (and cache) the schema document for ``version``."""
    if version not in _CACHE:
        with open(schema_path(version), "r") as handle:
            _CACHE[version] = json.load(handle)
    return _CACHE[version]
