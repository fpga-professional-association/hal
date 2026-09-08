"""``python tools/hal_fsm ...`` -- the side of hal_fsm that runs outside HAL.

``analyze`` is the only subcommand that needs a HAL build, and it does not link
against it: it writes a request file and runs ``hal --python-script
run_in_hal.py`` as a subprocess, reusing ``hal_runner``'s executor so the
timeout escalates SIGTERM -> SIGKILL across the whole process group.  That
matters here more than anywhere else in the repository: ``solve_fsm`` is an SMT
loop in C++ with no cancellation point, so **the process boundary is the only
hard limit there is**.  The per-query timeout handed to the plugin is not one:
a machine with many states can honour it on every query and still run for
hours.

When the child is killed at that limit, this process writes the ``timeout``
findings document itself, because a run that vanished without a record is
exactly how a pipeline ends up believing an FSM was recovered.

The other subcommands need nothing but an interpreter: ``validate-config``,
``compare`` (a recovered transition table against a reference) and ``diagram``
(a table into Graphviz DOT).
"""

import argparse
import json
import os
import sys

if __package__ in (None, ""):  # pragma: no cover - direct execution
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_findings import model as findings_model
from hal_findings import serialize as findings_serialize
from hal_findings.adapters.common import utc_now
from hal_runner.execute import ProcessExecutor, tail
from hal_runner.runner import RunnerError, resolve_hal_binary

from . import REQUEST_ENV, __version__, config as config_module
from . import diagram as diagram_module
from . import reference as reference_module
from . import transitions as transitions_module

__all__ = ["main"]

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TOOLS_DIR)
RUN_IN_HAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_in_hal.py")
REQUEST_VERSION = "1.0.0"


class Reporter(object):
    def __init__(self, quiet=False, stream=None):
        self.quiet = quiet
        self.stream = stream if stream is not None else sys.stderr

    def info(self, message):
        if not self.quiet:
            self.stream.write("[hal_fsm] {}\n".format(message))

    def warn(self, message):
        self.stream.write("[hal_fsm] warning: {}\n".format(message))

    def error(self, message):
        self.stream.write("[hal_fsm] error: {}\n".format(message))


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


def _configuration_from_args(args):
    if args.config:
        configuration = config_module.load(args.config)
    else:
        configuration = config_module.Configuration()
    if args.state_registers:
        configuration.override.state_registers = list(args.state_registers)
    if args.reference:
        configuration.reference = os.path.abspath(args.reference)
    if args.targets:
        configuration.targets = [int(target) for target in args.targets]
    if args.solver:
        configuration.solver = args.solver
    if args.solve:
        configuration.solve = args.solve
    return configuration


def _timeout_document(args, configuration, execution, netlist_path):
    """The findings document for a run whose child was killed at the limit."""
    sha256 = None
    unhashed_reason = None
    if os.path.isfile(netlist_path):
        sha256 = findings_serialize.sha256_file(netlist_path)
    else:
        unhashed_reason = (
            "the input is a directory ({}); hash the archive it came from to pin "
            "it".format(os.path.basename(netlist_path))
        )
    artifact = findings_model.artifact(
        "netlist",
        kind="netlist",
        path=netlist_path,
        sha256=sha256,
        unhashed_reason=unhashed_reason,
    )
    finding = findings_model.finding(
        "fsm/run/timeout",
        "FSM recovery was killed at its wall-clock limit",
        findings_model.STATUS_TIMEOUT,
        findings_model.method(
            "solve_fsm",
            "symbolic",
            True,
            description="the analysis process was terminated before it produced a "
            "result; solve_fsm has no partial state transition graph to return",
        ),
        findings_model.scope(["netlist"], description="the whole netlist"),
        summary=(
            "hal ran for {:.1f}s and was killed at the {}s limit. No state machine was "
            "recovered and nothing is claimed about this design. The HAL log tail is "
            "attached as evidence.".format(execution.duration_s, execution.timeout_s)
        ),
        severity="medium",
        bounds_dict=findings_model.bounds(
            False, unroll_depth=0, description="nothing was explored to completion"
        ),
        limits_dict=findings_model.limits(
            timeout_s=execution.timeout_s,
            wall_time_s=round(execution.duration_s, 3),
            hit=True,
            description="wall-clock limit enforced by killing the hal process group",
        ),
        evidence_list=[
            findings_model.evidence(
                "log",
                description="tail of the killed run's stderr",
                inline=tail(execution.stderr_path, 2000),
            ),
            findings_model.evidence(
                "command", description="the command that was killed", command=execution.command
            ),
        ],
        tags=["fsm", "timeout"],
    )
    return findings_model.document(
        {"name": "hal_fsm", "version": __version__},
        [artifact],
        {
            "plugin": {"name": "solve_fsm", "version": "unknown"},
            "entry_point": "solve_fsm.solve_fsm",
            "configuration": configuration.to_json(),
        },
        [finding],
        generated_at=utc_now(),
        notes=[
            "this document was written by the hal_fsm CLI, not by the analysis: the "
            "analysis never got to write one"
        ],
    )


def cmd_analyze(args, reporter):
    try:
        configuration = _configuration_from_args(args)
    except (config_module.ConfigError, reference_module.ReferenceError) as exc:
        reporter.error(str(exc))
        return 2

    netlist_path = os.path.abspath(os.path.expanduser(args.netlist))
    if not os.path.exists(netlist_path):
        reporter.error("netlist path does not exist: {}".format(netlist_path))
        return 2

    output_dir = os.path.abspath(args.output_dir or os.path.join("build", "hal_fsm"))
    os.makedirs(output_dir, exist_ok=True)

    try:
        hal_binary = resolve_hal_binary(args.hal_binary)
    except RunnerError as exc:
        reporter.error(str(exc))
        return 2

    request = {
        "request_version": REQUEST_VERSION,
        "analysis": "solve_fsm.discover",
        "tools_path": TOOLS_DIR,
        "netlist": netlist_path,
        "gate_library": os.path.abspath(args.gate_library) if args.gate_library else None,
        "output_dir": output_dir,
        "findings_file": "findings.json",
        "result_file": "result.json",
        "artifact_id": args.artifact_id,
        "config": configuration.to_json(),
        "config_path": os.path.abspath(args.config) if args.config else None,
        "command": ["python", "tools/hal_fsm", "analyze", args.netlist],
    }
    # The resolved configuration travels as JSON and is re-parsed in HAL, so the
    # in-HAL side validates exactly what the outside side wrote rather than a
    # summary of it (Configuration.to_json round-trips through from_dict).
    request["config"] = {
        key: value for key, value in request["config"].items() if value is not None
    }

    request_path = os.path.join(output_dir, "request.json")
    with open(request_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(request, indent=2, sort_keys=True) + "\n")

    command = [hal_binary] + list(args.hal_args or []) + ["--python-script", RUN_IN_HAL]
    environment = dict(os.environ)
    environment[REQUEST_ENV] = request_path

    logs_dir = os.path.join(output_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    reporter.info("running {}".format(" ".join(command)))
    execution = ProcessExecutor().execute(
        command,
        cwd=REPO_ROOT,
        env=environment,
        timeout_s=args.timeout,
        memory_mb=args.memory_mb,
        stdout_path=os.path.join(logs_dir, "stdout.log"),
        stderr_path=os.path.join(logs_dir, "stderr.log"),
    )

    findings_path = os.path.join(output_dir, "findings.json")
    if execution.timed_out:
        reporter.error(
            "hal was killed after {:.1f}s at the {}s limit; writing a timeout "
            "record".format(execution.duration_s, execution.timeout_s)
        )
        document = _timeout_document(args, configuration, execution, netlist_path)
        findings_serialize.write_document(document, findings_path)
        reporter.info("wrote {}".format(findings_path))
        return 1

    if execution.exit_code != 0:
        reporter.error(
            "hal exited with {}; see {}".format(execution.exit_code, execution.stderr_path)
        )
        reporter.error(tail(execution.stderr_path, 1200))
        return 1

    if not os.path.isfile(findings_path):
        reporter.error(
            "hal exited 0 but wrote no findings document; a step that says nothing has "
            "proved nothing"
        )
        return 1

    document = findings_serialize.read_document(findings_path)
    counts = {}
    for finding in document.get("findings", []):
        counts[finding["status"]] = counts.get(finding["status"], 0) + 1
    reporter.info(
        "wrote {} ({})".format(
            findings_path,
            ", ".join("{} {}".format(count, status) for status, count in sorted(counts.items()))
            or "no findings",
        )
    )
    if args.print_summary:
        for finding in document.get("findings", []):
            sys.stdout.write(
                "{:26} {:>10}  {}\n".format(
                    finding["id"][:26], finding["status"], finding["title"]
                )
            )
    bad = counts.get("error", 0) + counts.get("counterexample", 0)
    return 1 if (args.strict and bad) else 0


# ---------------------------------------------------------------------------
# offline subcommands
# ---------------------------------------------------------------------------


def cmd_validate_config(args, reporter):
    failures = 0
    for path in args.paths:
        try:
            configuration = config_module.load(path)
        except config_module.ConfigError as exc:
            reporter.error("{}: {}".format(path, exc))
            failures += 1
            continue
        if configuration.reference:
            try:
                reference_module.load(configuration.reference)
            except reference_module.ReferenceError as exc:
                reporter.error("{}: {}".format(path, exc))
                failures += 1
                continue
        reporter.info("{}: ok".format(path))
        if args.show:
            sys.stdout.write(json.dumps(configuration.to_json(), indent=2, sort_keys=True) + "\n")
    return 1 if failures else 0


def _load_table(path):
    with open(str(path), "r", encoding="utf-8") as handle:
        document = json.load(handle)
    table = transitions_module.TransitionTable(
        document["bit_order"],
        initial_state=document.get("initial_state", 0),
        solver=document.get("solver", "smt"),
        complete=document.get("complete", True),
        signals=document.get("signals"),
    )
    for entry in document.get("transitions", []):
        table.add(
            transitions_module.Transition(
                entry["source"],
                entry["target"],
                condition=entry.get("condition", ""),
                variables=entry.get("variables", ()),
            )
        )
    return table


def cmd_compare(args, reporter):
    try:
        table = _load_table(args.table)
        reference = reference_module.load(args.reference)
    except (OSError, ValueError, reference_module.ReferenceError) as exc:
        reporter.error(str(exc))
        return 2

    machine = (
        reference.get(args.machine) if args.machine else reference.for_register(table.bit_order)
    )
    if machine is None:
        reporter.error(
            "the reference describes no machine with the state register {}; give "
            "--machine explicitly".format(table.bit_order)
        )
        return 2

    try:
        report = machine.compare(table, restrict_to_reachable=args.reachable_only)
    except ValueError as exc:
        reporter.error(str(exc))
        return 2

    if not reporter.quiet:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if report["matches"]:
        reporter.info(
            "recovered relation matches reference machine {!r} ({} transitions)".format(
                machine.id, report["expected"]
            )
        )
        return 0
    reporter.error(
        "recovered relation differs from reference machine {!r}: {} missing, {} "
        "unexpected".format(machine.id, len(report["missing"]), len(report["unexpected"]))
    )
    return 1


def cmd_diagram(args, reporter):
    try:
        table = _load_table(args.table)
    except (OSError, ValueError, KeyError) as exc:
        reporter.error("could not read the transition table: {}".format(exc))
        return 2
    output = args.output or os.path.splitext(args.table)[0] + ".dot"
    path, nodes, edges = diagram_module.write_state_diagram(
        table,
        output,
        title=args.title,
        base=args.base,
        max_states=args.max_states,
    )
    reporter.info("wrote {} ({} nodes, {} edges)".format(path, nodes, edges))
    return 0


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_fsm",
        description="Discover, solve and explain finite state machines in a netlist.",
    )
    parser.add_argument("--version", action="version", version="hal_fsm " + __version__)
    parser.add_argument("-q", "--quiet", action="store_true", help="only report problems")
    subparsers = parser.add_subparsers(dest="command")

    analyze = subparsers.add_parser(
        "analyze", help="propose, solve and explain the FSMs of a netlist (needs a HAL build)"
    )
    analyze.add_argument("netlist", help="HAL project directory, .hal file or HDL netlist")
    analyze.add_argument("--gate-library", help="gate library for an HDL netlist")
    analyze.add_argument("-o", "--output-dir", help="where to write findings and diagrams")
    analyze.add_argument("--config", help="hal_fsm configuration file (overrides and limits)")
    analyze.add_argument(
        "--state-registers",
        nargs="+",
        metavar="GATE",
        help="override the candidate search with these flip-flops (names or IDs)",
    )
    analyze.add_argument("--reference", help="ground-truth file to compare the result against")
    analyze.add_argument(
        "--targets", nargs="+", type=int, metavar="STATE", help="states to find a witness for"
    )
    analyze.add_argument("--solver", choices=("auto", "smt", "brute_force"))
    analyze.add_argument("--solve", choices=("best", "all", "none"))
    analyze.add_argument("--artifact-id", default="netlist")
    analyze.add_argument("--hal-binary", help="path to the hal executable")
    analyze.add_argument(
        "--hal-args", nargs="*", default=[], help="extra arguments for the hal binary"
    )
    analyze.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help="wall-clock limit in seconds; the hal process group is killed at it "
        "(default: 900)",
    )
    analyze.add_argument("--memory-mb", type=float, help="memory limit for the hal process")
    analyze.add_argument("--print-summary", action="store_true", help="list the findings")
    analyze.add_argument(
        "--strict", action="store_true", help="exit 1 on any error or counterexample finding"
    )
    analyze.set_defaults(handler=cmd_analyze)

    validate = subparsers.add_parser(
        "validate-config", help="check a configuration file without running anything"
    )
    validate.add_argument("paths", nargs="+")
    validate.add_argument("--show", action="store_true", help="print the resolved configuration")
    validate.set_defaults(handler=cmd_validate_config)

    compare = subparsers.add_parser(
        "compare", help="compare a recovered transition table against a reference"
    )
    compare.add_argument("table", help="transitions-*.json written by a run")
    compare.add_argument("reference", help="ground-truth file")
    compare.add_argument("--machine", help="reference machine id (default: match by register)")
    compare.add_argument(
        "--reachable-only",
        action="store_true",
        help="compare only the reference's reachable states (what an SMT run explores)",
    )
    compare.set_defaults(handler=cmd_compare)

    diagram = subparsers.add_parser("diagram", help="render a transition table as Graphviz DOT")
    diagram.add_argument("table", help="transitions-*.json written by a run")
    diagram.add_argument("-o", "--output")
    diagram.add_argument("--title")
    diagram.add_argument("--base", type=int, choices=(2, 10, 16), default=2)
    diagram.add_argument("--max-states", type=int, default=64)
    diagram.set_defaults(handler=cmd_diagram)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2
    reporter = Reporter(quiet=args.quiet)
    try:
        return args.handler(args, reporter)
    except KeyboardInterrupt:  # pragma: no cover
        reporter.error("interrupted")
        return 130
