"""``python tools/hal_apb_check <command>``.

Commands::

    properties        list the property catalogue for a revision and role
    validate-mapping  check a bus mapping file, and (with --netlist) its signals
    check             run the bounded checks and write a findings document
    replay            re-run an exported counterexample bundle
    exclusions        list what this checker deliberately does not cover

Exit codes are meant to be used in CI and follow the same rule as the rest of
this repository -- a non-zero exit is a statement, not noise::

    0  every obligation either held up to the bound or was reported as a
       non-failure (unsupported, vacuous, timeout) and --strict was not given
    1  a bounded counterexample was found
    2  the run could not produce trustworthy results (bad mapping, missing
       design, overconstrained environment, an internal error), or --strict was
       given and something inconclusive was reported
"""

import argparse
import json
import os
import sys

from . import VERSION, PRODUCER_NAME
from . import engine, findings as findings_module, mapping as mapping_module
from . import reference_models, spec, witness

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_UNUSABLE = 2


class CliError(RuntimeError):
    """A problem that should exit with :data:`EXIT_UNUSABLE` and a clear message."""


class EvidenceWriter(object):
    """Writes SMT-LIB queries, replay bundles and VCD next to the report."""

    def __init__(self, directory, enabled=True):
        self.directory = directory
        self.enabled = enabled and directory is not None
        self.files = {}
        if self.enabled and not os.path.isdir(directory):
            os.makedirs(directory)

    @staticmethod
    def safe_name(name):
        return "".join(character if character.isalnum() else "_" for character in name)

    def smt2(self, name, text):
        if not self.enabled:
            return None
        path = os.path.join(self.directory, "{}.smt2".format(self.safe_name(name)))
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        self.files.setdefault("smt2", {})[name] = path
        return path

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


def load_system(bus_mapping, hal_libs=(), override=None):
    """Build the transition system named by the mapping (or by ``override``)."""
    design = dict(bus_mapping.design)
    if override:
        design = dict(override)

    if design.get("reference_model"):
        name = design["reference_model"]
        try:
            system = reference_models.build(name)
        except KeyError as error:
            raise CliError(str(error))
        source = os.path.abspath(reference_models.__file__)
        return system, {
            "kind": "other",
            "path": source,
            "description": "reference transition system {!r} from hal_apb_check."
                           "reference_models".format(name),
            "design_name": system.name,
        }

    netlist_path = bus_mapping.resolve_design_path("netlist")
    project_path = bus_mapping.resolve_design_path("project")
    library_path = bus_mapping.resolve_design_path("gate_library")
    if override:
        netlist_path = design.get("netlist") or netlist_path
        project_path = design.get("project") or project_path
        library_path = design.get("gate_library") or library_path

    from . import netlist as netlist_frontend

    try:
        return netlist_frontend.load(
            netlist_path=netlist_path,
            project_path=project_path,
            gate_library=library_path,
            hal_libs=hal_libs,
        )
    except netlist_frontend.NetlistFrontendError as error:
        raise CliError(str(error))


def _artifact(model, spec_dict, artifact_id):
    """Build a schema artifact, hashing the file when there is one."""
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
        gate_library=spec_dict.get("gate_library"),
        description=spec_dict.get("description"),
    )


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def command_properties(args):
    revision = args.revision
    role = args.role
    print("APB property catalogue -- revision {}, DUT role {}".format(revision, role))
    print("\nsignals in {}: {}".format(revision, ", ".join(spec.signals_for_revision(revision))))
    print("\nobligations of the DUT ({}):".format(role))
    for prop in spec.obligations_for(role, revision, include_optional=True):
        print(
            "  {:<52} {:<10} horizon={} {}".format(
                prop.id,
                prop.kind,
                "max_wait" if callable(prop.horizon) else prop.horizon,
                "(optional)" if prop.optional else "",
            )
        )
        print("      {}".format(prop.description))
    print("\nassumed of the environment:")
    for prop in spec.assumptions_for(role, revision):
        print("  {:<52} {}".format(prop.id, prop.kind))
    not_assumable = [
        prop
        for prop in spec.properties_for(revision, include_optional=True)
        if prop.obligation_of != role and not prop.assumable
    ]
    if not_assumable:
        print("\ndeliberately NOT assumed (assuming them would hide bugs):")
        for prop in not_assumable:
            print("  {}".format(prop.id))
    return EXIT_OK


def command_exclusions(args):
    print("What hal_apb_check does not cover:\n")
    for exclusion in spec.EXCLUSIONS:
        print("  {} [{}]".format(exclusion["title"], exclusion["kind"]))
        print("      {}\n".format(exclusion["reason"]))
    return EXIT_OK


def command_validate_mapping(args):
    bus_mapping = mapping_module.load(args.mapping)
    print("mapping {!r}: {} {} DUT, {} signal(s) mapped".format(
        bus_mapping.name, bus_mapping.revision, bus_mapping.role,
        len(bus_mapping.mapped_signals()),
    ))
    for name in bus_mapping.mapped_signals():
        print("  {:<9} <- {:<24} ({} as seen by the DUT)".format(
            name, ", ".join(bus_mapping.bits(name)), bus_mapping.expected_direction(name)
        ))

    status = EXIT_OK
    if args.check_design:
        system, _ = load_system(bus_mapping, hal_libs=args.hal_lib)
        missing = [
            name for name in bus_mapping.design_signals() if not system.has_signal(name)
        ]
        if missing:
            print("\nMISSING in design {!r}: {}".format(system.name, ", ".join(missing)))
            status = EXIT_UNUSABLE
        else:
            print("\nevery mapped design signal exists in {!r}".format(system.name))
    return status


def _print_report(report, verbose=True):
    order = {
        engine.CheckOutcome.VIOLATED: 0,
        engine.CheckOutcome.ERROR: 1,
        engine.CheckOutcome.TIMEOUT: 2,
        engine.CheckOutcome.VACUOUS: 3,
        engine.CheckOutcome.NOT_INSTANTIABLE: 4,
        engine.CheckOutcome.UNSUPPORTED: 5,
        engine.CheckOutcome.HOLDS_BOUNDED: 6,
    }
    print(
        "\n{} {} DUT, bound {} cycles, {} solver quer{}, {:.3f}s".format(
            report.mapping.revision,
            report.mapping.role,
            report.bound,
            report.queries,
            "y" if report.queries == 1 else "ies",
            report.duration_s,
        )
    )
    if report.diagnostics.get("overconstrained"):
        print(
            "  OVERCONSTRAINED: the environment assumptions admit no APB transfer within the "
            "bound; no result below is evidence of anything."
        )
    for result in sorted(report.results, key=lambda r: (order[r.outcome], r.property.id)):
        line = "  {:<24} {}".format(result.outcome, result.property.id)
        if result.outcome == engine.CheckOutcome.VIOLATED:
            line += "  (first violation at cycle {})".format(result.detail["violation_cycle"])
        print(line)
        if verbose and result.detail.get("reason"):
            print("      {}".format(result.detail["reason"]))
    return report


def command_check(args):
    bus_mapping = mapping_module.load(args.mapping)
    override = None
    if args.netlist or args.reference_model or args.project:
        override = {
            "netlist": args.netlist,
            "project": args.project,
            "gate_library": args.gate_library,
            "reference_model": args.reference_model,
        }
        override = {key: value for key, value in override.items() if value}
    system, design_spec = load_system(bus_mapping, hal_libs=args.hal_lib, override=override)

    problems = system.check()
    if problems:
        raise CliError(
            "the extracted transition system is not sound:\n  - " + "\n  - ".join(problems)
        )

    evidence_dir = args.evidence_dir
    if evidence_dir is None and args.output:
        evidence_dir = os.path.join(os.path.dirname(os.path.abspath(args.output)) or ".", "evidence")
    writer = EvidenceWriter(evidence_dir, enabled=not args.no_evidence)

    report = engine.check(bus_mapping, system, bound=args.bound, evidence=writer)
    _print_report(report)

    # Evidence is referenced relative to the findings document, so a results
    # directory stays portable when it is copied or uploaded from CI.
    document_dir = os.path.dirname(os.path.abspath(args.output)) if args.output else None

    def evidence_path(path):
        if path and document_dir:
            try:
                return os.path.relpath(path, document_dir).replace(os.sep, "/")
            except ValueError:
                return path
        return path

    # ---- counterexample bundles, replayed before they are believed --------
    evidence_by_property = {}
    replay_failures = []
    for result in report.violations:
        bundle = witness.build_bundle(
            bus_mapping,
            system,
            result.property,
            result.detail["trace"],
            result.detail["initial_state"],
            result.detail["violation_cycle"],
            report.bound,
            [entry["assumption"] for entry in report.assumptions],
            unconstrained=result.detail.get("unconstrained", ()),
            producer="{} {}".format(PRODUCER_NAME, VERSION),
        )
        try:
            witness.replay(
                bundle,
                system,
                bus_mapping,
                {prop.id: prop for prop in spec.PROPERTIES},
                report.assumption_properties,
                check_from=engine.CHECK_FROM,
            )
        except witness.ReplayError as error:
            replay_failures.append("{}: {}".format(result.property.id, error))
            continue
        if writer.enabled:
            bundle_path = os.path.join(
                writer.directory, "{}.replay.json".format(writer.safe_name(result.property.id))
            )
            witness.write_bundle(bundle, bundle_path)
            vcd_path = writer.write(
                "vcd", result.property.id, witness.to_vcd(result.detail["trace"])
            )
            model = findings_module.findings_model()
            evidence_by_property[result.property.id] = [
                model.evidence(
                    "trace",
                    description="replayable counterexample bundle (same mapping, reset and "
                                "environment assumptions as this run)",
                    path=evidence_path(bundle_path),
                    media_type="application/json",
                ),
                model.evidence(
                    "waveform",
                    description="the same counterexample as VCD, one time step per PCLK edge",
                    path=evidence_path(vcd_path),
                    media_type="text/vcd",
                ),
                model.evidence(
                    "command",
                    description="re-run this counterexample",
                    command=["python", "tools/hal_apb_check", "replay", bundle_path],
                ),
            ]
    if replay_failures:
        raise CliError(
            "a counterexample did not survive replay, so it must not be reported:\n  - "
            + "\n  - ".join(replay_failures)
        )

    model = findings_module.findings_model()
    for name, path in (writer.files.get("smt2") or {}).items():
        evidence_by_property.setdefault(name, []).append(
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
                "path": os.path.abspath(args.mapping),
                "description": "APB bus mapping {!r}".format(bus_mapping.name),
            },
            "bus-mapping",
        ),
        evidence_by_property=evidence_by_property,
        net_refs=net_refs,
        command=[os.path.basename(sys.argv[0])] + list(sys.argv[1:]),
        generated_at=_utc_now(),
    )

    package = findings_module.findings_package()
    package.validate_document(document)
    if args.output:
        package.write_document(document, args.output)
        print("\nwrote {}".format(args.output))
    else:
        sys.stdout.write(package.dumps(document))

    if report.violations:
        return EXIT_VIOLATION
    inconclusive = [
        result
        for result in report.results
        if result.outcome
        in (
            engine.CheckOutcome.TIMEOUT,
            engine.CheckOutcome.ERROR,
            engine.CheckOutcome.VACUOUS,
            engine.CheckOutcome.NOT_INSTANTIABLE,
        )
    ]
    if report.diagnostics.get("overconstrained"):
        return EXIT_UNUSABLE
    if not report.results:
        print(
            "\nnothing was checked: {} defines no {} obligation this analysis can state".format(
                bus_mapping.revision, bus_mapping.role
            ),
            file=sys.stderr,
        )
        return EXIT_UNUSABLE
    if args.strict and inconclusive:
        print(
            "\n--strict: {} inconclusive result(s): {}".format(
                len(inconclusive), ", ".join(result.property.id for result in inconclusive)
            )
        )
        return EXIT_UNUSABLE
    return EXIT_OK


def command_replay(args):
    bundle = witness.read_bundle(args.bundle)
    bus_mapping = mapping_module.loads(json.dumps(bundle["mapping"]), path=args.mapping)
    override = None
    if args.netlist or args.reference_model or args.project:
        override = {
            "netlist": args.netlist,
            "project": args.project,
            "gate_library": args.gate_library,
            "reference_model": args.reference_model,
        }
        override = {key: value for key, value in override.items() if value}
    system, _ = load_system(bus_mapping, hal_libs=args.hal_lib, override=override)

    assumed = [
        prop
        for prop in spec.assumptions_for(bus_mapping.role, bus_mapping.revision)
        if prop.id in set(bundle.get("environment_assumptions") or [])
    ]
    try:
        result = witness.replay(
            bundle,
            system,
            bus_mapping,
            {prop.id: prop for prop in spec.PROPERTIES},
            assumed,
            check_from=engine.CHECK_FROM,
        )
    except witness.ReplayError as error:
        print("REPLAY FAILED: {}".format(error), file=sys.stderr)
        return EXIT_UNUSABLE

    print("reproduced {} on {}".format(result["property"], result["design"]))
    print("  violating cycle(s): {}".format(result["failing_cycles"]))
    print("  environment assumptions re-checked and holding: {}".format(
        ", ".join(result["environment_assumptions_checked"])
        or "none were applied in the original run"
    ))
    if args.vcd:
        with open(args.vcd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(
                witness.to_vcd(
                    [
                        {name: bool(value) for name, value in cycle.items()}
                        for cycle in bundle["trace"]
                    ]
                )
            )
        print("  wrote {}".format(args.vcd))
    return EXIT_OK


def _utc_now():
    import datetime

    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_apb_check",
        description="Bounded APB protocol checks over a user-mapped bus, with replayable "
                    "counterexamples and schema-valid findings.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    subparsers = parser.add_subparsers(dest="command")

    properties = subparsers.add_parser("properties", help="list the property catalogue")
    properties.add_argument("--revision", default="APB4", choices=list(spec.REVISIONS))
    properties.add_argument("--role", default="completer", choices=list(spec.ROLES))
    properties.set_defaults(handler=command_properties)

    exclusions = subparsers.add_parser("exclusions", help="list what is deliberately not covered")
    exclusions.set_defaults(handler=command_exclusions)

    validate = subparsers.add_parser("validate-mapping", help="validate a bus mapping file")
    validate.add_argument("mapping")
    validate.add_argument(
        "--check-design",
        action="store_true",
        help="also load the design and confirm every mapped signal exists",
    )
    validate.add_argument("--hal-lib", action="append", default=[], metavar="DIR")
    validate.set_defaults(handler=command_validate_mapping)

    check = subparsers.add_parser("check", help="run the bounded APB checks")
    check.add_argument("mapping")
    check.add_argument("-o", "--output", help="write the findings document here")
    check.add_argument("--bound", type=int, help="override options.bound")
    check.add_argument("--evidence-dir", help="where to write SMT2/replay/VCD evidence")
    check.add_argument("--no-evidence", action="store_true", help="do not write evidence files")
    check.add_argument("--netlist", help="override design.netlist")
    check.add_argument("--project", help="override design.project")
    check.add_argument("--gate-library", help="override design.gate_library")
    check.add_argument("--reference-model", help="override design.reference_model")
    check.add_argument("--hal-lib", action="append", default=[], metavar="DIR")
    check.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero on vacuous, timed-out or uninstantiable checks too",
    )
    check.set_defaults(handler=command_check)

    replay = subparsers.add_parser("replay", help="re-run an exported counterexample")
    replay.add_argument("bundle")
    replay.add_argument("--mapping", help="path recorded for the embedded mapping (optional)")
    replay.add_argument("--vcd", help="also write the replayed trace as VCD")
    replay.add_argument("--netlist", help="override design.netlist")
    replay.add_argument("--project", help="override design.project")
    replay.add_argument("--gate-library", help="override design.gate_library")
    replay.add_argument("--reference-model", help="override design.reference_model")
    replay.add_argument("--hal-lib", action="append", default=[], metavar="DIR")
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
    except mapping_module.MappingError as error:
        print("{}".format(error), file=sys.stderr)
        return EXIT_UNUSABLE
    except (CliError, engine.EngineError, witness.ReplayError) as error:
        print("{}".format(error), file=sys.stderr)
        return EXIT_UNUSABLE
    except FileNotFoundError as error:
        print("{}".format(error), file=sys.stderr)
        return EXIT_UNUSABLE
