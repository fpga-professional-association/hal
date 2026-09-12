#!/usr/bin/env python3
"""Reproduce every artifact and image used by ``guide.html``.

Run it from a checkout, inside a container that has a built HAL:

    HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib \\
    PYTHONPATH=/work/build/lib \\
    python3 examples/agilex3_walkthroughs/02_traffic_fsm/run_analysis.py

Everything it writes lands in ``images/`` and ``artifacts/`` next to this file,
and the whole transcript is kept in ``artifacts/transcript.txt``.  The steps are
numbered the way the guide numbers them.

The analysis runs on ``netlist_anon.hal.v`` -- the blinded netlist -- so nothing
in the result can have come from an identifier Quartus happened to keep.  The
one exception is the final un-blinding step, which is the point.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TOOLS = ROOT / "tools"
GATE_LIBRARY = ROOT / "plugins" / "gate_libraries" / "definitions" / "AGILEX_TENNM.hgl"
NETLIST = HERE / "netlist_anon.hal.v"
EXPORT = HERE / "traffic_fsm.vo"
IMAGES = HERE / "images"
ARTIFACTS = HERE / "artifacts"

sys.path.insert(0, str(TOOLS))

LOG: list[str] = []
#: set once hal_py is imported; used by reaches_primary_output()
_SEQUENTIAL = None


def say(text: str = "") -> None:
    print(text)
    LOG.append(text)


def banner(title: str) -> None:
    say("")
    say("=" * 78)
    say(title)
    say("=" * 78)


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    say("$ " + " ".join(str(c) for c in cmd))
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          cwd=str(ROOT))
    for line in (proc.stdout or "").splitlines():
        say("  " + line)
    if proc.returncode != 0:
        for line in (proc.stderr or "").splitlines()[-25:]:
            say("  ! " + line)
        say("  ! exit code {}".format(proc.returncode))
    return proc


def render_dot(dot_path: Path, svg_path: Path, engine: str = "dot") -> bool:
    binary = shutil.which("dot")
    if binary is None:
        say("  ! graphviz 'dot' not found; {} was not rendered".format(dot_path.name))
        return False
    proc = subprocess.run([binary, "-K" + engine, "-Tsvg", str(dot_path),
                           "-o", str(svg_path)], capture_output=True, text=True)
    if proc.returncode != 0:
        say("  ! dot failed on {}: {}".format(dot_path.name, proc.stderr.strip()[:200]))
        return False
    say("  -> {}".format(svg_path.relative_to(HERE)))
    return True


# ===========================================================================
# step 1 -- first contact
# ===========================================================================


def step1_first_contact(hal_py, netlist):
    banner("STEP 1  first contact -- what the netlist is made of")
    types: dict[str, int] = {}
    for gate in netlist.get_gates():
        name = gate.get_type().get_name()
        types[name] = types.get(name, 0) + 1
    say("design name        : {}".format(netlist.get_design_name()))
    say("gate library       : {}".format(netlist.get_gate_library().get_name()))
    say("gates              : {}".format(len(netlist.get_gates())))
    say("nets               : {}".format(len(netlist.get_nets())))
    say("modules            : {}".format(len(netlist.get_modules())))
    say("top module         : {}".format(netlist.get_top_module().get_name()))
    say("")
    say("gate types:")
    for name in sorted(types):
        say("  {:<20} {}".format(name, types[name]))
    say("")
    inputs = sorted(n.get_name() for n in netlist.get_nets() if n.is_global_input_net())
    outputs = sorted(n.get_name() for n in netlist.get_nets() if n.is_global_output_net())
    say("global input nets  : {}".format(", ".join(inputs) or "(none)"))
    say("global output nets : {}".format(", ".join(outputs) or "(none)"))
    return inputs, outputs


# ===========================================================================
# step 2 -- clocks, resets, enables
# ===========================================================================


def step2_sequential(hal_py, netlist):
    banner("STEP 2  the sequential skeleton -- flip-flops, clocks, resets, enables")
    rows = []
    for gate in sorted(netlist.get_gates(), key=lambda g: g.get_name()):
        if not gate.get_type().has_property(hal_py.GateTypeProperty.ff):
            continue
        pins = {}
        for pin in ("clk", "d", "ena", "clrn"):
            net = gate.get_fan_in_net(pin)
            pins[pin] = net.get_name() if net is not None else "-"
        out = gate.get_fan_out_net("q")
        rows.append((gate.get_name(), pins["clk"], pins["clrn"], pins["ena"],
                     pins["d"], out.get_name() if out else "-"))
    say("{:<6} {:<6} {:<6} {:<6} {:<6} {}".format(
        "gate", "clk", "clrn", "ena", "d", "q"))
    for row in rows:
        say("{:<6} {:<6} {:<6} {:<6} {:<6} {}".format(*row))
    say("")
    say("distinct clock nets  : {}".format(sorted({r[1] for r in rows})))
    say("distinct clrn nets   : {}".format(sorted({r[2] for r in rows})))
    say("distinct enable nets : {}".format(sorted({r[3] for r in rows})))
    say("")
    say("One clock, one asynchronous clear, two enable nets. The enable split is")
    say("the first real structural signal in the design.")
    return rows


# ===========================================================================
# step 3 -- split the flip-flops into registers
# ===========================================================================


def reaches_primary_output(netlist, gate_name):
    """True when this flip-flop's output reaches a top-level output pin.

    Forward walk through combinational gates only: a sequential gate ends the
    path, because whatever it drives is that register's business, not this one's.
    """
    start = next((g for g in netlist.get_gates() if g.get_name() == gate_name), None)
    if start is None:
        return False
    seen = set()
    stack = list(start.get_fan_out_nets())
    while stack:
        net = stack.pop()
        if net.get_id() in seen:
            continue
        seen.add(net.get_id())
        if net.is_global_output_net():
            return True
        for endpoint in net.get_destinations():
            gate = endpoint.get_gate()
            if gate.get_type().has_property(_SEQUENTIAL):
                continue
            stack.extend(gate.get_fan_out_nets())
    return False


def step3_dependency_graph(netlist):
    banner("STEP 3  flip-flop dependency graph -- and why the SCC is not the answer")
    from hal_fsm import candidates as candidates_module
    from hal_fsm import extract as extract_module

    extraction = extract_module.extract(netlist)
    graph = extraction.graph
    names = {gid: graph.gates[gid].name for gid in graph.ids()}

    say("dependency edges (the next value of A reads the current value of B):")
    for gid in graph.ids():
        deps = sorted(names[d] for d in graph.depends[gid])
        say("  {:<4} <- {}".format(names[gid], ", ".join(deps) or "(nothing)"))

    components = candidates_module.strongly_connected_components(
        graph.ids(), graph.depends)
    say("")
    say("strongly connected components:")
    for scc in components:
        mark = "   <-- non-trivial" if len(scc) > 1 else ""
        say("  {{{}}}{}".format(", ".join(names[g] for g in scc), mark))
    say("")
    say("The single non-trivial SCC covers every flip-flop in the design. The")
    say("counter's next value reads the state register (its restart condition is")
    say("phase dependent) and the state register's next value reads the counter,")
    say("so 'take the SCC' gives one 8-bit blob, not a 4-bit controller.")

    # ---- split by clock enable -------------------------------------------
    enable_of = {}
    for gid in graph.ids():
        gate = graph.gates[gid]
        enables = [c["net_name"] for c in gate.control if c.get("pin") == "ena"]
        enable_of[gid] = enables[0] if enables else "(none)"
    by_enable: dict[str, list[int]] = {}
    for gid in graph.ids():
        by_enable.setdefault(enable_of[gid], []).append(gid)
    say("")
    say("signal 1 -- partition by the net on the ena pin:")
    for net, members in sorted(by_enable.items()):
        say("  ena = {:<6} -> {}".format(net, [names[g] for g in sorted(members)]))

    say("")
    say("shape of each part, judged only on the edges inside it:")
    for net, members in sorted(by_enable.items()):
        inner = {g: graph.depends[g] & set(members) for g in members}
        parts = candidates_module.strongly_connected_components(sorted(members), inner)
        say("  ena = {:<6} inner SCCs {}".format(
            net, [[names[g] for g in p] for p in parts]))
    say("  Both parts are internally mutually dependent, so 'is it an SCC' does")
    say("  not tell them apart either: a synchronous-clear counter IS an SCC,")
    say("  because every bit's next value reads the shared 'restart now' term")
    say("  and that term reads every bit.")

    # ---- split by reachability to the pins --------------------------------
    say("")
    say("signal 2 -- which flip-flops reach a primary output through logic only?")
    reaching = sorted(names[gid] for gid in graph.ids()
                      if reaches_primary_output(netlist, names[gid]))
    say("  {}".format(reaching))
    say("  A Moore machine decodes its outputs from the state register and from")
    say("  nothing else, so these are the state register.")

    state_group = reaching
    counter_group = [names[gid] for gid in graph.ids() if names[gid] not in state_group]
    always_on = [g for g, net in enable_of.items() if net in ("'1'", "1", "vcc")]
    agree = sorted(state_group) == sorted(names[g] for g in always_on)
    say("")
    say("the two signals agree: {}".format(agree))
    say("  state register : {}".format(state_group))
    say("  the other four : {}  (bit order unknown -- step 4)".format(counter_group))

    # ---- what hal_fsm's own candidate search says -------------------------
    proposals, proposal_notes = candidates_module.propose(graph)
    ranked = candidates_module.rank(proposals)
    say("")
    say("for comparison, hal_fsm's own candidate proposal and scoring:")
    for note in proposal_notes:
        say("  note: {}".format(note))
    for cand in ranked:
        say("  score {:.3f}  from {:<22} {}".format(
            cand.score, ",".join(sorted(cand.sources)),
            "[" + ", ".join(names[g] for g in cand.gate_ids) + "]"))
        for key, value in sorted(cand.features.items()):
            say("        {:<26} {}".format(key, value))

    # ---- the picture ------------------------------------------------------
    dot = ["digraph ff_dependency {",
           "  rankdir=LR;",
           '  labelloc="t";',
           '  label="flip-flop dependency graph (blinded netlist)";',
           '  fontname="Helvetica";',
           '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11];',
           '  edge [color="#4a6fa5", arrowsize=0.7];']
    for gid in graph.ids():
        fill = "#dfefff" if names[gid] in state_group else "#fdf0d5"
        dot.append('  "{}" [fillcolor="{}"];'.format(names[gid], fill))
    for i, scc in enumerate(components):
        if len(scc) > 1:
            dot.append('  subgraph cluster_{} {{'.format(i))
            dot.append('    label="one SCC: {} flip-flops"; color="#c2504a"; '
                       'style=dashed; fontname="Helvetica"; fontsize=10;'.format(len(scc)))
            for g in scc:
                dot.append('    "{}";'.format(names[g]))
            dot.append("  }")
    for gid in graph.ids():
        for dep in sorted(graph.depends[gid]):
            style = "" if dep != gid else ' [style=dotted]'
            dot.append('  "{}" -> "{}"{};'.format(names[gid], names[dep], style))
    dot.append("}")
    dot_path = ARTIFACTS / "ff_dependency.dot"
    dot_path.write_text("\n".join(dot) + "\n", encoding="utf-8")
    render_dot(dot_path, IMAGES / "ff_dependency.svg")
    return extraction, state_group, counter_group


# ===========================================================================
# step 4 -- the counter's bit order, from toggle rates
# ===========================================================================


def simulator(parsed, clock_net, reset_net, data_inputs):
    from hal_agilex import simulate

    sim = simulate.build(parsed)
    sim.set_input(clock_net, 0)
    sim.set_input(reset_net, 0)
    for name in data_inputs:
        sim.set_input(name, 0)
    sim.apply_async_clear()
    sim.set_input(reset_net, 1)
    return sim


def step4_bit_order(parsed, clock_net, reset_net, data_inputs, counter_ffs):
    banner("STEP 4  the second register's bit order, from toggle rates")
    say("The netlist has no buses: four flip-flops is four flip-flops. But bit i")
    say("of a counter toggles about half as often as bit i-1, and that survives")
    say("synthesis untouched.")
    sim = simulator(parsed, clock_net, reset_net, data_inputs)
    toggles = {f: 0 for f in counter_ffs}
    previous = {f: sim.state[f] for f in counter_ffs}
    for _ in range(310):
        sim.clock()
        for f in counter_ffs:
            if sim.state[f] != previous[f]:
                toggles[f] += 1
            previous[f] = sim.state[f]
    order = sorted(counter_ffs, key=lambda f: (-toggles[f], f))
    say("")
    for f in order:
        say("  {:<4} {:>4} toggles in 310 cycles".format(f, toggles[f]))
    say("")
    say("  bit order, LSB first: {}".format(order))
    return order


# ===========================================================================
# step 5 -- what hal_fsm does when you point it at an Agilex netlist
# ===========================================================================


def run_hal_fsm(hal_py, netlist, config_dict, out_dir, label):
    from hal_fsm.config import from_dict as config_from_dict
    from hal_fsm.run import analyse
    from hal_viz.halenv import import_plugin

    out_dir.mkdir(parents=True, exist_ok=True)
    say("")
    say("--- hal_fsm: {} ---".format(label))
    say("config: {}".format(json.dumps(config_dict, sort_keys=True)))
    try:
        configuration = config_from_dict(config_dict)
        solve_fsm_module = import_plugin("solve_fsm")
        document, artifacts, metrics, notes = analyse(
            hal_py, solve_fsm_module, netlist, configuration, str(out_dir),
            netlist_path=str(NETLIST), artifact_id="netlist",
        )
    except Exception as exc:  # noqa: BLE001 - the failure is the result here
        say("  hal_fsm raised {}: {}".format(type(exc).__name__, exc))
        say("  " + traceback.format_exc().splitlines()[-1])
        return None
    (out_dir / "findings.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for finding in document.get("findings", []):
        say("  {:<42} {:<26} {}".format(
            finding["id"], finding["status"], finding["summary"][:150]))
    for note in notes:
        say("  note: {}".format(note))
    for dot_path in sorted(out_dir.glob("*.dot")):
        render_dot(dot_path, IMAGES / (out_dir.name + "_" + dot_path.stem + ".svg"))
    return document


def step5_hal_fsm(hal_py, netlist, state_group, all_ffs):
    banner("STEP 5  point hal_fsm at it -- and read what it refuses to do")
    run_hal_fsm(hal_py, netlist,
                {"config_version": "1.0.0", "name": "controller",
                 "state_registers": list(state_group),
                 "solver": "brute_force",
                 "limits": {"brute_force_max_states": 4096}},
                ARTIFACTS / "fsm_controller", "the 4 state flip-flops")
    run_hal_fsm(hal_py, netlist,
                {"config_version": "1.0.0", "name": "product",
                 "state_registers": list(all_ffs),
                 "solver": "brute_force",
                 "limits": {"max_state_bits": 12, "brute_force_max_states": 4096}},
                ARTIFACTS / "fsm_product", "all 8 flip-flops")


# ===========================================================================
# step 6/7 -- solve the relation ourselves, exhaustively
# ===========================================================================


def transition_map(parsed, clock_net, reset_net, hold_net, ffs, order):
    """Exhaustive next-state map over every assignment of ``ffs``.

    Returns ``{(state tuple, hold): next state tuple}`` where a state tuple is
    the values of ``order`` (a list of flip-flop names).  This is a brute-force
    evaluation of the netlist itself with the validated primitive semantics --
    no solver, no SMT, 2**n * 2 settles.
    """
    from hal_agilex import simulate

    result = {}
    others = [f for f in ffs if f not in order]
    for value in range(1 << len(order)):
        assignment = {name: (value >> i) & 1 for i, name in enumerate(order)}
        for hold in (0, 1):
            sim = simulate.build(parsed)
            sim.set_input(clock_net, 0)
            sim.set_input(reset_net, 1)
            sim.set_input(hold_net, hold)
            state = dict(assignment)
            for name in others:
                state[name] = 0
            sim.state = state
            sim.set_input(reset_net, 1)  # forces a re-settle
            sim.clock()
            result[(value, hold)] = sum(
                sim.state[name] << i for i, name in enumerate(order))
    return result


def describe(pairs, counter_values, hold_name, counter_name):
    """A readable condition for a set of (environment, hold) assignments."""
    holds = {h for _, h in pairs}
    values = {v for v, _ in pairs}
    parts = []
    if len(holds) == 1:
        parts.append(hold_name if holds == {1} else "!" + hold_name)
    if len(values) == 1:
        parts.append("{} == {}".format(counter_name, next(iter(values))))
    elif len(values) < len(counter_values):
        parts.append("{} in {}".format(counter_name, sorted(values)))
    return " & ".join(parts) if parts else "otherwise"


def step6_controller_machine(parsed, clock_net, reset_net, hold_net, all_ffs,
                             state_group, counter_order):
    """The 4-bit controller, with its conditions over the counter and hold."""
    banner("STEP 6  solve the state register exhaustively, by hand")
    from hal_agilex import simulate
    from hal_fsm import diagram
    from hal_fsm.transitions import TransitionTable, format_state, reachable_states

    say("2**4 state assignments x 2**4 counter values x 2 hold values = 512")
    say("settles of a 22-gate netlist. At this size you do not need a solver.")

    edges: dict[int, dict[int, list]] = {}
    for state_value in range(1 << len(state_group)):
        for counter_value in range(1 << len(counter_order)):
            for hold in (0, 1):
                sim = simulate.build(parsed)
                sim.set_input(clock_net, 0)
                sim.set_input(hold_net, hold)
                state = {}
                for i, name in enumerate(state_group):
                    state[name] = (state_value >> i) & 1
                for i, name in enumerate(counter_order):
                    state[name] = (counter_value >> i) & 1
                sim.state = state
                sim.set_input(reset_net, 1)
                sim.clock()
                target = sum(sim.state[name] << i
                             for i, name in enumerate(state_group))
                edges.setdefault(state_value, {}).setdefault(target, []).append(
                    (counter_value, hold))

    counter_values = list(range(1 << len(counter_order)))
    mapping = {}
    for source, targets in edges.items():
        mapping[source] = {
            target: describe(pairs, counter_values, hold_net, "counter")
            for target, pairs in targets.items()
        }

    table = TransitionTable.from_mapping(
        state_group, mapping, initial_state=0, solver="brute_force", complete=True,
        notes=["relation enumerated by evaluating the netlist itself over every "
               "state / counter / hold assignment"])
    reachable, _ = reachable_states(table)
    say("")
    say("recovered relation: {} states, {} transitions, {} reachable from 0000"
        .format(len(table.states), len(table.transitions), len(reachable)))
    say("reachable states -- bit i is the output of {}[i], printed the way the"
        .format(state_group))
    say("diagram prints them, most significant bit first:")
    for state in sorted(reachable):
        say("  {:>2}  {}".format(
            state, format_state(state, len(state_group), 2)))
    say("")
    say("transitions:")
    for transition in sorted(table.transitions, key=lambda t: (t.source, t.target)):
        mark = " " if transition.source in reachable else "*"
        say("  {}{:>3} -> {:<3}  when  {}".format(
            mark, transition.source, transition.target, transition.condition))
    say("  (* = source state is not reachable from the reset state)")

    dot_path = ARTIFACTS / "controller_state_diagram.dot"
    diagram.write_state_diagram(
        table, str(dot_path),
        title="state register recovered from the netlist")
    render_dot(dot_path, IMAGES / "controller_state_diagram.svg")

    (ARTIFACTS / "controller_transitions.json").write_text(
        json.dumps(table.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return table, reachable


def hold_zero_cycle(nxt, start=0):
    """The states the machine loops through with ``hold = 0``, in order.

    ``nxt`` is deterministic and finite, so walking it from ``start`` always
    runs into a state it has already visited; everything from that state on is
    the cycle.  The prefix that leads into it (the reset ramp) is dropped --
    it is not part of the light cycle.  Returns ``[]`` only when the walk
    leaves the enumerated set, which means the caller built ``nxt`` wrong.
    """
    order_seen = []
    position = {}
    state = start
    while state not in position:
        if (state, 0) not in nxt:
            return []
        position[state] = len(order_seen)
        order_seen.append(state)
        state = nxt[(state, 0)]
    return order_seen[position[state]:]


def outputs_in_state(parsed, clock_net, reset_net, hold_net, all_ffs, order,
                     state, outputs):
    """The combinational output pins the netlist drives while in ``state``."""
    from hal_agilex import simulate

    sim = simulate.build(parsed)
    sim.set_input(clock_net, 0)
    sim.set_input(hold_net, 0)
    assignment = {name: 0 for name in all_ffs}
    for index, name in enumerate(order):
        assignment[name] = (state >> index) & 1
    sim.state = assignment
    sim.set_input(reset_net, 1)  # forces a re-settle over the new state
    return [sim.get_output(name) for name in outputs]


def step7_product_machine(parsed, clock_net, reset_net, hold_net, all_ffs,
                          state_group, counter_order, outputs):
    """All eight flip-flops as one machine: the light cycle, in full."""
    banner("STEP 7  refuse to decompose: all eight flip-flops as one machine")
    from hal_fsm import diagram
    from hal_fsm.transitions import TransitionTable, reachable_states

    order = list(state_group) + list(counter_order)
    say("state bit order: {}".format(order))
    nxt = transition_map(parsed, clock_net, reset_net, hold_net, all_ffs, order)

    reachable_set = {0}
    frontier = [0]
    while frontier:
        state = frontier.pop()
        for hold in (0, 1):
            target = nxt[(state, hold)]
            if target not in reachable_set:
                reachable_set.add(target)
                frontier.append(target)

    mapping: dict[int, dict[int, str]] = {}
    for state in sorted(reachable_set):
        by_target: dict[int, list[int]] = {}
        for hold in (0, 1):
            by_target.setdefault(nxt[(state, hold)], []).append(hold)
        mapping[state] = {}
        for target, holds in by_target.items():
            if len(holds) == 2:
                mapping[state][target] = "1"
            else:
                mapping[state][target] = hold_net if holds == [1] else "!" + hold_net

    table = TransitionTable.from_mapping(
        order, mapping, initial_state=0, solver="brute_force", complete=True,
        notes=["only the {} states reachable from reset are shown; the other {} "
               "encodings were enumerated too".format(
                   len(reachable_set), (1 << len(order)) - len(reachable_set))])
    reachable, _ = reachable_states(table)
    say("")
    say("{} of {} encodings are reachable from the reset state".format(
        len(reachable_set), 1 << len(order)))
    say("the cycle, with hold = 0 (state / counter / output pins):")
    say("")

    # Issue #56: this header used to be printed with nothing under it. The ring
    # was recovered correctly -- it is in artifacts/product_transitions.json --
    # but the walk that turns it into a table was never written, and a header
    # over zero rows reads as "the analysis found nothing".
    cycle = hold_zero_cycle(nxt)
    if not cycle:
        say("  no cycle: with hold = 0 the walk leaves the enumerated state set,")
        say("  which means the transition map above is incomplete")
    else:
        state_bits = len(state_group)
        header = ("  {:>5} | ".format("enc")
                  + " ".join("{:>3}".format(name) for name in state_group)
                  + "  | counter | "
                  + " ".join("{:>3}".format(name) for name in outputs))
        say(header)
        say("  " + "-" * (len(header) - 2))
        for state in cycle:
            counter = sum(
                ((state >> (state_bits + index)) & 1) << index
                for index in range(len(counter_order))
            )
            pins = outputs_in_state(parsed, clock_net, reset_net, hold_net,
                                    all_ffs, order, state, outputs)
            say("  {:>5} | ".format(state)
                + " ".join("{:>3}".format((state >> index) & 1)
                           for index in range(state_bits))
                + "  | {:>7} | ".format(counter)
                + " ".join("{:>3}".format(value) for value in pins))
        say("")
        say("  {} states in the cycle; the {} reachable encodings are those plus"
            .format(len(cycle), len(reachable_set)))
        say("  the {} state(s) of the reset ramp that lead into it"
            .format(len(reachable_set) - len(cycle)))
        say("")

    dot_path = ARTIFACTS / "product_state_diagram.dot"
    diagram.write_state_diagram(
        table, str(dot_path),
        title="the whole controller: state register + dwell counter")
    render_dot(dot_path, IMAGES / "product_state_diagram.svg", engine="circo")
    (ARTIFACTS / "product_transitions.json").write_text(
        json.dumps(table.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return table, reachable_set


# ===========================================================================
# step 8 -- drive it and watch the pins
# ===========================================================================


def step8_simulate(parsed, clock_net, reset_net, data_inputs, outputs,
                   state_group, counter_order, cycles=40):
    banner("STEP 8  drive the netlist and watch the output pins")
    sim = simulator(parsed, clock_net, reset_net, data_inputs)

    header = ("cycle | " + " ".join("{:>3}".format(o) for o in outputs)
              + "  | " + " ".join("{:>3}".format(f) for f in state_group)
              + "  | counter")
    say("(reset released before cycle 0; {} held at 0 throughout)".format(
        ", ".join(data_inputs)))
    say(header)
    say("-" * len(header))
    rows = []
    for cycle in range(cycles):
        vals = [sim.get_output(o) for o in outputs]
        state = [sim.state[f] for f in state_group]
        count = sum(sim.state[f] << i for i, f in enumerate(counter_order))
        rows.append((cycle, tuple(vals), tuple(state), count))
        say("{:>5} | ".format(cycle)
            + " ".join("{:>3}".format(v) for v in vals)
            + "  | " + " ".join("{:>3}".format(v) for v in state)
            + "  | {:>3}".format(count))
        sim.clock()

    phases = []
    last, length = None, 0
    for _, vals, _, _ in rows:
        if vals != last:
            if last is not None:
                phases.append((last, length))
            last, length = vals, 1
        else:
            length += 1
    phases.append((last, length))
    say("")
    say("output phases ({}) and how long each lasts:".format(", ".join(outputs)))
    for pattern, count in phases[:4]:
        say("  {} -> {:>2} clock cycles".format(pattern, count))
    say("  full period: {} clock cycles".format(sum(c for _, c in phases[:4])))

    say("")
    say("counter value against phase (the value it reaches just before it")
    say("restarts is the dwell limit of that phase):")
    limits = {}
    for index, row in enumerate(rows[:-1]):
        if rows[index + 1][3] == 0 and row[3] != 0:
            limits[row[1]] = row[3]
    for pattern, limit in limits.items():
        say("  outputs {} -> counter runs 0..{}  ({} cycles)".format(
            pattern, limit, limit + 1))

    say("")
    say("state encoding against phase:")
    seen = {}
    for _, vals, state, _ in rows:
        seen.setdefault(state, vals)
    for state, vals in seen.items():
        say("  {} -> outputs {}".format("".join(str(b) for b in state), vals))
    return rows, phases, limits


# ===========================================================================
# main
# ===========================================================================


def main() -> int:
    global _SEQUENTIAL
    IMAGES.mkdir(exist_ok=True)
    ARTIFACTS.mkdir(exist_ok=True)

    banner("STEP 0  pictures first: module tree and the whole gate graph")
    run([sys.executable, str(TOOLS / "hal_viz"), "module_tree", str(NETLIST),
         "-g", str(GATE_LIBRARY), "-o", str(IMAGES / "module_tree.svg")])
    run([sys.executable, str(TOOLS / "hal_viz"), "netlist_graph", str(NETLIST),
         "-g", str(GATE_LIBRARY), "--module", "top", "--pin-labels",
         "-o", str(IMAGES / "netlist_graph.svg")])

    banner("STEP 0b  DANA dataflow analysis (register grouping)")
    run([sys.executable, str(TOOLS / "hal_viz"), "dataflow", str(NETLIST),
         "-g", str(GATE_LIBRARY), "-o", str(ARTIFACTS / "dataflow")])
    groups = ARTIFACTS / "dataflow" / "groups.txt"
    if groups.exists():
        say("")
        say("DANA groups.txt:")
        for line in groups.read_text(encoding="utf-8", errors="replace").splitlines():
            say("  " + line)
    dana_dot = ARTIFACTS / "dataflow" / "graph.dot"
    if dana_dot.exists():
        render_dot(dana_dot, IMAGES / "dataflow.svg")

    banner("STEP 0c  hal_cdc: how many clock and reset domains are there?")
    run([sys.executable, str(TOOLS / "hal_cdc"), "discover", str(NETLIST),
         "--gate-library", str(GATE_LIBRARY), "-o", str(ARTIFACTS / "clocks.json")])
    clocks = ARTIFACTS / "clocks.json"
    if clocks.exists():
        say("")
        for line in clocks.read_text(encoding="utf-8").splitlines():
            say("  " + line)

    # --- everything below shares one loaded, elaborated netlist -------------
    from hal_agilex import hal_adapter, vo_netlist

    hal_py = hal_adapter.import_hal([os.environ.get("HAL_PY_PATH", "")])
    _SEQUENTIAL = hal_py.GateTypeProperty.sequential
    netlist = hal_adapter.load_netlist(hal_py, NETLIST, GATE_LIBRARY)
    report = hal_adapter.elaborate(hal_py, netlist)

    banner("STEP 0d  attach the ALM semantics (hal_agilex.hal_adapter.elaborate)")
    say("elaborated tennm_lcell_comb gates : {}".format(report["elaborated"]))
    say("checked tennm_ff gates            : {}".format(report["checked_ff"]))
    say("refused (left without semantics)  : {}".format(len(report["refused"])))
    for entry in report["refused"]:
        say("  {} ({}): {}".format(entry["gate"], entry["type"], entry["reason"]))

    inputs, outputs = step1_first_contact(hal_py, netlist)
    ff_rows = step2_sequential(hal_py, netlist)
    extraction, state_group, counter_group = step3_dependency_graph(netlist)

    clock_net = sorted({row[1] for row in ff_rows})[0]
    reset_net = sorted({row[2] for row in ff_rows})[0]
    data_inputs = [n for n in inputs if n not in (clock_net, reset_net)]
    hold_net = data_inputs[0]
    all_ffs = [row[0] for row in ff_rows]

    parsed = vo_netlist.parse_file(str(NETLIST))
    counter_order = step4_bit_order(parsed, clock_net, reset_net, data_inputs,
                                    counter_group)
    step5_hal_fsm(hal_py, netlist, state_group, all_ffs)
    step6_controller_machine(parsed, clock_net, reset_net, hold_net, all_ffs,
                             state_group, counter_order)
    step7_product_machine(parsed, clock_net, reset_net, hold_net, all_ffs,
                          state_group, counter_order, outputs)
    step8_simulate(parsed, clock_net, reset_net, data_inputs, outputs,
                   state_group, counter_order)

    banner("STEP 9  un-blind: what the vendor export actually called these gates")
    mapping = json.loads((ARTIFACTS / "anonymize_map.json").read_text(encoding="utf-8"))
    say("{:<6} {}".format("anon", "name Quartus wrote into the .vo"))
    for original, anon in sorted(mapping["gates"].items(),
                                 key=lambda kv: (kv[1][0], int(kv[1][1:]))):
        say("{:<6} {}".format(anon, original))
    say("")
    for original, anon in sorted(mapping["nets"].items(),
                                 key=lambda kv: kv[1]):
        if anon.startswith(("i", "o")) and len(anon) == 2:
            say("{:<6} {}".format(anon, original))

    (ARTIFACTS / "transcript.txt").write_text("\n".join(LOG) + "\n", encoding="utf-8")
    print("\nwrote {}".format(ARTIFACTS / "transcript.txt"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
