"""``python tools/hal_explain ...`` -- the command line.

::

    python tools/hal_explain collect  <netlist> --gate-library L -o build/explain
    python tools/hal_explain compose  --inventory build/explain/inventory.json \\
        --findings build/explain/findings-dataflow.json \\
        --findings build/explain/findings-module-identification.json \\
        --findings build/hal_fsm/controller/findings.json \\
        -o build/explain/blocks.json --dot build/explain/blocks.dot \\
        --report build/explain/report.md
    python tools/hal_explain diagram  build/explain/blocks.json -o blocks.dot
    python tools/hal_explain report   build/explain/blocks.json -o report.md
    python tools/hal_explain validate build/explain/blocks.json
    python tools/hal_explain prose    build/explain/blocks.json -o prose-input.json

Only ``collect`` needs a HAL build.  ``compose``, ``diagram``, ``report``,
``validate`` and ``prose`` run on a plain interpreter, which is what makes the
whole composition testable without one -- and what lets a reviewer re-derive the
report from the same JSON.

Exit codes follow the convention the rest of this fork uses:

===== ==============================================================
    0 the command ran and nothing crossed a ``--fail-*`` threshold
    1 it ran and something did (or the HAL run failed)
    2 it could not run: bad input, no HAL, unreadable document --
      **not** a statement about the design
===== ==============================================================
"""

import argparse
import json
import os
import sys

if __package__ in (None, ""):  # pragma: no cover - direct execution
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_explain import REQUEST_ENV, __version__
from hal_explain import compose as compose_module
from hal_explain import diagram as diagram_module
from hal_explain import inventory as inventory_module
from hal_explain import report as report_module
from hal_explain import serialize, validate as validate_module
from hal_explain.adapters.common import AdapterError

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TOOLS_DIR)
RUN_IN_HAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_in_hal.py")
REQUEST_VERSION = "1.0.0"


class CliError(RuntimeError):
    """A setup problem: exit 2, never exit 1."""


class Reporter(object):
    def __init__(self, quiet=False, stream=None):
        self.quiet = quiet
        self.stream = stream if stream is not None else sys.stderr

    def info(self, message):
        if not self.quiet:
            self.stream.write("[hal_explain] {}\n".format(message))

    def warn(self, message):
        self.stream.write("[hal_explain] warning: {}\n".format(message))

    def error(self, message):
        self.stream.write("[hal_explain] error: {}\n".format(message))


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _load_inventory(path):
    try:
        payload = serialize.read_json(path)
    except (OSError, ValueError) as exc:
        raise CliError("could not read the inventory {}: {}".format(path, exc))
    try:
        return inventory_module.from_json(payload)
    except inventory_module.InventoryError as exc:
        raise CliError(str(exc))


def _load_model(path):
    try:
        document = serialize.read_document(path)
    except (OSError, ValueError) as exc:
        raise CliError("could not read the block model {}: {}".format(path, exc))
    try:
        validate_module.validate_document(document)
    except validate_module.BlockModelValidationError as exc:
        raise CliError("{} is not a valid recovered-block document:\n{}".format(path, exc))
    return document


def _inventory_from_netlist(args, reporter):
    """Build an inventory from a netlist, through HAL or the fixture reader."""
    from hal_cdc.fixture_netlist import FixtureError, load_fixture
    from hal_cdc.netlist_view import from_hal_netlist

    netlist_path = os.path.abspath(os.path.expanduser(args.netlist))
    if not os.path.exists(netlist_path):
        raise CliError("netlist path does not exist: {}".format(netlist_path))

    if getattr(args, "fixture_reader", False):
        if not args.gate_library:
            raise CliError("--fixture-reader needs --gate-library")
        try:
            view = load_fixture(netlist_path, os.path.abspath(args.gate_library))
        except (FixtureError, OSError, ValueError) as exc:
            raise CliError("the fixture reader could not read {}: {}".format(netlist_path, exc))
        reporter.warn(
            "using hal_explain's fixture reader, not HAL's parser; only the fixtures "
            "in tools/*/fixtures are supported this way"
        )
        return view, netlist_path

    # halenv.load_netlist loads HAL's plugin set itself (the gate library and netlist parsers are
    # plugins), so this path does not have to -- and cannot forget to.
    from hal_viz.halenv import HalUnavailable, NetlistLoadError, import_hal_py, load_netlist

    try:
        hal_py = import_hal_py(getattr(args, "hal_lib", ()) or ())
        netlist = load_netlist(hal_py, netlist_path, args.gate_library)
    except (HalUnavailable, NetlistLoadError) as exc:
        raise CliError(str(exc))
    return from_hal_netlist(netlist), netlist_path


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_inventory(args, reporter):
    view, netlist_path = _inventory_from_netlist(args, reporter)
    inventory = inventory_module.from_netlist_view(
        view, artifact_id=args.artifact_id, path=netlist_path
    )
    output = args.output or "inventory.json"
    serialize.write_json(inventory.to_json(), output)
    reporter.info(
        "wrote {} ({} gates, {} nets)".format(output, len(inventory.gates), len(inventory.nets))
    )
    return EXIT_OK


def cmd_collect(args, reporter):
    from hal_runner.execute import ProcessExecutor, tail
    from hal_runner.runner import RunnerError, resolve_hal_binary

    netlist_path = os.path.abspath(os.path.expanduser(args.netlist))
    if not os.path.exists(netlist_path):
        raise CliError("netlist path does not exist: {}".format(netlist_path))

    output_dir = os.path.abspath(args.output_dir or os.path.join("build", "hal_explain"))
    os.makedirs(output_dir, exist_ok=True)

    try:
        hal_binary = resolve_hal_binary(args.hal_binary)
    except RunnerError as exc:
        raise CliError(str(exc))

    request = {
        "request_version": REQUEST_VERSION,
        "analysis": "hal_explain.collect",
        "tools_path": TOOLS_DIR,
        "netlist": netlist_path,
        "gate_library": os.path.abspath(args.gate_library) if args.gate_library else None,
        "output_dir": output_dir,
        "result_file": "result.json",
        "artifact_id": args.artifact_id,
        "dataflow": not args.no_dataflow,
        "module_identification": not args.no_module_identification,
        "min_group_size": args.min_group_size,
        "max_control_signals": args.max_control_signals,
        "command": ["python", "tools/hal_explain", "collect", args.netlist],
    }
    request_path = os.path.join(output_dir, "request.json")
    serialize.write_json(request, request_path)

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

    result_path = os.path.join(output_dir, "result.json")
    if execution.timed_out:
        reporter.error(
            "hal was killed after {:.1f}s at the {}s limit; nothing was "
            "collected".format(execution.duration_s, execution.timeout_s)
        )
        return EXIT_FINDINGS
    if execution.exit_code not in (0, 1):
        reporter.error(
            "hal exited with {}; see {}".format(execution.exit_code, execution.stderr_path)
        )
        reporter.error(tail(execution.stderr_path, 1200))
        return EXIT_FINDINGS
    if not os.path.isfile(result_path):
        reporter.error(
            "hal exited {} but wrote no result record; a step that says nothing has "
            "collected nothing".format(execution.exit_code)
        )
        return EXIT_FINDINGS

    result = serialize.read_json(result_path)
    for step in result.get("steps", []):
        line = "{}: {}".format(step["step"], step["status"])
        if step["status"] in ("ok", "skipped"):
            reporter.info(line)
        else:
            reporter.warn("{} ({})".format(line, (step.get("detail") or "").splitlines()[0:1]))
    reporter.info(
        "wrote {} ({} findings document(s))".format(
            output_dir, len(result.get("artifacts", {}).get("findings", []))
        )
    )
    if args.print_summary:
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return EXIT_OK if result.get("status") == "ok" else EXIT_FINDINGS


def cmd_compose(args, reporter):
    inventory = _load_inventory(args.inventory)
    try:
        sources = compose_module.load_sources(
            args.findings, args.adapter, validate=not args.no_validate_findings
        )
    except (AdapterError, compose_module.ComposeError) as exc:
        raise CliError(str(exc))

    options = compose_module.ComposeOptions(
        max_net_loads=args.max_net_loads,
        max_edge_nets=args.max_edge_nets,
    )
    document = compose_module.compose(
        inventory,
        sources,
        options=options,
        generated_at=args.generated_at,
    )
    try:
        validate_module.validate_document(document)
    except validate_module.BlockModelValidationError as exc:
        reporter.error(str(exc))
        return EXIT_ERROR

    output = args.output or "blocks.json"
    serialize.write_document(document, output)
    reporter.info(
        "wrote {} ({} blocks, {} unknown regions, {}/{} gates classified)".format(
            output,
            len(document["blocks"]),
            len(document["unknown_regions"]),
            document["coverage"]["gates_in_blocks"],
            document["coverage"]["gates_total"],
        )
    )

    if args.dot:
        path, nodes, edges = diagram_module.write_block_diagram(document, args.dot)
        reporter.info("wrote {} ({} nodes, {} edges)".format(path, nodes, edges))
    if args.report:
        diagram_hint = os.path.basename(args.dot) if args.dot else None
        report_module.write_report(document, args.report, diagram_path=diagram_hint)
        reporter.info("wrote {}".format(args.report))
    if args.print_summary:
        _print_summary(document)

    return _threshold_exit(document, args, reporter)


def _print_summary(document):
    for block in document["blocks"]:
        sys.stdout.write(
            "{:28} {:>12}  {}\n".format(
                block["block_id"][:28], block["confidence"], block["label"]
            )
        )
    for region in document["unknown_regions"]:
        sys.stdout.write(
            "{:28} {:>12}  {}\n".format(region["region_id"][:28], "unclassified", region["label"])
        )


def _threshold_exit(document, args, reporter):
    coverage = document["coverage"]
    status = EXIT_OK
    if args.min_classified is not None:
        fraction = coverage.get("classified_fraction", 0.0)
        if fraction < args.min_classified:
            reporter.error(
                "only {:.1%} of the gates are in a block, below the --min-classified "
                "threshold of {:.1%}".format(fraction, args.min_classified)
            )
            status = EXIT_FINDINGS
    if args.require_verified and not any(
        block["confidence"] == "verified" for block in document["blocks"]
    ):
        reporter.error("--require-verified: this model contains no verified block")
        status = EXIT_FINDINGS
    if args.strict:
        unresolved = sum(
            len(source.get("unresolved_gates") or []) for source in document["sources"]
        )
        if unresolved:
            reporter.error(
                "--strict: {} gate reference(s) could not be resolved against the "
                "inventory".format(unresolved)
            )
            status = EXIT_FINDINGS
    return status


def cmd_diagram(args, reporter):
    document = _load_model(args.model)
    output = args.output or "blocks.dot"
    path, nodes, edges = diagram_module.write_block_diagram(
        document, output, max_gate_names=args.max_gate_names, legend=not args.no_legend
    )
    reporter.info("wrote {} ({} nodes, {} edges)".format(path, nodes, edges))
    return EXIT_OK


def cmd_report(args, reporter):
    document = _load_model(args.model)
    options = report_module.ReportOptions(
        max_gate_names=args.max_gate_names,
        include_evidence_index=not args.no_evidence_index,
    )
    text = report_module.build_report(document, options=options, diagram_path=args.diagram)
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        reporter.info("wrote {}".format(args.output))
    else:
        sys.stdout.write(text)
    return EXIT_OK


def cmd_validate(args, reporter):
    failures = 0
    for path in args.models:
        try:
            document = serialize.read_document(path)
        except (OSError, ValueError) as exc:
            reporter.error("{}: {}".format(path, exc))
            failures += 1
            continue
        errors = validate_module.collect_errors(document)
        if errors:
            reporter.error("{}: {} problem(s)".format(path, len(errors)))
            for error in errors:
                reporter.error("  - {}".format(error))
            failures += 1
        else:
            reporter.info("{}: ok".format(path))
    return EXIT_FINDINGS if failures else EXIT_OK


def cmd_prose(args, reporter):
    """Emit the evidence bundle an optional LLM step would consume.

    This command does **not** call a model, and nothing in the pipeline depends
    on it.  It exists so that "let a model paraphrase this" stays a strictly
    downstream, optional step over structured evidence -- the core extraction,
    the diagram and the report never involve one.
    """
    document = _load_model(args.model)
    bundle = {
        "prose_input_version": "1.0.0",
        "instructions": (
            "Paraphrase ONLY the claims below. Do not add functionality that is not "
            "claimed. Preserve every confidence label: a 'verified' claim may be "
            "stated as fact under its assumptions; a 'heuristic' claim must be "
            "described as a candidate; an 'unknown' claim must be described as "
            "inconclusive; unknown regions must be mentioned as unexplained."
        ),
        "design": document.get("design"),
        "coverage": document.get("coverage"),
        "blocks": [
            {
                "block_id": block["block_id"],
                "kind": block["kind"],
                "label": block["label"],
                "confidence": block["confidence"],
                "gate_names": [ref.get("name") for ref in block.get("gates", [])],
                "claims": [
                    {
                        "text": claim["text"],
                        "confidence": claim["confidence"],
                        "status": claim["status"],
                        "evidence": [
                            "{}#{}".format(ref["source_id"], ref["finding_id"])
                            for ref in claim.get("evidence", [])
                        ],
                    }
                    for claim in block.get("claims", [])
                ],
            }
            for block in document.get("blocks", [])
        ],
        "unknown_regions": [
            {
                "region_id": region["region_id"],
                "gate_names": [ref.get("name") for ref in region.get("gates", [])],
                "reason": region["reason"],
            }
            for region in document.get("unknown_regions", [])
        ],
        "notes": document.get("notes") or [],
    }
    text = json.dumps(bundle, indent=2, sort_keys=True) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        reporter.info("wrote {}".format(args.output))
        reporter.info(
            "no model was called: this bundle is the input an optional prose step "
            "would take, and the report you already have needs none"
        )
    else:
        sys.stdout.write(text)
    return EXIT_OK


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_explain",
        description="Compose dataflow, module identification and FSM results into an "
        "evidence-linked recovered-block model, a block diagram and a report.",
    )
    parser.add_argument("--version", action="version", version="hal_explain " + __version__)
    parser.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    subparsers = parser.add_subparsers(dest="command")

    inventory = subparsers.add_parser(
        "inventory", help="write the netlist snapshot the composition resolves against"
    )
    inventory.add_argument("netlist", help="a HAL project directory, a .hal file, or a netlist")
    inventory.add_argument("--gate-library", metavar="FILE", help="gate library for .v/.vhd")
    inventory.add_argument("--hal-lib", action="append", default=[], metavar="DIR")
    inventory.add_argument("--artifact-id", default="netlist")
    inventory.add_argument(
        "--fixture-reader",
        action="store_true",
        help="parse with hal_cdc's dependency-free fixture reader instead of HAL; only "
        "for the fixtures in tools/*/fixtures",
    )
    inventory.add_argument("-o", "--output", metavar="FILE")

    collect = subparsers.add_parser(
        "collect", help="run dataflow and module identification in HAL and record both"
    )
    collect.add_argument("netlist")
    collect.add_argument("--gate-library", metavar="FILE")
    collect.add_argument("--artifact-id", default="netlist")
    collect.add_argument("-o", "--output-dir", metavar="DIR")
    collect.add_argument("--hal-binary", metavar="PATH")
    collect.add_argument("--hal-args", nargs=argparse.REMAINDER, default=[])
    collect.add_argument("--timeout", type=float, metavar="SECONDS")
    collect.add_argument("--memory-mb", type=int, metavar="MB")
    collect.add_argument("--no-dataflow", action="store_true")
    collect.add_argument("--no-module-identification", action="store_true")
    collect.add_argument("--min-group-size", type=int)
    collect.add_argument("--max-control-signals", type=int)
    collect.add_argument("--print-summary", action="store_true")

    compose = subparsers.add_parser(
        "compose", help="compose findings documents into a recovered-block model"
    )
    compose.add_argument("--inventory", required=True, metavar="FILE")
    compose.add_argument(
        "--findings",
        action="append",
        default=[],
        required=True,
        metavar="FILE",
        help="a findings document (repeatable)",
    )
    compose.add_argument(
        "--adapter",
        action="append",
        default=None,
        metavar="NAME",
        help="force the adapter for the corresponding --findings (dataflow, "
        "module_identification, fsm); pass one per document or none at all",
    )
    compose.add_argument("-o", "--output", metavar="FILE")
    compose.add_argument("--dot", metavar="FILE", help="also write the block diagram here")
    compose.add_argument("--report", metavar="FILE", help="also write the Markdown report here")
    compose.add_argument("--generated-at", metavar="RFC3339")
    compose.add_argument("--max-net-loads", type=int, default=8)
    compose.add_argument("--max-edge-nets", type=int, default=8)
    compose.add_argument(
        "--no-validate-findings",
        action="store_true",
        help="skip schema validation of the input documents (not recommended)",
    )
    compose.add_argument(
        "--min-classified",
        type=float,
        metavar="FRACTION",
        help="exit 1 when a smaller fraction of the gates ends up in a block",
    )
    compose.add_argument("--require-verified", action="store_true")
    compose.add_argument(
        "--strict", action="store_true", help="exit 1 on any unresolved gate reference"
    )
    compose.add_argument("--print-summary", action="store_true")

    diagram = subparsers.add_parser("diagram", help="write the block diagram of a model")
    diagram.add_argument("model")
    diagram.add_argument("-o", "--output", metavar="FILE")
    diagram.add_argument("--max-gate-names", type=int, default=6)
    diagram.add_argument("--no-legend", action="store_true")

    report = subparsers.add_parser("report", help="write the templated report of a model")
    report.add_argument("model")
    report.add_argument("-o", "--output", metavar="FILE")
    report.add_argument("--diagram", metavar="PATH", help="path to mention in the report")
    report.add_argument("--max-gate-names", type=int, default=24)
    report.add_argument("--no-evidence-index", action="store_true")

    validate = subparsers.add_parser("validate", help="validate recovered-block documents")
    validate.add_argument("models", nargs="+")

    prose = subparsers.add_parser(
        "prose", help="emit the evidence bundle an optional LLM step would consume"
    )
    prose.add_argument("model")
    prose.add_argument("-o", "--output", metavar="FILE")

    return parser


_COMMANDS = {
    "inventory": cmd_inventory,
    "collect": cmd_collect,
    "compose": cmd_compose,
    "diagram": cmd_diagram,
    "report": cmd_report,
    "validate": cmd_validate,
    "prose": cmd_prose,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_ERROR
    reporter = Reporter(quiet=args.quiet)
    try:
        return _COMMANDS[args.command](args, reporter)
    except CliError as exc:
        reporter.error(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        reporter.error("interrupted")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
