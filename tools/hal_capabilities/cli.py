"""``python tools/hal_capabilities <command>`` -- the plugin discovery command.

Commands
--------

``list``
    every plugin under ``plugins/``, with the three states apart: declared,
    built, loadable.  Add ``--netlist`` for the fourth -- whether the plugin is
    supported *for that design*.
``show <plugin>``
    one plugin's full declaration.
``check <plugin> --netlist <path>``
    the actionable answer: can this plugin say anything about this netlist, and
    if not, what exactly is missing.
``validate [<file> ...]``
    check declarations against the schema; defaults to every
    ``plugins/*/capabilities.json``.
``schema``
    print the schema (or its path with ``--path``).

Exit codes (``hal --python-script``-compatible: 0 means the answer is yes)::

    0   success / supported
    1   a declaration is invalid, or the command failed
    2   usage error (argparse)
    3   the plugin cannot run on this netlist (unsupported)
    4   a declared dependency is missing, or the plugin is not built/loadable

``check`` returns 0 for ``partial`` as well, because a partial answer is still
an answer; the unsupported gate types are printed either way.
"""

import argparse
import json
import os
import sys

from . import discover, support
from .schema import CAPABILITIES_VERSION, load_schema, schema_path
from .validate import collect_errors

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNSUPPORTED = 3
EXIT_MISSING_DEPENDENCY = 4

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _split_paths(value):
    if not value:
        return []
    return [entry for entry in value.split(os.pathsep) if entry]


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python tools/hal_capabilities",
        description="Discover HAL plugins and what they can be applied to.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--repo-root",
        default=REPO_ROOT,
        help="repository root that holds plugins/ (default: this checkout)",
    )
    parser.add_argument(
        "--build-dir",
        default=os.environ.get("HAL_BASE_PATH"),
        help="HAL build or install directory; decides the 'built' state "
        "(default: $HAL_BASE_PATH)",
    )
    parser.add_argument(
        "--hal-lib",
        action="append",
        default=[],
        help="directory holding hal_py (repeatable; default: $HAL_PY_PATH)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")

    sub = parser.add_subparsers(dest="command")

    list_parser = sub.add_parser("list", help="list plugins and their states")
    list_parser.add_argument(
        "--probe", action="store_true", help="load HAL and resolve the 'loadable' state"
    )
    list_parser.add_argument(
        "--netlist", help="also report whether each plugin supports this netlist"
    )
    list_parser.add_argument("--gate-library", help="gate library for --netlist")
    list_parser.add_argument(
        "--declared-only", action="store_true", help="skip plugins without a declaration"
    )

    show_parser = sub.add_parser("show", help="print one plugin's declaration")
    show_parser.add_argument("plugin")

    check_parser = sub.add_parser("check", help="can this plugin run on this netlist?")
    check_parser.add_argument("plugin")
    check_parser.add_argument("--netlist", required=True)
    check_parser.add_argument("--gate-library")
    check_parser.add_argument(
        "--probe", action="store_true", help="also require that the plugin loads"
    )

    validate_parser = sub.add_parser("validate", help="validate capability declarations")
    validate_parser.add_argument("files", nargs="*")
    validate_parser.add_argument(
        "--jsonschema",
        action="store_true",
        help="use the jsonschema library instead of the built-in validator",
    )

    schema_parser = sub.add_parser("schema", help="print the capability schema")
    schema_parser.add_argument("--path", action="store_true", help="print its path instead")

    return parser


def _load_profile(args, stream):
    """Load ``args.netlist`` and summarize it, or return ``None``."""
    from hal_viz import halenv

    hal_py = halenv.import_hal_py(_split_paths(os.environ.get("HAL_PY_PATH")) + list(args.hal_lib))
    halenv.load_all_plugins(hal_py)
    netlist = halenv.load_netlist(hal_py, args.netlist, getattr(args, "gate_library", None))
    profile = support.netlist_profile(netlist)
    stream.write(
        "netlist {}: {} gates, gate library {!r}, {} gate type(s)\n".format(
            args.netlist,
            profile["gate_count"],
            profile["gate_library"] or "<unknown>",
            len(profile["gate_types"]),
        )
    )
    return profile


def _records(args, probe=False, profile=None):
    records = discover.scan_source_tree(args.repo_root, build_dir=args.build_dir)
    if probe:
        discover.probe_loadable(records, _split_paths(os.environ.get("HAL_PY_PATH")) + list(args.hal_lib))
    if profile is not None:
        for record in records:
            if record.declared and not record.capability_errors:
                record.support = support.evaluate(record.capabilities, profile)
    return records


def _command_list(args, out, err):
    profile = None
    if args.netlist:
        profile = _load_profile(args, err)
    records = _records(args, probe=args.probe, profile=profile)
    if args.declared_only:
        records = [record for record in records if record.declared]

    problems = discover.dependency_problems(records)

    if args.json:
        json.dump(
            {
                "capabilities_version": CAPABILITIES_VERSION,
                "build_dir": args.build_dir,
                "plugins": [record.to_json() for record in records],
                "dependency_problems": problems,
            },
            out,
            indent=2,
            sort_keys=True,
        )
        out.write("\n")
        return EXIT_OK

    header = ["plugin", "declared", "built", "loadable"]
    if profile is not None:
        header.append("netlist")
    rows = []
    for record in records:
        row = [record.name]
        states = record.state_row()
        row.extend([states["declared"], states["built"], states["loadable"]])
        if profile is not None:
            row.append(record.support.state if record.support is not None else "-")
        rows.append(row)

    widths = [max(len(str(row[i])) for row in [header] + rows) for i in range(len(header))]
    out.write("  ".join(name.ljust(widths[i]) for i, name in enumerate(header)) + "\n")
    out.write("  ".join("-" * widths[i] for i in range(len(header))) + "\n")
    for row in rows:
        out.write("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)) + "\n")

    invalid = [record for record in records if record.capability_errors]
    for record in invalid:
        err.write("\n{}: invalid declaration\n".format(record.name))
        for message in record.capability_errors:
            err.write("  - {}\n".format(message))
    for record in records:
        if record.drift:
            err.write("\n{}: declaration drift\n".format(record.name))
            for message in record.drift:
                err.write("  - {}\n".format(message))
    for name, messages in sorted(problems.items()):
        err.write("\n{}: dependency problem\n".format(name))
        for message in messages:
            err.write("  - {}\n".format(message))

    if invalid:
        return EXIT_ERROR
    if problems:
        return EXIT_MISSING_DEPENDENCY
    return EXIT_OK


def _find(records, name, err):
    for record in records:
        if record.name == name:
            return record
    err.write(
        "no plugin {!r} under {}/plugins. Known: {}\n".format(
            name, REPO_ROOT, ", ".join(record.name for record in records)
        )
    )
    return None


def _command_show(args, out, err):
    records = _records(args)
    record = _find(records, args.plugin, err)
    if record is None:
        return EXIT_ERROR
    if not record.declared:
        err.write(
            "plugin {!r} has no {}. Generate one with tools/new_plugin.py or copy the "
            "one from plugins/example_analysis.\n".format(
                record.name, discover.CAPABILITY_FILE_NAME
            )
        )
        return EXIT_ERROR
    json.dump(record.capabilities, out, indent=2, sort_keys=True)
    out.write("\n")
    for message in record.capability_errors:
        err.write("invalid: {}\n".format(message))
    return EXIT_ERROR if record.capability_errors else EXIT_OK


def _command_check(args, out, err):
    profile = _load_profile(args, err)
    records = _records(args, probe=args.probe, profile=profile)
    record = _find(records, args.plugin, err)
    if record is None:
        return EXIT_ERROR
    if not record.declared:
        err.write("plugin {!r} declares no capabilities; nothing to check.\n".format(record.name))
        return EXIT_ERROR
    if record.capability_errors:
        for message in record.capability_errors:
            err.write("invalid declaration: {}\n".format(message))
        return EXIT_ERROR

    problems = discover.dependency_problems(records).get(record.name, [])
    report = record.support

    if args.json:
        json.dump(
            {
                "plugin": record.name,
                "built": record.built,
                "loadable": record.loadable,
                "dependency_problems": problems,
                "support": report.to_json(),
            },
            out,
            indent=2,
            sort_keys=True,
        )
        out.write("\n")
    else:
        out.write("{}: {}\n".format(record.name, report.state))
        if report.matched_gate_types:
            out.write("  usable gate types: {}\n".format(", ".join(report.matched_gate_types)))
        for entry in report.unsupported_gate_types:
            out.write("  unsupported: {}\n".format(entry["reason"]))
        for reason in report.reasons:
            out.write("  reason: {}\n".format(reason))
        for message in problems:
            out.write("  dependency: {}\n".format(message))
        if args.probe and record.loadable is False:
            out.write("  load: {}\n".format(record.load_error))

    if problems or (args.probe and record.loadable is False):
        return EXIT_MISSING_DEPENDENCY
    if report.state == support.UNSUPPORTED:
        return EXIT_UNSUPPORTED
    return EXIT_OK


def _command_validate(args, out, err):
    paths = list(args.files)
    if not paths:
        records = discover.scan_source_tree(args.repo_root)
        paths = [
            os.path.join(record.source_dir, discover.CAPABILITY_FILE_NAME)
            for record in records
            if record.declared or os.path.isfile(
                os.path.join(record.source_dir, discover.CAPABILITY_FILE_NAME)
            )
        ]
    if not paths:
        err.write("no capability declarations found under {}/plugins\n".format(args.repo_root))
        return EXIT_ERROR

    failed = 0
    for path in paths:
        try:
            with open(path, "r") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as exc:
            err.write("{}: {}\n".format(path, exc))
            failed += 1
            continue
        errors = collect_errors(document, prefer_jsonschema=args.jsonschema)
        if errors:
            failed += 1
            err.write("{}: invalid\n".format(path))
            for message in errors:
                err.write("  - {}\n".format(message))
        else:
            out.write("{}: ok\n".format(path))
    return EXIT_ERROR if failed else EXIT_OK


def _command_schema(args, out, err):
    if args.path:
        out.write(schema_path() + "\n")
        return EXIT_OK
    json.dump(load_schema(), out, indent=2, sort_keys=True)
    out.write("\n")
    return EXIT_OK


_COMMANDS = {
    "list": _command_list,
    "show": _command_show,
    "check": _command_check,
    "validate": _command_validate,
    "schema": _command_schema,
}


def main(argv=None, out=None, err=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    if not args.command:
        args = parser.parse_args((argv or []) + ["list"])

    handler = _COMMANDS[args.command]

    guard = None
    if getattr(args, "json", False) and out is sys.stdout:
        # HAL's native logging writes straight to file descriptor 1; with --json
        # that interleaves log lines with the document. Reserve the real stdout
        # for the JSON alone and send everything else to stderr.
        sys.stdout.flush()
        saved = os.dup(1)
        os.dup2(2, 1)
        out = os.fdopen(saved, "w")
        guard = out
    try:
        return handler(args, out, err)
    except Exception as exc:
        err.write("{}: {}\n".format(type(exc).__name__, exc))
        return EXIT_ERROR
    finally:
        if guard is not None:
            guard.flush()
