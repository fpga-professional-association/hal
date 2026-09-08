"""Schema and cross-reference validation for findings documents.

Validation happens in two layers:

1. **Schema** -- structure, vocabularies, and the status/bounds rules that can
   be expressed declaratively (a bounded result can never omit its cycle bound,
   an unbounded proof can never carry one, ...).  Enforced by
   :mod:`hal_findings.jsonschema_mini`, or by ``jsonschema`` when it is
   installed and ``prefer_jsonschema`` is requested.
2. **Semantics** -- everything JSON Schema cannot see: unique IDs, and the rule
   that every gate/net/module reference resolves to an artifact that the
   document actually declares and that the finding's scope allows.
"""

from . import jsonschema_mini
from .schema import SUPPORTED_SCHEMA_VERSIONS, load_schema

__all__ = [
    "FindingsValidationError",
    "schema_errors",
    "semantic_errors",
    "collect_errors",
    "validate_document",
    "is_valid",
]


class FindingsValidationError(ValueError):
    """Raised when a document violates the schema or its reference rules."""

    def __init__(self, errors):
        self.errors = list(errors)
        ValueError.__init__(
            self,
            "findings document is invalid ({} problem{}):\n  - {}".format(
                len(self.errors),
                "" if len(self.errors) == 1 else "s",
                "\n  - ".join(self.errors),
            ),
        )


def _schema_for(document):
    version = document.get("schema_version")
    if version is None:
        raise FindingsValidationError(["document has no 'schema_version'"])
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise FindingsValidationError(
            [
                "unsupported schema_version {!r}; this build understands {}".format(
                    version, ", ".join(SUPPORTED_SCHEMA_VERSIONS)
                )
            ]
        )
    return load_schema(version)


def schema_errors(document, prefer_jsonschema=False):
    """Return schema violations as human readable strings."""
    schema = _schema_for(document)

    if prefer_jsonschema:
        try:
            import jsonschema  # noqa: F401  (optional dependency)
        except ImportError:
            prefer_jsonschema = False

    if prefer_jsonschema:
        import jsonschema

        validator = jsonschema.Draft202012Validator(schema)
        return [
            "#{}: {}".format(
                "".join("/" + str(part) for part in error.absolute_path), error.message
            )
            for error in sorted(validator.iter_errors(document), key=str)
        ]

    return [str(error) for error in jsonschema_mini.iter_errors(document, schema)]


def _ref_errors(refs, kind, declared, allowed, finding_id, errors):
    for ref in refs or []:
        artifact_id = ref.get("artifact_id")
        where = "finding {!r} {} reference {!r}".format(finding_id, kind, ref.get("name"))
        if artifact_id not in declared:
            errors.append(
                "{} points at undeclared artifact {!r}; gate and net IDs are only "
                "meaningful relative to a declared artifact".format(where, artifact_id)
            )
        elif artifact_id not in allowed:
            errors.append(
                "{} points at artifact {!r}, which is not in the finding's "
                "scope.artifact_ids".format(where, artifact_id)
            )


def semantic_errors(document):
    """Return violations of the rules JSON Schema cannot express."""
    errors = []

    artifacts = document.get("artifacts", [])
    declared = set()
    for artifact in artifacts:
        artifact_id = artifact.get("artifact_id")
        if artifact_id in declared:
            errors.append("duplicate artifact_id {!r}".format(artifact_id))
        declared.add(artifact_id)

    seen_findings = set()
    for finding in document.get("findings", []):
        finding_id = finding.get("id")
        if finding_id in seen_findings:
            errors.append("duplicate finding id {!r}".format(finding_id))
        seen_findings.add(finding_id)

        scope = finding.get("scope", {})
        allowed = set(scope.get("artifact_ids", []))
        for artifact_id in sorted(allowed):
            if artifact_id not in declared:
                errors.append(
                    "finding {!r} is scoped to undeclared artifact {!r}".format(
                        finding_id, artifact_id
                    )
                )

        _ref_errors(scope.get("gates"), "gate", declared, allowed, finding_id, errors)
        _ref_errors(scope.get("nets"), "net", declared, allowed, finding_id, errors)
        _ref_errors(scope.get("modules"), "module", declared, allowed, finding_id, errors)

        for primitive in finding.get("unsupported", {}).get("primitives", []):
            _ref_errors(
                primitive.get("example_gates"), "gate", declared, allowed, finding_id, errors
            )
        for entry in finding.get("counterexample", {}).get("witness", []):
            if "net" in entry:
                _ref_errors([entry["net"]], "net", declared, allowed, finding_id, errors)

        assumption_ids = set()
        for assumption in finding.get("assumptions", []):
            assumption_id = assumption.get("id")
            if assumption_id in assumption_ids:
                errors.append(
                    "finding {!r} has duplicate assumption id {!r}".format(
                        finding_id, assumption_id
                    )
                )
            assumption_ids.add(assumption_id)

        bounds = finding.get("bounds", {})
        counterexample = finding.get("counterexample", {})
        if (
            counterexample.get("cycle_bound") is not None
            and bounds.get("cycle_bound") is not None
            and counterexample["cycle_bound"] > bounds["cycle_bound"]
        ):
            errors.append(
                "finding {!r}: counterexample.cycle_bound ({}) exceeds bounds.cycle_bound "
                "({})".format(finding_id, counterexample["cycle_bound"], bounds["cycle_bound"])
            )

    return errors


def collect_errors(document, prefer_jsonschema=False):
    """Return all schema and semantic problems, schema problems first."""
    errors = schema_errors(document, prefer_jsonschema=prefer_jsonschema)
    if errors:
        # Semantic checks assume a structurally valid document.
        return errors
    return semantic_errors(document)


def validate_document(document, prefer_jsonschema=False):
    """Raise :class:`FindingsValidationError` if ``document`` is invalid."""
    errors = collect_errors(document, prefer_jsonschema=prefer_jsonschema)
    if errors:
        raise FindingsValidationError(errors)
    return document


def is_valid(document, prefer_jsonschema=False):
    """Return ``True`` if ``document`` validates."""
    try:
        return not collect_errors(document, prefer_jsonschema=prefer_jsonschema)
    except FindingsValidationError:
        return False
