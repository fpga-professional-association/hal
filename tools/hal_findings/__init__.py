"""hal_findings - a versioned findings and evidence contract shared by HAL analyses.

Analyses in this repository answer very different kinds of question: a SAT
equivalence check can prove something, dataflow analysis can only suggest it,
and a symbolic run may simply time out.  Reports, agents and CI need to consume
all of them without ever mistaking a heuristic for a proof, so every result is
serialized as a *findings document* that states, per finding:

* the **status** -- ``proven_under_assumptions``, ``proven_bounded``,
  ``counterexample``, ``bounded_counterexample``, ``heuristic``, ``unknown``,
  ``timeout``, ``error`` or ``unsupported``;
* the **method** and whether it is **bounded** (a bounded result carries its
  cycle bound and can never be rendered as an unbounded proof);
* the **assumptions** the claim rests on;
* the **artifact** each gate/net ID is scoped to, pinned by content hash;
* the **evidence** backing it, and the solver limits it ran under.

Layout (mirrors ``tools/hal_viz``: everything except the adapters' callers runs
on a plain interpreter, with no HAL build)::

    hal_findings.schema            schema file lookup, SCHEMA_VERSION
    hal_findings.model             builders that refuse contradictory findings
    hal_findings.serialize         deterministic JSON + document digests
    hal_findings.validate          schema + cross-reference validation
    hal_findings.jsonschema_mini   dependency-free JSON Schema subset validator
    hal_findings.adapters.*        wrappers for existing analyses
    hal_findings.cli               ``python -m hal_findings validate ...``

Run the unit tests with a plain interpreter from the repository root::

    python -m unittest discover -s tools/hal_findings -t tools -p "test_*.py"
"""

from .schema import SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS, load_schema, schema_path
from .serialize import (
    canonical_json,
    document_digest,
    dumps,
    loads,
    normalize_document,
    read_document,
    sha256_file,
    write_document,
)
from .validate import FindingsValidationError, collect_errors, is_valid, validate_document

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "load_schema",
    "schema_path",
    "canonical_json",
    "document_digest",
    "dumps",
    "loads",
    "normalize_document",
    "read_document",
    "sha256_file",
    "write_document",
    "FindingsValidationError",
    "collect_errors",
    "is_valid",
    "validate_document",
]
