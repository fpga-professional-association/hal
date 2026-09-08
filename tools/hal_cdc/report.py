"""Turn an :class:`~hal_cdc.audit.AuditResult` into schema-valid findings.

Every claim this tool makes is structural, so every finding is emitted as
``heuristic``, ``unknown``, ``unsupported`` or ``error`` -- never as a proof.
The limitation finding at the end of :func:`build_document` is always present,
so a report can never be read as timing or metastability sign-off just because
the design happened to be clean.
"""

import os
import re
import sys

from . import patterns, resets
from .crossings import ROLE_CONTROL

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from hal_findings import model, serialize  # noqa: E402
from hal_viz import dot as dot_module  # noqa: E402

from . import __version__  # noqa: E402

__all__ = [
    "ARTIFACT_ID",
    "PLUGIN_NAME",
    "ENTRY_POINT",
    "LIMITATIONS",
    "build_document",
    "build_domain_graph",
    "text_summary",
]

ARTIFACT_ID = "netlist"
PLUGIN_NAME = "hal_cdc"
ENTRY_POINT = "hal_cdc.audit.run_audit"

_MAX_REFS = 64

_METHOD_DESCRIPTION = (
    "Structural clock/reset-domain screening. User clock and reset declarations are "
    "bound to nets, each sequential gate's clock net is resolved backwards through "
    "buffers and inverters to a declared clock (or left unknown), domains are "
    "propagated forward through combinational logic to a fixpoint, and every "
    "sequential-gate input fed by a foreign domain is enumerated and matched against "
    "the two-flop synchroniser pattern. No timing, no metastability model, no "
    "simulation."
)

#: The limitations this analysis always reports, whatever the design looks like.
LIMITATIONS = (
    "Multi-bit coherency is not analysed. Two signals that each cross through a "
    "recognised synchroniser can still be sampled in different destination cycles; "
    "a bus needs a handshake, a gray code or an asynchronous FIFO, and hal_cdc "
    "cannot tell whether one is present.",
    "Reconvergence is not analysed. Two synchronised copies of the same source "
    "signal that meet again in the destination domain can disagree for a cycle.",
    "Gated and generated clocks are screened, not verified. A clock gate is treated "
    "as carrying its parent clock's domain and the enable's timing is not checked; a "
    "clock produced by a mux or by sequential logic leaves the domain unknown.",
    "Black boxes and gate types HAL classifies as neither combinational nor "
    "sequential are opaque; their outputs carry an unknown domain and no crossing "
    "through them is classified.",
    "No physical or timing sign-off and no metastability model. This pass computes "
    "no setup/hold, recovery/removal or MTBF figure, and 'recognised two-flop "
    "synchroniser' is a statement about topology only -- it says nothing about "
    "whether the settling time is enough at this frequency. It is not a substitute "
    "for CDC sign-off in an STA-aware flow.",
    "Findings depend entirely on the declarations. Registers whose clock cannot be "
    "traced to a declared clock stay in the 'unknown' domain and their inputs are "
    "reported as unknown rather than as safe.",
)

_SEVERITY_BY_CLASS = {
    patterns.CLASS_TWO_FLOP: "info",
    patterns.CLASS_MULTI_FLOP: "info",
    patterns.CLASS_UNSYNCHRONIZED: "high",
    patterns.CLASS_UNSYNCHRONIZED_CONTROL: "high",
    patterns.CLASS_WAIVED: "info",
    patterns.CLASS_UNSUPPORTED_DESTINATION: "info",
}

_SEVERITY_BY_RELEASE = {
    resets.RELEASE_DECLARED_SYNCHRONOUS: "info",
    resets.RELEASE_SYNCHRONIZED: "info",
    resets.RELEASE_SYNCHRONIZER_STAGE: "info",
    resets.RELEASE_REREGISTERED: "medium",
    resets.RELEASE_ASYNCHRONOUS: "medium",
    resets.RELEASE_THROUGH_LOGIC: "medium",
    resets.RELEASE_UNKNOWN: "info",
}

_SLUG_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


def _slug(value, fallback="x", limit=24):
    """A findings-schema identifier fragment: ``[A-Za-z0-9_.:-]``.

    Kept short on purpose: finding ids are capped at 128 characters by the
    schema and several of them concatenate four fragments.
    """
    text = _SLUG_RE.sub("-", str(value if value is not None else fallback)).strip("-")
    return (text or fallback)[:limit]


def _gate_ref(view, gate_id):
    gate = view.gate(gate_id)
    if gate is None:
        return None
    module = None
    if gate.module_id is not None or gate.module_name is not None:
        module = {}
        if gate.module_id is not None:
            module["id"] = gate.module_id
        if gate.module_name is not None:
            module["name"] = gate.module_name
        module = module or None
    return model.gate_ref(
        ARTIFACT_ID, gate.id, gate.name, gate_type=gate.type.name, module=module
    )


def _net_ref(view, net_id, role=None):
    net = view.net(net_id)
    if net is None:
        return None
    return model.net_ref(ARTIFACT_ID, net.id, net.name, role=role)


def _refs(view, gate_ids, limit=_MAX_REFS):
    result = []
    for gate_id in sorted(set(gate_ids))[:limit]:
        ref = _gate_ref(view, gate_id)
        if ref is not None:
            result.append(ref)
    return result


def _artifact(view, netlist_path=None):
    source = netlist_path or view.input_filename or ""
    source = str(source) if source else ""
    sha256 = None
    size_bytes = None
    unhashed_reason = None
    if source and os.path.isfile(source):
        sha256 = serialize.sha256_file(source)
        size_bytes = os.path.getsize(source)
    elif source and os.path.isdir(source):
        unhashed_reason = (
            "input is a HAL project directory ({}); hash the archive it was extracted "
            "from to pin it".format(os.path.basename(source))
        )
    else:
        unhashed_reason = "netlist has no readable source file (in-memory or modified netlist)"

    gate_library = None
    if view.gate_library_name or view.gate_library_path:
        gate_library = {}
        if view.gate_library_name:
            gate_library["name"] = view.gate_library_name
        if view.gate_library_path:
            gate_library["path"] = view.gate_library_path
            if os.path.isfile(view.gate_library_path):
                gate_library["sha256"] = serialize.sha256_file(view.gate_library_path)

    return model.artifact(
        ARTIFACT_ID,
        kind="netlist",
        path=source or None,
        sha256=sha256,
        unhashed_reason=unhashed_reason,
        size_bytes=size_bytes,
        design_name=view.design_name or None,
        device_name=view.device_name or None,
        netlist_id=view.netlist_id,
        gate_count=len(view.gates),
        net_count=len(view.nets),
        gate_library=gate_library,
    )


def _base_assumptions(result):
    assumptions = [
        model.assumption(
            "declarations/complete",
            "The clock, reset and input declarations name every clock and reset of the "
            "design. A clock that is not declared produces an unknown domain, not a "
            "safe one.",
            kind="user_provided",
            discharged=False,
        ),
        model.assumption(
            "library/pin-types",
            "The gate library marks clock, reset, set, enable and data pins correctly. "
            "hal_cdc reads pin types straight from the library and does not infer them "
            "from Boolean functions.",
            kind="library",
            discharged=False,
        ),
        model.assumption(
            "structural/no-timing",
            "Domain membership is decided by connectivity alone. No timing constraint, "
            "false path or multicycle path is read, and none is honoured.",
            kind="tool",
            discharged=False,
        ),
    ]
    if result.bound.declarations.source_path:
        assumptions.append(
            model.assumption(
                "declarations/source",
                "Declarations were read from {}.".format(
                    os.path.basename(result.bound.declarations.source_path)
                ),
                kind="user_provided",
                discharged=True,
            )
        )
    return assumptions


def _method(result):
    parameters = {
        "limits": result.limits.to_json(),
        "declarations": result.bound.declarations.to_json(),
    }
    return model.method(
        "hal_cdc structural clock/reset-domain screen",
        "structural",
        False,
        description=_METHOD_DESCRIPTION,
        parameters=parameters,
    )


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


def _declaration_findings(result, method):
    findings = []
    for index, problem in enumerate(result.bound.problems):
        findings.append(
            model.finding(
                "cdc/declarations/{:03d}".format(index),
                "Declaration could not be bound to the netlist",
                model.STATUS_ERROR,
                method,
                model.scope([ARTIFACT_ID], description="the declaration document"),
                summary=problem,
                severity="medium",
                error_dict=model.error("invalid_input", problem),
                tags=["declarations"],
            )
        )
    for index, problem in enumerate(result.clock_problems):
        findings.append(
            model.finding(
                "cdc/declarations/clock-pins/{:03d}".format(index),
                "Sequential primitive outside the single-clock model",
                model.STATUS_UNSUPPORTED,
                method,
                model.scope([ARTIFACT_ID]),
                summary=problem,
                severity="info",
                unsupported_dict=model.unsupported("construct", problem),
                tags=["coverage"],
            )
        )
    return findings


def _domain_findings(result, method, assumptions):
    view = result.view
    findings = []
    by_domain = result.registers_by_domain()

    for domain in result.domains:
        gate_ids = by_domain.get(domain, [])
        clock = result.bound.declarations.clock(domain)
        example = result.clock_resolutions[gate_ids[0]] if gate_ids else None
        kinds = sorted(
            {
                result.clock_resolutions[gate_id].kind
                for gate_id in gate_ids
                if gate_id in result.clock_resolutions
            }
        )
        data = {
            "domain": domain,
            "register_count": len(gate_ids),
            "resolution_kinds": kinds,
        }
        if clock is not None and clock.period_ns is not None:
            data["declared_period_ns"] = clock.period_ns
        if example is not None:
            data["example_resolution"] = example.to_json(view)
        derived = [
            result.clock_resolutions[gate_id].to_json(view)
            for gate_id in gate_ids
            if result.clock_resolutions[gate_id].kind == "derived"
        ]
        if derived:
            data["derived_clock_resolutions"] = derived[:8]

        findings.append(
            model.finding(
                "cdc/domain/{}".format(_slug(domain)),
                "Clock domain {!r}: {} register(s)".format(domain, len(gate_ids)),
                model.STATUS_HEURISTIC,
                method,
                model.scope(
                    [ARTIFACT_ID],
                    description="registers whose clock resolves to {!r}".format(domain),
                    gates=_refs(view, gate_ids),
                    nets=[
                        ref
                        for ref in [
                            _net_ref(view, net_id, role="clock")
                            for net_id, entry in sorted(result.bound.clock_nets.items())
                            if entry.name == domain
                        ]
                        if ref is not None
                    ]
                    or None,
                ),
                summary=(
                    "{} sequential gate(s) resolve to the declared clock {!r}{}. Domain "
                    "membership is structural: it follows the clock net through buffers "
                    "and inverters only.".format(
                        len(gate_ids),
                        domain,
                        " (including generated clocks derived from it)" if "derived" in kinds else "",
                    )
                ),
                severity="info",
                assumptions=assumptions,
                metrics={"register_count": len(gate_ids)},
                data=data,
                tags=["domain"],
            )
        )

    for clock in result.bound.declarations.clocks:
        if clock.name in result.domains:
            continue
        bound_net = [
            net_id
            for net_id, entry in sorted(result.bound.clock_nets.items())
            if entry.name == clock.name
        ]
        findings.append(
            model.finding(
                "cdc/domain/{}/empty".format(_slug(clock.name)),
                "Declared clock {!r} drives no register".format(clock.name),
                model.STATUS_UNKNOWN,
                method,
                model.scope(
                    [ARTIFACT_ID],
                    description="the declared clock {!r}".format(clock.name),
                    nets=[
                        ref
                        for ref in (_net_ref(view, net_id, role="clock") for net_id in bound_net)
                        if ref is not None
                    ]
                    or None,
                ),
                summary=(
                    "No sequential gate resolved its clock to {!r}. Either the declaration "
                    "names the wrong net, or the clock reaches its registers through logic "
                    "this pass could not follow -- in both cases the registers that should "
                    "be in this domain are somewhere in the unknown "
                    "domain instead.".format(clock.name)
                ),
                severity="medium" if bound_net else "info",
                assumptions=assumptions,
                data={"domain": clock.name, "bound_to_a_net": bool(bound_net)},
                tags=["domain", "unknown-domain"],
            )
        )

    for index, (reason, gate_ids) in enumerate(sorted(result.ambiguous_clocks().items())):
        unresolved = [
            gate_id
            for gate_id in gate_ids
            if not result.clock_resolutions[gate_id].is_known
        ]
        example = result.clock_resolutions[gate_ids[0]]
        findings.append(
            model.finding(
                "cdc/clock/unresolved/{:03d}".format(index),
                "Clock could not be resolved to a declared domain"
                if unresolved
                else "Generated clock resolved only under an assumption",
                model.STATUS_UNKNOWN,
                method,
                model.scope(
                    [ARTIFACT_ID],
                    description="registers affected by one clock-resolution outcome",
                    gates=_refs(view, gate_ids),
                ),
                summary="{} register(s): {}".format(len(gate_ids), reason),
                severity="medium" if unresolved else "info",
                assumptions=assumptions,
                metrics={"register_count": len(gate_ids)},
                data={
                    "reason": reason,
                    "register_count": len(gate_ids),
                    "resolution": example.to_json(view),
                    "unresolved": bool(unresolved),
                },
                tags=["domain", "unknown-domain"],
            )
        )
    return findings


def _group_crossings(result):
    groups = {}
    for entry in result.crossings:
        key = (
            entry.crossing.source_domain,
            entry.crossing.destination_domain,
            entry.crossing.role,
            entry.effective_class,
        )
        groups.setdefault(key, []).append(entry)
    return groups


def _crossing_findings(result, method, assumptions):
    view = result.view
    findings = []
    groups = _group_crossings(result)

    for index, key in enumerate(sorted(groups)):
        source, destination, role, effective = key
        entries = groups[key]
        gate_ids, net_ids = set(), set()
        for entry in entries:
            gate_ids.add(entry.crossing.destination_gate_id)
            gate_ids.update(entry.crossing.source_gate_ids)
            gate_ids.update(entry.crossing.path_gate_ids)
            gate_ids.update(entry.classification.stage_gate_ids)
            net_ids.add(entry.crossing.net_id)

        waiver = entries[0].waiver
        finding_assumptions = list(assumptions)
        if waiver is not None:
            finding_assumptions.append(
                model.assumption(
                    "waiver/{}".format(_slug(waiver.id)),
                    "Waived by {}: {}".format(waiver.id, waiver.rationale),
                    kind="user_provided",
                    discharged=False,
                )
            )

        if effective == patterns.CLASS_UNSUPPORTED_DESTINATION:
            status = model.STATUS_UNSUPPORTED
            unsupported_dict = model.unsupported(
                "construct", entries[0].classification.reason
            )
        else:
            status = model.STATUS_HEURISTIC
            unsupported_dict = None

        title = {
            patterns.CLASS_TWO_FLOP: "Two-flop synchroniser recognised: {} -> {}",
            patterns.CLASS_MULTI_FLOP: "Multi-flop synchroniser recognised: {} -> {}",
            patterns.CLASS_UNSYNCHRONIZED: "Unsynchronised crossing candidate: {} -> {}",
            patterns.CLASS_UNSYNCHRONIZED_CONTROL: (
                "Unsynchronised control path: {} -> {}"
            ),
            patterns.CLASS_WAIVED: "Waived crossing: {} -> {}",
            patterns.CLASS_UNSUPPORTED_DESTINATION: "Crossing into an unmodelled primitive: {} -> {}",
        }[effective].format(source, destination)

        findings.append(
            model.finding(
                "cdc/crossing/{}/{}/{}/{}".format(
                    _slug(source), _slug(destination), _slug(role, limit=8),
                    _slug(effective, limit=32)
                ),
                "{} ({} path(s))".format(title, len(entries)),
                status,
                method,
                model.scope(
                    [ARTIFACT_ID],
                    description="the {} path(s) from {} into {}".format(role, source, destination),
                    gates=_refs(view, gate_ids),
                    nets=[
                        ref
                        for ref in (
                            _net_ref(view, net_id, role=role) for net_id in sorted(net_ids)[:_MAX_REFS]
                        )
                        if ref is not None
                    ]
                    or None,
                ),
                summary="{}. {}".format(title, entries[0].classification.reason),
                severity=_SEVERITY_BY_CLASS.get(effective, "info"),
                confidence=0.5 if effective == patterns.CLASS_UNSYNCHRONIZED else None,
                assumptions=finding_assumptions,
                unsupported_dict=unsupported_dict,
                metrics={"path_count": len(entries)},
                data={
                    "source_domain": source,
                    "destination_domain": destination,
                    "role": role,
                    "classification": effective,
                    "paths": [entry.to_json(view) for entry in entries[:_MAX_REFS]],
                    "paths_truncated": len(entries) > _MAX_REFS,
                },
                tags=["crossing", role, effective]
                + (["waived"] if effective == patterns.CLASS_WAIVED else []),
            )
        )

    # multi-bit coherency: several recognised synchronisers on the same pair
    coherency = {}
    for entry in result.crossings:
        if not entry.classification.is_recognized:
            continue
        pair = (entry.crossing.source_domain, entry.crossing.destination_domain)
        coherency.setdefault(pair, []).append(entry)
    for source, destination in sorted(coherency):
        entries = coherency[(source, destination)]
        if len(entries) < 2:
            continue
        findings.append(
            model.finding(
                "cdc/coherency/{}/{}".format(_slug(source), _slug(destination)),
                "Multi-bit coherency not analysed: {} bits cross {} -> {}".format(
                    len(entries), source, destination
                ),
                model.STATUS_UNKNOWN,
                method,
                model.scope(
                    [ARTIFACT_ID],
                    description="signals crossing {} -> {} through recognised "
                    "synchronisers".format(source, destination),
                    gates=_refs(
                        view, [entry.crossing.destination_gate_id for entry in entries]
                    ),
                ),
                summary=(
                    "{} separate signals cross from {} to {}, each through its own "
                    "recognised synchroniser. Per-bit synchronisation does not make a "
                    "multi-bit value coherent: the bits can be captured in different "
                    "destination cycles. Whether these bits must be coherent is a design "
                    "question hal_cdc cannot answer.".format(len(entries), source, destination)
                ),
                severity="medium",
                assumptions=assumptions,
                metrics={"synchronized_bit_count": len(entries)},
                data={
                    "source_domain": source,
                    "destination_domain": destination,
                    "destination_gates": [
                        view.gate(entry.crossing.destination_gate_id).name
                        for entry in entries
                        if view.gate(entry.crossing.destination_gate_id) is not None
                    ][:_MAX_REFS],
                },
                tags=["crossing", "coherency", "unknown"],
            )
        )
    return findings


def _unknown_input_findings(result, method, assumptions):
    view = result.view
    grouped = {}
    for entry in result.unknown_inputs:
        grouped.setdefault((entry.destination_domain, entry.role, entry.reasons), []).append(entry)

    findings = []
    for index, key in enumerate(sorted(grouped, key=lambda item: (item[0], item[1], item[2]))):
        domain, role, reasons = key
        entries = grouped[key]
        findings.append(
            model.finding(
                "cdc/unknown-source/{:03d}".format(index),
                "Register input in {} fed by an unknown domain ({} input(s))".format(
                    domain, len(entries)
                ),
                model.STATUS_UNKNOWN,
                method,
                model.scope(
                    [ARTIFACT_ID],
                    description="sequential {} inputs in {} with an unresolved source "
                    "domain".format(role, domain),
                    gates=_refs(view, [entry.gate_id for entry in entries]),
                ),
                summary=(
                    "{} {} input(s) of registers clocked by {} are fed from source(s) whose "
                    "domain could not be determined ({}). These are neither safe nor unsafe: "
                    "declare the missing clock or input domain to turn them into a "
                    "verdict.".format(len(entries), role, domain, ", ".join(reasons))
                ),
                severity="medium" if role == ROLE_CONTROL else "low",
                assumptions=assumptions,
                metrics={"input_count": len(entries)},
                data={
                    "destination_domain": domain,
                    "role": role,
                    "unknown_sources": list(reasons),
                    "inputs": [entry.to_json(view) for entry in entries[:_MAX_REFS]],
                },
                tags=["crossing", "unknown-domain"],
            )
        )
    return findings


def _reset_findings(result, method, assumptions):
    view = result.view
    grouped = {}
    for target in result.reset_targets:
        key = (
            target.reset_name,
            target.domain,
            target.release,
            target.waiver.id if target.waiver is not None else None,
        )
        grouped.setdefault(key, []).append(target)

    findings = []
    for key in sorted(grouped, key=lambda item: (item[0], str(item[1]), item[2], str(item[3]))):
        reset_name, domain, release, waiver_id = key
        targets = grouped[key]
        gate_ids = {target.gate_id for target in targets}
        for target in targets:
            gate_ids.update(target.stage_gate_ids)

        status = (
            model.STATUS_UNKNOWN if release == resets.RELEASE_UNKNOWN else model.STATUS_HEURISTIC
        )
        finding_assumptions = list(assumptions)
        if waiver_id is not None:
            waiver = next(
                w for w in result.bound.waivers if w.id == waiver_id
            )
            finding_assumptions.append(
                model.assumption(
                    "waiver/{}".format(_slug(waiver.id)),
                    "Waived by {}: {}".format(waiver.id, waiver.rationale),
                    kind="user_provided",
                    discharged=False,
                )
            )

        findings.append(
            model.finding(
                "cdc/reset/{}/{}/{}{}".format(
                    _slug(reset_name),
                    _slug(domain or "unknown-domain"),
                    _slug(release, limit=32),
                    "/waived" if waiver_id else "",
                ),
                "Reset {!r} in domain {}: {} ({} register(s))".format(
                    reset_name, domain or "unknown", release.replace("_", " "), len(targets)
                ),
                status,
                method,
                model.scope(
                    [ARTIFACT_ID],
                    description="registers reset by {!r} and clocked by {}".format(
                        reset_name, domain or "an unresolved clock"
                    ),
                    gates=_refs(view, gate_ids),
                    nets=[
                        ref
                        for ref in (
                            _net_ref(view, net_id, role="reset")
                            for net_id in sorted({target.net_id for target in targets})[:_MAX_REFS]
                        )
                        if ref is not None
                    ]
                    or None,
                ),
                summary=targets[0].reason,
                severity="info" if waiver_id else _SEVERITY_BY_RELEASE.get(release, "info"),
                assumptions=finding_assumptions,
                metrics={"register_count": len(targets)},
                data={
                    "reset": reset_name,
                    "clock_domain": domain,
                    "release": release,
                    "domains_reached_by_reset": [
                        name for name in result.reset_domain_map.get(reset_name, []) if name
                    ],
                    "targets": [target.to_json(view) for target in targets[:_MAX_REFS]],
                },
                tags=["reset", "reset-release", release]
                + (["waived"] if waiver_id else []),
            )
        )

    for reset_name in sorted(result.reset_domain_map):
        domains = [name for name in result.reset_domain_map[reset_name] if name]
        if len(domains) < 2:
            continue
        findings.append(
            model.finding(
                "cdc/reset/{}/multi-domain".format(_slug(reset_name)),
                "Reset {!r} spans {} clock domains".format(reset_name, len(domains)),
                model.STATUS_HEURISTIC,
                method,
                model.scope([ARTIFACT_ID], description="the fan-out of reset {!r}".format(reset_name)),
                summary=(
                    "Reset {!r} reaches registers clocked by {}. Each domain needs its own "
                    "release synchronisation; one reset synchroniser does not cover "
                    "them all.".format(reset_name, ", ".join(domains))
                ),
                severity="medium",
                assumptions=assumptions,
                metrics={"domain_count": len(domains)},
                data={"reset": reset_name, "domains": domains},
                tags=["reset", "reset-release"],
            )
        )
    return findings


def _coverage_findings(result, method, assumptions):
    view = result.view
    findings = []

    primitives = []
    for type_name, entry in sorted(result.unsupported_gate_types.items()):
        primitives.append(
            model.unsupported_primitive(
                type_name,
                entry["reason"],
                count=entry["count"],
                properties=entry["properties"],
                gate_library=view.gate_library_name,
                example_gates=_refs(view, entry["gates"], limit=3),
            )
        )
    if primitives:
        findings.append(
            model.finding(
                "cdc/coverage/primitives",
                "Gate types whose domain behaviour hal_cdc does not model",
                model.STATUS_UNSUPPORTED,
                method,
                model.scope(
                    [ARTIFACT_ID],
                    description="gate types encountered but not modelled",
                    gate_types=sorted(result.unsupported_gate_types),
                ),
                summary=(
                    "{} gate type(s) are outside the model: {}. Their outputs carry an "
                    "unknown domain, and the absence of a crossing finding through them is "
                    "not evidence that no crossing exists.".format(
                        len(primitives), ", ".join(sorted(result.unsupported_gate_types))
                    )
                ),
                severity="info",
                unsupported_dict=model.unsupported(
                    "primitive",
                    "hal_cdc models flip-flops with exactly one clock pin and "
                    "combinational gates; everything else is opaque",
                    primitives,
                ),
                tags=["coverage"],
            )
        )

    if result.unused_waivers:
        findings.append(
            model.finding(
                "cdc/waivers/unused",
                "{} waiver(s) matched nothing".format(len(result.unused_waivers)),
                model.STATUS_UNKNOWN,
                method,
                model.scope([ARTIFACT_ID], description="the waiver list"),
                summary=(
                    "These waivers did not suppress anything in this run: {}. Either the "
                    "structure they were written for is gone -- in which case delete them "
                    "-- or their scope no longer matches, in which case they are silently "
                    "not protecting what their author thought.".format(
                        ", ".join(waiver.id for waiver in result.unused_waivers)
                    )
                ),
                severity="low",
                data={"waivers": [waiver.to_json() for waiver in result.unused_waivers]},
                tags=["waivers"],
            )
        )

    if result.propagation_limit_hit:
        findings.append(
            model.finding(
                "cdc/limits/propagation",
                "Domain propagation stopped at its step budget",
                model.STATUS_UNKNOWN,
                method,
                model.scope([ARTIFACT_ID]),
                summary=(
                    "Forward domain propagation hit its budget of {} steps. Domains "
                    "computed for this netlist are incomplete and the crossing list may be "
                    "short; raise --max-propagation-steps and re-run.".format(
                        result.limits.max_propagation_steps
                    )
                ),
                severity="medium",
                limits_dict=model.limits(
                    query_limit=result.limits.max_propagation_steps,
                    hit=True,
                    description="forward domain propagation steps",
                ),
                tags=["limits"],
            )
        )

    clock_tree = result.clock_tree
    if clock_tree is not None:
        if clock_tree.available:
            coverage = clock_tree.coverage(view, result.clock_resolutions)
            evidence_list = None
            if clock_tree.dot_path:
                evidence_list = [
                    model.evidence(
                        "dot",
                        description="clock tree recovered by the clock_tree_extractor plugin",
                        path=clock_tree.dot_path,
                    )
                ]
            findings.append(
                model.finding(
                    "cdc/clock-tree/cross-check",
                    "Clock-tree extractor cross-check",
                    model.STATUS_HEURISTIC,
                    method,
                    model.scope([ARTIFACT_ID], description="the recovered clock tree"),
                    summary=(
                        "The clock_tree_extractor plugin recovered {} clock-tree gates and "
                        "{} clock-tree nets. hal_cdc resolved {} of {} sequential gates to a "
                        "declared domain. The two passes answer different questions and are "
                        "compared, not merged: a disagreement is a hint, not a "
                        "verdict.".format(
                            coverage["clock_tree_gates"],
                            coverage["clock_tree_nets"],
                            coverage["clock_resolved_by_hal_cdc"],
                            coverage["sequential_gates"],
                        )
                    ),
                    severity="info",
                    assumptions=assumptions,
                    evidence_list=evidence_list,
                    metrics=coverage,
                    data={"clock_tree": clock_tree.to_json(), "coverage": coverage},
                    tags=["clock-tree"],
                )
            )
        else:
            findings.append(
                model.finding(
                    "cdc/clock-tree/unavailable",
                    "Clock-tree extractor cross-check did not run",
                    model.STATUS_UNKNOWN,
                    method,
                    model.scope([ARTIFACT_ID]),
                    summary=clock_tree.reason
                    or clock_tree.error
                    or "the clock_tree_extractor plugin was not run",
                    severity="info",
                    data={"clock_tree": clock_tree.to_json()},
                    tags=["clock-tree"],
                )
            )

    findings.append(
        model.finding(
            "cdc/limitations",
            "What this clock-domain audit does not cover",
            model.STATUS_UNSUPPORTED,
            method,
            model.scope([ARTIFACT_ID], description="the analysis itself"),
            summary=(
                "hal_cdc is a structural screen. It is not timing or metastability "
                "sign-off, and a clean report is not a CDC sign-off."
            ),
            severity="info",
            unsupported_dict=model.unsupported(
                "construct",
                " ".join(LIMITATIONS),
            ),
            data={"limitations": list(LIMITATIONS)},
            tags=["limitations", "coverage"],
        )
    )
    return findings


def build_document(result, netlist_path=None, generated_at=None, producer_command=None,
                   duration_s=None, hal_version=None):
    """Assemble the complete findings document for an audit run."""
    method = _method(result)
    assumptions = _base_assumptions(result)

    findings = []
    findings.extend(_declaration_findings(result, method))
    findings.extend(_domain_findings(result, method, assumptions))
    findings.extend(_crossing_findings(result, method, assumptions))
    findings.extend(_unknown_input_findings(result, method, assumptions))
    findings.extend(_reset_findings(result, method, assumptions))
    findings.extend(_coverage_findings(result, method, assumptions))

    analysis = {
        "plugin": {
            "name": PLUGIN_NAME,
            "version": __version__,
            "description": "structural clock-domain and reset-domain audit",
        },
        "entry_point": ENTRY_POINT,
        "configuration": {
            "limits": result.limits.to_json(),
            "declarations": result.bound.declarations.to_json(),
            "summary": result.summary(),
        },
    }
    if hal_version:
        analysis["hal"] = {"version": str(hal_version)}
    if duration_s is not None:
        analysis["duration_s"] = float(duration_s)

    producer = {"name": PLUGIN_NAME, "version": __version__}
    if producer_command:
        producer["command"] = [str(part) for part in producer_command]

    notes = [
        "Every finding in this document is structural. 'heuristic' and 'unknown' are the "
        "only verdicts this analysis can reach; none of them is a proof.",
        "A recognised two-flop synchroniser is a topology match, not a metastability "
        "budget. See the finding 'cdc/limitations'.",
    ]
    if result.bound.problems:
        notes.append(
            "{} declaration(s) could not be bound to a net; the domains they would have "
            "named are unknown.".format(len(result.bound.problems))
        )

    return model.document(
        producer,
        [_artifact(result.view, netlist_path=netlist_path)],
        analysis,
        findings,
        generated_at=generated_at if generated_at is not None else _utc_now(),
        notes=notes,
    )


def _utc_now():
    import datetime

    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# visualisation
# ---------------------------------------------------------------------------

_EDGE_STYLE = {
    patterns.CLASS_TWO_FLOP: {"color": "darkgreen", "style": "solid"},
    patterns.CLASS_MULTI_FLOP: {"color": "darkgreen", "style": "solid"},
    patterns.CLASS_UNSYNCHRONIZED: {"color": "red", "style": "bold"},
    patterns.CLASS_UNSYNCHRONIZED_CONTROL: {"color": "red", "style": "bold"},
    patterns.CLASS_WAIVED: {"color": "gray50", "style": "dashed"},
    patterns.CLASS_UNSUPPORTED_DESTINATION: {"color": "orange", "style": "dotted"},
}


def build_domain_graph(result, name="clock_domains"):
    """A Graphviz view of the domains and the crossings between them."""
    graph = dot_module.DotGraph(
        name,
        comment="clock-domain crossing summary produced by hal_cdc {}".format(__version__),
    )
    graph.graph_attrs.update({"rankdir": "LR", "labelloc": "t", "label": "clock domains"})
    graph.node_defaults.update({"shape": "box", "style": "rounded,filled", "fillcolor": "white"})

    by_domain = result.registers_by_domain()
    for domain in result.domains:
        graph.add_node(
            "d_" + dot_module.sanitize_id(domain, prefix="d"),
            label="{}\\n{} registers".format(domain, len(by_domain.get(domain, []))),
        )
    unknown_count = sum(
        1 for resolution in result.clock_resolutions.values() if not resolution.is_known
    )
    if unknown_count:
        graph.add_node(
            "d_unknown",
            label="unknown domain\\n{} registers".format(unknown_count),
            fillcolor="lightgray",
            style="rounded,filled,dashed",
        )

    grouped = {}
    for entry in result.crossings:
        key = (
            entry.crossing.source_domain,
            entry.crossing.destination_domain,
            entry.effective_class,
        )
        grouped[key] = grouped.get(key, 0) + 1

    for (source, destination, effective), count in sorted(grouped.items()):
        style = dict(_EDGE_STYLE.get(effective, {}))
        graph.add_edge(
            "d_" + dot_module.sanitize_id(source, prefix="d"),
            "d_" + dot_module.sanitize_id(destination, prefix="d"),
            label="{} x {}".format(count, effective.replace("_", " ")),
            **style
        )
    return graph


# ---------------------------------------------------------------------------
# console
# ---------------------------------------------------------------------------


def text_summary(result):
    """A short human-readable summary of an audit run."""
    summary = result.summary()
    lines = [
        "domains          : {}".format(", ".join(summary["domains"]) or "(none resolved)"),
        "registers        : {} ({} with an unknown domain)".format(
            summary["sequential_gates"], summary["registers_with_unknown_domain"]
        ),
        "crossings        : {}".format(summary["crossings"]),
    ]
    for name, count in sorted(summary["crossings_by_classification"].items()):
        lines.append("    {:<28} {}".format(name, count))
    lines.append(
        "unknown inputs   : {}".format(summary["sequential_inputs_with_unknown_domain"])
    )
    lines.append("reset targets    : {}".format(summary["reset_targets"]))
    for name, count in sorted(summary["reset_release_classification"].items()):
        lines.append("    {:<28} {}".format(name, count))
    if result.unused_waivers:
        lines.append(
            "unused waivers   : {}".format(
                ", ".join(waiver.id for waiver in result.unused_waivers)
            )
        )
    lines.append(
        "NOTE: structural screening only -- no timing, no metastability sign-off."
    )
    return "\n".join(lines)
