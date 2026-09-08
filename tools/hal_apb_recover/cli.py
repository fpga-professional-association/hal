"""Command line for the APB register-map recovery.

Two ways in, one analysis:

* ``--source verilog`` reads a structural Verilog netlist and a ``.hgl`` gate
  library with the standard library only.  No HAL build needed; this is how the
  fixture and the offline tests run.
* ``--source hal`` loads the netlist through ``hal_py`` (a HAL project
  directory, a ``.hal`` file or any format HAL has a parser for).

Both build the same circuit model, so the recovery, the exports and the replay
are literally the same code.

Exit codes are meant to be trusted in CI: ``0`` success, ``1`` a real failure
(bad mapping, unreadable netlist, replay mismatch), ``2`` a usage error.
"""

import argparse
import json
import os
import sys

from . import __version__
from .circuit import CircuitError
from .mapping import MappingError, load_mapping
from .recover import RecoveryError, RecoveryOptions, recover

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


class UsageError(RuntimeError):
    pass


def _load_circuit(args):
    if args.source == "verilog":
        from .hgl_library import GateLibrary, GateLibraryError
        from .verilog_source import VerilogError, read_netlist

        if not args.gate_library:
            raise UsageError(
                "--source verilog needs --gate-library pointing at a .hgl file "
                "(for example plugins/gate_libraries/definitions/NangateOpenCellLibrary.hgl)"
            )
        try:
            library = GateLibrary.from_file(args.gate_library)
            return read_netlist(args.netlist, library, top_module=args.top_module)
        except (GateLibraryError, VerilogError) as exc:
            raise RuntimeError(str(exc))

    for entry in args.hal_lib or []:
        path = os.path.abspath(os.path.expanduser(entry))
        if not os.path.isdir(path):
            raise UsageError("--hal-lib directory does not exist: {}".format(entry))
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        import hal_py
    except ImportError as exc:
        raise RuntimeError(
            "could not import hal_py ({}). Point at a build with --hal-lib <build>/lib "
            "or PYTHONPATH=<build>/lib, and set HAL_BASE_PATH=<build>. Use "
            "--source verilog to run without a HAL build.".format(exc)
        )
    from .hal_source import build_circuit, load_netlist

    hal_py.plugin_manager.load_all_plugins()
    netlist = load_netlist(hal_py, args.netlist, args.gate_library)
    return build_circuit(hal_py, netlist)


def _write(path, text, quiet):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    if not quiet:
        print(path)
    return path


def command_recover(args):
    from . import findings as findings_module
    from . import regmap
    from . import replay as replay_module

    circuit = _load_circuit(args)
    mapping = load_mapping(args.mapping, circuit)
    document = recover(
        circuit,
        mapping,
        RecoveryOptions(guard_scan_limit=args.guard_scan_limit, tag=args.tag),
    )
    regmap.validate_document(document)

    base = args.output or os.path.join(os.getcwd(), "apb_register_map")
    directory = os.path.dirname(os.path.abspath(base))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)

    _write(base + ".json", regmap.dumps(document), args.quiet)
    _write(base + ".md", regmap.to_markdown(document), args.quiet)

    if not args.no_findings:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from hal_findings import serialize as findings_serialize
        from hal_findings import validate as findings_validate

        findings_document = findings_module.build_document(
            document,
            netlist_path=circuit.source,
            producer_command=[os.path.basename(sys.argv[0])] + sys.argv[1:],
        )
        findings_validate.validate_document(findings_document)
        _write(
            base + ".findings.json",
            findings_serialize.dumps(findings_document),
            args.quiet,
        )

    if not args.no_replay:
        replay_document = replay_module.build_replay(document)
        _write(
            base + ".replay.json",
            json.dumps(replay_document, indent=2, sort_keys=True) + "\n",
            args.quiet,
        )

    if not args.quiet:
        print(_summary_text(document), file=sys.stderr)

    if args.fail_on_unresolved and document["unresolved_fields"]:
        print(
            "FAILED: {} unresolved field(s); --fail-on-unresolved was passed".format(
                len(document["unresolved_fields"])
            ),
            file=sys.stderr,
        )
        return EXIT_FAILURE
    return EXIT_OK


def _summary_text(document):
    coverage = document["coverage"]
    lines = [
        "recovered {} register(s) over {} probed address(es) in {} probes".format(
            len(document["registers"]),
            coverage["addresses_probed"],
            document["metrics"]["probes"],
        )
    ]
    tiers = {}
    for register in document["registers"]:
        for field in register["fields"]:
            tiers[field["confidence"]] = tiers.get(field["confidence"], 0) + 1
    lines.append(
        "  fields by confidence: "
        + ", ".join("{} {}".format(count, tier) for tier, count in sorted(tiers.items()))
    )
    lines.append(
        "  {} alias class(es), {} unmapped address(es), {} unresolved field(s)".format(
            len(document["alias_classes"]),
            len(document["unmapped_addresses"]),
            len(document["unresolved_fields"]),
        )
    )
    if coverage["unsupported_gate_types"]:
        lines.append(
            "  unsupported primitives: "
            + ", ".join(
                "{} x {}".format(entry["count"], entry["gate_type"])
                for entry in coverage["unsupported_gate_types"]
            )
        )
    return "\n".join(lines)


def command_replay(args):
    from . import replay as replay_module

    circuit = _load_circuit(args)
    mapping = load_mapping(args.mapping, circuit)
    with open(args.replay, "r", encoding="utf-8") as handle:
        replay_document = json.load(handle)
    results = replay_module.run_replay(circuit, mapping, replay_document)
    ok, lines = replay_module.format_results(results, allow_heuristic=args.allow_heuristic)
    print("\n".join(lines))
    return EXIT_OK if ok else EXIT_FAILURE


def command_validate(args):
    from . import regmap

    failures = 0
    for path in args.documents:
        try:
            regmap.validate_document(regmap.read_document(path))
            print("ok    {}".format(path))
        except (regmap.RegisterMapError, ValueError) as exc:
            failures += 1
            print("FAIL  {}: {}".format(path, exc))
    return EXIT_FAILURE if failures else EXIT_OK


def command_summary(args):
    from . import regmap

    for path in args.documents:
        document = regmap.read_document(path)
        print("== {}".format(path))
        print(_summary_text(document))
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_apb_recover",
        description="Recover an APB register map from a flattened netlist whose "
        "internal names have been stripped.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    def add_netlist_arguments(target):
        target.add_argument("netlist", help="netlist file or HAL project directory")
        target.add_argument("mapping", help="user-supplied APB mapping (JSON)")
        target.add_argument(
            "--source",
            choices=("verilog", "hal"),
            default="verilog",
            help="how to read the netlist (default: verilog, no HAL build needed)",
        )
        target.add_argument(
            "--gate-library",
            help="gate library: a .hgl file for --source verilog, a library file for "
            "--source hal",
        )
        target.add_argument("--top-module", help="top module name (--source verilog)")
        target.add_argument(
            "--hal-lib",
            action="append",
            default=[],
            metavar="DIR",
            help="directory containing hal_py (repeatable, --source hal)",
        )

    recover_parser = subparsers.add_parser(
        "recover", help="recover the register map and write every export"
    )
    add_netlist_arguments(recover_parser)
    recover_parser.add_argument(
        "-o",
        "--output",
        help="output path prefix; .json, .md, .findings.json and .replay.json are "
        "appended (default: ./apb_register_map)",
    )
    recover_parser.add_argument("--tag", help="free-form tag recorded in the document")
    recover_parser.add_argument(
        "--guard-scan-limit",
        type=int,
        default=128,
        help="how many state bits to try when looking for the guard of a conditional "
        "write (default: 128)",
    )
    recover_parser.add_argument("--no-findings", action="store_true")
    recover_parser.add_argument("--no-replay", action="store_true")
    recover_parser.add_argument(
        "--fail-on-unresolved",
        action="store_true",
        help="exit 1 when any field could not be characterised",
    )
    recover_parser.add_argument("-q", "--quiet", action="store_true")
    recover_parser.set_defaults(handler=command_recover)

    replay_parser = subparsers.add_parser(
        "replay", help="run a generated replay against the netlist"
    )
    add_netlist_arguments(replay_parser)
    replay_parser.add_argument("replay", help="replay document (JSON)")
    replay_parser.add_argument(
        "--allow-heuristic",
        action="store_true",
        help="do not fail on transactions that exercise an inferred claim",
    )
    replay_parser.set_defaults(handler=command_replay)

    validate_parser = subparsers.add_parser(
        "validate", help="validate register-map documents"
    )
    validate_parser.add_argument("documents", nargs="+")
    validate_parser.set_defaults(handler=command_validate)

    summary_parser = subparsers.add_parser("summary", help="summarise register maps")
    summary_parser.add_argument("documents", nargs="+")
    summary_parser.set_defaults(handler=command_summary)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if not getattr(args, "handler", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.handler(args)
    except UsageError as exc:
        print("usage error: {}".format(exc), file=sys.stderr)
        return EXIT_USAGE
    except (MappingError, RecoveryError, CircuitError) as exc:
        print("FAILED: {}".format(exc), file=sys.stderr)
        return EXIT_FAILURE
    except (RuntimeError, OSError, ValueError) as exc:
        print("FAILED: {}".format(exc), file=sys.stderr)
        return EXIT_FAILURE
