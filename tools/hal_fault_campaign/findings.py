"""Campaign results as a ``hal_findings`` document.

The mapping from a classification to a finding status is the whole ethical
content of this tool, so it is written out here rather than inlined:

============================  ==============================  ==========================
classification                status                          why
============================  ==============================  ==========================
``silent_divergence``         ``bounded_counterexample``      A concrete witness that an
                                                              observable corruption got
                                                              past the detector, valid
                                                              up to the simulated cycle
                                                              bound.
``detected``                  ``proven_bounded``              Verified for this trace and
                                                              only up to the cycle bound.
                                                              Not a statement about other
                                                              workloads, other cycles or
                                                              silicon.
``unobserved_in_window``      ``proven_bounded``              The bounded claim is "nothing
                                                              reached the observed signals
                                                              inside this window" -- which
                                                              the title and summary say in
                                                              those words. It is *not*
                                                              masking, and no finding here
                                                              ever says it is.
``indeterminate``             ``unknown``                     The simulation produced X/Z
                                                              where the baseline was
                                                              defined; silicon would have
                                                              a level and we do not know
                                                              which.
============================  ==============================  ==========================

Two more findings always accompany the per-fault ones:

* a **campaign summary** -- ``proven_bounded`` when the campaign was exhaustive
  over the declared grid, ``heuristic`` with a ``statistical`` method when it
  was a seeded sample, because counts over a sample are evidence and not a rate;
* a **coverage gap** (``unsupported``) whenever some sequential gate could not
  be instrumented, naming the gate types, so that "no finding" is never read as
  "no fault site".
"""

from hal_findings import model

from . import __version__
from .faultmodel import (
    ASSUMPTIONS,
    CLASS_DETECTED,
    CLASS_INDETERMINATE,
    CLASS_SILENT,
    CLASS_UNOBSERVED,
    CLASSIFICATION_DESCRIPTIONS,
    LIMITATIONS,
    MODEL_DESCRIPTIONS,
)

__all__ = [
    "PRODUCER_NAME",
    "assumptions",
    "method_for",
    "fault_finding",
    "summary_finding",
    "coverage_finding",
    "build_document",
]

PRODUCER_NAME = "hal_fault_campaign"

_METHOD_NAME = "register-bit fault injection by netlist instrumentation + simulation"


def assumptions(extra=()):
    """The fault-model assumptions every finding carries, plus any extras."""
    built = [
        model.assumption(identifier, description, kind=kind)
        for identifier, description, kind in ASSUMPTIONS
    ]
    built.extend(extra)
    return built


def method_for(config, engine, kind="simulation"):
    return model.method(
        _METHOD_NAME,
        kind,
        True,
        description=(
            "The netlist is instrumented with one XOR per fault site, driven by a "
            "dedicated control input that is 0 in the fault-free baseline. Baseline and "
            "faulty runs are simulated with the '{engine}' engine through "
            "netlist_simulator_controller over the same {cycles}-cycle workload, and the "
            "recorded traces are compared cycle by cycle. {model}".format(
                engine=engine,
                cycles=config["workload"]["cycles"],
                model=MODEL_DESCRIPTIONS[config["faults"]["model"]],
            )
        ),
        parameters={
            "engine": engine,
            "fault_model": config["faults"]["model"],
            "clock_period_ps": config["clock"]["period_ps"],
            "cycles": config["workload"]["cycles"],
            "hold_cycles": config["faults"]["hold_cycles"],
            "observation_window": dict(config["observation"]["window"]),
        },
    )


def _site_scope(artifact_id, site, config):
    gates = [
        model.gate_ref(
            artifact_id, site["gate_id"], site["gate_name"], gate_type=site.get("gate_type")
        )
    ]
    nets = [
        model.net_ref(artifact_id, site["net_id"], site["net_name"], role="internal"),
    ]
    return model.scope(
        [artifact_id],
        description="register {!r} ({}), observed on {}".format(
            site["gate_name"],
            site.get("gate_type", "?"),
            ", ".join(config["observation"]["outputs"]),
        ),
        gates=gates,
        nets=nets,
    )


def _witness(result, config, injection_cycle, site):
    entries = [
        model.witness_entry(
            "inject:{}".format(site["gate_name"]), "1", cycle=injection_cycle
        )
    ]
    for difference in result.get("differences", [])[:8]:
        for item in difference.get("differs", []):
            entries.append(
                model.witness_entry(
                    item["signal"],
                    _value_string(item["faulty"]),
                    cycle=difference["cycle"],
                )
            )
    stimulus = sorted(config["workload"]["stimulus"], key=lambda entry: entry["cycle"])
    for entry in stimulus[:8]:
        for name, value in sorted(entry["inputs"].items()):
            entries.append(
                model.witness_entry(name, str(value), cycle=entry["cycle"])
            )
    return entries


def _value_string(value):
    return {0: "0", 1: "1", -1: "X", -2: "Z"}.get(value, str(value))


def _latency_text(result):
    parts = []
    if result.get("detection_latency_cycles") is not None:
        parts.append(
            "detection latency {} cycle(s)".format(result["detection_latency_cycles"])
        )
    if result.get("divergence_latency_cycles") is not None:
        parts.append(
            "first output divergence after {} cycle(s)".format(
                result["divergence_latency_cycles"]
            )
        )
    return "; ".join(parts) if parts else "no detection and no divergence in the window"


def fault_finding(artifact_id, fault, site, result, config, engine):
    """One finding per injected fault."""
    classification = result["classification"]
    window = result["window"]
    cycle_bound = config["workload"]["cycles"]
    scope = _site_scope(artifact_id, site, config)
    method = method_for(config, engine)
    bounds = model.bounded(
        cycle_bound,
        description=(
            "Simulated for {} clock cycles of the recorded workload; the observation "
            "window covered cycles {}.".format(
                cycle_bound,
                "{}..{}".format(window["cycles"][0], window["cycles"][1])
                if window["cycles"]
                else "(none -- the window fell outside the run)",
            )
        ),
    )
    data = {
        "fault_id": fault["id"],
        "classification": classification,
        "site": site["gate_name"],
        "injection_cycle": fault["cycle"],
        "hold_cycles": fault["hold_cycles"],
        "window": window,
        "detection_cycles": result["detection_cycles"],
        "divergence_cycles": result["divergence_cycles"][:32],
        "detection_latency_cycles": result["detection_latency_cycles"],
        "divergence_latency_cycles": result["divergence_latency_cycles"],
        "indeterminate_cycles": result["indeterminate_cycles"][:32],
        "control_net": fault.get("control_net"),
    }
    metrics = {
        "observed_cycles": window["observed_cycles"],
        "divergent_cycles": len(result["divergence_cycles"]),
    }
    tags = ["fault-injection", "seu", classification.replace("_", "-")]

    title = "{} at cycle {}: {}".format(
        site["gate_name"], fault["cycle"], classification.replace("_", " ")
    )
    common = dict(
        summary=None,
        severity=None,
        method_dict=method,
        scope_dict=scope,
        assumptions=assumptions(),
        bounds_dict=bounds,
        metrics=metrics,
        data=data,
        tags=tags,
    )

    if classification == CLASS_SILENT:
        counterexample = model.counterexample(
            "A single-bit transient flip of register {!r} at cycle {} changed {} without "
            "any detection signal becoming active inside the observation window.".format(
                site["gate_name"],
                fault["cycle"],
                ", ".join(
                    sorted(
                        {
                            item["signal"]
                            for difference in result["differences"]
                            for item in difference["differs"]
                        }
                    )
                )
                or "an observed output",
            ),
            cycle_bound=min(result["divergence_cycles"][0], cycle_bound),
            witness=_witness(result, config, fault["cycle"], site),
            witness_available=True,
        )
        common.update(
            summary=(
                "Externally observable, undetected corruption. {}. This refutes, for this "
                "workload and within {} cycles, the claim that every single-bit transient "
                "in this register is flagged before it reaches the observed "
                "outputs.".format(_latency_text(result), cycle_bound)
            ),
            severity="high",
            counterexample_dict=counterexample,
        )
        return model.finding(
            _finding_id(fault, classification), title, "bounded_counterexample", **common
        )

    if classification == CLASS_INDETERMINATE:
        common.update(
            summary=(
                "The faulty run produced an undefined (X/Z) value on an observed signal "
                "where the baseline was defined, at cycle(s) {}. Whether the design would "
                "diverge cannot be decided from this simulation, so no verdict is "
                "recorded.".format(
                    ", ".join(str(cycle) for cycle in result["indeterminate_cycles"][:8])
                )
            ),
            severity="medium",
        )
        # 'unknown' must not claim unbounded validity; the bounds stay as they are.
        return model.finding(
            _finding_id(fault, classification), title, "unknown", **common
        )

    if classification == CLASS_DETECTED:
        common.update(
            summary=(
                "Detection signal(s) {} became active at cycle {} ({}). Verified for this "
                "workload and these {} cycles only.".format(
                    ", ".join(result["detection_signals_fired"]),
                    result["detection_cycles"][0],
                    _latency_text(result),
                    cycle_bound,
                )
            ),
            severity="info",
        )
        return model.finding(
            _finding_id(fault, classification), title, "proven_bounded", **common
        )

    common.update(
        summary=(
            "No observed output and no detection signal differed from the fault-free "
            "baseline inside the observation window. This is a statement about the "
            "window only: the fault is NOT shown to be masked, and the same injection "
            "can become visible later, under different stimulus, or on a signal this "
            "campaign does not observe."
        ),
        severity="low",
    )
    return model.finding(
        _finding_id(fault, CLASS_UNOBSERVED), title, "proven_bounded", **common
    )


def _finding_id(fault, classification):
    return "fault-campaign/{}/{}".format(classification.replace("_", "-"), fault["id"])


def summary_finding(artifact_id, config, engine, enumeration, summary, notes=()):
    """The campaign-level finding: what was covered, and what that does not mean."""
    mode = enumeration.get("mode")
    exhaustive = mode == "exhaustive"
    selection_text = {
        "random": ", chosen by seeded random sampling",
        "replay": ", replayed by name from an earlier campaign's manifest",
    }.get(mode, "")
    cycle_bound = config["workload"]["cycles"]
    counts = summary["counts"]
    coverage = (
        float(enumeration["selected"]) / enumeration["grid_size"]
        if enumeration.get("grid_size")
        else 0.0
    )

    scope = model.scope(
        [artifact_id],
        description="{} register site(s) x {} injection cycle(s)".format(
            enumeration["site_count"], enumeration["cycle_count"]
        ),
    )
    method = method_for(config, engine, kind="simulation" if exhaustive else "statistical")
    extra = [
        model.assumption(
            "campaign-coverage",
            "The campaign covers {} of the {} (register, cycle) pairs it declares "
            "({:.1%}){}. Nothing is claimed about pairs outside it, about registers "
            "excluded by the site filter, or about cycles outside 0..{}.".format(
                enumeration["selected"],
                enumeration["grid_size"],
                coverage,
                selection_text,
                cycle_bound - 1,
            ),
            kind="environment",
        )
    ]

    data = {
        "counts": counts,
        "coverage_fraction": round(coverage, 6),
        "grid_size": enumeration["grid_size"],
        "selected": enumeration["selected"],
        "site_count": enumeration["site_count"],
        "cycle_count": enumeration["cycle_count"],
        "sampling": {
            key: enumeration[key]
            for key in ("mode", "count", "seed", "algorithm")
            if key in enumeration
        },
        "detection_latency_cycles": summary["detection_latency_cycles"],
        "divergence_latency_cycles": summary["divergence_latency_cycles"],
        "limitations": list(LIMITATIONS),
        "classification_meaning": dict(CLASSIFICATION_DESCRIPTIONS),
    }
    if summary.get("baseline_detection_active"):
        data["baseline_detection_active_faults"] = summary["baseline_detection_active"]

    summary_text = (
        "{total} injection(s): {detected} detected, {silent} externally observable and "
        "undetected, {unobserved} unobserved inside the window, {indeterminate} "
        "indeterminate. Coverage {coverage:.1%} of the declared (register, cycle) grid. "
        "Counts are counts over the simulated sample; they are not a failure rate and "
        "not a diagnostic-coverage figure.".format(
            total=summary["total"],
            detected=counts.get(CLASS_DETECTED, 0),
            silent=counts.get(CLASS_SILENT, 0),
            unobserved=counts.get(CLASS_UNOBSERVED, 0),
            indeterminate=counts.get(CLASS_INDETERMINATE, 0),
            coverage=coverage,
        )
    )

    if exhaustive:
        return model.finding(
            "fault-campaign/summary",
            "Fault-injection campaign over {} (register, cycle) pairs".format(
                enumeration["selected"]
            ),
            "proven_bounded",
            method,
            scope,
            summary=summary_text,
            severity="info",
            assumptions=assumptions(extra),
            bounds_dict=model.bounded(
                cycle_bound,
                description="Every injection was simulated for {} cycles.".format(cycle_bound),
            ),
            metrics={"faults": summary["total"]},
            data=data,
            tags=["fault-injection", "campaign-summary", "exhaustive"],
        )

    return model.finding(
        "fault-campaign/summary",
        "{} fault-injection campaign over {} of {} (register, cycle) pairs".format(
            "Replayed" if mode == "replay" else "Sampled",
            enumeration["selected"],
            enumeration["grid_size"],
        ),
        "heuristic",
        method,
        scope,
        summary=summary_text,
        severity="info",
        confidence=None,
        assumptions=assumptions(extra),
        bounds_dict=model.bounded(
            cycle_bound,
            description="Every injection was simulated for {} cycles.".format(cycle_bound),
        ),
        metrics={"faults": summary["total"]},
        data=data,
        tags=["fault-injection", "campaign-summary", mode or "sampled"],
    )


def coverage_finding(artifact_id, config, engine, skipped):
    """Sequential gates that could not become fault sites, named by type.

    ``skipped`` is ``[{"gate_name", "gate_id", "gate_type", "reason"}]``.
    Without this finding, "no result for that register" would be
    indistinguishable from "that register is fine".
    """
    if not skipped:
        return None
    by_type = {}
    for entry in skipped:
        by_type.setdefault(entry["gate_type"], []).append(entry)

    primitives = []
    for gate_type in sorted(by_type):
        entries = by_type[gate_type]
        primitives.append(
            model.unsupported_primitive(
                gate_type,
                entries[0]["reason"],
                count=len(entries),
                example_gates=[
                    model.gate_ref(
                        artifact_id, entry["gate_id"], entry["gate_name"],
                        gate_type=gate_type
                    )
                    for entry in entries[:4]
                ],
            )
        )

    return model.finding(
        "fault-campaign/coverage-gap",
        "{} sequential gate(s) could not be used as fault sites".format(len(skipped)),
        "unsupported",
        method_for(config, engine),
        model.scope(
            [artifact_id],
            description="sequential gates excluded from the campaign",
            gate_types=sorted(by_type),
        ),
        summary=(
            "These gates hold state but the injector could not attach to them, so the "
            "campaign says nothing about faults in them. Absence of a finding for a "
            "register listed here is absence of evidence, not evidence of absence."
        ),
        severity="medium",
        unsupported_dict=model.unsupported(
            "primitive",
            "the fault model needs a state output pin to insert the injection XOR on",
            primitives=primitives,
        ),
        tags=["fault-injection", "coverage-gap"],
    )


def build_document(artifact, config, engine, sites, faults, results, enumeration,
                   summary, skipped=(), analysis_extra=None, generated_at=None,
                   notes=()):
    """Assemble the complete findings document for one campaign run."""
    sites_by_name = {site["gate_name"]: site for site in sites}
    findings = [summary_finding(artifact["artifact_id"], config, engine, enumeration, summary)]

    gap = coverage_finding(artifact["artifact_id"], config, engine, list(skipped))
    if gap is not None:
        findings.append(gap)

    for fault in faults:
        result = results[fault["id"]]
        findings.append(
            fault_finding(
                artifact["artifact_id"],
                fault,
                sites_by_name[fault["site"]],
                result,
                config,
                engine,
            )
        )

    analysis = {
        "plugin": {
            "name": "netlist_simulator_controller",
            "version": (analysis_extra or {}).get("plugin_version", "unknown"),
            "description": (
                "simulation engine '{}' driven through NetlistSimulatorController".format(
                    engine
                )
            ),
        },
        "entry_point": "netlist_simulator_controller.NetlistSimulatorController.run_simulation",
        "configuration": config,
    }
    for key in ("hal", "started_at", "finished_at", "duration_s", "environment"):
        value = (analysis_extra or {}).get(key)
        if value is not None:
            analysis[key] = value

    return model.document(
        {"name": PRODUCER_NAME, "version": __version__},
        [artifact],
        analysis,
        findings,
        generated_at=generated_at,
        notes=list(notes) + list(LIMITATIONS),
    )
