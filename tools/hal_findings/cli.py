"""Command line front end: validate, normalize and summarize findings documents.

    python tools/hal_findings validate results/*.json
    python tools/hal_findings normalize results/run.json --in-place
    python tools/hal_findings summary results/run.json
    python tools/hal_findings schema --path
"""

import argparse
import json
import os
import sys

from . import __version__
from .schema import SCHEMA_VERSION, SUPPORTED_SCHEMA_VERSIONS, schema_path
from .serialize import document_digest, dumps, read_document, write_document
from .validate import FindingsValidationError, collect_errors

__all__ = ["build_parser", "main"]

#: Findings whose presence makes ``summary --strict`` exit non-zero.
_ALARMING = ("counterexample", "bounded_counterexample", "error")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_findings",
        description="Validate and inspect HAL findings documents (schema {}).".format(
            SCHEMA_VERSION
        ),
    )
    parser.add_argument("--version", action="version", version="hal_findings " + __version__)
    subparsers = parser.add_subparsers(dest="command")

    validate = subparsers.add_parser("validate", help="validate findings documents")
    validate.add_argument("paths", nargs="+", help="findings JSON files")
    validate.add_argument(
        "--jsonschema",
        action="store_true",
        help="use the installed jsonschema library instead of the built-in validator",
    )
    validate.add_argument("--quiet", action="store_true", help="only report failures")

    normalize = subparsers.add_parser(
        "normalize", help="rewrite documents in the deterministic canonical form"
    )
    normalize.add_argument("paths", nargs="+")
    normalize.add_argument(
        "--in-place", action="store_true", help="rewrite the files instead of printing"
    )

    summary = subparsers.add_parser("summary", help="print a per-status summary")
    summary.add_argument("paths", nargs="+")
    summary.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if any counterexample or error finding is present",
    )

    schema = subparsers.add_parser("schema", help="print the schema or its location")
    schema.add_argument("--path", action="store_true", help="print the file path only")
    schema.add_argument("--schema-version", default=SCHEMA_VERSION)

    return parser


def _validate(args, stream):
    failed = 0
    for path in args.paths:
        try:
            document = read_document(path)
            errors = collect_errors(document, prefer_jsonschema=args.jsonschema)
        except FindingsValidationError as exc:
            errors = exc.errors
        except (OSError, ValueError) as exc:
            errors = ["could not read {}: {}".format(path, exc)]
        if errors:
            failed += 1
            stream.write("FAIL {}\n".format(path))
            for error in errors:
                stream.write("     {}\n".format(error))
        elif not args.quiet:
            stream.write(
                "ok   {}  ({} findings, digest {})\n".format(
                    path, len(document.get("findings", [])), document_digest(document)[:12]
                )
            )
    return 1 if failed else 0


def _normalize(args, stream):
    for path in args.paths:
        document = read_document(path)
        if args.in_place:
            write_document(document, path)
            stream.write("normalized {}\n".format(path))
        else:
            stream.write(dumps(document))
    return 0


def _summary(args, stream):
    alarming = 0
    for path in args.paths:
        document = read_document(path)
        counts = {}
        for finding in document.get("findings", []):
            counts[finding["status"]] = counts.get(finding["status"], 0) + 1
        stream.write("{} (schema {})\n".format(path, document.get("schema_version")))
        for status in sorted(counts):
            marker = "!" if status in _ALARMING else " "
            stream.write("  {} {:<24} {}\n".format(marker, status, counts[status]))
            if status in _ALARMING:
                alarming += counts[status]
        for artifact in document.get("artifacts", []):
            stream.write(
                "    artifact {}: {}\n".format(
                    artifact["artifact_id"],
                    artifact.get("sha256", "unhashed ({})".format(
                        artifact.get("unhashed_reason", "")
                    )),
                )
            )
    return 1 if (args.strict and alarming) else 0


def _schema(args, stream):
    if args.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        stream.write("unsupported schema version {}\n".format(args.schema_version))
        return 1
    path = schema_path(args.schema_version)
    if args.path:
        stream.write(os.path.abspath(path) + "\n")
        return 0
    with open(path, "r", encoding="utf-8") as handle:
        stream.write(json.dumps(json.load(handle), indent=2, sort_keys=True) + "\n")
    return 0


def main(argv=None, stream=None):
    """Entry point; returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    stream = stream or sys.stdout

    if args.command == "validate":
        return _validate(args, stream)
    if args.command == "normalize":
        return _normalize(args, stream)
    if args.command == "summary":
        return _summary(args, stream)
    if args.command == "schema":
        return _schema(args, stream)

    parser.print_help(stream)
    return 2
