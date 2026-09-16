"""``python tools/hal_secprop <command>``.

Commands::

    validate-policy   check a policy file, and (with --check-design) its signals
    properties        list the obligations a policy asks for
    cones             print the structural cone report (candidate reachability)
    check             run the bounded checks and write a findings document
    replay            re-run an exported witness bundle
    exclusions        list what this analysis deliberately does not cover

Exit codes follow the same rule as the rest of this repository -- a non-zero
exit is a statement, not noise::

    0  every obligation either held up to the bound or was reported as a
       non-failure (unsupported, vacuous, timeout) and --strict was not given
    1  a policy violation was found, with a witness that replays
    2  the run could not produce trustworthy results (bad policy, missing
       design, overconstrained environment, an internal error), or --strict was
       given and something inconclusive was reported
"""

import argparse
import json
import os
import sys

from . import VERSION, PRODUCER_NAME
from . import cones as cones_module
from . import engine as engine_module
from . import findings as findings_module
from . import halsource
from . import policy as policy_module
from . import properties as properties_module
from . import transitions
from . import witness as witness_module
from .errors import (
    DesignError,
    EngineError,
    PolicyError,
    SecpropError,
    UnsupportedPrimitives,
)

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNUSABLE = 2


class CliError(SecpropError):
    """A problem that should exit with :data:`EXIT_UNUSABLE` and a clear message."""


class EvidenceWriter(object):
    """Writes SMT-LIB queries, witness bundles and VCD next to the report."""

    def __init__(self, directory, enabled=True):
        self.directory = directory
        self.enabled = enabled and directory is not None
        self.files = {}
        if self.enabled and not os.path.isdir(directory):
            os.makedirs(directory)

    @staticmethod
    def ensure_parent(path):
        """Create the directory a report is about to be written into."""
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        return path

    @staticmethod
    def safe_name(name):
        return "".join(character if character.isalnum() else "_" for character in name)

    def smt2(self, name, text):
        return self.write("smt2", name, text)

    def write(self, kind, name, text):
        if not self.enabled:
            return None
        path = os.path.join(self.directory, "{}.{}".format(self.safe_name(name), kind))
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        self.files.setdefault(kind, {})[name] = path
        return path


# ---------------------------------------------------------------------------
# design loading
# ---------------------------------------------------------------------------


def load_system(policy, args):
    """Build the transition system the policy names.

    ``--source`` decides which front end runs; it is never inferred from what
    happens to be importable, because a run that silently changes engine is a
    run whose results cannot be compared with the previous one.
    """
    netlist_path = args.netlist or policy.resolve_path("netlist")
    library_path = args.gate_library or policy.resolve_path("gate_library")
    project_path = policy.resolve_path("project")

    source = args.source
    if source == "auto":
        source = "hal" if project_path else "offline"
    if source == "offline" and project_path:
        raise CliError(
            "policy {!r} names a HAL project directory, which only the hal_py front end "
            "can read; re-run with --source hal".format(policy.name)
        )

    if source == "hal":
        return halsource.load(
            netlist_path=netlist_path,
            project_path=project_path,
            gate_library=library_path,
            hal_libs=args.hal_lib,
        )
    return transitions.load(netlist_path, library_path)


def _artifact(model, spec_dict, artifact_id):
    serialize = findings_module.findings_package().serialize
    path = spec_dict.get("path")
    sha256 = None
    size_bytes = None
    unhashed_reason = None
    if path and os.path.isfile(path):
        sha256 = serialize.sha256_file(path)
        size_bytes = os.path.getsize(path)
    elif path and os.path.isdir(path):
        unhashed_reason = "the design is a HAL project directory; hash its archive to pin it"
    else:
        unhashed_reason = "the design has no readable source file on disk"
    gate_library = spec_dict.get("gate_library")
    if isinstance(gate_library, str):
        gate_library = {"path": gate_library}
    if isinstance(gate_library, dict):
        gate_library = {
            key: value
            for key, value in gate_library.items()
            if key in ("name", "path", "sha256")
        }
        if gate_library.get("path") and os.path.isfile(gate_library["path"]):
            gate_library["sha256"] = serialize.sha256_file(gate_library["path"])
    return model.artifact(
        artifact_id,
        kind=spec_dict.get("kind", "netlist"),
        path=path,
        sha256=sha256,
        size_bytes=size_bytes,
        unhashed_reason=unhashed_reason,
        design_name=spec_dict.get("design_name"),
        netlist_id=spec_dict.get("netlist_id"),
        gate_count=spec_dict.get("gate_count"),
        net_count=spec_dict.get("net_count"),
        gate_library=gate_library,
        description=spec_dict.get("description"),
    )


def _design_source(design_spec, policy, args):
    """Resolved design paths plus the netlist hash, for the witness bundle."""
    serialize = findings_module.findings_package().serialize
    netlist = design_spec.get("path") or args.netlist or policy.resolve_path("netlist")
    library = args.gate_library or policy.resolve_path("gate_library")
    source = {"front_end": design_spec.get("front_end", "offline")}
    if netlist:
        source["netlist"] = os.path.abspath(netlist)
        if os.path.isfile(netlist):
            source["sha256"] = serialize.sha256_file(netlist)
    if library:
        source["gate_library"] = os.path.abspath(library)
    return source


def _apply_overrides(policy, args):
    for option, value in (
        ("bound", getattr(args, "bound", None)),
        ("decision_limit", getattr(args, "decision_limit", None)),
        ("conflict_limit", getattr(args, "conflict_limit", None)),
        ("timeout_s", getattr(args, "timeout_s", None)),
    ):
        if value is not None:
            policy.options[option] = value


def _utc_now():
    import datetime

    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def command_validate_policy(args):
    policy = policy_module.load(args.policy)
    print("policy {!r}: {} sensitive register(s), {} declared access(es)".format(
        policy.name, len(policy.registers), len(policy.accesses)
    ))
    print("  clock  {} ({} edge, single-clock model)".format(
        policy.clock_signal, policy.clock_edge
    ))
    print("  reset  {} active {} for {} cycle(s)".format(
        policy.reset_signal, "low" if policy.reset_active_low else "high", policy.reset_cycles
    ))
    print("  lock   {} locked when {}".format(policy.lock_signal, policy.lock_locked_value))
    for register in policy.registers:
        print("  register {:<10} {} bit(s), obligations: {}".format(
            register.name, len(register.bits), ", ".join(register.properties) or "none"
        ))
    for access in policy.accesses:
        print("  access   {:<12} {}".format(
            access.id,
            " ".join(
                "{}={}".format(signal, value)
                for signal, value in sorted(access.condition.items())
            ),
        ))

    status = EXIT_OK
    if args.check_design:
        system, _ = load_system(policy, args)
        missing = [name for name in policy.design_signals() if not system.has_signal(name)]
        if missing:
            print("\nMISSING in design {!r}: {}".format(system.name, ", ".join(missing)))
            status = EXIT_UNUSABLE
        else:
            print("\nevery policy signal exists in {!r}".format(system.name))
    return status


def command_properties(args):
    policy = policy_module.load(args.policy)
    print("obligations requested by policy {!r}:\n".format(policy.name))
    for prop in properties_module.build(policy):
        print("  {:<44} {:<15} horizon={} severity={}".format(
            prop.id, prop.kind, prop.horizon, prop.severity
        ))
        print("      {}".format(prop.description))
        print("      signals: {}\n".format(", ".join(prop.signals)))
    return EXIT_OK


def command_exclusions(args):
    print("What hal_secprop does not cover:\n")
    for exclusion in properties_module.EXCLUSIONS:
        print("  {} [{}]".format(exclusion["title"], exclusion["kind"]))
        print("      {}\n".format(exclusion["reason"]))
    return EXIT_OK


def _stdout_guard():
    """The shared fd guard that keeps HAL's native logging off stdout.

    It lives in ``hal_viz.halenv`` so every ``--json`` mode in ``tools/`` can
    use the same one; ``tools/hal_viz`` is always importable from ``tools/``,
    which is where every CLI here is run from.
    """
    from hal_viz import halenv

    return halenv.stdout_reserved_for_document()


def _cones_report(args, policy, prose, document):
    """Write the cone report: prose to ``prose``, JSON to ``document`` if given."""
    system, _ = load_system(policy, args)
    report = cones_module.analyse(system, policy)
    controls = sorted(
        {signal for signals in policy.interface_signals.values() for signal in signals}
    )
    print(
        "structural fan-in cones for policy {!r} on design {!r}\n"
        "these are CANDIDATE paths: reachability is not a verdict\n".format(
            policy.name, system.name
        ),
        file=prose,
    )
    for target in sorted(report.cones):
        data = report.cones[target].to_dict(
            external_controls=controls, lock_signal=policy.lock_signal
        )
        print("  {}: {} input(s), {} register stage(s) in the cone".format(
            target, data["input_count"], data["state_count"]
        ), file=prose)
        print("    external controls in cone: {}".format(
            ", ".join(data["external_controls_in_cone"]) or "none"
        ), file=prose)
        print("    lock in cone: {}".format(data["lock_in_cone"]), file=prose)
        for name in data["external_controls_in_cone"]:
            print("      {} at depth {} via {}".format(
                name,
                data["external_control_depths"][name],
                " -> ".join(reversed(data["example_paths"].get(name, [name]))),
            ), file=prose)
        print("", file=prose)
    summary = report.summary()
    print("  {} of {} register bit(s) are in the fan-in of a policy target".format(
        summary["states_in_selected_cones"], summary["design_states"]
    ), file=prose)

    if document is not None:
        json.dump(
            {
                "summary": summary,
                "cones": {
                    target: report.cones[target].to_dict(
                        external_controls=controls, lock_signal=policy.lock_signal
                    )
                    for target in sorted(report.cones)
                },
            },
            document,
            indent=2,
            sort_keys=True,
        )
        document.write("\n")
    return EXIT_OK


def command_cones(args):
    policy = policy_module.load(args.policy)
    if not args.json:
        return _cones_report(args, policy, sys.stdout, None)

    # --json is exclusive (issue #66). stdout carries the document and nothing
    # else: the human report goes to stderr, and the netlist load runs inside
    # the fd guard so HAL's own '[core] [info] ...' lines -- which spdlog writes
    # straight to file descriptor 1, past sys.stdout -- land on stderr too.
    # A consumer can then do json.loads(check_output(...)) and be right.
    if sys.stdout is not sys.__stdout__:
        # stdout has been replaced in-process (a test, an embedding caller). The
        # fd guard would write past the replacement to the real descriptor, so
        # honour the replacement instead; --json stays exclusive either way.
        return _cones_report(args, policy, sys.stderr, sys.stdout)
    with _stdout_guard() as document:
        return _cones_report(args, policy, sys.stderr, document)


def _print_report(report):
    order = {
        engine_module.CheckOutcome.VIOLATED: 0,
        engine_module.CheckOutcome.ERROR: 1,
        engine_module.CheckOutcome.TIMEOUT: 2,
        engine_module.CheckOutcome.VACUOUS: 3,
        engine_module.CheckOutcome.NOT_INSTANTIABLE: 4,
        engine_module.CheckOutcome.UNSUPPORTED: 5,
        engine_module.CheckOutcome.HOLDS_BOUNDED: 6,
    }
    print("\npolicy {}, bound {} cycles, {} solver quer{}, {:.3f}s".format(
        report.policy.name,
        report.bound,
        report.queries,
        "y" if report.queries == 1 else "ies",
        report.duration_s,
    ))
    if report.overconstrained:
        print(
            "  OVERCONSTRAINED: the declared environment admits no run within the bound; "
            "no result below is evidence of anything."
        )
    for result in sorted(report.results, key=lambda item: (order[item.outcome], item.property.id)):
        line = "  {:<20} {}".format(result.outcome, result.property.id)
        if result.outcome == engine_module.CheckOutcome.VIOLATED:
            line += "  (first violation at cycle {}; bits {})".format(
                result.detail["violation_cycle"],
                ", ".join(result.detail.get("failing_bits") or []) or "-",
            )
        print(line)
        if result.detail.get("reason"):
            print("      {}".format(result.detail["reason"]))
    return report


def _unsupported_report(policy, error, args, design_spec):
    """Write the "we did not model this" document and say so on stdout."""
    model = findings_module.findings_model()
    document = findings_module.build_unsupported_document(
        policy,
        error.primitives,
        str(error),
        _artifact(model, design_spec, "design"),
        _artifact(
            model,
            {
                "kind": "other",
                "path": os.path.abspath(args.policy),
                "description": "security policy {!r}".format(policy.name),
            },
            "policy",
        ),
        command=[os.path.basename(sys.argv[0])] + list(sys.argv[1:]),
        generated_at=_utc_now(),
    )
    package = findings_module.findings_package()
    package.validate_document(document)
    if args.output:
        package.write_document(document, EvidenceWriter.ensure_parent(args.output))
        print("wrote {}".format(args.output))
    else:
        sys.stdout.write(package.dumps(document))
    print(
        "\nUNSUPPORTED: {}\nNo obligation was checked; the report says so for every one of "
        "them.".format(error),
        file=sys.stderr,
    )
    return EXIT_UNUSABLE if args.strict else EXIT_OK


def command_check(args):
    policy = policy_module.load(args.policy)
    _apply_overrides(policy, args)

    design_spec = {
        "kind": "netlist",
        "path": os.path.abspath(args.netlist or policy.resolve_path("netlist") or ""),
        "design_name": policy.name,
        "description": "the design named by the policy; it could not be read",
    }
    try:
        system, design_spec = load_system(policy, args)
    except UnsupportedPrimitives as error:
        return _unsupported_report(policy, error, args, design_spec)
    except DesignError as error:
        raise CliError(str(error))

    evidence_dir = args.evidence_dir
    if evidence_dir is None and args.output:
        evidence_dir = os.path.join(
            os.path.dirname(os.path.abspath(args.output)) or ".", "evidence"
        )
    writer = EvidenceWriter(evidence_dir, enabled=not args.no_evidence)

    report = engine_module.check(policy, system, bound=policy.bound, evidence=writer)
    _print_report(report)

    document_dir = os.path.dirname(os.path.abspath(args.output)) if args.output else None

    def evidence_path(path):
        if path and document_dir:
            try:
                return os.path.relpath(path, document_dir).replace(os.sep, "/")
            except ValueError:
                return path
        return path

    model = findings_module.findings_model()
    obligations = {prop.id: prop for prop in properties_module.build(policy)}
    design_source = _design_source(design_spec, policy, args)

    # ---- witness bundles, replayed before they are believed ---------------
    evidence_by_property = {}
    witness_entries_by_property = {}
    replay_failures = []
    for result in report.violations:
        bundle = witness_module.build_bundle(
            policy,
            system,
            result.property,
            result,
            report.bound,
            producer="{} {}".format(PRODUCER_NAME, VERSION),
            design_source=design_source,
        )
        try:
            witness_module.replay(
                bundle, system, policy, obligations, check_from=engine_module.CHECK_FROM
            )
        except witness_module.ReplayError as error:
            replay_failures.append("{}: {}".format(result.property.id, error))
            continue

        net_refs = {
            signal: model.net_ref("design", net_id, signal)
            for signal, net_id in (design_spec.get("net_refs") or {}).items()
        }
        witness_entries_by_property[result.property.id] = witness_module.witness_entries(
            model, policy, result.property, result.detail["trace"], net_refs
        )
        if writer.enabled:
            bundle_path = writer.write(
                "replay.json",
                result.property.id,
                witness_module.dumps_bundle(bundle),
            )
            vcd_path = writer.write(
                "vcd", result.property.id, witness_module.to_vcd(result.detail["trace"])
            )
            transactions_path = writer.write(
                "transactions.txt",
                result.property.id,
                witness_module.format_transactions(
                    bundle["transactions"], highlight=result.detail["violation_cycle"]
                )
                + "\n",
            )
            evidence_by_property[result.property.id] = [
                model.evidence(
                    "trace",
                    description="replayable witness bundle (same policy, reset schedule "
                    "and environment as this run)",
                    path=evidence_path(bundle_path),
                    media_type="application/json",
                ),
                model.evidence(
                    "report",
                    description="the same witness as a transaction sequence, one line per "
                    "clock cycle",
                    path=evidence_path(transactions_path),
                    media_type="text/plain",
                ),
                model.evidence(
                    "waveform",
                    description="the same witness as VCD, one time step per clock edge",
                    path=evidence_path(vcd_path),
                    media_type="text/vcd",
                ),
                model.evidence(
                    "command",
                    description="re-run this witness",
                    command=["python", "tools/hal_secprop", "replay", bundle_path],
                ),
            ]
    if replay_failures:
        raise CliError(
            "a witness did not survive replay, so it must not be reported:\n  - "
            + "\n  - ".join(replay_failures)
        )

    for name, path in (writer.files.get("smt2") or {}).items():
        key = name.split("/exercise")[0] if name.endswith("/exercise") else name
        evidence_by_property.setdefault(key, []).append(
            model.evidence(
                "smt2",
                description="the exact satisfiability query behind this verdict; check it "
                "independently with 'z3 <file>'",
                path=evidence_path(path),
                media_type="text/plain",
            )
        )

    net_refs = {
        signal: model.net_ref("design", net_id, signal)
        for signal, net_id in (design_spec.get("net_refs") or {}).items()
    }
    document = findings_module.build_document(
        report,
        _artifact(model, design_spec, "design"),
        _artifact(
            model,
            {
                "kind": "other",
                "path": os.path.abspath(args.policy),
                "description": "security policy {!r}".format(policy.name),
            },
            "policy",
        ),
        evidence_by_property=evidence_by_property,
        net_refs=net_refs,
        witness_entries_by_property=witness_entries_by_property,
        command=[os.path.basename(sys.argv[0])] + list(sys.argv[1:]),
        generated_at=_utc_now(),
    )

    package = findings_module.findings_package()
    package.validate_document(document)
    if args.output:
        package.write_document(document, EvidenceWriter.ensure_parent(args.output))
        print("\nwrote {}".format(args.output))
    else:
        sys.stdout.write(package.dumps(document))

    if report.violations:
        return EXIT_VIOLATION
    if report.overconstrained:
        return EXIT_UNUSABLE
    if not report.results:
        print(
            "\nnothing was checked: the policy asks for no obligation this analysis can "
            "state",
            file=sys.stderr,
        )
        return EXIT_UNUSABLE
    inconclusive = [
        result
        for result in report.results
        if result.outcome
        in (
            engine_module.CheckOutcome.TIMEOUT,
            engine_module.CheckOutcome.ERROR,
            engine_module.CheckOutcome.VACUOUS,
            engine_module.CheckOutcome.NOT_INSTANTIABLE,
            engine_module.CheckOutcome.UNSUPPORTED,
        )
    ]
    if args.strict and inconclusive:
        print("\n--strict: {} inconclusive result(s): {}".format(
            len(inconclusive), ", ".join(result.property.id for result in inconclusive)
        ))
        return EXIT_UNUSABLE
    return EXIT_OK


def command_replay(args):
    bundle = witness_module.read_bundle(args.bundle)
    policy = policy_module.Policy(bundle["policy"], path=args.policy or args.bundle)

    # The bundle records where the design was and what it hashed to, so a
    # witness stays replayable after the results directory has been copied --
    # and so a replay against *different bytes* is refused rather than quietly
    # confirming or denying a finding about a design nobody looked at.
    source = bundle.get("design_source") or {}
    if not args.netlist and source.get("netlist"):
        args.netlist = source["netlist"]
    if not args.gate_library and source.get("gate_library"):
        args.gate_library = source["gate_library"]
    system, design_spec = load_system(policy, args)
    recorded = source.get("sha256")
    path = design_spec.get("path")
    if recorded and path and os.path.isfile(path):
        serialize = findings_module.findings_package().serialize
        digest = serialize.sha256_file(path)
        if digest != recorded and not args.allow_changed_design:
            print(
                "REPLAY REFUSED: {} has changed since the witness was written "
                "(recorded {}, found {}). Replaying a witness against different bytes "
                "proves nothing about either design; pass --allow-changed-design if that "
                "is really what you want.".format(path, recorded[:12], digest[:12]),
                file=sys.stderr,
            )
            return EXIT_UNUSABLE
    obligations = {prop.id: prop for prop in properties_module.build(policy)}
    try:
        result = witness_module.replay(
            bundle, system, policy, obligations, check_from=engine_module.CHECK_FROM
        )
    except witness_module.ReplayError as error:
        print("REPLAY FAILED: {}".format(error), file=sys.stderr)
        return EXIT_UNUSABLE

    print("reproduced {} on {}".format(result["property"], result["design"]))
    print("  violating cycle(s): {}".format(result["failing_cycles"]))
    print("  bits that moved:    {}".format(", ".join(result["failing_bits"]) or "-"))
    print("  environment assumptions re-checked and holding: {}".format(
        ", ".join(result["environment_assumptions_checked"]) or "none"
    ))
    print("\ntransaction sequence:")
    print(witness_module.format_transactions(
        result["transactions"], highlight=result["violation_cycle"]
    ))
    if args.vcd:
        with open(args.vcd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(
                witness_module.to_vcd(
                    [
                        {name: bool(value) for name, value in cycle.items()}
                        for cycle in bundle["trace"]
                    ]
                )
            )
        print("\nwrote {}".format(args.vcd))
    return EXIT_OK


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def _add_design_arguments(parser):
    parser.add_argument(
        "--source",
        choices=("auto", "offline", "hal"),
        default="auto",
        help="which front end reads the design: 'offline' uses hal_apb_recover's "
        "structural Verilog reader, 'hal' uses hal_py (needed for HAL projects). "
        "'auto' picks offline for a netlist and hal for a project.",
    )
    parser.add_argument("--netlist", help="override design.netlist")
    parser.add_argument("--gate-library", help="override design.gate_library")
    parser.add_argument("--hal-lib", action="append", default=[], metavar="DIR")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_secprop",
        description="Bounded interface-to-sensitive-state security property checks over an "
        "explicit policy, with replayable transaction-level witnesses and schema-valid "
        "findings.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    subparsers = parser.add_subparsers(dest="command")

    validate = subparsers.add_parser("validate-policy", help="validate a policy file")
    validate.add_argument("policy")
    validate.add_argument(
        "--check-design",
        action="store_true",
        help="also load the design and confirm every policy signal exists",
    )
    _add_design_arguments(validate)
    validate.set_defaults(handler=command_validate_policy)

    listing = subparsers.add_parser("properties", help="list the obligations a policy asks for")
    listing.add_argument("policy")
    listing.set_defaults(handler=command_properties)

    exclusions = subparsers.add_parser(
        "exclusions", help="list what is deliberately not covered"
    )
    exclusions.set_defaults(handler=command_exclusions)

    cones = subparsers.add_parser(
        "cones", help="print the structural cone report (candidate reachability only)"
    )
    cones.add_argument("policy")
    cones.add_argument(
        "--json",
        action="store_true",
        help="write the raw cone data to stdout as a single JSON document, and nothing "
        "else: the human report and HAL's own log lines go to stderr",
    )
    _add_design_arguments(cones)
    cones.set_defaults(handler=command_cones)

    check = subparsers.add_parser("check", help="run the bounded security checks")
    check.add_argument("policy")
    check.add_argument("-o", "--output", help="write the findings document here")
    check.add_argument("--bound", type=int, help="override options.bound")
    check.add_argument("--decision-limit", type=int, help="override options.decision_limit")
    check.add_argument("--conflict-limit", type=int, help="override options.conflict_limit")
    check.add_argument("--timeout-s", type=int, help="override options.timeout_s")
    check.add_argument("--evidence-dir", help="where to write SMT2/witness/VCD evidence")
    check.add_argument("--no-evidence", action="store_true", help="do not write evidence")
    check.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero on vacuous, unsupported or timed-out results too",
    )
    _add_design_arguments(check)
    check.set_defaults(handler=command_check)

    replay = subparsers.add_parser("replay", help="re-run an exported witness bundle")
    replay.add_argument("bundle")
    replay.add_argument("--policy", help="path recorded for the embedded policy (optional)")
    replay.add_argument("--vcd", help="also write the replayed trace as VCD")
    replay.add_argument(
        "--allow-changed-design",
        action="store_true",
        help="replay even though the netlist no longer hashes to what the bundle recorded",
    )
    _add_design_arguments(replay)
    replay.set_defaults(handler=command_replay)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if not getattr(args, "handler", None):
        parser.print_help()
        return EXIT_UNUSABLE
    try:
        return args.handler(args)
    except PolicyError as error:
        print("{}".format(error), file=sys.stderr)
        return EXIT_UNUSABLE
    except (CliError, EngineError, DesignError, witness_module.ReplayError) as error:
        print("{}".format(error), file=sys.stderr)
        return EXIT_UNUSABLE
    except FileNotFoundError as error:
        print("{}".format(error), file=sys.stderr)
        return EXIT_UNUSABLE
