"""Command line for the clock/reset-domain audit.

::

    python tools/hal_cdc discover <netlist> --gate-library <lib>
    python tools/hal_cdc audit    <netlist> --gate-library <lib> \\
        --declarations clocks.json -o findings.json --dot domains.dot

Exit codes follow the convention the rest of this fork uses:

===== ==============================================================
    0 the audit ran and nothing exceeded ``--fail-on``
    1 the audit ran and something did
    2 the audit could not run (bad declarations, no HAL, unreadable
      netlist) -- **not** a statement about the design
===== ==============================================================

That separation matters in CI: exit 2 must never be read as "the design is
clean", and exit 1 must never be read as "the tool broke".
"""

import argparse
import json
import os
import sys
import time

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from hal_findings import serialize, validate  # noqa: E402

from . import __version__, audit as audit_module, clock_tree, declarations, report  # noqa: E402
from .domains import Limits  # noqa: E402
from .netlist_view import from_hal_netlist  # noqa: E402
from .patterns import CLASS_UNSYNCHRONIZED, CLASS_UNSYNCHRONIZED_CONTROL  # noqa: E402

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

_FAIL_ON = ("never", "unsynchronized", "any-alarm")


class CliError(RuntimeError):
    """A setup problem: exit 2, never exit 1."""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_cdc",
        description="Structural clock-domain and reset-domain audit for HAL netlists. "
        "Screening only: no timing, no metastability sign-off.",
    )
    parser.add_argument("--version", action="version", version="hal_cdc " + __version__)
    subparsers = parser.add_subparsers(dest="command")

    for name, help_text in (
        ("discover", "print a declaration skeleton from the nets that drive clock and reset pins"),
        ("audit", "run the audit and write a findings document"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("netlist", help="a HAL project directory, a .hal file, or a netlist file")
        sub.add_argument(
            "--gate-library",
            metavar="FILE",
            help="gate library for netlist formats that do not carry one (.v, .vhd)",
        )
        sub.add_argument(
            "--hal-lib",
            action="append",
            default=[],
            metavar="DIR",
            help="directory containing hal_py (repeatable); $HAL_PY_PATH works too",
        )
        sub.add_argument(
            "--fixture-reader",
            action="store_true",
            help="parse the netlist with hal_cdc's own structural Verilog reader instead "
            "of HAL. Only for the fixtures in tools/hal_cdc/fixtures; HAL's parser is "
            "the authority everywhere else.",
        )

    discover = subparsers.choices["discover"]
    discover.add_argument("-o", "--output", metavar="FILE", help="write the skeleton here")

    run = subparsers.choices["audit"]
    run.add_argument(
        "-d",
        "--declarations",
        metavar="FILE",
        required=True,
        help="the clock/reset declaration document (see hal_cdc/README.md)",
    )
    run.add_argument("-o", "--output", metavar="FILE", help="write the findings document here")
    run.add_argument("--dot", metavar="FILE", help="write a Graphviz domain graph here")
    run.add_argument(
        "--clock-tree-dot",
        metavar="FILE",
        help="ask the clock_tree_extractor plugin to export its recovered tree here",
    )
    run.add_argument(
        "--no-clock-tree",
        action="store_true",
        help="skip the clock_tree_extractor cross-check",
    )
    run.add_argument(
        "--fail-on",
        choices=_FAIL_ON,
        default="unsynchronized",
        help="exit 1 on: nothing (never), unsynchronised crossings (default), or any "
        "unsynchronised crossing or unsafe reset release (any-alarm)",
    )
    run.add_argument("-q", "--quiet", action="store_true", help="only print the exit summary")
    run.add_argument(
        "--max-clock-depth", type=int, default=32, help="clock-source search depth budget"
    )
    run.add_argument(
        "--max-clock-nodes", type=int, default=4096, help="clock-source search node budget"
    )
    run.add_argument(
        "--max-propagation-steps",
        type=int,
        default=200000,
        help="forward domain-propagation step budget",
    )
    return parser


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def _load_view(args):
    """Return ``(view, hal_netlist_or_None)``."""
    path = os.path.abspath(os.path.expanduser(args.netlist))
    if not os.path.exists(path):
        raise CliError("netlist path does not exist: {}".format(path))

    if args.fixture_reader:
        from .fixture_netlist import FixtureError, load_fixture

        if not args.gate_library:
            raise CliError("--fixture-reader needs --gate-library")
        try:
            return load_fixture(path, args.gate_library), None
        except (FixtureError, ValueError) as exc:
            raise CliError(str(exc))

    try:
        from hal_viz import halenv
    except ImportError as exc:  # pragma: no cover - tools/ is on sys.path above
        raise CliError("could not import hal_viz.halenv: {}".format(exc))

    try:
        hal_py = halenv.import_hal_py(args.hal_lib)
        halenv.load_all_plugins(hal_py)
        netlist = halenv.load_netlist(hal_py, path, args.gate_library)
    except (halenv.HalUnavailable, halenv.NetlistLoadError) as exc:
        raise CliError(str(exc))
    return from_hal_netlist(netlist), netlist


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _discover(args, stream):
    view, _netlist = _load_view(args)

    clock_nets, reset_nets = {}, {}
    for gate in view.sequential_gates():
        for pin in gate.type.input_pins():
            net_id = gate.fan_in.get(pin.name)
            if net_id is None or view.is_constant_net(net_id):
                continue
            net = view.net(net_id)
            if net is None:
                continue
            if pin.type == "clock":
                clock_nets.setdefault(net.name, 0)
                clock_nets[net.name] += 1
            elif pin.type in ("reset", "set"):
                reset_nets.setdefault(net.name, 0)
                reset_nets[net.name] += 1

    skeleton = {
        "version": declarations.DECLARATIONS_VERSION,
        "description": "skeleton generated by hal_cdc discover from {}; edit the names, "
        "delete what is not a real clock or reset, and add 'inputs' for primary inputs "
        "that are already synchronous to a clock".format(os.path.basename(args.netlist)),
        "clocks": [
            {"name": name, "net": name, "description": "drives {} clock pin(s)".format(count)}
            for name, count in sorted(clock_nets.items())
        ],
        "resets": [
            {
                "name": name,
                "net": name,
                "description": "drives {} reset/set pin(s); set 'synchronous_to' if this "
                "reset is already synchronous to a clock".format(count),
            }
            for name, count in sorted(reset_nets.items())
        ],
    }
    text = json.dumps(skeleton, indent=2, sort_keys=False) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        print(args.output, file=stream)
    else:
        stream.write(text)
    return EXIT_OK


def _audit(args, stream):
    try:
        declaration_document = declarations.load(args.declarations)
    except declarations.DeclarationError as exc:
        raise CliError(str(exc))

    view, netlist = _load_view(args)
    bound = declaration_document.bind(view)

    tree = None
    if netlist is not None and not args.no_clock_tree:
        tree = clock_tree.extract_clock_tree(netlist, dot_path=args.clock_tree_dot)

    limits = Limits(
        max_clock_nodes=args.max_clock_nodes,
        max_clock_depth=args.max_clock_depth,
        max_propagation_steps=args.max_propagation_steps,
    )

    started = time.time()
    result = audit_module.run_audit(view, bound, limits=limits, clock_tree=tree)
    duration = time.time() - started

    document = report.build_document(
        result,
        netlist_path=os.path.abspath(os.path.expanduser(args.netlist)),
        producer_command=[os.path.basename(sys.argv[0])] + sys.argv[1:],
        duration_s=round(duration, 4),
    )
    validate.validate_document(document)

    if args.output:
        serialize.write_document(document, args.output)
    elif not args.quiet:
        stream.write(serialize.dumps(document))

    if args.dot:
        report.build_domain_graph(result).write(args.dot)

    if not args.quiet:
        print("", file=stream)
        print(report.text_summary(result), file=stream)
        if args.output:
            print("findings: {}".format(args.output), file=stream)
        if args.dot:
            print("domain graph: {}".format(args.dot), file=stream)

    alarming = len(result.alarming_crossings)
    unsafe_resets = sum(1 for target in result.reset_targets if target.is_alarming)
    if args.fail_on == "never":
        return EXIT_OK
    if args.fail_on == "unsynchronized" and alarming:
        print(
            "FAIL: {} unsynchronised crossing path(s) ({} / {})".format(
                alarming, CLASS_UNSYNCHRONIZED, CLASS_UNSYNCHRONIZED_CONTROL
            ),
            file=stream,
        )
        return EXIT_FINDINGS
    if args.fail_on == "any-alarm" and (alarming or unsafe_resets):
        print(
            "FAIL: {} unsynchronised crossing path(s), {} unsafe reset release(s)".format(
                alarming, unsafe_resets
            ),
            file=stream,
        )
        return EXIT_FINDINGS
    return EXIT_OK


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    stream = sys.stdout

    if args.command is None:
        parser.print_help(stream)
        return EXIT_ERROR

    try:
        if args.command == "discover":
            return _discover(args, stream)
        if args.command == "audit":
            return _audit(args, stream)
    except CliError as exc:
        print("hal_cdc: {}".format(exc), file=sys.stderr)
        return EXIT_ERROR
    except validate.FindingsValidationError as exc:
        print(
            "hal_cdc produced a findings document that does not validate; this is a bug "
            "in hal_cdc:\n{}".format(exc),
            file=sys.stderr,
        )
        return EXIT_ERROR

    parser.print_help(stream)
    return EXIT_ERROR
