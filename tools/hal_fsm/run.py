"""The orchestration that runs inside HAL.

This is the only place where the pieces meet: extract the flip-flop dependency
graph, propose candidates (or take the user's), hand each selected candidate to
``solve_fsm``, check what came back, and write a findings document plus the
artifacts a human can open.

Two rules shape the control flow:

* **A failure never removes what was already earned.**  If the solver fails on
  a candidate, the structural findings -- candidates, their scores, the
  asynchronous-control gap -- are still written, and the failure becomes its own
  ``error``/``timeout`` finding.  That is the "partial recovery" this analysis
  can honestly offer: ``solve_fsm`` has no partial transition graph to give.
* **Every check we can run, we run, and its result is a finding.**  The
  determinism/totality check, the cone-hole check and the explored-set
  cross-check exist because a recovered relation that nobody questioned is
  indistinguishable from a wrong one.
"""

import os
import sys
import time
import traceback

if __package__ in (None, ""):  # pragma: no cover - direct execution
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_findings import model as findings_model
from hal_findings import serialize as findings_serialize
from hal_findings import validate as findings_validate
from hal_findings.adapters.common import netlist_artifact, utc_now
from hal_runner import protocol

from . import candidates as candidates_module, extract as extract_module
from . import diagram, findings as findings_module, reference as reference_module
from . import solve as solve_module
from . import transitions as transitions_module
from .config import Configuration, from_dict as config_from_dict
from .findings import MachineResult, assumption

__all__ = ["RunError", "execute", "analyse"]

ANALYSIS_NAME = "solve_fsm.discover"


class RunError(RuntimeError):
    def __init__(self, message, kind="internal", detail=None):
        RuntimeError.__init__(self, message)
        self.kind = kind
        self.detail = detail


# ---------------------------------------------------------------------------
# assumptions
# ---------------------------------------------------------------------------


def _machine_assumptions(extraction, candidate, outcome, configuration, external_state=()):
    """Every assumption the transition relation rests on, discharged or not."""
    graph = extraction.graph
    order = outcome.state_register_order
    members = [graph.gates[gate_id] for gate_id in order]

    assumptions = []
    if candidate.origin == "user_override":
        assumptions.append(
            assumption(
                "state-register",
                "the state register was named in the configuration file: {}".format(
                    ", ".join(gate.name for gate in members)
                ),
                "user_provided",
                discharged=False,
            )
        )
    else:
        assumptions.append(
            assumption(
                "state-register",
                "these {} flip-flops are the state register: {}. This is a structural "
                "heuristic scoring {:.2f} (see fsm/candidate/*), not a proven "
                "decomposition of the design.".format(
                    len(members),
                    ", ".join(gate.name for gate in members),
                    candidate.score,
                ),
                "structural",
                discharged=False,
            )
        )

    assumptions.append(
        assumption(
            "state-bit-order",
            "state bit i is the output of {} -- state values in this document mean "
            "nothing under any other order".format(
                "[" + ", ".join(gate.name for gate in members) + "]"
            ),
            "tool",
            discharged=True,
        )
    )

    holes = outcome.holes
    assumptions.append(
        assumption(
            "transition-cone-complete",
            "the combinational gates handed to solve_fsm close the cone between the "
            "state flip-flops; any net left as a free variable really is an input. "
            + (
                "Checked: every free variable in the recovered conditions is a primary "
                "input or another register's output."
                if not holes
                else "NOT satisfied: {} free variable(s) are driven by combinational "
                "logic outside the cone.".format(len(holes))
            ),
            "structural",
            discharged=not holes,
        )
    )

    driven_control = [
        entry
        for gate in members
        for entry in gate.control
        if entry["pin_type"] in ("set", "reset") and entry.get("constant") is None
    ]
    inactive_control = [
        entry
        for gate in members
        for entry in gate.control
        if entry["pin_type"] in ("set", "reset")
    ]
    assumptions.append(
        assumption(
            "asynchronous-control-inactive",
            "asynchronous set/reset inputs of the state register stay inactive. "
            "solve_fsm models only the gate library's next_state expression, so these "
            "pins never appear in a recovered condition. "
            + (
                "No state flip-flop has such a pin."
                if not inactive_control
                else (
                    "All {} such pin(s) are tied to a constant driver, so the assumption "
                    "holds structurally.".format(len(inactive_control))
                    if not driven_control
                    else "{} of {} such pin(s) are driven by logic, so the assumption is "
                    "NOT discharged: the recovered graph describes the machine only while "
                    "they are inactive.".format(len(driven_control), len(inactive_control))
                )
            ),
            "structural",
            discharged=not driven_control,
        )
    )

    clocks = set()
    for gate in members:
        clocks |= set(gate.clock_nets)
    assumptions.append(
        assumption(
            "single-clock-domain",
            "all state flip-flops are clocked by the same net; solve_fsm has no notion "
            "of clock domains and would silently mix them. "
            + (
                "Checked: {} clock net(s).".format(len(clocks))
                if len(clocks) == 1
                else "NOT satisfied: {} distinct clock net(s).".format(len(clocks))
            ),
            "structural",
            discharged=len(clocks) == 1,
        )
    )

    assumptions.append(
        assumption(
            "initial-state",
            "the machine starts in state {} ({}). Source: {}.".format(
                outcome.table.initial_state if outcome.table else 0,
                transitions_module.format_state(
                    outcome.table.initial_state if outcome.table else 0, len(order), 2
                ),
                {
                    "from_init_attribute": "the INIT attribute of every state flip-flop",
                    "zero": "assumed all-zero (solve_fsm's own default)",
                    "explicit": "given in the configuration file",
                }.get(outcome.initial_state_source, outcome.initial_state_source),
            ),
            "initial_state",
            discharged=bool(outcome.initial_state_discharged),
        )
    )

    assumptions.append(
        assumption(
            "closed-machine",
            "every condition is over primary inputs of this machine. "
            + (
                "Checked: no recovered condition reads a net driven by a flip-flop "
                "outside the state register."
                if not external_state
                else "NOT satisfied: {} condition variable(s) are driven by flip-flops "
                "outside the state register ({}), so the graph describes this machine "
                "*given* those signals.".format(
                    len(external_state),
                    ", ".join(
                        sorted({entry.get("net_name") or "?" for entry in external_state})[:6]
                    ),
                )
            ),
            "structural",
            discharged=not external_state,
        )
    )

    assumptions.append(
        assumption(
            "library-next-state",
            "the gate library's next_state expression is a faithful model of the state "
            "flip-flops, and the netlist is X-free: solve_fsm evaluates that expression "
            "symbolically and treats an X result as a failure rather than a state.",
            "library",
            discharged=False,
        )
    )
    return assumptions


# ---------------------------------------------------------------------------
# per-candidate work
# ---------------------------------------------------------------------------


def _dataflow_groups(netlist, notes):
    """DANA register groups, when the plugin is there.  Never fatal."""
    try:
        from hal_viz.halenv import import_plugin

        dataflow = import_plugin("dataflow")
    except Exception as exc:  # noqa: BLE001 - an optional input
        notes.append(
            "the dataflow (DANA) plugin is not available, so its register groups were "
            "not used as a candidate source: {}".format(exc)
        )
        return None
    try:
        configuration = dataflow.Configuration(netlist).with_flip_flops()
        result = dataflow.analyze(configuration)
        if result is None:
            notes.append("dataflow.analyze() returned None; its groups were not used")
            return None
        groups = result.get_groups() or {}
        return {
            int(group_id): sorted(gate.get_id() for gate in gates)
            for group_id, gates in groups.items()
        }
    except Exception as exc:  # noqa: BLE001
        notes.append("dataflow analysis failed and was ignored: {}".format(exc))
        return None


def _witness_for(table, target, evaluate, extraction, member_ids, limits, complete):
    """One witness entry for the findings, in every outcome the search can have."""
    reachable, _ = transitions_module.reachable_states(table)
    if target not in set(reachable):
        if complete:
            return {"target": target, "status": "unreachable"}
        return {
            "target": target,
            "status": "unknown",
            "max_cycles": limits.max_cycles,
            "reason": (
                "state {} is not in the recovered relation, and the recovery was "
                "incomplete, so nothing follows about its reachability".format(target)
            ),
        }
    path, depth = transitions_module.shortest_path(
        table, target, max_cycles=limits.max_cycles
    )
    if path is None:
        return {
            "target": target,
            "status": "unknown",
            "max_cycles": limits.max_cycles,
            "reason": (
                "no path of at most {} transitions reaches state {}".format(
                    limits.max_cycles, target
                )
            ),
        }
    try:
        steps = transitions_module.build_witness(
            table, path, evaluate, max_condition_vars=limits.max_condition_vars
        )
    except transitions_module.WitnessError as exc:
        return {
            "target": target,
            "status": "unknown",
            "max_cycles": limits.max_cycles,
            "reason": str(exc),
        }
    external = sorted(
        {
            table.signals.get(name, {}).get("name", name)
            for step in steps
            for name in step["inputs"]
            if table.signals.get(name, {}).get("role") == "external_state"
        }
    )
    return {
        "target": target,
        "status": "found",
        "path": path,
        "steps": steps,
        "depth": depth,
        "external_state_inputs": external,
    }


def _default_targets(table, max_cycles):
    """Without configured targets, witness the state that is hardest to reach."""
    best_state, best_depth = None, -1
    for state in table.states:
        path, _ = transitions_module.shortest_path(table, state, max_cycles=max_cycles)
        if path is None:
            continue
        if len(path) - 1 > best_depth:
            best_state, best_depth = state, len(path) - 1
    return [best_state] if best_state is not None and best_depth > 0 else []


def _solve_candidate(
    hal_py,
    solve_fsm_module,
    netlist,
    extraction,
    candidate,
    configuration,
    machine_id,
    output_dir,
    reference,
    transition_logic_ids=None,
):
    """Run and check one candidate.  Always returns a :class:`MachineResult`."""
    limits = configuration.limits
    evidence = []
    notes = []

    try:
        outcome = solve_module.solve(
            hal_py,
            solve_fsm_module,
            netlist,
            extraction,
            candidate,
            configuration,
            output_dir=output_dir,
            transition_logic_ids=transition_logic_ids,
        )
    except solve_module.SolveError as exc:
        return (
            MachineResult(
                candidate,
                machine_id,
                outcome="unsupported" if exc.kind == "resource" else "error",
                unsupported={"kind": "scale", "reason": str(exc)}
                if exc.kind == "resource"
                else None,
                error={"kind": exc.kind, "message": str(exc), "detail": exc.detail},
                cone={"unmodelled_control": _driven_control(extraction, candidate)},
                notes=[str(exc)],
            ),
            [],
        )

    notes.extend(outcome.notes)
    cone_info = {
        "gates": len(outcome.transition_logic_ids),
        "holes": outcome.holes,
        "unmodelled_control": _driven_control(extraction, candidate),
        "attempts": outcome.attempts,
    }

    if outcome.table is None:
        return (
            MachineResult(
                candidate,
                machine_id,
                outcome="timeout" if outcome.timed_out else "error",
                error=outcome.error,
                limits={
                    "timeout_s": limits.smt_timeout_s,
                    "wall_time_s": round(outcome.wall_time_s, 3),
                    "hit": outcome.timed_out,
                    "description": "solve_fsm returns no partial graph; nothing was "
                    "recovered",
                }
                if outcome.timed_out
                else None,
                cone=cone_info,
                notes=notes,
            ),
            [],
        )

    table = outcome.table
    cone_info["external_state"] = solve_module.external_state_signals(table, extraction)
    assumptions = _machine_assumptions(
        extraction,
        candidate,
        outcome,
        configuration,
        external_state=cone_info["external_state"],
    )
    evaluate = solve_module.make_evaluator(hal_py, outcome.functions)

    determinism = transitions_module.check_determinism(
        table, evaluate, max_condition_vars=limits.max_condition_vars
    )

    matches, only_explored, only_reachable = transitions_module.explored_set_matches(table)
    reachable, truncated = transitions_module.reachable_states(
        table, max_states=limits.max_states
    )
    reachability = {
        "reachable": reachable,
        "reachable_count": len(reachable),
        "truncated": truncated,
        "explored_mismatch": None if matches or table.solver == "brute_force" else {
            "only_explored_by_solver": only_explored,
            "only_reachable_in_relation": only_reachable,
            "note": (
                "solve_fsm explored forward from the initial state it was given; a "
                "difference means it started somewhere else -- most likely the "
                "initial_state encoding described in "
                "hal_fsm.transitions.initial_state_argument"
            ),
        },
        "initial_state_encoding": outcome.initial_state_encoding,
    }
    if table.solver == "brute_force":
        reachability["unreachable_in_encoding"] = sorted(
            set(range(1 << table.width)) - set(reachable)
        )

    targets = configuration.targets or _default_targets(table, limits.max_cycles)
    witnesses = [
        _witness_for(
            table,
            int(target),
            evaluate,
            extraction,
            candidate.gate_ids,
            limits,
            table.complete and (matches or table.solver == "brute_force"),
        )
        for target in targets
    ]

    if outcome.dot_path:
        evidence.append(
            {
                "kind": "dot",
                "description": "the state transition graph solve_fsm wrote itself",
                "path": os.path.basename(outcome.dot_path),
            }
        )

    artifacts = []
    if outcome.dot_path:
        artifacts.append(
            {
                "path": os.path.basename(outcome.dot_path),
                "role": "dot",
                "description": "solve_fsm's own state transition graph",
            }
        )

    table_path = os.path.join(output_dir, "transitions-{}.json".format(machine_id))
    protocol.write_json(table.to_json(), table_path)
    artifacts.append(
        {
            "path": os.path.basename(table_path),
            "role": "report",
            "description": "the recovered transition table with its state bit order",
        }
    )
    evidence.append(
        {
            "kind": "report",
            "description": "the recovered transition table, machine readable",
            "path": os.path.basename(table_path),
        }
    )

    if configuration.emit_diagram:
        witness_path = None
        for witness in witnesses:
            if witness.get("status") == "found":
                witness_path = witness["path"]
                break
        diagram_path = os.path.join(output_dir, "state-diagram-{}.dot".format(machine_id))
        diagram.write_state_diagram(
            table,
            diagram_path,
            title="{} ({})".format(machine_id, table.solver),
            base=configuration.diagram_base,
            max_states=limits.max_diagram_states,
            witness_path=witness_path,
        )
        artifacts.append(
            {
                "path": os.path.basename(diagram_path),
                "role": "dot",
                "description": "state diagram scoped to at most {} states, witness path "
                "highlighted".format(limits.max_diagram_states),
            }
        )
        evidence.append(
            {
                "kind": "dot",
                "description": "state diagram with the initial state, reachability and "
                "the witness path marked",
                "path": os.path.basename(diagram_path),
            }
        )

    comparison = None
    if reference is not None:
        names = [extraction.graph.gates[gate_id].name for gate_id in outcome.state_register_order]
        machine = reference.for_register(names)
        if machine is None:
            notes.append(
                "the reference file describes no machine with the state register {}; no "
                "comparison was made".format(names)
            )
        else:
            comparison = machine.compare(table, restrict_to_reachable=table.solver == "smt")

    return MachineResult(
        candidate,
        machine_id,
        outcome="solved",
        table=table,
        assumptions=assumptions,
        solver={
            "name": "solve_fsm ({})".format(table.solver),
            "wall_time_s": round(outcome.wall_time_s, 3),
            "options": None,
        },
        limits={
            "timeout_s": limits.smt_timeout_s,
            "wall_time_s": round(outcome.wall_time_s, 3),
            "cycle_limit": limits.max_cycles,
            "hit": not table.complete,
        },
        cone=cone_info,
        determinism=determinism,
        reachability=reachability,
        witnesses=witnesses,
        comparison=comparison,
        evidence=[findings_model.evidence(**entry) for entry in evidence],
        metrics={
            "states": len(table.states),
            "transitions": len(table.transitions),
            "state_bits": table.width,
        },
        notes=notes,
    ), artifacts


def _driven_control(extraction, candidate):
    """Set/reset pins of the candidate that are driven by logic, not tied off."""
    gaps = []
    for gate_id in candidate.gate_ids:
        gate = extraction.graph.gates[gate_id]
        for entry in gate.control:
            if entry["pin_type"] not in ("set", "reset"):
                continue
            if entry.get("constant") is not None:
                continue
            record = dict(entry)
            record["gate_id"] = gate.id
            record["gate_name"] = gate.name
            record["gate_type"] = gate.type
            gaps.append(record)
    return gaps


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


def analyse(hal_py, solve_fsm_module, netlist, configuration, output_dir, netlist_path=None,
            artifact_id="netlist", plugin_version="unknown", hal_version=None,
            producer_command=None):
    """Run the whole workflow.  Returns ``(document, artifacts, metrics, notes)``."""
    started = time.time()
    notes = []
    os.makedirs(output_dir, exist_ok=True)

    extraction = extract_module.extract(netlist)
    notes.extend(extraction.notes)
    graph = extraction.graph

    if not graph.gates:
        notes.append(
            "the netlist contains no sequential gates; there is no state register to "
            "propose"
        )

    reference = None
    if configuration.reference:
        reference = reference_module.load(configuration.reference)

    override_ids = []
    if configuration.override.active:
        override_ids, problems = extract_module.resolve_gates(
            extraction, configuration.override.state_registers
        )
        if problems:
            raise RunError(
                "the state_registers override could not be resolved: "
                + "; ".join(problems),
                kind="invalid_input",
            )
        missing = [gate_id for gate_id in override_ids if gate_id not in graph.gates]
        if missing:
            raise RunError(
                "the state_registers override names gate(s) that are not sequential: "
                "{}".format(missing),
                kind="invalid_input",
            )

    transition_logic_ids = None
    if configuration.override.transition_logic:
        transition_logic_ids, problems = extract_module.resolve_gates(
            extraction, configuration.override.transition_logic
        )
        if problems:
            raise RunError(
                "the transition_logic override could not be resolved: "
                + "; ".join(problems),
                kind="invalid_input",
            )

    exclude_ids = set()
    if configuration.override.exclude_gates:
        exclude_ids, problems = extract_module.resolve_gates(
            extraction, configuration.override.exclude_gates
        )
        exclude_ids = set(exclude_ids)
        notes.extend(problems)

    if override_ids:
        proposed = [
            candidates_module.Candidate(
                override_ids, sources=("configuration",), origin="user_override"
            )
        ]
        candidate_notes = [
            "the state register was taken from the configuration file; no candidate "
            "proposal was run"
        ]
        ambiguous = []
    else:
        groups = _dataflow_groups(netlist, notes)
        proposed, candidate_notes = candidates_module.propose(
            graph, dataflow_groups=groups, limits=configuration.limits
        )
        if exclude_ids:
            proposed = [
                candidate
                for candidate in proposed
                if not (set(candidate.gate_ids) & exclude_ids)
            ]
        ambiguous = candidates_module.ambiguous_group(
            proposed, configuration.ambiguity_margin
        )
    notes.extend(candidate_notes)

    # A user override is never filtered out: if it is too wide or too weak, the
    # run must say so as a finding rather than quietly do nothing with it.
    solvable = [
        candidate
        for candidate in proposed
        if candidate.origin == "user_override"
        or (
            candidate.size <= configuration.limits.max_state_bits
            and candidate.score >= configuration.min_confidence
        )
    ]
    if configuration.solve == "none":
        selected = []
        notes.append("solve is 'none'; candidates were proposed but not solved")
    elif configuration.solve == "all":
        selected = solvable[: configuration.limits.max_solved_candidates]
    else:
        selected = solvable[:1]
    if configuration.solve != "none" and not selected and proposed:
        notes.append(
            "no candidate was solved: every proposal is either above max_state_bits or "
            "below min_confidence"
        )

    results = []
    artifacts = []
    for index, candidate in enumerate(selected, start=1):
        machine_id = "machine{:02d}".format(index)
        result, produced_artifacts = _solve_candidate(
            hal_py,
            solve_fsm_module,
            netlist,
            extraction,
            candidate,
            configuration,
            machine_id,
            output_dir,
            reference,
            transition_logic_ids=(
                transition_logic_ids if candidate.origin == "user_override" else None
            ),
        )
        artifacts.extend(produced_artifacts)
        results.append(result)
        notes.extend(result.notes)

    artifact = netlist_artifact(netlist, artifact_id, path=netlist_path)
    document = findings_module.build_document(
        artifact,
        graph,
        proposed,
        results,
        configuration=configuration,
        ambiguous=ambiguous,
        plugin_version=plugin_version,
        hal_version=hal_version,
        notes=notes,
        producer_command=producer_command,
        duration_s=time.time() - started,
    )

    metrics = {
        "sequential_gates": len(graph.gates),
        "candidates": len(proposed),
        "solved": sum(1 for result in results if result.outcome == "solved"),
        "failed": sum(1 for result in results if result.outcome != "solved"),
        "states": sum(
            len(result.table.states) for result in results if result.table is not None
        ),
    }
    return document, artifacts, metrics, notes


def execute(request):
    """Run one request inside HAL.  Returns the process exit code."""
    started_at = utc_now()
    started = time.time()
    output_dir = request["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    result_path = os.path.join(output_dir, request.get("result_file", "result.json"))
    findings_name = request.get("findings_file", "findings.json")

    def fail(message, kind="plugin_error", detail=None):
        protocol.write_json(
            protocol.result(
                "error",
                ANALYSIS_NAME,
                error={"kind": kind, "message": message, "detail": detail},
                started_at=started_at,
                finished_at=utc_now(),
                duration_s=time.time() - started,
            ),
            result_path,
        )
        return 1

    try:
        configuration = (
            config_from_dict(request["config"], source_path=request.get("config_path"))
            if request.get("config")
            else Configuration()
        )
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc), kind="invalid_input", detail=traceback.format_exc())

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

    try:
        hal_py = import_hal_py()
        # --python-script takes over before HAL loads plugins: without this there
        # is no verilog parser and no solve_fsm.
        load_all_plugins(hal_py)
        solve_fsm_module = import_plugin("solve_fsm")
        netlist = load_netlist(hal_py, request["netlist"], request.get("gate_library"))
    except HalUnavailable as exc:
        return fail(str(exc), kind="resource", detail=traceback.format_exc())
    except NetlistLoadError as exc:
        return fail(str(exc), kind="invalid_input", detail=traceback.format_exc())
    except Exception as exc:  # noqa: BLE001
        return fail(
            "could not set up the analysis: {}: {}".format(type(exc).__name__, exc),
            kind="internal",
            detail=traceback.format_exc(),
        )

    plugin_version = "unknown"
    try:
        instance = hal_py.plugin_manager.get_plugin_instance("solve_fsm")
        if instance is not None:
            plugin_version = str(instance.get_version() or "unknown")
    except Exception:  # noqa: BLE001 - 'unknown' is an honest answer
        plugin_version = "unknown"

    try:
        document, artifacts, metrics, notes = analyse(
            hal_py,
            solve_fsm_module,
            netlist,
            configuration,
            output_dir,
            netlist_path=request["netlist"],
            artifact_id=request.get("artifact_id", "netlist"),
            plugin_version=plugin_version,
            hal_version=request.get("hal_version"),
            producer_command=request.get("command"),
        )
    except RunError as exc:
        return fail(str(exc), kind=exc.kind, detail=exc.detail)
    except Exception as exc:  # noqa: BLE001
        return fail(
            "hal_fsm raised {}: {}".format(type(exc).__name__, exc),
            kind="exception",
            detail=traceback.format_exc(),
        )

    finished_at = utc_now()
    duration = time.time() - started
    analysis = document.setdefault("analysis", {})
    analysis["started_at"] = started_at
    analysis["finished_at"] = finished_at
    analysis["duration_s"] = round(duration, 3)

    try:
        findings_validate.validate_document(document)
    except findings_validate.FindingsValidationError as exc:
        return fail(
            "hal_fsm produced a findings document that does not validate",
            kind="internal",
            detail="\n".join(exc.errors[:20]),
        )

    findings_path = os.path.join(output_dir, findings_name)
    try:
        findings_serialize.write_document(document, findings_path)
    except OSError as exc:
        return fail("could not write {}: {}".format(findings_path, exc), kind="io")

    protocol.write_json(
        protocol.result(
            "ok",
            ANALYSIS_NAME,
            artifacts=[
                {
                    "path": findings_name,
                    "role": "findings",
                    "description": "findings document written through hal_findings",
                }
            ]
            + artifacts,
            metrics=metrics,
            started_at=started_at,
            finished_at=finished_at,
            duration_s=duration,
            notes=notes,
        ),
        result_path,
    )
    return 0
