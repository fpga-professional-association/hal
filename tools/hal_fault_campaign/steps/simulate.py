"""Driving ``NetlistSimulatorController`` and reading a cycle-indexed trace back.

The controller's API is small and its lifecycle is easy to get subtly wrong, so
the exact sequence is spelled out here once:

1. ``create_simulator_controller(name, workdir)`` -- a *fresh* controller per
   run.  Controllers accumulate input waveforms; reusing one across the baseline
   and a faulty run would leak stimulus between them.
2. ``add_gates(netlist.get_gates())`` -- the simulation set.  The injector XORs
   are ordinary gates in the netlist and are added with everything else.
3. ``create_simulation_engine(name)`` -- ``hal_simulator`` is HAL's built-in
   event-driven engine (``NetlistSimulatorFactory``, registered by the
   ``netlist_simulator`` plugin) and runs in-process with no external tool;
   ``verilator`` is the other registered engine and shells out to the verilator
   binary, which is why the shipped C++ simulator tests need it and this tool
   does not default to it.
4. ``add_clock_period(clk, period)`` -- the clock starts low at t=0 and toggles
   every ``period/2`` (``WaveDataClock::dataFactory``), so the rising edge of
   cycle k is at ``k*P + P/2``; :mod:`hal_fault_campaign.workload` owns that
   arithmetic.
5. ``set_input(net, value)`` then ``simulate(duration_ps)``, repeatedly, to build
   the input waveform.
6. ``run_simulation()`` -- **asynchronous**.  ``SimulationEngineEventDriven::run``
   starts a ``std::thread`` and returns immediately, so the caller has to wait
   for ``engine.get_state()`` to leave ``Running`` (2=Preparing, 1=Running,
   0=Done, -1=Failed).  Polling with a deadline is the only option the bindings
   offer; a run that never leaves ``Running`` is a failure, not a hang.
7. ``get_results()`` -- pulls the waveforms out of the engine.  Without it
   ``get_waveform_by_net`` returns the *input* waveforms only.
8. ``get_waveform_by_net(net).get_value_at(t)`` -- sampled once per cycle at the
   cycle's sample point.

Values come back as ``BooleanFunction::Value``: 0, 1, -1 (X), -2 (Z).
"""

import os
import time

__all__ = [
    "SimulationError",
    "ENGINE_DONE",
    "ENGINE_FAILED",
    "ENGINE_RUNNING",
    "ENGINE_PREPARING",
    "available_engines",
    "run_trace",
]


class SimulationError(RuntimeError):
    """Raised when a simulation could not be set up, run or read back."""


ENGINE_DONE = 0
ENGINE_RUNNING = 1
ENGINE_PREPARING = 2
ENGINE_FAILED = -1

#: How long to wait for the engine thread before calling a run failed.
DEFAULT_ENGINE_TIMEOUT_S = 900.0
_POLL_INTERVAL_S = 0.02

#: The engine thread sets Done inside ``finalize()`` and only afterwards reports
#: back to the controller (``SimulationThread::terminateThread``). Waiting a beat
#: after Done rather than racing that hand-off costs nothing and avoids reading
#: -- or destroying -- the controller from under a thread that is still using it.
_ENGINE_HANDOFF_SETTLE_S = 0.05


def available_engines(controller):
    try:
        return list(controller.get_engine_names())
    except Exception:  # pragma: no cover - bindings without the getter
        return []


def _value(hal_py, integer):
    """Map 0/1 onto ``BooleanFunction.Value``; the MVP drives defined levels only."""
    if integer == 1:
        return hal_py.BooleanFunction.Value.ONE
    if integer == 0:
        return hal_py.BooleanFunction.Value.ZERO
    raise SimulationError(
        "only the values 0 and 1 can be driven onto an input, got {!r}".format(integer)
    )


def _net_by_name(netlist, name, cache):
    if name in cache:
        return cache[name]
    matches = netlist.get_nets(lambda net: net.get_name() == name)
    if not matches:
        raise SimulationError(
            "the netlist has no net named {!r}; check the workload, the observed "
            "outputs and the detection signals against the netlist".format(name)
        )
    if len(matches) > 1:
        raise SimulationError(
            "the netlist has {} nets named {!r}; a campaign addresses nets by name, so "
            "the name has to be unique".format(len(matches), name)
        )
    cache[name] = matches[0]
    return matches[0]


def _wait_for_engine(engine, timeout_s):
    deadline = time.time() + timeout_s
    while True:
        state = engine.get_state()
        if state in (ENGINE_DONE, ENGINE_FAILED):
            return state
        if time.time() > deadline:
            raise SimulationError(
                "the simulation engine was still in state {} after {:.0f}s; giving up "
                "rather than waiting forever".format(state, timeout_s)
            )
        time.sleep(_POLL_INTERVAL_S)


def run_trace(hal_py, controller_plugin, netlist, grid, cycles, schedule, clock_net,
              signals, engine_name, work_dir, run_name,
              engine_timeout_s=DEFAULT_ENGINE_TIMEOUT_S):
    """Simulate one run and return ``{signal: [value per cycle]}``.

    ``schedule`` is the ``[(time_ps, {net_name: value}, duration_ps)]`` sequence
    :func:`hal_fault_campaign.workload.event_schedule` produces; it already
    contains both the workload stimulus and the injection control pulses, so
    the baseline and a faulty run differ *only* in that list.
    """
    if work_dir:
        os.makedirs(work_dir, exist_ok=True)
    controller = controller_plugin.create_simulator_controller(run_name, work_dir or "")
    if controller is None:
        raise SimulationError(
            "create_simulator_controller() refused the working directory {!r}. "
            "NetlistSimulatorController::is_legal_directory_name rejects paths with "
            "spaces in them; point --output-dir somewhere without one.".format(work_dir)
        )

    cache = {}
    controller.add_gates(netlist.get_gates())

    engines = available_engines(controller)
    engine = controller.create_simulation_engine(engine_name)
    if engine is None:
        raise SimulationError(
            "no simulation engine named {!r} is registered; this build offers {}. "
            "'hal_simulator' comes from the netlist_simulator plugin and needs no "
            "external tool; 'verilator' needs the verilator binary.".format(
                engine_name, ", ".join(engines) or "none"
            )
        )

    clock = _net_by_name(netlist, clock_net, cache)
    controller.add_clock_period(clock, grid.period_ps)

    for _time, assignments, duration in schedule:
        for name in sorted(assignments):
            controller.set_input(
                _net_by_name(netlist, name, cache), _value(hal_py, assignments[name])
            )
        controller.simulate(duration)

    if not controller.run_simulation():
        raise SimulationError(
            "run_simulation() refused to start the {!r} engine; see the HAL log and "
            "{}".format(engine_name, controller.get_working_directory())
        )
    state = _wait_for_engine(engine, engine_timeout_s)
    time.sleep(_ENGINE_HANDOFF_SETTLE_S)
    if state == ENGINE_FAILED:
        raise SimulationError(
            "the {!r} engine failed; its working directory is {}".format(
                engine_name, engine.get_working_directory()
            )
        )
    if not controller.get_results():
        raise SimulationError(
            "get_results() could not read the simulation back from the {!r} "
            "engine".format(engine_name)
        )

    trace = {}
    for name in signals:
        net = _net_by_name(netlist, name, cache)
        waveform = controller.get_waveform_by_net(net)
        if waveform is None:
            raise SimulationError(
                "no waveform was recorded for {!r}; it is in the netlist but not in the "
                "simulation set".format(name)
            )
        trace[name] = [
            int(waveform.get_value_at(grid.sample_time(cycle))) for cycle in range(cycles)
        ]
    return trace
