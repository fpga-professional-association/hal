"""Command line interface: ``compare`` two builds, ``report`` on the result.

Exit codes are meaningful, because a CI job should be able to gate on them:

===  ============================================================
  0  the comparison ran and the verdict satisfies ``--fail-on``
  1  a handled failure (no ``hal_py``, a netlist would not load,
     a malformed correspondence, an invalid findings document)
  2  bad command line
  3  the comparison ran and the verdict does *not* satisfy
     ``--fail-on`` -- i.e. a behavioural difference (or, with
     ``--fail-on inconclusive``, anything short of a proof)
130  interrupted
===  ============================================================

Note the separation of 1 and 3: "the tool broke" and "the designs differ" are
different facts, and collapsing them is how an equivalence check ends up
reporting green for the wrong reason.
"""

import argparse
import json
import os
import sys
import time

from hal_findings import serialize as findings_serialize
from hal_findings import validate as findings_validate
from hal_findings import model as findings_model

from . import __version__, compare, correspondence, diagrams, findings, report

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_VERDICT = 3
EXIT_INTERRUPTED = 130

_FAIL_ON_CHOICES = ("difference", "inconclusive", "never")


class _Reporter(object):
    """Progress on stderr, produced files on stdout -- as in ``hal_viz``."""

    def __init__(self, quiet=False):
        self.quiet = quiet

    def info(self, message):
        if not self.quiet:
            sys.stderr.write("[semantic_diff] {}\n".format(message))
            sys.stderr.flush()

    def warn(self, message):
        sys.stderr.write("[semantic_diff] warning: {}\n".format(message))
        sys.stderr.flush()

    def produced(self, path):
        sys.stdout.write("{}\n".format(path))
        sys.stdout.flush()


class CliError(RuntimeError):
    """A handled failure; the message is printed without a traceback."""


def _artifact_id(label, fallback):
    safe = "".join(
        character if (character.isalnum() or character in "_.:/-") else "_"
        for character in str(label or "")
    ).lstrip("_.:/-")
    return (safe or fallback)[:100]


def _sha_artifact(path, artifact_id, description):
    return findings_model.artifact(
        artifact_id,
        kind="other",
        path=path,
        sha256=findings_serialize.sha256_file(path),
        size_bytes=os.path.getsize(path),
        description=description,
    )


def _safe_name(text):
    return "".join(
        character if character.isalnum() or character in "_.-" else "_" for character in str(text)
    )


def _write_function_dump(path, outcome):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("observation point: {}\n".format(outcome.point.label))
        handle.write("status: {}\n".format(outcome.status))
        handle.write(
            "cone signatures: A={} B={}\n\n".format(
                outcome.cone_a.signature if outcome.cone_a else "-",
                outcome.cone_b.signature if outcome.cone_b else "-",
            )
        )
        handle.write("build A cone function:\n  {}\n\n".format(outcome.function_text_a))
        handle.write("build B cone function:\n  {}\n".format(outcome.function_text_b))
        if outcome.witness:
            handle.write("\nwitness:\n")
            for entry in outcome.witness:
                handle.write("  {} = {}\n".format(entry["signal"], entry["value"]))
        if outcome.replay:
            handle.write(
                "\nreplay: A evaluates to {}, B evaluates to {} (confirmed: {})\n".format(
                    outcome.replay.get("value_a"),
                    outcome.replay.get("value_b"),
                    outcome.replay.get("confirmed"),
                )
            )
    return path


def _hal_viz_commands(outcome, netlist_paths, gate_libraries, out_dir):
    """The exact ``hal_viz`` invocations that widen the view around a change.

    The cone diagram this tool writes is scoped to the cone; when that is not
    enough, the next question is always "what else touches this gate", and
    ``hal_viz netlist_graph`` already answers it. Recording the command as
    evidence beats describing it in prose.
    """
    commands = []
    changed_a, changed_b = outcome.changed_gates()
    for gates, path, library, label in (
        (changed_a, netlist_paths[0], gate_libraries[0], "A"),
        (changed_b, netlist_paths[1], gate_libraries[1], "B"),
    ):
        if not gates:
            continue
        name = str(gates[0].get_name())
        command = [
            sys.executable,
            os.path.join("tools", "hal_viz"),
            "netlist_graph",
            path,
            "--gate",
            name,
            "--depth",
            "2",
            "--show-boundary",
            "-o",
            os.path.join(out_dir, "neighbourhood_{}_{}".format(label.lower(), _safe_name(name))),
        ]
        if library:
            command += ["--gate-library", library]
        commands.append(
            findings_model.evidence(
                "command",
                description=(
                    "widen the view: the depth-2 neighbourhood of the changed gate {!r} "
                    "in build {}".format(name, label)
                ),
                command=command,
            )
        )
    return commands


def _evidence_for(outcome, out_dir, reporter, render_format, netlist_paths, gate_libraries):
    """Write the localization artifacts for one outcome; return evidence dicts."""
    evidence = []
    dot_path = diagrams.write_cone_pair(outcome, out_dir)
    reporter.produced(dot_path)
    evidence.append(
        findings_model.evidence(
            "dot",
            description=(
                "the two combinational cones feeding {}, with the gates that occur in "
                "only one of them highlighted".format(outcome.point.label)
            ),
            path=os.path.basename(dot_path),
            sha256=findings_serialize.sha256_file(dot_path),
            media_type="text/vnd.graphviz",
        )
    )
    if render_format != "none":
        rendered = diagrams.render_if_possible(dot_path, fmt=render_format)
        if rendered:
            reporter.produced(rendered)
            evidence.append(
                findings_model.evidence(
                    "file",
                    description="rendered changed-cone diagram",
                    path=os.path.basename(rendered),
                    sha256=findings_serialize.sha256_file(rendered),
                    media_type="image/svg+xml" if render_format == "svg" else None,
                )
            )
        else:
            reporter.info(
                "Graphviz 'dot' is not available; {} was written but not "
                "rendered".format(os.path.basename(dot_path))
            )

    if outcome.function_text_a or outcome.function_text_b:
        text_path = os.path.join(
            out_dir, "functions_{}.txt".format(_safe_name(outcome.point.key))
        )
        _write_function_dump(text_path, outcome)
        reporter.produced(text_path)
        evidence.append(
            findings_model.evidence(
                "file",
                description=(
                    "the two cone functions over the shared boundary variables, plus the "
                    "witness and its replay"
                ),
                path=os.path.basename(text_path),
                sha256=findings_serialize.sha256_file(text_path),
                media_type="text/plain",
            )
        )

    evidence.extend(_hal_viz_commands(outcome, netlist_paths, gate_libraries, out_dir))
    return evidence


def cmd_compare(args, reporter):
    from . import halbridge

    started = time.time()

    if not args.correspondence and not args.auto_correspondence:
        raise CliError(
            "a correspondence is required: pass --correspondence <file.json>, or "
            "--auto-correspondence to use the identity mapping over shared names "
            "(which is then recorded as an explicit assumption)."
        )

    if args.correspondence:
        mapping = correspondence.load(args.correspondence)
        reporter.info("correspondence: {}".format(args.correspondence))
    else:
        mapping = correspondence.identity(
            description="identity mapping over shared names (--auto-correspondence)",
            labels=("build_a", "build_b"),
        )
        reporter.warn(
            "no correspondence file given; using the identity mapping over shared "
            "names. Unmapped registers will be reported as unsupported."
        )

    out_dir = os.path.abspath(os.path.expanduser(args.output))
    os.makedirs(out_dir, exist_ok=True)

    try:
        hal_py = halbridge.import_hal_py(args.hal_lib)
        halbridge.load_plugins(hal_py)
        reporter.info("loading build A: {}".format(args.netlist_a))
        netlist_a = halbridge.load_netlist(
            hal_py, args.netlist_a, args.gate_library_a or args.gate_library
        )
        reporter.info("loading build B: {}".format(args.netlist_b))
        netlist_b = halbridge.load_netlist(
            hal_py, args.netlist_b, args.gate_library_b or args.gate_library
        )
    except halbridge.HalUnavailable as exc:
        raise CliError(str(exc))

    points, problems, coverage = correspondence.build_observation_points(
        netlist_a, netlist_b, mapping
    )
    reporter.info(
        "{} observation point(s), {} correspondence gap(s)".format(len(points), len(problems))
    )

    options = compare.Options(
        solver_timeout_s=args.solver_timeout,
        max_cone_gates=args.max_cone_gates,
        structural_fast_path=args.structural_fast_path,
        witness=not args.no_witness,
    )

    try:
        engine = halbridge.SmtEngine(hal_py)
    except halbridge.NoSolverAvailable as exc:
        raise CliError(str(exc))
    reporter.info("solver backend: {}".format(engine.solver_label))

    z3_utils = None
    if args.cross_check:
        z3_utils = halbridge.import_z3_utils()
        if z3_utils is None:
            reporter.info(
                "the z3_utils plugin is not available; the cross-check is skipped "
                "(this does not change any verdict)"
            )
        elif not mapping.is_identity:
            reporter.info(
                "the correspondence renames boundary points, which z3_utils cannot "
                "express; the cross-check is skipped"
            )
            z3_utils = None

    def progress(index, total, point):
        reporter.info("[{}/{}] {}".format(index + 1, total, point.label))

    unmatched = (
        frozenset(coverage.get("unmatched_sequential_a", ())),
        frozenset(coverage.get("unmatched_sequential_b", ())),
    )
    outcomes = compare.compare_points(
        engine,
        netlist_a,
        netlist_b,
        points,
        mapping,
        options,
        progress=progress,
        unmatched=unmatched,
    )

    if z3_utils is not None:
        for outcome in outcomes:
            outcome.cross_check = engine.cross_check(
                z3_utils, netlist_a, netlist_b, outcome.point, args.solver_timeout
            )
            expected = {
                compare.EQUIVALENT: "equivalent",
                compare.DIFFERENT: "different",
            }.get(outcome.status)
            actual = (outcome.cross_check or {}).get("verdict")
            if expected and actual and expected != actual:
                outcome.notes.append(
                    "z3_utils.compare_nets disagrees with this verdict (it says {!r}); "
                    "both answers are recorded and neither is silently preferred".format(actual)
                )
                reporter.warn(
                    "cross-check disagreement at {}: this run says {!r}, z3_utils says "
                    "{!r}".format(outcome.point.key, expected, actual)
                )

    evidence_by_point = {}
    interesting = [
        outcome
        for outcome in outcomes
        if args.diagrams == "all"
        or (args.diagrams == "changed" and outcome.status != compare.EQUIVALENT)
    ]
    netlist_paths = (os.path.abspath(args.netlist_a), os.path.abspath(args.netlist_b))
    gate_libraries = (
        os.path.abspath(args.gate_library_a or args.gate_library)
        if (args.gate_library_a or args.gate_library)
        else None,
        os.path.abspath(args.gate_library_b or args.gate_library)
        if (args.gate_library_b or args.gate_library)
        else None,
    )
    for outcome in interesting:
        evidence_by_point[outcome.point.key] = _evidence_for(
            outcome, out_dir, reporter, args.format, netlist_paths, gate_libraries
        )

    correspondence_artifact = None
    if args.correspondence and os.path.isfile(args.correspondence):
        correspondence_artifact = _sha_artifact(
            os.path.abspath(args.correspondence),
            "correspondence",
            "the explicit correspondence between the two builds",
        )

    artifact_id_a = _artifact_id(mapping.labels[0], "build_a")
    artifact_id_b = _artifact_id(mapping.labels[1], "build_b")
    if artifact_id_a == artifact_id_b:
        artifact_id_a, artifact_id_b = artifact_id_a + "_a", artifact_id_b + "_b"

    document = findings.build_document(
        netlist_a,
        netlist_b,
        outcomes,
        problems,
        coverage,
        mapping,
        options,
        artifact_id_a=artifact_id_a,
        artifact_id_b=artifact_id_b,
        netlist_path_a=os.path.abspath(args.netlist_a),
        netlist_path_b=os.path.abspath(args.netlist_b),
        correspondence_artifact=correspondence_artifact,
        hal_version=halbridge.hal_version(hal_py),
        solver_backend=engine.solver_label,
        producer_command=["hal_semantic_diff"] + list(sys.argv[1:]),
        evidence_by_point=evidence_by_point,
        wall_time_s=round(time.time() - started, 3),
        record_timings=args.timings,
    )

    try:
        findings_validate.validate_document(document)
    except findings_validate.FindingsValidationError as exc:
        raise CliError(
            "the comparison produced a findings document that does not validate:\n  - "
            + "\n  - ".join(exc.errors[:20])
        )

    findings_path = os.path.join(out_dir, args.findings_name)
    findings_serialize.write_document(document, findings_path)
    reporter.produced(findings_path)

    if args.html:
        title = "Semantic diff: {} vs {}".format(mapping.labels[0], mapping.labels[1])
        if args.report_backend in ("auto", "hal_viz") and report.hal_viz_report_available():
            reporter.info("using the hal_viz 'report' subcommand for the HTML report")
            from hal_viz.cli import main as viz_main

            report_path = os.path.join(out_dir, args.report_name)
            code = viz_main(["report", findings_path, "-o", report_path])
            if code != 0:
                raise CliError("hal_viz report failed with exit code {}".format(code))
        elif args.report_backend == "hal_viz":
            raise CliError(
                "--report-backend hal_viz was requested but this checkout's hal_viz has "
                "no 'report' subcommand"
            )
        else:
            report_path = report.write_report(document, os.path.join(out_dir, args.report_name),
                                              title=title)
        reporter.produced(report_path)

    measurements = {
        "netlist_a": os.path.abspath(args.netlist_a),
        "netlist_b": os.path.abspath(args.netlist_b),
        "observation_points": len(points),
        "correspondence_gaps": len(problems),
        "solver_queries": engine.queries,
        "solver_wall_time_s": round(engine.solver_wall_time_s, 4),
        "total_wall_time_s": round(time.time() - started, 3),
        "per_point_wall_time_s": {
            outcome.point.key: outcome.wall_time_s for outcome in outcomes
        },
        "status_counts": {
            status: len([o for o in outcomes if o.status == status])
            for status in compare.STATUSES
        },
        "options": options.as_dict(),
        "hal_semantic_diff_version": __version__,
    }
    measurements_path = os.path.join(out_dir, "measurements.json")
    with open(measurements_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(measurements, handle, indent=2, sort_keys=True)
        handle.write("\n")
    reporter.produced(measurements_path)

    differing = [o for o in outcomes if o.status == compare.DIFFERENT]
    inconclusive = [o for o in outcomes if o.status in compare.INCONCLUSIVE]
    reporter.info(
        "verdict: {} equivalent, {} different, {} inconclusive, {} correspondence "
        "gap(s)".format(
            len([o for o in outcomes if o.status == compare.EQUIVALENT]),
            len(differing),
            len(inconclusive),
            len(problems),
        )
    )

    if args.fail_on == "never":
        return EXIT_OK
    if differing:
        return EXIT_VERDICT
    if args.fail_on == "inconclusive" and (inconclusive or problems or not outcomes):
        return EXIT_VERDICT
    return EXIT_OK


def cmd_report(args, reporter):
    document = findings_serialize.read_document(args.findings)
    try:
        findings_validate.validate_document(document)
    except findings_validate.FindingsValidationError as exc:
        raise CliError(
            "{} is not a valid findings document:\n  - ".format(args.findings)
            + "\n  - ".join(exc.errors[:20])
        )
    out_path = args.output or os.path.splitext(args.findings)[0] + ".html"
    report.write_report(document, out_path, title=args.title)
    reporter.produced(os.path.abspath(out_path))
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_semantic_diff",
        description=(
            "Behavioural diff of two synthesized builds: which observation points still "
            "implement the same function, which changed, and what input makes them differ."
        ),
    )
    parser.add_argument("--version", action="version", version="hal_semantic_diff " + __version__)
    parser.add_argument("-q", "--quiet", action="store_true", help="no progress on stderr")
    parser.add_argument(
        "--traceback", action="store_true", help="show the full traceback on failure"
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = True

    compare_parser = subparsers.add_parser(
        "compare",
        help="compare two builds",
        description=(
            "Compare two builds at every observation point derived from the "
            "correspondence, localize the cones that changed, and write a findings "
            "document, changed-cone diagrams and an HTML report."
        ),
    )
    compare_parser.add_argument("netlist_a", help="build A: project dir, .hal or HDL netlist")
    compare_parser.add_argument("netlist_b", help="build B: project dir, .hal or HDL netlist")
    compare_parser.add_argument(
        "-c", "--correspondence", metavar="FILE", help="correspondence mapping file (JSON)"
    )
    compare_parser.add_argument(
        "--auto-correspondence",
        action="store_true",
        help="use the identity mapping over shared names instead of a file",
    )
    compare_parser.add_argument(
        "-o", "--output", default="semantic_diff_out", metavar="DIR",
        help="output directory (default: ./semantic_diff_out)",
    )
    compare_parser.add_argument(
        "-g", "--gate-library", metavar="FILE", help="gate library for both HDL netlists"
    )
    compare_parser.add_argument("--gate-library-a", metavar="FILE", help="gate library for build A")
    compare_parser.add_argument("--gate-library-b", metavar="FILE", help="gate library for build B")
    compare_parser.add_argument(
        "--solver-timeout", type=int, default=10, metavar="SECONDS",
        help="per-observation-point SMT timeout (default: 10)",
    )
    compare_parser.add_argument(
        "--max-cone-gates", type=int, default=4096, metavar="N",
        help="refuse to build a cone larger than this (default: 4096)",
    )
    compare_parser.add_argument(
        "--structural-fast-path",
        action="store_true",
        help="skip the solver for points whose two cones have identical canonical "
        "signatures; the finding then reports a structural, not a formal, method",
    )
    compare_parser.add_argument(
        "--no-witness", action="store_true", help="do not retain solver models"
    )
    compare_parser.add_argument(
        "--no-cross-check",
        dest="cross_check",
        action="store_false",
        help="do not also ask z3_utils.compare_nets about each point",
    )
    compare_parser.set_defaults(cross_check=True)
    compare_parser.add_argument(
        "--diagrams", choices=("changed", "all", "none"), default="changed",
        help="which observation points get a cone diagram (default: changed)",
    )
    compare_parser.add_argument(
        "-f", "--format", choices=("svg", "png", "pdf", "none"), default="svg",
        help="rendered diagram format; 'none' writes only .dot (default: svg)",
    )
    compare_parser.add_argument(
        "--no-html", dest="html", action="store_false", help="do not write the HTML report"
    )
    compare_parser.set_defaults(html=True)
    compare_parser.add_argument(
        "--report-backend", choices=("auto", "builtin", "hal_viz"), default="auto",
        help="who renders the HTML: hal_viz's 'report' subcommand when it exists, else "
        "the built-in renderer (default: auto)",
    )
    compare_parser.add_argument(
        "--no-timings",
        dest="timings",
        action="store_false",
        help="drop every wall-clock measurement from the findings document so two runs "
        "on identical inputs produce byte-identical output",
    )
    compare_parser.set_defaults(timings=True)
    compare_parser.add_argument("--findings-name", default="findings.json", metavar="NAME")
    compare_parser.add_argument("--report-name", default="report.html", metavar="NAME")
    compare_parser.add_argument(
        "--fail-on", choices=_FAIL_ON_CHOICES, default="difference",
        help="exit 3 on a behavioural difference (default), on anything short of a "
        "proof ('inconclusive'), or never",
    )
    compare_parser.add_argument(
        "--hal-lib", action="append", default=[], metavar="DIR",
        help="directory containing hal_py (repeatable; $HAL_PY_PATH works too)",
    )
    compare_parser.set_defaults(func=cmd_compare)

    report_parser = subparsers.add_parser(
        "report",
        help="render an existing findings document to HTML",
        description=(
            "Render any hal_findings document -- not only this tool's -- to a "
            "self-contained HTML report with its evidence linked."
        ),
    )
    report_parser.add_argument("findings", help="a findings JSON document")
    report_parser.add_argument("-o", "--output", metavar="FILE", help="output HTML path")
    report_parser.add_argument("--title", metavar="TEXT", help="report title")
    report_parser.set_defaults(func=cmd_report)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    reporter = _Reporter(quiet=getattr(args, "quiet", False))
    # Cone signatures are computed recursively; a deep combinational chain must
    # not die on the interpreter's default recursion limit.
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
    try:
        return args.func(args, reporter)
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted\n")
        return EXIT_INTERRUPTED
    except (CliError, correspondence.CorrespondenceError) as exc:
        if getattr(args, "traceback", False):
            raise
        sys.stderr.write("[semantic_diff] error: {}\n".format(exc))
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001
        if getattr(args, "traceback", False):
            raise
        sys.stderr.write("[semantic_diff] error: {}: {}\n".format(type(exc).__name__, exc))
        return EXIT_ERROR
