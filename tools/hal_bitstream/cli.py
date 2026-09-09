"""Command line for bitstream ingestion.

::

    python tools/hal_bitstream families
    python tools/hal_bitstream detect  blinky.bin
    python tools/hal_bitstream convert blinky.bin -o blinky.v
    python tools/hal_bitstream load    blinky.bin --project-dir build/blinky_project

Exit codes follow the convention the rest of this fork uses:

===== ==============================================================
    0 the command did what it was asked to do
    1 the command ran and the *result* is negative (nothing to load)
    2 the command could not run: unknown family, missing converter,
      converter failure, no built HAL -- **never** a statement about
      the design
===== ==============================================================
"""

import argparse
import json
import os
import sys

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from . import __version__, convert as convert_module, registry  # noqa: E402
from .convert import ConversionError  # noqa: E402
from .detect import DetectionError, detect_family  # noqa: E402

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_RESULT = 1
EXIT_ERROR = 2


class CliError(RuntimeError):
    """A setup problem: exit 2, never exit 1."""


def _log(quiet):
    def log(message):
        if not quiet:
            print("[hal_bitstream] {}".format(message), file=sys.stderr)

    return log


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_families(args):
    overrides = convert_module.parse_converter_overrides(args.converter)
    if args.json:
        document = []
        for family in registry.families():
            converters = convert_module.available_converters(family, overrides)
            entry = family.to_json()
            entry["converters"] = [
                {"program": step.program, "path": path} for step, path in converters
            ]
            entry["ready"] = bool(converters) and all(path for _, path in converters)
            document.append(entry)
        json.dump(document, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return EXIT_OK

    for family in registry.families():
        converters = convert_module.available_converters(family, overrides)
        ready = bool(converters) and all(path for _, path in converters)
        print(
            "{key:10s} {name:32s} {toolchain:18s} [{status}]".format(
                key=family.key,
                name=family.name,
                toolchain=family.toolchain,
                status=family.status,
            )
        )
        print("           extensions: {}".format(", ".join(family.extensions) or "-"))
        print(
            "           gate library: {}".format(
                family.gate_library or "none in this fork (pass --gate-library)"
            )
        )
        if not converters:
            print("           converter: none -- {}".format(family.notes or "not available"))
        else:
            for step, path in converters:
                print(
                    "           converter: {:14s} {}".format(
                        step.program, path or "NOT INSTALLED ({})".format(step.install_hint())
                    )
                )
            print("           ready: {}".format("yes" if ready else "no"))
        print("")
    return EXIT_OK


def cmd_detect(args):
    try:
        detection = detect_family(args.bitstream, args.family)
    except (DetectionError, registry.UnknownFamilyError) as error:
        raise CliError(str(error))
    if args.json:
        json.dump(detection.to_json(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return EXIT_OK
    print("family:       {} ({})".format(detection.family.key, detection.family.name))
    print("vendor:       {}".format(detection.family.vendor))
    print("toolchain:    {}".format(detection.family.toolchain))
    print("detected by:  {} -- {}".format(detection.method, detection.evidence))
    print("gate library: {}".format(detection.family.gate_library or "none in this fork"))
    print("converter:    {}".format(
        " -> ".join(step.program for step in detection.family.steps) or "none available"
    ))
    if detection.family.notes:
        print("notes:        {}".format(detection.family.notes))
    return EXIT_OK


def _convert(args, log):
    overrides = convert_module.parse_converter_overrides(args.converter)
    try:
        result = convert_module.convert(
            args.bitstream,
            output=args.output,
            family=args.family,
            overrides=overrides,
            gate_library=args.gate_library,
            keep_intermediates=getattr(args, "keep_intermediates", False),
            timeout=args.timeout,
            log=log,
        )
    except (ConversionError, DetectionError, registry.UnknownFamilyError) as error:
        raise CliError(str(error))
    return result


def cmd_convert(args):
    log = _log(args.quiet)
    result = _convert(args, log)
    manifest_path = args.manifest or (os.path.splitext(result.netlist)[0] + ".bitstream.json")
    convert_module.write_manifest(result.manifest, manifest_path)
    print(result.netlist)
    print(manifest_path)
    log(
        "netlist written; load it with: python tools/hal_viz netlist_graph {}{}".format(
            result.netlist,
            " --gate-library {}".format(result.gate_library) if result.gate_library else "",
        )
    )
    return EXIT_OK


def cmd_load(args):
    log = _log(args.quiet)
    result = _convert(args, log)
    if not result.gate_library:
        raise CliError(
            "no gate library for {}: the netlist was written to {} but HAL cannot read it "
            "without one. Pass --gate-library <file.hgl>.".format(
                result.detection.family.name, result.netlist
            )
        )

    from hal_viz.halenv import HalUnavailable, NetlistLoadError, import_hal_py, load_netlist

    try:
        # halenv.load_netlist loads HAL's plugin set itself: the netlist parsers are plugins, and
        # a parser that is not registered makes every load return None.
        hal_py = import_hal_py(args.hal_lib or ())
        netlist = load_netlist(hal_py, result.netlist, result.gate_library)
    except (HalUnavailable, NetlistLoadError) as error:
        raise CliError(str(error))

    gate_types = {}
    for gate in netlist.get_gates():
        name = gate.get_type().get_name()
        gate_types[name] = gate_types.get(name, 0) + 1

    summary = {
        "bitstream": result.manifest["bitstream"],
        "family": result.detection.family.key,
        "netlist": result.netlist,
        "gate_library": result.gate_library,
        "design_name": netlist.get_design_name(),
        "gate_count": len(netlist.get_gates()),
        "net_count": len(netlist.get_nets()),
        "gate_types": gate_types,
        "conversion": result.manifest,
    }

    if args.project_dir:
        summary["project_dir"] = _write_project(hal_py, netlist, args.project_dir, log)

    manifest_path = args.manifest or (os.path.splitext(result.netlist)[0] + ".bitstream.json")
    convert_module.write_manifest(result.manifest, manifest_path)

    if args.output_summary:
        with open(args.output_summary, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, sort_keys=True)
            handle.write("\n")
        print(args.output_summary)
    else:
        json.dump(summary, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")

    if summary["gate_count"] == 0:
        log("the converted netlist contains no gate")
        return EXIT_RESULT
    return EXIT_OK


def _write_project(hal_py, netlist, project_dir, log):
    project_dir = os.path.abspath(os.path.expanduser(project_dir))
    if "." in os.path.basename(project_dir):
        raise CliError(
            "a HAL project directory name must not contain a dot (HAL strips what it takes for "
            "an extension): {}".format(project_dir)
        )
    if os.path.exists(project_dir):
        raise CliError(
            "project directory already exists: {}. HAL refuses to write into an existing "
            "one.".format(project_dir)
        )
    manager = hal_py.ProjectManager.instance()
    if not manager.create_project_directory(project_dir):
        raise CliError("HAL could not create the project directory {}".format(project_dir))
    if not manager.serialize_project(netlist):
        raise CliError("HAL could not serialize the netlist into {}".format(project_dir))
    log("wrote HAL project {}".format(project_dir))
    return project_dir


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def _add_conversion_arguments(parser):
    parser.add_argument("bitstream", help="the bitstream file")
    parser.add_argument(
        "--family",
        help="skip detection and use this family (see 'families'); a mismatch with the detected "
        "family is reported, not hidden",
    )
    parser.add_argument(
        "--converter",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="use PATH for the converter NAME instead of looking it up on $PATH (repeatable)",
    )
    parser.add_argument(
        "--gate-library",
        metavar="FILE",
        help="gate library for the produced netlist; defaults to the family's library",
    )
    parser.add_argument(
        "--timeout", type=int, default=900, help="per-converter timeout in seconds (default: 900)"
    )
    parser.add_argument("--manifest", metavar="FILE", help="where the provenance manifest goes")
    parser.add_argument("-q", "--quiet", action="store_true")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_bitstream",
        description="Detect a bitstream's family, convert it to a netlist with the open-source "
        "converters, and hand the result to HAL.",
        epilog="A converter that is not installed is an error naming the program and the project "
        "that ships it -- hal_bitstream never invents a netlist it could not produce.",
    )
    parser.add_argument("--version", action="version", version="hal_bitstream " + __version__)
    subparsers = parser.add_subparsers(dest="command")

    families = subparsers.add_parser(
        "families", help="list the known families and which converters are installed"
    )
    families.add_argument("--json", action="store_true")
    families.add_argument(
        "--converter", action="append", default=[], metavar="NAME=PATH",
        help="also consider this converter path (repeatable)",
    )
    families.set_defaults(handler=cmd_families)

    detect = subparsers.add_parser("detect", help="report which family a file belongs to")
    detect.add_argument("bitstream")
    detect.add_argument("--family", help="check this family instead of detecting one")
    detect.add_argument("--json", action="store_true")
    detect.set_defaults(handler=cmd_detect)

    convert = subparsers.add_parser("convert", help="convert a bitstream into a Verilog netlist")
    _add_conversion_arguments(convert)
    convert.add_argument("-o", "--output", metavar="FILE", help="netlist path (default: <input>.v)")
    convert.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="keep the converter chain's intermediate files next to the output",
    )
    convert.set_defaults(handler=cmd_convert)

    load = subparsers.add_parser(
        "load", help="convert a bitstream and load the netlist into HAL"
    )
    _add_conversion_arguments(load)
    load.add_argument("-o", "--output", metavar="FILE", help="netlist path (default: <input>.v)")
    load.add_argument(
        "--keep-intermediates", action="store_true", help="keep intermediate converter files"
    )
    load.add_argument(
        "--output-summary", metavar="FILE", help="write the netlist summary here (default: stdout)"
    )
    load.add_argument(
        "--project-dir", metavar="DIR", help="also save the netlist as a HAL project directory"
    )
    load.add_argument(
        "--hal-lib",
        action="append",
        default=[],
        metavar="DIR",
        help="directory containing hal_py (repeatable); $HAL_PY_PATH works too",
    )
    load.set_defaults(handler=cmd_load)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return EXIT_ERROR
    try:
        return args.handler(args)
    except CliError as error:
        print("[hal_bitstream] error: {}".format(error), file=sys.stderr)
        return EXIT_ERROR
