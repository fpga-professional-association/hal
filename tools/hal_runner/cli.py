"""Command line front end.

    python tools/hal_runner run tools/hal_runner/examples/uart_components.json
    python tools/hal_runner run run.json --dry-run
    python tools/hal_runner validate run.json
    python tools/hal_runner manifest out/manifest.json --summary
    python tools/hal_runner analyses

Exit codes are part of the interface, because the whole point of the runner is
that a pipeline can believe them:

===  =========================================================================
0    every step succeeded (or reused a verified checkpoint)
1    the run finished but at least one step failed, timed out or was skipped
2    the run could not start: bad configuration, missing input, no hal binary
130  interrupted
===  =========================================================================

Progress goes to stderr; stdout carries only the paths and documents a caller
would want to parse.
"""

import argparse
import json
import os
import sys

from . import __version__, analyses, config as config_module, manifest as manifest_module
from .runner import Reporter, Runner, RunnerError

__all__ = ["build_parser", "main"]


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_runner",
        description="Run a declared sequence of HAL analyses reproducibly and write a "
        "manifest pinning every input, tool version, configuration and result.",
        epilog="Steps execute as 'hal --python-script' subprocesses, so a built HAL is "
        "required for 'run' (see --hal-binary / $HAL_BASE_PATH). 'validate', "
        "'analyses' and 'run --dry-run' need only a Python interpreter.",
    )
    parser.add_argument("--version", action="version", version="hal_runner " + __version__)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    run = subparsers.add_parser(
        "run",
        help="execute a run configuration",
        description="Execute every step of a run configuration in order and write "
        "<output_dir>/manifest.json. The manifest is written whether the run "
        "succeeded or not.",
    )
    run.add_argument("config", help="run configuration JSON file")
    run.add_argument(
        "-o",
        "--output-dir",
        metavar="DIR",
        help="override the configuration's output_dir",
    )
    run.add_argument(
        "--hal-binary",
        metavar="PATH",
        help="the hal executable to run steps with (default: $HAL_RUNNER_HAL_BINARY, "
        "$HAL_BASE_PATH/bin/hal, then 'hal' on PATH)",
    )
    run.add_argument(
        "--cache-dir",
        metavar="DIR",
        help="checkpoint store location (default: <output_dir>/cache)",
    )
    run.add_argument(
        "--no-cache",
        action="store_true",
        help="neither read nor write checkpoints",
    )
    run.add_argument(
        "--refresh-cache",
        action="store_true",
        help="ignore existing checkpoints but write new ones",
    )
    run.add_argument(
        "--step",
        action="append",
        default=[],
        metavar="ID",
        help="run only this step (repeatable); the order stays the configured one",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan, including each step's cache key and command, and exit",
    )
    run.add_argument("-q", "--quiet", action="store_true", help="suppress progress messages")
    run.add_argument(
        "--traceback", action="store_true", help="show the full Python traceback on error"
    )

    validate = subparsers.add_parser(
        "validate",
        help="check run configurations without running anything",
        description="Validate run configurations against the schema and the analysis "
        "registry. Does not look at the netlist, so a configuration written for "
        "another machine can be checked here.",
    )
    validate.add_argument("paths", nargs="+", help="run configuration JSON files")
    validate.add_argument("--quiet", action="store_true", help="only report failures")

    manifest = subparsers.add_parser(
        "manifest", help="inspect a run manifest", description="Summarize or digest a manifest."
    )
    manifest.add_argument("path", help="manifest.json produced by a run")
    manifest.add_argument(
        "--digest",
        action="store_true",
        help="print only the reproducible digest (timings and machine paths excluded)",
    )
    manifest.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero unless the manifest records a successful run",
    )

    subparsers.add_parser(
        "analyses",
        help="list the analyses a step may name",
        description="List every registered analysis with its options and defaults.",
    )

    schema = subparsers.add_parser("schema", help="print the run configuration schema")
    schema.add_argument("--path", action="store_true", help="print the file path only")

    return parser


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def cmd_run(args, stream):
    reporter = Reporter(quiet=args.quiet)
    run_config = config_module.load(args.config)
    runner = Runner(
        run_config,
        output_dir=args.output_dir,
        hal_binary=args.hal_binary,
        cache_dir=args.cache_dir,
        use_cache=not args.no_cache,
        refresh_cache=args.refresh_cache,
        reporter=reporter,
        only=args.step or None,
        command=[os.path.basename(sys.argv[0] or "hal_runner")] + list(sys.argv[1:]),
    )
    runner.prepare(require_hal=not args.dry_run, materialize=not args.dry_run)

    if args.dry_run:
        stream.write(
            json.dumps(
                {
                    "run": run_config.name,
                    "output_dir": runner.output_dir,
                    "hal": runner.tool["hal"],
                    "inputs": runner.inputs,
                    "steps": runner.plan(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return 0

    exit_code, document = runner.run()
    reporter.info(manifest_module.summarize(document))
    stream.write(os.path.join(runner.output_dir, "manifest.json") + "\n")
    return exit_code


def cmd_validate(args, stream):
    failed = 0
    for path in args.paths:
        try:
            run_config = config_module.load(path)
        except config_module.ConfigError as exc:
            failed += 1
            stream.write("FAIL {}\n".format(path))
            for error in exc.errors:
                stream.write("     {}\n".format(error))
            continue
        if not args.quiet:
            stream.write(
                "ok   {}  (run {!r}, {} step(s): {})\n".format(
                    path,
                    run_config.name,
                    len(run_config.steps),
                    ", ".join(step.analysis_name for step in run_config.steps),
                )
            )
    return 1 if failed else 0


def cmd_manifest(args, stream):
    document = manifest_module.read(args.path)
    if args.digest:
        stream.write(manifest_module.digest(document) + "\n")
    else:
        stream.write(manifest_module.summarize(document) + "\n")
    if args.strict and document.get("run", {}).get("status") != "success":
        return 1
    return 0


def cmd_analyses(args, stream):
    for name in analyses.names():
        analysis = analyses.get(name)
        stream.write("{}\n".format(name))
        stream.write("  plugin       {}\n".format(analysis.plugin))
        stream.write("  entry point  {}\n".format(analysis.entry_point))
        stream.write(
            "  claims       {} ({})\n".format(
                analysis.method_kind,
                "deterministic" if analysis.deterministic else "not guaranteed deterministic",
            )
        )
        stream.write("  {}\n".format(analysis.description))
        for option_name in sorted(analysis.options):
            option = analysis.options[option_name]
            stream.write(
                "    {:<26} default {!r:<8} {}\n".format(
                    option_name, option.default, option.description
                )
            )
        stream.write("\n")
    return 0


def cmd_schema(args, stream):
    path = config_module.schema_path()
    if args.path:
        stream.write(os.path.abspath(path) + "\n")
        return 0
    stream.write(json.dumps(config_module.load_schema(), indent=2, sort_keys=True) + "\n")
    return 0


_COMMANDS = {
    "run": cmd_run,
    "validate": cmd_validate,
    "manifest": cmd_manifest,
    "analyses": cmd_analyses,
    "schema": cmd_schema,
}


def main(argv=None, stream=None):
    """Entry point; returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    stream = stream or sys.stdout

    handler = _COMMANDS.get(args.command)
    if handler is None:
        parser.print_help(sys.stderr)
        return 2

    try:
        return handler(args, stream)
    except config_module.ConfigError as exc:
        if getattr(args, "traceback", False):
            raise
        sys.stderr.write("[hal_runner] {}\n".format(exc))
        return 2
    except (RunnerError, analyses.UnknownAnalysis, analyses.AnalysisConfigError) as exc:
        if getattr(args, "traceback", False):
            raise
        sys.stderr.write("[hal_runner] error: {}\n".format(exc))
        return 2
    except (OSError, ValueError) as exc:
        if getattr(args, "traceback", False):
            raise
        sys.stderr.write("[hal_runner] error: {}: {}\n".format(type(exc).__name__, exc))
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        sys.stderr.write("[hal_runner] interrupted\n")
        return 130
