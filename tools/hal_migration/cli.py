"""Command line interface for the migration assessment tool.

Exit codes are part of the contract (a HAL ``--python-script`` run and CI both
gate on them):

==== =========================================================================
0    the command did what it was asked to do
1    the command failed: bad input, an invalid document, or no HAL to load a
     netlist with
2    the assessment succeeded *and* found unresolved primitives, and
     ``--fail-on-unresolved`` was requested
==== =========================================================================

Note that a successful assessment full of unresolved primitives is a *success*
by default: "this migration has 14 open questions" is the answer, not an error.
"""

import argparse
import os
import sys

from hal_findings import serialize as findings_serialize
from hal_findings import validate as findings_validate

from . import __version__
from . import assess as assess_module
from . import catalogue as catalogue_module
from . import formats
from . import inventory as inventory_module
from . import report as report_module

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNRESOLVED = 2


def _error(message):
    sys.stderr.write("hal_migration: {}\n".format(message))
    return EXIT_ERROR


def _load_netlist(args):
    """Import hal_py through hal_viz.halenv and load the netlist."""
    try:
        from hal_viz import halenv
    except ImportError as exc:
        raise RuntimeError(
            "could not import hal_viz.halenv ({}). Run this tool from the repository "
            "checkout so that 'tools' is on sys.path.".format(exc)
        )
    hal_py = halenv.import_hal_py(args.hal_lib)
    # The netlist and gate library parsers (Verilog, VHDL, HGL, Liberty, ...) are
    # HAL plugins: without loading them, HAL has no parser registered for '.hgl'
    # or '.v' and load_netlist() fails on a perfectly valid file.
    halenv.load_all_plugins(hal_py)
    netlist = halenv.load_netlist(hal_py, args.netlist, args.gate_library)
    # hal_py exposes no version attribute today; record one only if a future build
    # or the environment provides it, never a guess.
    version = getattr(hal_py, "__version__", None) or os.environ.get("HAL_VERSION")
    return netlist, version


def _source_tool(args):
    if not (args.source_tool or args.source_tool_version):
        return None
    tool = {}
    if args.source_tool:
        tool["name"] = args.source_tool
    if args.source_tool_version:
        tool["version"] = args.source_tool_version
    return tool


def _write(document, path, quiet):
    formats.write_json(document, path)
    if not quiet:
        print(path)
    return path


def command_inventory(args):
    try:
        netlist, hal_version = _load_netlist(args)
    except Exception as exc:  # halenv raises HalUnavailable/NetlistLoadError
        return _error(str(exc))

    document = inventory_module.build_inventory(
        netlist,
        artifact_id=args.artifact_id,
        netlist_path=args.netlist,
        source_tool=_source_tool(args),
        vendor=args.vendor,
        family=args.family,
        device=args.device,
        hal_version=hal_version,
        producer_command=["hal_migration", "inventory", str(args.netlist)],
    )
    try:
        formats.validate(document, "inventory")
    except formats.FormatError as exc:
        return _error("the extracted inventory is not schema-valid: {}".format(exc))

    if args.output:
        _write(document, args.output, args.quiet)
    else:
        sys.stdout.write(formats.dumps(document))
    if not args.quiet:
        totals = document["totals"]
        sys.stderr.write(
            "inventoried {} gate(s) of {} type(s); {} metadata gap(s)\n".format(
                totals["gates"], totals["gate_types"], totals["metadata_gaps"]
            )
        )
    return EXIT_OK


def _assess(inventory_document, catalogue_document, catalogue_path, command):
    document = assess_module.build_document(
        inventory_document,
        catalogue_document,
        catalogue_path=catalogue_path,
        producer_command=["hal_migration", command],
    )
    try:
        findings_validate.validate_document(document)
    except findings_validate.FindingsValidationError as exc:
        raise RuntimeError("the assessment is not a valid findings document: {}".format(exc))
    return document


def _emit_assessment(document, inventory_document, args):
    if args.output:
        findings_serialize.write_document(document, args.output)
        if not args.quiet:
            print(args.output)
    elif not args.report:
        sys.stdout.write(findings_serialize.dumps(document))

    if args.report:
        text = report_module.render_markdown(document, inventory=inventory_document)
        directory = os.path.dirname(os.path.abspath(args.report))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(args.report, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        if not args.quiet:
            print(args.report)

    summary = assess_module.summarize(document)
    if not args.quiet:
        sys.stderr.write(
            "assessment: {} supported, {} candidate, {} unresolved gate type(s); "
            "{} distinct verification obligation(s) open\n".format(
                summary["supported"],
                summary["candidate"],
                summary["unresolved"],
                summary["obligations"],
            )
        )
    if summary["unresolved"] and args.fail_on_unresolved:
        sys.stderr.write(
            "hal_migration: {} unresolved gate type(s): {}\n".format(
                summary["unresolved"], ", ".join(summary["unresolved_types"])
            )
        )
        return EXIT_UNRESOLVED
    return EXIT_OK


def command_assess(args):
    try:
        inventory_document = formats.read_json(args.inventory)
        formats.validate(inventory_document, "inventory")
        catalogue_path = catalogue_module.resolve_path(args.catalogue)
        catalogue_document = catalogue_module.load(catalogue_path)
    except (formats.FormatError, catalogue_module.CatalogueError, OSError) as exc:
        return _error(str(exc))

    try:
        document = _assess(inventory_document, catalogue_document, catalogue_path, "assess")
    except RuntimeError as exc:
        return _error(str(exc))
    return _emit_assessment(document, inventory_document, args)


def command_run(args):
    try:
        catalogue_path = catalogue_module.resolve_path(args.catalogue)
        catalogue_document = catalogue_module.load(catalogue_path)
    except (formats.FormatError, catalogue_module.CatalogueError, OSError) as exc:
        return _error(str(exc))
    try:
        netlist, hal_version = _load_netlist(args)
    except Exception as exc:
        return _error(str(exc))

    inventory_document = inventory_module.build_inventory(
        netlist,
        artifact_id=args.artifact_id,
        netlist_path=args.netlist,
        source_tool=_source_tool(args),
        vendor=args.vendor,
        family=args.family,
        device=args.device,
        hal_version=hal_version,
        producer_command=["hal_migration", "run", str(args.netlist)],
    )
    try:
        formats.validate(inventory_document, "inventory")
    except formats.FormatError as exc:
        return _error("the extracted inventory is not schema-valid: {}".format(exc))
    if args.inventory_output:
        _write(inventory_document, args.inventory_output, args.quiet)

    try:
        document = _assess(inventory_document, catalogue_document, catalogue_path, "run")
    except RuntimeError as exc:
        return _error(str(exc))
    return _emit_assessment(document, inventory_document, args)


def command_catalogues(args):
    bundled = catalogue_module.bundled_catalogues()
    if not bundled:
        sys.stderr.write("no catalogues are bundled with this tool\n")
        return EXIT_OK
    for catalogue_id in sorted(bundled):
        path = bundled[catalogue_id]
        document = formats.read_json(path)
        target = document.get("target", {})
        print(
            "{}\trevision {}\t{} {} -> {} {}\t{}".format(
                catalogue_id,
                document.get("revision"),
                (document.get("source") or {}).get("vendor", "?"),
                (document.get("source") or {}).get("family", "?"),
                target.get("vendor", "?"),
                target.get("family", "?"),
                path,
            )
        )
    return EXIT_OK


def command_validate(args):
    failures = 0
    for path in args.files:
        try:
            document = formats.read_json(path)
        except (formats.FormatError, OSError) as exc:
            failures += 1
            sys.stderr.write("{}: {}\n".format(path, exc))
            continue
        kind = args.kind or formats.document_kind(document)
        errors = formats.collect_errors(
            document, kind=kind, prefer_jsonschema=args.jsonschema
        )
        if kind == "catalogue" and not errors:
            try:
                catalogue_module.check_obligation_ids(document, path)
            except catalogue_module.CatalogueError as exc:
                errors = [str(exc)]
        if errors:
            failures += 1
            sys.stderr.write("{}: invalid {}\n".format(path, kind or "document"))
            for error in errors:
                sys.stderr.write("  - {}\n".format(error))
        elif not args.quiet:
            print("{}: valid {} {}".format(path, kind, formats.version_of(document, kind)))
    return EXIT_ERROR if failures else EXIT_OK


def command_schema(args):
    path = formats.schema_path(args.kind)
    if args.path:
        print(path)
        return EXIT_OK
    with open(path, "r", encoding="utf-8") as handle:
        sys.stdout.write(handle.read())
    return EXIT_OK


def _add_netlist_arguments(parser):
    parser.add_argument("netlist", help="HAL project directory, .hal file, or HDL netlist")
    parser.add_argument(
        "--gate-library",
        help="gate library (.hgl/.lib); required for HDL netlists",
    )
    parser.add_argument(
        "--hal-lib",
        action="append",
        default=[],
        metavar="DIR",
        help="directory containing hal_py (repeatable); $HAL_PY_PATH works too",
    )
    parser.add_argument(
        "--artifact-id",
        default="source_netlist",
        help="id the gate references are scoped to (default: source_netlist)",
    )
    parser.add_argument("--source-tool", help="name of the tool that produced the netlist")
    parser.add_argument("--source-tool-version", help="version of that tool")
    parser.add_argument("--vendor", help="source silicon vendor, if known")
    parser.add_argument("--family", help="source device family, if known")
    parser.add_argument("--device", help="source device, if known")


def _add_assessment_arguments(parser):
    parser.add_argument(
        "-o", "--output", help="write the assessment findings document here"
    )
    parser.add_argument("--report", help="also render a Markdown assessment report here")
    parser.add_argument(
        "--fail-on-unresolved",
        action="store_true",
        help="exit with 2 when any source primitive is unresolved",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_migration",
        description="Assess what a vendor migration would require, from a netlist and "
        "a reviewed target capability catalogue. Converts nothing.",
    )
    parser.add_argument("--version", action="version", version="hal_migration " + __version__)
    parser.add_argument("-q", "--quiet", action="store_true", help="only report failures")
    subparsers = parser.add_subparsers(dest="command")

    inventory_parser = subparsers.add_parser(
        "inventory", help="extract a source inventory from a netlist (needs a built HAL)"
    )
    _add_netlist_arguments(inventory_parser)
    inventory_parser.add_argument("-o", "--output", help="write the inventory here")
    inventory_parser.add_argument("-q", "--quiet", action="store_true")
    inventory_parser.set_defaults(func=command_inventory)

    assess_parser = subparsers.add_parser(
        "assess", help="assess an inventory against a target catalogue (no HAL needed)"
    )
    assess_parser.add_argument("--inventory", required=True, help="source inventory JSON")
    assess_parser.add_argument(
        "--catalogue",
        required=True,
        help="catalogue file, or the id of a bundled catalogue (see 'catalogues')",
    )
    _add_assessment_arguments(assess_parser)
    assess_parser.add_argument("-q", "--quiet", action="store_true")
    assess_parser.set_defaults(func=command_assess)

    run_parser = subparsers.add_parser(
        "run", help="inventory a netlist and assess it in one step (needs a built HAL)"
    )
    _add_netlist_arguments(run_parser)
    run_parser.add_argument("--catalogue", required=True)
    run_parser.add_argument(
        "--inventory-output", help="also write the intermediate inventory here"
    )
    _add_assessment_arguments(run_parser)
    run_parser.add_argument("-q", "--quiet", action="store_true")
    run_parser.set_defaults(func=command_run)

    catalogues_parser = subparsers.add_parser(
        "catalogues", help="list the catalogues bundled with this tool"
    )
    catalogues_parser.add_argument("-q", "--quiet", action="store_true")
    catalogues_parser.set_defaults(func=command_catalogues)

    validate_parser = subparsers.add_parser(
        "validate", help="validate inventory and catalogue documents"
    )
    validate_parser.add_argument("files", nargs="+")
    validate_parser.add_argument(
        "--kind",
        choices=["inventory", "catalogue"],
        help="force the document kind instead of detecting it",
    )
    validate_parser.add_argument(
        "--jsonschema",
        action="store_true",
        help="use the jsonschema library instead of the built-in validator",
    )
    validate_parser.add_argument("-q", "--quiet", action="store_true")
    validate_parser.set_defaults(func=command_validate)

    schema_parser = subparsers.add_parser("schema", help="print a format schema")
    schema_parser.add_argument(
        "--kind", choices=["inventory", "catalogue"], default="inventory"
    )
    schema_parser.add_argument("--path", action="store_true", help="print the path only")
    schema_parser.add_argument("-q", "--quiet", action="store_true")
    schema_parser.set_defaults(func=command_schema)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_ERROR
    return args.func(args)
