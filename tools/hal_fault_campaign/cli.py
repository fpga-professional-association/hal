"""``python tools/hal_fault_campaign <command>``.

    validate   check a campaign configuration (no HAL needed)
    plan       show what a run would execute, without running it
    run        run the campaign and write a manifest
    replay     re-run named faults from a manifest and compare the verdicts
    recheck    re-derive every verdict from the recorded traces (no HAL needed)
    manifest   summarize / digest / strict-check a manifest
    schema     print the configuration schema or its path

Exit codes, mirroring ``hal_runner``:

    0   the command succeeded / the campaign succeeded / the replay matched
    1   the campaign failed, or a replay or check found a mismatch
    2   the command could not start: bad configuration, missing input, no hal
    130 interrupted
"""

import argparse
import json
import os
import sys

from . import __version__
from . import config as config_module
from . import manifest as manifest_module
from . import replay as replay_module

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNUSABLE = 2
EXIT_INTERRUPTED = 130


def _print(message=""):
    sys.stdout.write("{}\n".format(message))


def _error(message):
    sys.stderr.write("hal_fault_campaign: {}\n".format(message))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_fault_campaign",
        description="Reproducible register-bit fault-injection campaigns for HAL.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")

    validate = subparsers.add_parser(
        "validate", help="check a campaign configuration without running it"
    )
    validate.add_argument("config", nargs="+")

    plan = subparsers.add_parser("plan", help="show what a run would execute")
    plan.add_argument("config")
    plan.add_argument("--hal-binary")

    run = subparsers.add_parser("run", help="run a campaign")
    run.add_argument("config")
    run.add_argument("--hal-binary")
    run.add_argument("--output-dir")
    run.add_argument(
        "--keep-simulation-dirs",
        action="store_true",
        help="keep each run's simulator working directory instead of deleting it",
    )
    run.add_argument(
        "--no-verify-instrumentation",
        action="store_true",
        help="skip the check that the instrumented baseline matches the uninstrumented "
        "netlist (not recommended: it is the check that makes a divergence "
        "attributable to the injection)",
    )
    run.add_argument("--quiet", action="store_true")

    replay = subparsers.add_parser(
        "replay", help="re-run faults from a manifest and compare the verdicts"
    )
    replay.add_argument("manifest")
    replay.add_argument(
        "--fault",
        action="append",
        default=[],
        metavar="ID",
        help="fault id (f00007) or '<register>@<cycle>'; repeatable, default: all",
    )
    replay.add_argument("--hal-binary")
    replay.add_argument("--output-dir")
    replay.add_argument(
        "--verify-enumeration",
        action="store_true",
        help="also re-derive the fault list from the recorded seed and compare",
    )
    replay.add_argument(
        "--allow-input-drift",
        action="store_true",
        help="continue even when the netlist no longer hashes to what the manifest "
        "recorded (the result is then not a replay)",
    )
    replay.add_argument("--keep-simulation-dirs", action="store_true")
    replay.add_argument("--quiet", action="store_true")

    recheck = subparsers.add_parser(
        "recheck",
        help="re-derive every verdict from the recorded traces; needs no HAL",
    )
    recheck.add_argument("manifest")
    recheck.add_argument("--traces", help="default: traces.json next to the manifest")
    recheck.add_argument(
        "--verify-enumeration",
        action="store_true",
        help="also re-derive the fault list from the recorded seed",
    )

    manifest = subparsers.add_parser("manifest", help="inspect a campaign manifest")
    manifest.add_argument("manifest")
    manifest.add_argument("--digest", action="store_true")
    manifest.add_argument("--faults", action="store_true", help="list every fault")
    manifest.add_argument(
        "--strict", action="store_true", help="exit 1 unless the campaign succeeded"
    )
    manifest.add_argument("--json", action="store_true")

    schema = subparsers.add_parser("schema", help="print the configuration schema")
    schema.add_argument("--path", action="store_true")

    return parser


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _load_config(path):
    return config_module.load(path)


def command_validate(args):
    failed = False
    for path in args.config:
        try:
            config = _load_config(path)
        except config_module.ConfigError as exc:
            _error(str(exc))
            failed = True
            continue
        _print(
            "ok  {}: {} cycle(s) at {} ps, {} observed output(s), {} detection "
            "signal(s), engine {}".format(
                path,
                config.cycles,
                config.clock_period_ps,
                len(config.outputs),
                len(config.detection_signals),
                config.engine,
            )
        )
        if not config.detection_signals:
            _print(
                "    note: no detection signal is declared, so no fault can ever be "
                "classified as detected"
            )
    return EXIT_FAILED if failed else EXIT_OK


def command_plan(args):
    from .runner import CampaignRunner

    try:
        config = _load_config(args.config)
        runner = CampaignRunner(config, hal_binary=args.hal_binary)
    except config_module.ConfigError as exc:
        _error(str(exc))
        return EXIT_UNUSABLE
    except Exception as exc:  # noqa: BLE001 - resolve_hal_binary raises RunnerError
        _error(str(exc))
        return EXIT_UNUSABLE
    _print(json.dumps(runner.plan(), indent=2, sort_keys=True))
    return EXIT_OK


def command_run(args):
    from .runner import CampaignError, CampaignRunner

    try:
        config = _load_config(args.config)
    except config_module.ConfigError as exc:
        _error(str(exc))
        return EXIT_UNUSABLE
    if args.output_dir:
        config.output_dir = os.path.abspath(os.path.expanduser(args.output_dir))

    reporter = None if args.quiet else lambda message: _print("-- {}".format(message))
    try:
        runner = CampaignRunner(
            config,
            hal_binary=args.hal_binary,
            reporter=reporter,
            keep_simulation_dirs=args.keep_simulation_dirs,
            verify_instrumentation=not args.no_verify_instrumentation,
        )
    except Exception as exc:  # noqa: BLE001
        _error(str(exc))
        return EXIT_UNUSABLE

    try:
        exit_code, manifest = runner.run()
    except CampaignError as exc:
        _error(str(exc))
        return EXIT_UNUSABLE
    except KeyboardInterrupt:  # pragma: no cover
        return EXIT_INTERRUPTED

    _print(manifest_module.summarize_text(manifest))
    for diagnostic in manifest.get("diagnostics", []):
        _error("{}: {}".format(diagnostic["kind"], diagnostic["message"]))
    return exit_code


def command_replay(args):
    from .runner import CampaignError, CampaignRunner

    try:
        manifest = manifest_module.read(args.manifest)
        document = manifest_module.source_config_of(manifest)
    except manifest_module.ManifestError as exc:
        _error(str(exc))
        return EXIT_UNUSABLE

    manifest_dir = os.path.dirname(os.path.abspath(args.manifest))
    try:
        config = config_module.parse(document, path=os.path.join(manifest_dir, "manifest.json"))
    except config_module.ConfigError as exc:
        _error("the manifest's configuration no longer validates: {}".format(exc))
        return EXIT_UNUSABLE

    config.output_dir = os.path.abspath(
        os.path.expanduser(args.output_dir or os.path.join(manifest_dir, "replay"))
    )

    problems = replay_module.verify_inputs(manifest, config)
    if problems:
        for problem in problems:
            _error(problem)
        if not args.allow_input_drift:
            _error(
                "refusing to call this a replay; pass --allow-input-drift to run it "
                "anyway (the comparison is then against a different design)"
            )
            return EXIT_UNUSABLE

    if args.verify_enumeration:
        enumeration_problems = replay_module.verify_enumeration(manifest)
        if enumeration_problems:
            for problem in enumeration_problems:
                _error(problem)
            return EXIT_FAILED
        _print("-- enumeration re-derived from the recorded seed: identical")

    try:
        chosen = replay_module.faults_named(manifest, args.fault)
    except replay_module.ReplayError as exc:
        _error(str(exc))
        return EXIT_UNUSABLE

    reporter = None if args.quiet else lambda message: _print("-- {}".format(message))
    try:
        runner = CampaignRunner(
            config,
            hal_binary=args.hal_binary,
            reporter=reporter,
            keep_simulation_dirs=args.keep_simulation_dirs,
            verify_instrumentation=True,
        )
        exit_code, replayed = runner.run(
            faults=[
                {
                    "id": record["id"],
                    "site": record["site"],
                    "cycle": record["cycle"],
                    "hold_cycles": record["hold_cycles"],
                }
                for record in chosen
            ],
            enumeration=dict(manifest.get("enumeration", {})),
        )
    except CampaignError as exc:
        _error(str(exc))
        return EXIT_UNUSABLE
    except Exception as exc:  # noqa: BLE001
        _error(str(exc))
        return EXIT_UNUSABLE

    if exit_code != EXIT_OK:
        _print(manifest_module.summarize_text(replayed))
        for diagnostic in replayed.get("diagnostics", []):
            _error("{}: {}".format(diagnostic["kind"], diagnostic["message"]))
        return exit_code

    mismatches, compared = replay_module.compare_verdicts(
        chosen, replayed.get("faults", [])
    )
    return _report_comparison("replay", mismatches, compared, replayed.get("faults", []))


def command_recheck(args):
    try:
        manifest = manifest_module.read(args.manifest)
    except manifest_module.ManifestError as exc:
        _error(str(exc))
        return EXIT_UNUSABLE

    traces_path = args.traces or os.path.join(
        os.path.dirname(os.path.abspath(args.manifest)), "traces.json"
    )
    try:
        with open(traces_path, "r", encoding="utf-8") as handle:
            traces = json.load(handle)
    except (OSError, ValueError) as exc:
        _error("could not read the traces at {}: {}".format(traces_path, exc))
        return EXIT_UNUSABLE

    if args.verify_enumeration:
        problems = replay_module.verify_enumeration(manifest)
        if problems:
            for problem in problems:
                _error(problem)
            return EXIT_FAILED
        _print("-- enumeration re-derived from the recorded seed: identical")

    try:
        mismatches, compared = replay_module.recheck_from_traces(manifest, traces)
    except replay_module.ReplayError as exc:
        _error(str(exc))
        return EXIT_UNUSABLE

    _print(
        "-- re-classified {} fault(s) from {}".format(len(compared), traces_path)
    )
    _print(
        "-- note: this checks the verdicts against the recorded evidence only. It "
        "cannot tell you whether the simulation itself was right."
    )
    return _report_comparison("recheck", mismatches, compared, manifest.get("faults", []))


def _report_comparison(what, mismatches, compared, records):
    by_id = {record["id"]: record for record in records}
    for fault_id in compared:
        record = by_id.get(fault_id, {})
        _print(
            "   {:<8} {:<24} cycle {:<4} -> {:<22} detect {:<5} diverge {}".format(
                fault_id,
                record.get("site", "?"),
                record.get("cycle", "?"),
                record.get("classification", "?"),
                _latency(record.get("detection_latency_cycles")),
                _latency(record.get("divergence_latency_cycles")),
            )
        )
    if mismatches:
        _error("{} found {} mismatch(es):".format(what, len(mismatches)))
        for mismatch in mismatches:
            _error("  {}".format(mismatch))
        return EXIT_FAILED
    _print("ok  {}: {} fault(s) reproduced exactly".format(what, len(compared)))
    return EXIT_OK


def _latency(value):
    return "-" if value is None else str(value)


def command_manifest(args):
    try:
        manifest = manifest_module.read(args.manifest)
    except manifest_module.ManifestError as exc:
        _error(str(exc))
        return EXIT_UNUSABLE

    if args.digest:
        _print(manifest_module.manifest_digest(manifest))
        return EXIT_OK
    if args.json:
        _print(json.dumps(manifest, indent=2, sort_keys=True))
        return EXIT_OK

    _print(manifest_module.summarize_text(manifest))
    if args.faults:
        _print("")
        for record in manifest.get("faults", []):
            _print(
                "   {:<8} {:<24} cycle {:<4} hold {:<3} -> {:<22} detect {:<5} "
                "diverge {}".format(
                    record["id"],
                    record["site"],
                    record["cycle"],
                    record["hold_cycles"],
                    record.get("classification", "?"),
                    _latency(record.get("detection_latency_cycles")),
                    _latency(record.get("divergence_latency_cycles")),
                )
            )
    if args.strict and manifest.get("campaign", {}).get("status") != "success":
        return EXIT_FAILED
    return EXIT_OK


def command_schema(args):
    if args.path:
        _print(config_module.schema_path())
        return EXIT_OK
    _print(json.dumps(config_module.load_schema(), indent=2, sort_keys=True))
    return EXIT_OK


_COMMANDS = {
    "validate": command_validate,
    "plan": command_plan,
    "run": command_run,
    "replay": command_replay,
    "recheck": command_recheck,
    "manifest": command_manifest,
    "schema": command_schema,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_UNUSABLE
    try:
        return _COMMANDS[args.command](args)
    except KeyboardInterrupt:  # pragma: no cover
        return EXIT_INTERRUPTED
