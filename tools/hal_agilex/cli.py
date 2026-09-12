"""Command line for the Agilex support package.

::

    python tools/hal_agilex inventory  export.vo [-o findings.json]
    python tools/hal_agilex import     export.vo -o export.hal.v
    python tools/hal_agilex behavior   export.vo --reference reference.py
    python tools/hal_agilex recognize  export.vo [-o findings.json]
    python tools/hal_agilex fixture    tools/hal_agilex/fixtures/agilex3_counter_adder
    python tools/hal_agilex library    [-o plugins/gate_libraries/definitions/AGILEX_TENNM.hgl]
    python tools/hal_agilex elaborate  export.hal.v --hal-lib <build>/lib

Exit codes follow the convention the rest of this repository uses: ``0`` when
the command did what it says, ``1`` when it failed, and ``2`` with ``--strict``
when the findings contain a counterexample, an error or an unsupported result.

``--strict`` is a top-level flag and must be placed **before** the subcommand
(``python tools/hal_agilex --strict inventory export.vo``); argparse rejects it
after one.
"""

import argparse
import os
import sys

from . import behavior, inventory, library, recognize, vo_import, vo_netlist
from .primitives import UnsupportedConfiguration
from .simulate import SimulationError

from hal_findings import serialize, validate

BLOCKING_STATUSES = ("counterexample", "bounded_counterexample", "error", "unsupported")

FIXTURE_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _emit(document, output, strict):
    validate.validate_document(document)
    text = serialize.dumps(document)
    if output:
        with open(output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        print(output)
    else:
        sys.stdout.write(text)
    if strict:
        for finding in document["findings"]:
            if finding["status"] in BLOCKING_STATUSES:
                print(
                    "strict: {} is {}".format(finding["id"], finding["status"]),
                    file=sys.stderr,
                )
                return 2
    return 0


def _fixture_paths(directory):
    files = sorted(os.listdir(directory))
    export = next((name for name in files if name.endswith(".vo")), None)
    if export is None:
        raise SystemExit("no .vo export in {}".format(directory))
    reference = os.path.join(directory, "reference.py")
    return os.path.join(directory, export), reference if os.path.isfile(reference) else None


def command_inventory(args):
    document = inventory.build_document_for_file(args.export, artifact_id=args.artifact_id)
    return _emit(document, args.output, args.strict)


def command_import(args):
    netlist = vo_netlist.parse_file(args.export)
    try:
        text = vo_import.convert(netlist, source_note=os.path.basename(args.export))
    except vo_import.ImportRefused as exc:
        print("import refused: {}".format(exc), file=sys.stderr)
        return 1
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        print(args.output)
    else:
        sys.stdout.write(text)
    return 0


def command_behavior(args):
    netlist = vo_netlist.parse_file(args.export)
    reference = behavior.load_reference(args.reference)
    try:
        result = behavior.run_reference_check(netlist, reference, cycles=args.cycles)
    except SimulationError as exc:
        print("cannot simulate: {}".format(exc), file=sys.stderr)
        return 1
    artifact = inventory._artifact(netlist, args.export, args.artifact_id or netlist.name)
    document = behavior.build_document(netlist, artifact, result)
    return _emit(document, args.output, args.strict)


def command_recognize(args):
    netlist = vo_netlist.parse_file(args.export)
    artifact = inventory._artifact(netlist, args.export, args.artifact_id or netlist.name)
    document = recognize.build_document(netlist, artifact)
    return _emit(document, args.output, args.strict)


def command_fixture(args):
    export, reference = _fixture_paths(args.directory)
    netlist = vo_netlist.parse_file(export)
    status = 0

    print("== inventory")
    document = inventory.build_document(netlist, export)
    validate.validate_document(document)
    for finding in document["findings"]:
        print("   {:<26} {}".format(finding["status"], finding["title"]))
        if args.strict and finding["status"] in BLOCKING_STATUSES:
            status = 2

    if reference:
        print("== behaviour against the RTL reference")
        module = behavior.load_reference(reference)
        result = behavior.run_reference_check(netlist, module, cycles=args.cycles)
        artifact = inventory._artifact(netlist, export, netlist.name)
        document = behavior.build_document(netlist, artifact, result)
        validate.validate_document(document)
        for finding in document["findings"]:
            print("   {:<26} {}".format(finding["status"], finding["title"]))
            if finding["status"] in BLOCKING_STATUSES:
                status = 2

        print("== recognition")
        document = recognize.build_document(netlist, artifact)
        validate.validate_document(document)
        for finding in document["findings"]:
            print("   {:<26} {}".format(finding["status"], finding["title"]))
    return status


def command_library(args):
    path = args.output or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        library.LIBRARY_PATH,
    )
    if args.check:
        with open(path, "r", encoding="utf-8") as handle:
            current = handle.read()
        if current != library.dumps():
            print(
                "{} differs from what tools/hal_agilex/library.py generates".format(path),
                file=sys.stderr,
            )
            return 1
        print("{}: up to date".format(path))
        return 0
    library.write(path)
    print(path)
    return 0


def command_elaborate(args):
    from . import hal_adapter

    hal_py = hal_adapter.import_hal(args.hal_lib)
    netlist = hal_adapter.load_netlist(hal_py, args.netlist, args.gate_library)
    report = hal_adapter.elaborate(hal_py, netlist)
    document = hal_adapter.build_document(
        netlist, report, artifact_id=args.artifact_id or netlist.get_design_name(),
        path=args.netlist,
    )
    return _emit(document, args.output, args.strict)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_agilex",
        description="Validated Altera Agilex primitive support: inventory a Quartus "
        "export, import it for HAL, validate it against its RTL and recognize "
        "carry-chain arithmetic.",
    )
    parser.add_argument("--strict", action="store_true",
                        help="exit 2 when the findings contain a blocking status")
    subparsers = parser.add_subparsers(dest="command")

    def add_common(subparser):
        subparser.add_argument("-o", "--output", help="write the findings here")
        subparser.add_argument("--artifact-id", help="artifact id used in the findings")

    inventory_parser = subparsers.add_parser(
        "inventory", help="report which primitives an export uses and which are covered"
    )
    inventory_parser.add_argument("export")
    add_common(inventory_parser)
    inventory_parser.set_defaults(func=command_inventory)

    import_parser = subparsers.add_parser(
        "import", help="rewrite a .vo export into Verilog that HAL can read"
    )
    import_parser.add_argument("export")
    import_parser.add_argument("-o", "--output")
    import_parser.set_defaults(func=command_import)

    behavior_parser = subparsers.add_parser(
        "behavior", help="simulate an export against a Python model of its RTL"
    )
    behavior_parser.add_argument("export")
    behavior_parser.add_argument("--reference", required=True)
    behavior_parser.add_argument("--cycles", type=int, default=behavior.DEFAULT_CYCLES)
    add_common(behavior_parser)
    behavior_parser.set_defaults(func=command_behavior)

    recognize_parser = subparsers.add_parser(
        "recognize", help="recognize carry-chain adders and accumulators"
    )
    recognize_parser.add_argument("export")
    add_common(recognize_parser)
    recognize_parser.set_defaults(func=command_recognize)

    fixture_parser = subparsers.add_parser(
        "fixture", help="run inventory, behaviour and recognition over a fixture directory"
    )
    fixture_parser.add_argument("directory")
    fixture_parser.add_argument("--cycles", type=int, default=behavior.DEFAULT_CYCLES)
    fixture_parser.set_defaults(func=command_fixture)

    library_parser = subparsers.add_parser(
        "library", help="regenerate (or check) the AGILEX_TENNM gate library"
    )
    library_parser.add_argument("-o", "--output")
    library_parser.add_argument("--check", action="store_true",
                                help="fail if the committed library is out of date")
    library_parser.set_defaults(func=command_library)

    elaborate_parser = subparsers.add_parser(
        "elaborate", help="load an imported netlist in HAL and attach primitive semantics"
    )
    elaborate_parser.add_argument("netlist")
    elaborate_parser.add_argument("--gate-library", required=True)
    elaborate_parser.add_argument("--hal-lib", action="append", default=[],
                                  help="directory containing hal_py (repeatable)")
    add_common(elaborate_parser)
    elaborate_parser.set_defaults(func=command_elaborate)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except (vo_netlist.VerilogSubsetError, SimulationError, UnsupportedConfiguration) as exc:
        print("{}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print("{}".format(exc), file=sys.stderr)
        return 1
