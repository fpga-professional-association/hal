"""The campaign, executed inside HAL. One process, one netlist, N+2 simulations.

Order of business, and why each step is where it is:

1. **Load the netlist twice.**  One copy stays pristine, one is instrumented.
   The pristine copy exists for exactly one reason: to prove empirically that
   the instrumentation is transparent.  A campaign whose baseline already
   differs from the uninstrumented design is measuring its own scaffolding, so
   that check fails the run rather than annotating it.
2. **Discover and instrument the sites** (:mod:`instrument`).  Sequential gates
   that cannot host the model are collected, not skipped silently.
3. **Enumerate the faults** (:mod:`hal_fault_campaign.campaign`) -- unless the
   request carries an explicit list, which is what replay does.
4. **Simulate the baseline once** with every injection control at 0, then each
   fault with exactly one control pulsed.  Every run uses a freshly created
   controller: controllers accumulate input waveforms, so reusing one would
   leak stimulus from one fault into the next.
5. **Classify** (:mod:`hal_fault_campaign.classify`) -- a pure function of the
   recorded traces, which is what makes the verdicts re-checkable from the
   manifest without HAL.
6. **Write** the traces, the findings document (schema-validated *before* the
   result record is written) and finally the result.

Anything that goes wrong produces a result record with a typed error and a
nonzero exit code.  Never a partial success.
"""

import os
import shutil
import time
import traceback

from hal_findings import serialize as findings_serialize
from hal_findings import validate as findings_validate
from hal_findings.adapters.common import utc_now

from .. import campaign as campaign_module
from .. import classify as classify_module
from .. import findings as findings_module
from .. import protocol
from ..config import parse as parse_config
from ..workload import event_schedule, input_events, merge_event_maps
from . import instrument as instrument_module
from . import simulate as simulate_module

__all__ = ["StepError", "execute"]

#: One controller name for every run: LogManager::add_channel is keyed by name,
#: so a unique name per run would add a log channel per simulation.
CONTROLLER_NAME = "hal_fault_campaign"


class StepError(RuntimeError):
    """A failure of the campaign itself, with a typed kind for the result record."""

    def __init__(self, message, kind="plugin_error", detail=None):
        RuntimeError.__init__(self, message)
        self.kind = kind
        self.detail = detail


def _control_events(grid, control_net, cycle, hold_cycles, all_controls, total_cycles):
    """Input events that hold every control at 0 and pulse one of them.

    The zero at t=0 is not optional: an injection control is a *global input*
    with no driver, and an undriven input simulates as X, which would turn every
    instrumented register output into X instead of leaving it alone.

    The falling edge is dropped when the window reaches the end of the run --
    there is nothing left to observe after it, and an event at the very end of
    the simulated span is not a valid schedule point.
    """
    events = {0: {name: 0 for name in all_controls}}
    if control_net is None:
        return events
    start = grid.cycle_start(cycle)
    stop_cycle = cycle + hold_cycles
    events.setdefault(start, {})[control_net] = 1
    if stop_cycle < total_cycles:
        events.setdefault(grid.cycle_start(stop_cycle), {})[control_net] = 0
    return events


def _terminator_events(grid, events, total_cycles):
    """One guaranteed input transition at ``total_cycles * period``. Not cosmetic.

    ``SimulationThread::run`` replays the input waveform and calls the engine
    once per *distinct input transition time*, with the duration to the **next**
    one -- and the final batch is never flushed, so an event-driven engine
    simulates only up to the last-but-one input transition
    (``plugins/simulator/netlist_simulator_controller/src/simulation_thread.cpp``).
    Without a transition past the end of the workload, the last cycles of the run
    would simply not be simulated and every trace would silently flatten out at
    whatever value it happened to hold.

    So the schedule gets one extra guard cycle and one transition at the start of
    it.  Every driven net is written its *complement*, because
    ``WaveData::insertBooleanValueWithoutSync`` drops a write that does not change
    the value -- a terminator that re-wrote the same value would not exist in the
    waveform at all, and the problem would come back silently.  The values
    themselves are never observed: they land a full cycle after the last sample
    point, and they are the batch the engine drops.
    """
    final = {}
    for time in sorted(events):
        for net, value in events[time].items():
            final[net] = value
    if not final:
        raise StepError(
            "the workload drives no input net at all, so the run cannot be terminated "
            "with an input transition and its last cycles would not be simulated. Give "
            "the workload at least one stimulus assignment.",
            kind="invalid_input",
        )
    return {
        grid.cycle_start(total_cycles): {net: 1 - value for net, value in final.items()}
    }


def _run_dir(base, name, keep):
    path = os.path.join(base, name)
    if os.path.exists(path) and not keep:
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def _cleanup(path, keep):
    if not keep:
        shutil.rmtree(path, ignore_errors=True)


def _compare_baselines(pristine, instrumented, signals):
    """Every cycle where the instrumented baseline differs from the pristine one."""
    problems = []
    for signal in signals:
        left = pristine.get(signal)
        right = instrumented.get(signal)
        if left is None or right is None:
            problems.append(
                "{!r} is missing from the {} trace".format(
                    signal, "pristine" if left is None else "instrumented"
                )
            )
            continue
        for cycle, (a, b) in enumerate(zip(left, right)):
            if a != b:
                problems.append(
                    "{!r} differs at cycle {}: uninstrumented {}, instrumented {}".format(
                        signal, cycle, a, b
                    )
                )
                break
    return problems


def execute(request):  # noqa: C901 - a linear pipeline reads better in one place
    """Run one campaign request. Returns the process exit code."""
    started_at = utc_now()
    started = time.time()
    output_dir = request["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    result_path = os.path.join(output_dir, request.get("result_file", "result.json"))

    def fail(message, kind="plugin_error", detail=None):
        protocol.write_json(
            protocol.result(
                "error",
                error={"kind": kind, "message": message, "detail": detail},
                started_at=started_at,
                finished_at=utc_now(),
                duration_s=time.time() - started,
            ),
            result_path,
        )
        return 1

    try:
        config = parse_config(request["config"])
    except Exception as exc:  # noqa: BLE001
        return fail(
            "the campaign configuration in the request is invalid: {}".format(exc),
            kind="invalid_input",
            detail=traceback.format_exc(),
        )

    try:
        from hal_viz.halenv import (
            HalUnavailable,
            NetlistLoadError,
            import_hal_py,
            import_plugin,
            load_all_plugins,
            load_netlist,
        )
    except ImportError as exc:  # pragma: no cover - a broken checkout
        return fail(
            "could not import hal_viz.halenv from the tools directory: {}".format(exc),
            kind="internal",
            detail=traceback.format_exc(),
        )

    engine_name = request.get("engine") or config.engine
    keep_sims = bool(request.get("keep_simulation_dirs"))
    engine_timeout_s = float(
        request.get("engine_timeout_s") or simulate_module.DEFAULT_ENGINE_TIMEOUT_S
    )
    sim_root = os.path.join(output_dir, "sim")
    os.makedirs(sim_root, exist_ok=True)

    try:
        hal_py = import_hal_py()
        # --python-script hands control to the python shell before HAL loads its
        # plugins, so nothing is registered unless the step loads them itself.
        load_all_plugins(hal_py)
        import_plugin("netlist_simulator_controller")
        controller_plugin = hal_py.plugin_manager.get_plugin_instance(
            "netlist_simulator_controller"
        )
        if controller_plugin is None:
            raise StepError(
                "the netlist_simulator_controller plugin is not available in this HAL "
                "build; configure it with -DPL_SIMULATOR=ON or -DBUILD_ALL_PLUGINS=ON",
                kind="resource",
            )
        netlist = load_netlist(hal_py, request["netlist"], request.get("gate_library"))
        pristine = None
        if request.get("verify_instrumentation", True):
            pristine = load_netlist(hal_py, request["netlist"], request.get("gate_library"))
    except HalUnavailable as exc:
        return fail(str(exc), kind="resource", detail=traceback.format_exc())
    except NetlistLoadError as exc:
        return fail(str(exc), kind="invalid_input", detail=traceback.format_exc())
    except StepError as exc:
        return fail(str(exc), kind=exc.kind, detail=exc.detail)
    except Exception as exc:  # noqa: BLE001
        return fail(
            "could not set up the campaign: {}: {}".format(type(exc).__name__, exc),
            kind="internal",
            detail=traceback.format_exc(),
        )

    try:
        plugin_version = str(controller_plugin.get_version())
    except Exception:  # noqa: BLE001 - a missing version is not a failure
        plugin_version = "unknown"

    # Recorded before instrumentation: the artifact the findings are scoped to is the
    # netlist as loaded, not the one the campaign then edited.
    config.loaded_gate_count = len(netlist.get_gates())
    config.loaded_net_count = len(netlist.get_nets())

    grid = config.grid
    signals = config.recorded_signals
    stimulus_events = input_events(grid, config.stimulus)

    try:
        discovered, skipped = instrument_module.find_sites(hal_py, netlist)
        sites = campaign_module.select_sites(
            discovered, config.site_include, config.site_exclude
        )
        if not sites:
            raise StepError(
                "no fault site matched include={} exclude={}; the netlist offers {}".format(
                    config.site_include,
                    config.site_exclude,
                    ", ".join(sorted(site.gate_name for site in discovered)) or "none",
                ),
                kind="invalid_input",
            )
        instrumentation = instrument_module.instrument(hal_py, netlist, sites)
    except instrument_module.InstrumentationError as exc:
        return fail(str(exc), kind="invalid_input", detail=traceback.format_exc())
    except StepError as exc:
        return fail(str(exc), kind=exc.kind, detail=exc.detail)
    except Exception as exc:  # noqa: BLE001
        return fail(
            "instrumentation failed: {}: {}".format(type(exc).__name__, exc),
            kind="internal",
            detail=traceback.format_exc(),
        )

    sites_by_name = {site.gate_name: site for site in sites}
    control_nets = sorted(site.control_net for site in sites)

    explicit = request.get("faults")
    try:
        if explicit:
            faults = [campaign_module.fault_from_dict(entry) for entry in explicit]
            for fault in faults:
                if fault.site_name not in sites_by_name:
                    raise campaign_module.EnumerationError(
                        "the replayed fault {!r} names register {!r}, which this netlist "
                        "does not offer as a site".format(fault.fault_id, fault.site_name)
                    )
                fault.control_net = sites_by_name[fault.site_name].control_net
                fault.gate_id = sites_by_name[fault.site_name].gate_id
            enumeration = dict(request.get("enumeration") or {})
            enumeration.update(
                {
                    # A replay runs a named subset, so it is never an exhaustive sweep
                    # of the grid, whatever the campaign it replays was. Keeping the
                    # original mode here would let a three-fault replay inherit the
                    # exhaustive campaign's summary status.
                    "original_mode": enumeration.get("mode"),
                    "mode": "replay",
                    "selected": len(faults),
                    "grid_size": enumeration.get("grid_size", len(faults)),
                    "site_count": enumeration.get("site_count", len(sites)),
                    "cycle_count": enumeration.get(
                        "cycle_count", len({fault.cycle for fault in faults})
                    ),
                    "replayed": True,
                }
            )
        else:
            cycles = campaign_module.resolve_cycles(config.fault_cycles, config.cycles)
            faults, enumeration = campaign_module.enumerate_faults(
                sites, cycles, config.hold_cycles, config.sampling
            )
    except campaign_module.EnumerationError as exc:
        return fail(str(exc), kind="invalid_input", detail=traceback.format_exc())

    # One guard cycle past the workload, so that the last workload cycle is
    # actually simulated -- see _terminator_events.
    simulated_cycles = config.cycles + 1

    def simulate(netlist_object, events, run_name):
        driven = merge_event_maps(stimulus_events, events)
        schedule = event_schedule(
            grid,
            merge_event_maps(driven, _terminator_events(grid, driven, config.cycles)),
            simulated_cycles,
        )
        work_dir = _run_dir(sim_root, run_name, keep_sims)
        try:
            return simulate_module.run_trace(
                hal_py,
                controller_plugin,
                netlist_object,
                grid,
                config.cycles,
                schedule,
                config.clock_net,
                signals,
                engine_name,
                work_dir,
                CONTROLLER_NAME,
                engine_timeout_s=engine_timeout_s,
            )
        finally:
            _cleanup(work_dir, keep_sims)

    notes = []
    try:
        baseline = simulate(
            netlist,
            _control_events(grid, None, 0, 0, control_nets, config.cycles),
            "baseline",
        )
        if pristine is not None:
            reference = simulate(pristine, {}, "uninstrumented")
            problems = _compare_baselines(reference, baseline, signals)
            if problems:
                raise StepError(
                    "the instrumented netlist does not reproduce the uninstrumented "
                    "baseline, so no divergence could be attributed to an injection: "
                    + "; ".join(problems[:5]),
                    kind="internal",
                )
            notes.append(
                "The instrumented baseline was checked against the uninstrumented "
                "netlist over all {} observed signal(s) and {} cycle(s); they "
                "agree.".format(len(signals), config.cycles)
            )
        else:
            notes.append(
                "Instrumentation transparency was NOT verified in this run "
                "(verify_instrumentation was disabled)."
            )
    except simulate_module.SimulationError as exc:
        return fail(str(exc), kind="plugin_error", detail=traceback.format_exc())
    except StepError as exc:
        return fail(str(exc), kind=exc.kind, detail=exc.detail)
    except Exception as exc:  # noqa: BLE001
        return fail(
            "the fault-free baseline simulation failed: {}: {}".format(
                type(exc).__name__, exc
            ),
            kind="internal",
            detail=traceback.format_exc(),
        )

    traces = {"baseline": baseline, "faults": {}}
    results = {}
    try:
        for fault in faults:
            site = sites_by_name[fault.site_name]
            events = _control_events(
                grid,
                site.control_net,
                fault.cycle,
                fault.hold_cycles,
                control_nets,
                config.cycles,
            )
            trace = simulate(netlist, events, "fault_{}".format(fault.fault_id))
            traces["faults"][fault.fault_id] = trace
            results[fault.fault_id] = classify_module.classify(
                baseline,
                trace,
                fault.cycle,
                config.cycles,
                config.outputs,
                config.detection_signals,
                window=config.window,
                detection_active_value=config.detection_active_value,
            )
    except simulate_module.SimulationError as exc:
        return fail(str(exc), kind="plugin_error", detail=traceback.format_exc())
    except classify_module.ClassificationError as exc:
        return fail(str(exc), kind="invalid_input", detail=traceback.format_exc())
    except Exception as exc:  # noqa: BLE001
        return fail(
            "a faulty simulation failed: {}: {}".format(type(exc).__name__, exc),
            kind="internal",
            detail=traceback.format_exc(),
        )

    summary = classify_module.summarize(list(results.values()))
    finished_at = utc_now()
    duration = time.time() - started

    site_records = [site.as_dict() for site in sites]
    fault_records = [fault.as_dict() for fault in faults]

    artifact = _artifact(request, config, netlist)
    document = findings_module.build_document(
        artifact,
        config.resolved(),
        engine_name,
        site_records,
        fault_records,
        results,
        enumeration,
        summary,
        skipped=skipped,
        analysis_extra={
            "plugin_version": plugin_version,
            "hal": {"version": str(request.get("hal_version") or "unknown")},
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_s": round(duration, 3),
        },
        generated_at=finished_at,
        notes=notes,
    )

    try:
        findings_validate.validate_document(document)
    except findings_validate.FindingsValidationError as exc:
        return fail(
            "the campaign produced a findings document that does not validate: "
            "{}".format(exc),
            kind="internal",
            detail=traceback.format_exc(),
        )

    traces_name = request.get("traces_file", "traces.json")
    findings_name = request.get("findings_file", "findings.json")
    protocol.write_json(
        {
            "traces_version": "1.0.0",
            "clock": grid.as_dict(),
            "cycles": config.cycles,
            "signals": signals,
            "value_encoding": {"0": "ZERO", "1": "ONE", "-1": "X", "-2": "Z"},
            "baseline": traces["baseline"],
            "faults": traces["faults"],
        },
        os.path.join(output_dir, traces_name),
    )
    findings_serialize.write_document(document, os.path.join(output_dir, findings_name))

    protocol.write_json(
        protocol.result(
            "ok",
            artifacts=[
                {"path": findings_name, "role": "findings"},
                {"path": traces_name, "role": "traces"},
            ],
            metrics={
                "simulations": len(faults) + (2 if pristine is not None else 1),
                "faults": len(faults),
                "sites": len(sites),
                "skipped_sites": len(skipped),
            },
            started_at=started_at,
            finished_at=finished_at,
            duration_s=duration,
            notes=notes,
            summary=summary,
            sites=site_records,
            faults=[
                dict(record, **_verdict(results[record["id"]])) for record in fault_records
            ],
            enumeration=enumeration,
            instrumentation=dict(instrumentation, skipped=skipped),
            engine=engine_name,
        ),
        result_path,
    )
    return 0


def _verdict(result):
    return {
        "classification": result["classification"],
        "detection_latency_cycles": result["detection_latency_cycles"],
        "divergence_latency_cycles": result["divergence_latency_cycles"],
        "window": result["window"]["cycles"],
        "observed_cycles": result["window"]["observed_cycles"],
        "detection_cycles": result["detection_cycles"],
        "divergence_cycles": result["divergence_cycles"][:32],
        "indeterminate": result["indeterminate"],
    }


def _artifact(request, config, netlist):
    """The findings artifact record, pinned by the digest the *host* computed."""
    from hal_findings import model

    pin = request.get("pin") or {}
    library = None
    try:
        gate_library = netlist.get_gate_library()
        library = {"name": gate_library.get_name()}
        if request.get("gate_library"):
            library["path"] = os.path.basename(request["gate_library"])
        if pin.get("gate_library_sha256"):
            library["sha256"] = pin["gate_library_sha256"]
    except Exception:  # noqa: BLE001 - a missing library name is not fatal
        library = None

    kwargs = {
        "kind": pin.get("kind", "netlist"),
        "path": pin.get("path") or os.path.basename(request["netlist"]),
        "design_name": netlist.get_design_name(),
        "gate_count": config.loaded_gate_count,
        "net_count": config.loaded_net_count,
        "gate_library": library,
        "description": (
            "the netlist as loaded from the pinned input, before the campaign inserted "
            "its injection XOR gates and control inputs"
        ),
    }
    if pin.get("sha256"):
        kwargs["sha256"] = pin["sha256"]
        if pin.get("size_bytes") is not None:
            kwargs["size_bytes"] = pin["size_bytes"]
    else:
        kwargs["unhashed_reason"] = pin.get(
            "unhashed_reason", "the host did not pin this input by content"
        )
    return model.artifact(request.get("artifact_id", "netlist"), **kwargs)
