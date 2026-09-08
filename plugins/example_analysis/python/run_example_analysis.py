#!/usr/bin/env python3
"""Run the example_analysis plugin over a netlist and write a findings document.

This is the plugin's headless entry point: the C++ side answers the question, this
side says what kind of answer it is.  Every clock domain becomes a ``heuristic``
finding -- the analysis is structural, so it can never be more than that -- and
the gate types it could not evaluate become one ``unsupported`` finding that
names them.  A failure becomes an ``error`` finding rather than a stack trace, so
that a caller reading the JSON can tell "the analysis failed" from "the analysis
found nothing".

See ``tools/hal_findings/README.md`` for the schema and the status vocabulary.

Usage::

    HAL_PY_PATH=<build>/lib python3 plugins/example_analysis/python/run_example_analysis.py \
        <netlist-or-project-dir> --output findings.json

Exit codes: ``0`` the analysis ran, ``1`` it did not (an ``error`` finding is
still written when ``--output`` was given).
"""

import argparse
import os
import sys
import time

PLUGIN_NAME = "example_analysis"
ENTRY_POINT = "hal_plugins.example_analysis.analyze"

_METHOD_DESCRIPTION = (
    "Structural grouping: every sequential gate is assigned to the net that drives its "
    "clock pin, using the gate type's declared PinType::clock rather than pin names. "
    "A domain is therefore a set of gates sharing a clock *net*, which is not the same "
    "thing as a verified clock domain -- buffers and clock gating split one physical "
    "clock across several nets."
)


def _tools_directory(explicit):
    """Locate the repository's ``tools/`` directory (hal_findings, hal_viz live there)."""
    for candidate in (explicit, os.environ.get("HAL_TOOLS_PATH")):
        if candidate:
            return os.path.abspath(os.path.expanduser(candidate))
    # plugins/<name>/python/run_<name>.py -> repository root
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(here))), "tools")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run the example_analysis analysis and emit a hal_findings document.",
    )
    parser.add_argument("netlist", help="HAL project directory, .hal file, or netlist file")
    parser.add_argument("--gate-library", help="gate library for a plain netlist file")
    parser.add_argument(
        "--hal-lib",
        action="append",
        default=[],
        help="directory holding hal_py (repeatable; default: $HAL_PY_PATH)",
    )
    parser.add_argument(
        "--tools-dir",
        help="repository tools/ directory (default: $HAL_TOOLS_PATH, else inferred)",
    )
    parser.add_argument("--output", help="write the findings document here (default: stdout)")
    parser.add_argument(
        "--artifact-id",
        default="netlist",
        help="ID the netlist is registered and referenced under (default: netlist)",
    )
    parser.add_argument(
        "--max-gates-per-finding",
        type=int,
        default=512,
        help="cap on gate references recorded per domain; the count stays exact "
        "(default: 512, 0 disables the cap)",
    )
    return parser


def _domain_finding(model, common, domain, index, artifact_id, method, limit):
    gates = list(domain.gates)
    recorded = gates if not limit else gates[:limit]
    gate_refs = [common.gate_reference(gate, artifact_id) for gate in recorded]
    gate_types = sorted({ref["type"] for ref in gate_refs if ref.get("type")})
    clock_ref = common.net_reference(domain.clock_net, artifact_id, role="clock")

    data = {
        "clock_net": clock_ref,
        "gate_count": len(gates),
        "recorded_gate_count": len(gate_refs),
    }
    if len(gate_refs) != len(gates):
        data["truncated"] = (
            "only the first {} of {} gate references are recorded; the counts above are "
            "exact".format(len(gate_refs), len(gates))
        )

    return model.finding(
        "example_analysis/clock-domain/{:04d}".format(index),
        "Clock domain driven by net '{}' ({} sequential gates)".format(
            domain.clock_net.get_name(), len(gates)
        ),
        model.STATUS_HEURISTIC,
        method,
        model.scope(
            [artifact_id],
            description="sequential gates whose clock pin is driven by one net",
            gates=gate_refs,
            nets=[clock_ref],
            gate_types=gate_types or None,
        ),
        summary=(
            "{} sequential gate(s) have their clock pin driven by net '{}'. Structural "
            "evidence only: a clock buffer or a clock gate would split one physical clock "
            "into several nets and therefore into several of these findings.".format(
                len(gates), domain.clock_net.get_name()
            )
        ),
        severity="info",
        metrics={"gate_count": len(gates)},
        data=data,
        tags=["example_analysis", "clock-domain", "structural"],
    )


def _unsupported_finding(model, common, report, artifact_id, method):
    if not report.unsupported:
        return None
    primitives = [
        model.unsupported_primitive(
            entry.gate_type.get_name(),
            entry.reason,
            count=entry.count,
            properties=common.gate_type_properties(entry.gate_type) or None,
        )
        for entry in report.unsupported
    ]
    names = sorted(primitive["gate_type"] for primitive in primitives)
    return model.finding(
        "example_analysis/coverage/unsupported-primitives",
        "Sequential gate types without a clock pin",
        model.STATUS_UNSUPPORTED,
        method,
        model.scope(
            [artifact_id],
            description="sequential gate types the analysis could not assign to a clock",
            gate_types=names,
        ),
        summary=(
            "{} sequential gate type(s) expose no input pin of type 'clock': {}. Their gates "
            "are absent from every clock domain above; that absence is a gap in this "
            "analysis, not evidence that the gates are unclocked.".format(len(names), ", ".join(names))
        ),
        severity="info",
        unsupported_dict=model.unsupported(
            "primitive",
            "this analysis reads the gate type's declared clock pin; a sequential type "
            "without one cannot be assigned to a clock net",
            primitives,
        ),
        tags=["example_analysis", "coverage"],
    )


def _unresolved_finding(model, common, report, artifact_id, method):
    if not report.unresolved_gates:
        return None
    gate_refs = [
        common.gate_reference(gate, artifact_id) for gate in report.unresolved_gates[:64]
    ]
    return model.finding(
        "example_analysis/clock-domain/unresolved",
        "Sequential gates whose clock pin is driven by nothing",
        model.STATUS_UNKNOWN,
        method,
        model.scope(
            [artifact_id],
            description="sequential gates with an undriven clock pin",
            gates=gate_refs,
        ),
        summary=(
            "{} sequential gate(s) have a clock pin that no net drives. Either the netlist "
            "is incomplete or these gates are unreachable; this analysis cannot tell "
            "which.".format(len(report.unresolved_gates))
        ),
        severity="medium",
        metrics={"gate_count": len(report.unresolved_gates)},
        tags=["example_analysis", "incomplete-netlist"],
    )


def build_document(model, common, findings_version, report, netlist, artifact_id,
                   netlist_path=None, plugin_version="unknown", duration_s=None,
                   producer_command=None, max_gates_per_finding=512):
    """Wrap a ``example_analysis.Report`` in a findings document."""
    artifact = common.netlist_artifact(netlist, artifact_id, path=netlist_path)
    method = model.method(
        "example_analysis clock-net grouping",
        "structural",
        False,
        description=_METHOD_DESCRIPTION,
    )

    findings = []
    for index, domain in enumerate(report.domains):
        findings.append(
            _domain_finding(
                model, common, domain, index, artifact_id, method, max_gates_per_finding
            )
        )
    for builder in (_unresolved_finding, _unsupported_finding):
        finding = builder(model, common, report, artifact_id, method)
        if finding is not None:
            findings.append(finding)

    notes = [
        "clock domains are structural: gates sharing a clock net, not a verified clock "
        "domain",
        "gate references are scoped to artifact {!r}".format(artifact_id),
        "{} gate(s) in this netlist carry the 'sequential' property".format(
            report.sequential_gate_count
        ),
    ]
    if not report.unsupported:
        notes.append(
            "every sequential gate type in this netlist exposes a clock pin; no "
            "unsupported primitives to report"
        )

    analysis = {
        "plugin": {
            "name": PLUGIN_NAME,
            "version": str(plugin_version),
            "description": "Group sequential gates by the net that drives their clock pin (reference output of tools/new_plugin.py).",
        },
        "entry_point": ENTRY_POINT,
    }
    if duration_s is not None:
        analysis["duration_s"] = float(duration_s)

    producer = {"name": "plugins/example_analysis/python/run_example_analysis.py", "version": findings_version}
    if producer_command:
        producer["command"] = list(producer_command)

    return model.document(
        producer,
        [artifact],
        analysis,
        findings,
        generated_at=common.utc_now(),
        notes=notes,
    )


def build_error_document(model, common, findings_version, message, artifact_id,
                         netlist_path=None, producer_command=None):
    """A document whose single finding says the analysis itself failed."""
    artifact = model.artifact(
        artifact_id,
        kind="netlist",
        path=netlist_path,
        unhashed_reason="the analysis failed before the netlist could be described",
    )
    method = model.method("example_analysis clock-net grouping", "structural", False,
                          description=_METHOD_DESCRIPTION)
    finding = model.finding(
        "example_analysis/error",
        "The example_analysis analysis did not run",
        model.STATUS_ERROR,
        method,
        model.scope([artifact_id], description="the netlist the run was pointed at"),
        summary="The analysis failed; this says nothing about the design.",
        severity="high",
        error_dict=model.error("plugin_error", message),
        tags=["example_analysis"],
    )
    producer = {"name": "plugins/example_analysis/python/run_example_analysis.py", "version": findings_version}
    if producer_command:
        producer["command"] = list(producer_command)
    return model.document(
        producer,
        [artifact],
        {"plugin": {"name": PLUGIN_NAME, "version": "unknown"}, "entry_point": ENTRY_POINT},
        [finding],
        generated_at=common.utc_now(),
    )


def _emit(serialize, validate, document, output, stream):
    validate.validate_document(document)
    if output:
        serialize.write_document(document, output)
        stream.write("wrote {}\n".format(output))
    else:
        stream.write(serialize.dumps(document))


def main(argv=None, out=None, err=None):
    args = build_parser().parse_args(argv)
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    tools_dir = _tools_directory(args.tools_dir)
    if not os.path.isdir(tools_dir):
        err.write(
            "cannot find the repository's tools/ directory (looked at {!r}). Pass "
            "--tools-dir or set HAL_TOOLS_PATH; this script needs hal_findings and "
            "hal_viz from it.\n".format(tools_dir)
        )
        return 1
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    from hal_findings import model, serialize, validate, __version__ as findings_version
    from hal_findings.adapters import common
    from hal_viz import halenv

    max_gates_per_finding = max(0, args.max_gates_per_finding)
    command = [os.path.basename(__file__), args.netlist]

    try:
        hal_py = halenv.import_hal_py(args.hal_lib)
        halenv.load_all_plugins(hal_py)
        plugin_module = halenv.import_plugin(PLUGIN_NAME)
        netlist = halenv.load_netlist(hal_py, args.netlist, args.gate_library)
        started = time.time()
        report = plugin_module.analyze(netlist)
        duration = time.time() - started
    except Exception as exc:
        message = "{}: {}".format(type(exc).__name__, exc)
        err.write(message + "\n")
        try:
            document = build_error_document(
                model, common, findings_version, message, args.artifact_id,
                netlist_path=args.netlist, producer_command=command,
            )
            _emit(serialize, validate, document, args.output, out)
        except Exception as nested:  # pragma: no cover - only on a broken schema
            err.write("could not even write an error document: {}\n".format(nested))
        return 1

    plugin_version = "unknown"
    try:
        plugin_version = hal_py.plugin_manager.get_plugin_instance(
            PLUGIN_NAME, True, True
        ).get_version()
    except Exception:
        pass

    document = build_document(
        model, common, findings_version, report, netlist, args.artifact_id,
        netlist_path=args.netlist, plugin_version=plugin_version, duration_s=duration,
        producer_command=command, max_gates_per_finding=max_gates_per_finding,
    )
    _emit(serialize, validate, document, args.output, out)
    err.write(
        "{}: {} clock domain(s), {} sequential gate(s), {} unsupported gate type(s)\n".format(
            PLUGIN_NAME,
            len(report.domains),
            report.sequential_gate_count,
            len(report.unsupported),
        )
    )
    halenv.unload_all_plugins(hal_py)
    return 0


if __name__ == "__main__":
    sys.exit(main())
