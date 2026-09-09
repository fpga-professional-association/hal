#!/usr/bin/env python3
"""Every HAL analysis step of the 06_accumulator_alu walkthrough, in order.

This is the script that produced everything under ``artifacts/``.  It is meant
to be *read* alongside ``guide.html``: each ``step_*`` function is one section
of the guide, and each writes exactly one text file so a claim in the guide can
be checked against the output it came from.

Run it inside the HAL build container, from the repository root::

    HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib \\
    PYTHONPATH=/work/build/lib:tools \\
        python3 examples/agilex3_walkthroughs/06_accumulator_alu/analyze.py

Nothing here knows anything that is not in the netlist: the design source is
never read.  The names that *are* in the netlist (``acc``, ``add_0~1``, ``op``)
come from Quartus and are what a real stripped-but-not-obfuscated vendor export
looks like; the guide is explicit about which conclusions lean on them and
which do not.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
TOOLS = os.path.join(REPO, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

NETLIST_HAL_V = os.path.join(HERE, "netlist", "netlist.hal.v")
NETLIST_VO = os.path.join(HERE, "netlist", "accumulator_alu.vo")
GATE_LIBRARY = os.path.join(
    REPO, "plugins", "gate_libraries", "definitions", "AGILEX_TENNM.hgl"
)
ARTIFACTS = os.path.join(HERE, "artifacts")


def emit(name, lines):
    os.makedirs(ARTIFACTS, exist_ok=True)
    path = os.path.join(ARTIFACTS, name)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")
    print("wrote", os.path.relpath(path, REPO))
    return path


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def load():
    """Load the netlist and attach the ALM semantics (hal_agilex.hal_adapter)."""
    from hal_agilex import hal_adapter

    hal_py = hal_adapter.import_hal()
    hal_adapter.load_plugins(hal_py)
    netlist = hal_adapter.load_netlist(hal_py, NETLIST_HAL_V, GATE_LIBRARY)
    report = hal_adapter.elaborate(hal_py, netlist)
    return hal_py, netlist, report


# ---------------------------------------------------------------------------
# step 1 -- first contact
# ---------------------------------------------------------------------------


def step_stats(hal_py, netlist, report):
    lines = ["== netlist statistics =="]
    lines.append("design name        : {}".format(netlist.get_design_name()))
    lines.append("gate library       : {}".format(netlist.get_gate_library().get_name()))
    lines.append("gates              : {}".format(len(netlist.get_gates())))
    lines.append("nets               : {}".format(len(netlist.get_nets())))
    lines.append("modules            : {}".format(len(netlist.get_modules())))
    lines.append("")
    lines.append("gate types:")
    for name in sorted(report["types"]):
        lines.append("  {:<20} {}".format(name, report["types"][name]))
    lines.append("")
    lines.append("elaboration: {} lcell(s) given a Boolean function, {} ff(s) checked, "
                 "{} refused".format(report["elaborated"], report["checked_ff"],
                                     len(report["refused"])))
    lines.append("")

    inputs = sorted(n.get_name() for n in netlist.get_global_input_nets())
    outputs = sorted(n.get_name() for n in netlist.get_global_output_nets())
    lines.append("global input nets  ({}): {}".format(len(inputs), ", ".join(inputs)))
    lines.append("global output nets ({}): {}".format(len(outputs), ", ".join(outputs)))
    lines.append("")

    lines.append("sequential gates:")
    for gate in sorted(netlist.get_gates(), key=lambda g: g.get_name()):
        if gate.get_type().get_name() != "tennm_ff":
            continue
        drivers = {}
        for pin in ("clk", "d", "ena", "clrn"):
            net = gate.get_fan_in_net(pin)
            drivers[pin] = net.get_name() if net is not None else "<open>"
        lines.append("  {:<16} clk={:<8} ena={:<22} clrn={:<8} d={}".format(
            gate.get_name(), drivers["clk"], drivers["ena"], drivers["clrn"],
            drivers["d"]))
    return emit("01_stats.txt", lines)


# ---------------------------------------------------------------------------
# step 2 -- the carry chain
# ---------------------------------------------------------------------------


def carry_chain(netlist):
    """Every maximal cout->cin path in the netlist, as lists of gates."""
    succ, pred = {}, {}
    for gate in netlist.get_gates():
        out = gate.get_fan_out_net("cout")
        if out is None:
            continue
        for endpoint in out.get_destinations():
            if endpoint.get_pin().get_name() == "cin":
                succ[gate.get_id()] = endpoint.get_gate()
                pred[endpoint.get_gate().get_id()] = gate

    chains = []
    for gate in sorted(netlist.get_gates(), key=lambda g: g.get_id()):
        if gate.get_id() in pred:
            continue
        if gate.get_id() not in succ:
            continue
        chain, cursor = [gate], gate
        while cursor.get_id() in succ:
            cursor = succ[cursor.get_id()]
            chain.append(cursor)
        chains.append(chain)
    return chains


def _mask(gate):
    value = gate.get_data("generic", "lut_mask")
    return int(str(value[1]), 16) if value and value[1] else 0


def _bit(hal_py, value):
    """A BooleanFunction.Value as 0, 1 or its name -- never a silent int cast."""
    if value == hal_py.BooleanFunction.Value.ZERO:
        return 0
    if value == hal_py.BooleanFunction.Value.ONE:
        return 1
    return str(value)


def _driver_names(gate, pins):
    out = []
    for pin in pins:
        net = gate.get_fan_in_net(pin)
        if net is None:
            out.append("{}=<open>".format(pin))
        elif net.is_gnd_net():
            out.append("{}=0".format(pin))
        elif net.is_vcc_net():
            out.append("{}=1".format(pin))
        else:
            out.append("{}={}".format(pin, net.get_name()))
    return out


def step_carry_chain(hal_py, netlist):
    chains = carry_chain(netlist)
    lines = ["== carry chains (cout -> cin) =="]
    lines.append("{} chain(s) found".format(len(chains)))
    for index, chain in enumerate(chains):
        lines.append("")
        lines.append("chain {}: {} cell(s)".format(index, len(chain)))
        for position, gate in enumerate(chain):
            mask = _mask(gate)
            drives_sum = gate.get_fan_out_net("sumout") is not None
            drives_cout = gate.get_fan_out_net("cout") is not None
            lines.append("  [{:>2}] {:<12} lut_mask=0x{:016X} sumout={} cout={}".format(
                position, gate.get_name(), mask, "yes" if drives_sum else "no",
                "yes" if drives_cout else "no"))
            lines.append("       inputs: {}".format(
                " ".join(_driver_names(gate, ("dataa", "datab", "datac", "datad",
                                              "datae", "dataf")))))
            for pin in ("sumout", "cout"):
                function = gate.get_boolean_function(pin)
                text = str(function)
                if text and text != "<empty>":
                    lines.append("       {:<7} = {}".format(pin, text))
    return emit("02_carry_chain.txt", lines), chains


def step_chain_diagram(netlist, chains):
    """Draw just the carry chain, using hal_viz's own DOT emitter.

    hal_viz's `netlist_graph --gate ... --depth N` draws a *neighbourhood*; a
    ten-cell chain needs depth 9 from the head, by which point the picture has
    swallowed the whole design again.  The chain is the thing worth seeing on
    its own, so it gets its own drawing.
    """
    from hal_viz import dot as dotmod
    from hal_viz import render as rendermod

    images = os.path.join(HERE, "images")
    os.makedirs(images, exist_ok=True)

    graph = dotmod.DotGraph(
        "carry_chain", comment="carry chain of accumulator_alu, drawn by analyze.py")
    graph.graph_attrs.update({"rankdir": "LR", "fontname": "Helvetica",
                              "labelloc": "t",
                              "label": "carry chain of accumulator_alu "
                                       "(cout -> cin), drawn from netlist.hal.v"})
    graph.node_defaults.update({"shape": "box", "fontname": "Helvetica",
                                "fontsize": "10", "style": "filled",
                                "fillcolor": "white"})
    graph.edge_defaults.update({"fontname": "Helvetica", "fontsize": "9"})

    chain = max(chains, key=len)
    for position, gate in enumerate(chain):
        drives_sum = gate.get_fan_out_net("sumout") is not None
        drives_cout = gate.get_fan_out_net("cout") is not None
        if drives_sum and drives_cout:
            role, colour = "bit slice", "#dbe9f6"
        elif drives_cout:
            role, colour = "carry-in generator", "#f6e2c8"
        else:
            role, colour = "carry-out tap", "#d9f0dc"
        data = [name for name in _driver_names(
            gate, ("dataa", "datab", "datac", "datad")) if not name.endswith("=0")]
        label = "\n".join([gate.get_name(),
                           "[{}]".format(role),
                           "mask 0x{:016X}".format(_mask(gate))] + data)
        graph.add_node("g{}".format(gate.get_id()), label=label, fillcolor=colour)
        if position:
            graph.add_edge("g{}".format(chain[position - 1].get_id()),
                           "g{}".format(gate.get_id()), label="cout->cin")
        if drives_sum:
            net = gate.get_fan_out_net("sumout")
            graph.add_node("s{}".format(gate.get_id()), label=net.get_name(),
                           shape="ellipse", fillcolor="#f2f2f2")
            graph.add_edge("g{}".format(gate.get_id()), "s{}".format(gate.get_id()),
                           label="sumout", style="dashed")

    dot_path = os.path.join(images, "03_carry_chain.dot")
    graph.write(dot_path)
    print("wrote", os.path.relpath(dot_path, REPO))
    try:
        svg = rendermod.render_dot(dot_path, os.path.join(images, "03_carry_chain.svg"),
                                   "svg")
        print("wrote", os.path.relpath(svg, REPO))
    except Exception as exc:
        print("dot unavailable, kept the .dot only: {}".format(exc))
    return dot_path


# ---------------------------------------------------------------------------
# step 3 -- symbolic check of one adder slice
# ---------------------------------------------------------------------------


def step_slice_truth_table(hal_py, netlist, chains):
    """Prove a slice is a full adder by enumerating its own Boolean function."""
    lines = ["== is a slice really a full adder? =="]
    lines.append("Enumerating the Boolean functions HAL attached to one arithmetic")
    lines.append("cell, over all assignments of its driven inputs.")
    lines.append("")

    chain = max(chains, key=len)
    slices = [g for g in chain
              if g.get_fan_out_net("sumout") is not None
              and g.get_fan_out_net("cout") is not None]
    if not slices:
        lines.append("no cell in the chain drives both sumout and cout")
        return emit("03_slice_truth_table.txt", lines)

    gate = slices[0]
    lines.append("cell        : {}".format(gate.get_name()))
    lines.append("lut_mask    : 0x{:016X}".format(_mask(gate)))
    lines.append("inputs      : {}".format(
        " ".join(_driver_names(gate, ("dataa", "datab", "datac", "datad")))))
    lines.append("")

    sumout = gate.get_boolean_function("sumout")
    cout = gate.get_boolean_function("cout")
    variables = sorted(set(sumout.get_variable_names()) | set(cout.get_variable_names()))
    lines.append("free variables: {}".format(", ".join(variables)))
    lines.append("")
    header = " ".join("{:>16}".format(v) for v in variables)
    lines.append("{} | sumout cout".format(header))
    lines.append("-" * (len(header) + 14))

    for vector in range(1 << len(variables)):
        assignment = {}
        for bit, name in enumerate(variables):
            assignment[name] = hal_py.BooleanFunction.Value.ONE if (vector >> bit) & 1 \
                else hal_py.BooleanFunction.Value.ZERO
        s = _bit(hal_py, sumout.evaluate(assignment))
        c = _bit(hal_py, cout.evaluate(assignment))
        values = [(vector >> bit) & 1 for bit in range(len(variables))]
        lines.append("{} |   {}      {}".format(
            " ".join("{:>16}".format(v) for v in values), s, c))

    lines.append("")
    lines.append("Check against a full adder over the three non-opcode inputs, for each")
    lines.append("fixed opcode.  A full adder satisfies sum = x^y^cin and")
    lines.append("cout = majority(x, y, cin).")
    return emit("03_slice_truth_table.txt", lines)


# ---------------------------------------------------------------------------
# step 4 -- what gates the registers
# ---------------------------------------------------------------------------


def step_control(hal_py, netlist):
    lines = ["== control cone of the register file =="]
    lines.append("For every flip-flop: the Boolean function of its enable and of its")
    lines.append("data input, expressed in terms of the nets that drive them.")
    lines.append("")

    by_net = {}
    for gate in netlist.get_gates():
        for pin in ("combout", "sumout", "cout"):
            net = gate.get_fan_out_net(pin)
            if net is not None:
                by_net[net.get_name()] = (gate, pin)

    seen_enable = {}
    for gate in sorted(netlist.get_gates(), key=lambda g: g.get_name()):
        if gate.get_type().get_name() != "tennm_ff":
            continue
        lines.append("flip-flop {}".format(gate.get_name()))
        for pin in ("ena", "d"):
            net = gate.get_fan_in_net(pin)
            if net is None:
                lines.append("  {:<4}: <open>".format(pin))
                continue
            driver = by_net.get(net.get_name())
            if driver is None:
                lines.append("  {:<4}: {} (primary input)".format(pin, net.get_name()))
                continue
            source, source_pin = driver
            function = source.get_boolean_function(source_pin)
            lines.append("  {:<4}: {} <- {}.{}".format(pin, net.get_name(),
                                                       source.get_name(), source_pin))
            lines.append("        = {}".format(function))
            if pin == "ena":
                seen_enable.setdefault(str(function), []).append(gate.get_name())
        lines.append("")

    lines.append("distinct enable functions: {}".format(len(seen_enable)))
    for function, gates in seen_enable.items():
        lines.append("  {}  <- {} flip-flop(s)".format(function, len(gates)))
    return emit("04_control_cone.txt", lines)


# ---------------------------------------------------------------------------
# step 5 -- opcode sweep on the netlist itself
# ---------------------------------------------------------------------------


def step_opcode_sweep():
    """Black-box the *netlist* (not the RTL): what does each opcode do?"""
    from hal_agilex import simulate, vo_netlist

    netlist = vo_netlist.parse_file(NETLIST_VO)
    lines = ["== opcode sweep, by simulating the exported netlist =="]
    lines.append("The reference simulator in tools/hal_agilex drives the .vo with the")
    lines.append("modelled ALM semantics.  For each opcode we load a known accumulator")
    lines.append("value, then apply one clock edge with a known operand.")
    lines.append("")
    lines.append("  op | acc before | operand | acc after | carry after | zero after")
    lines.append("  ---+------------+---------+-----------+-------------+-----------")

    table = {}
    for op in range(4):
        for acc0, operand in ((0x05, 0x03), (0x80, 0x80), (0x03, 0x05), (0xFF, 0x01)):
            simulator = simulate.Simulator(netlist)
            simulator.reset()
            for name in ("op", "operand", "rst_n"):
                simulator.set_input(name, 0)
            simulator.apply_async_clear()
            # Load acc0 by adding it to a cleared accumulator with op = 1.
            simulator.set_input("rst_n", 1)
            simulator.set_input("op", 1)
            simulator.set_input("operand", acc0)
            simulator.clock()
            before = simulator.get_output("acc")
            simulator.set_input("op", op)
            simulator.set_input("operand", operand)
            simulator.clock()
            after = simulator.get_output("acc")
            carry = simulator.get_output("carry")
            zero = simulator.get_output("zero")
            lines.append("  {:>2} | 0x{:02X}       | 0x{:02X}    | 0x{:02X}      "
                         "| {}           | {}".format(op, before, operand, after,
                                                      carry, zero))
            table.setdefault(op, []).append((before, operand, after, carry))
        lines.append("  ---+------------+---------+-----------+-------------+-----------")

    lines.append("")
    lines.append("Hypotheses, checked over a sampled lattice of (acc, operand) pairs")
    lines.append("(accumulator in strides of 7, operand in strides of 5: 37 x 52 = 1924")
    lines.append("pairs per opcode).  This is a sample, not an exhaustive proof; the")
    lines.append("exhaustive-per-opcode claim in the guide is the 200-cycle reference")
    lines.append("comparison in artifacts/behavior_recovered.json.")
    for op in range(4):
        candidates = {
            "hold":  lambda a, b: (a, None),
            "add":   lambda a, b: ((a + b) & 0xFF, (a + b) >> 8),
            "sub":   lambda a, b: ((a - b) & 0xFF, 1 if a >= b else 0),
            "clear": lambda a, b: (0, 0),
        }
        verdicts = []
        for label, model in candidates.items():
            ok = True
            for acc0 in range(0, 256, 7):
                for operand in range(0, 256, 5):
                    simulator = simulate.Simulator(netlist)
                    simulator.reset()
                    for name in ("op", "operand", "rst_n"):
                        simulator.set_input(name, 0)
                    simulator.apply_async_clear()
                    simulator.set_input("rst_n", 1)
                    simulator.set_input("op", 1)
                    simulator.set_input("operand", acc0)
                    simulator.clock()
                    simulator.set_input("op", op)
                    simulator.set_input("operand", operand)
                    simulator.clock()
                    want_acc, want_carry = model(acc0, operand)
                    if simulator.get_output("acc") != want_acc:
                        ok = False
                        break
                    if want_carry is not None and simulator.get_output("carry") != want_carry:
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                verdicts.append(label)
        lines.append("  op = {:>2}  consistent with: {}".format(
            op, ", ".join(verdicts) if verdicts else "none of the candidates"))
    return emit("05_opcode_sweep.txt", lines)


# ---------------------------------------------------------------------------
# step 6 -- graph_algorithm: strongly connected components
# ---------------------------------------------------------------------------


def step_graph_algorithm(hal_py, netlist):
    lines = ["== graph_algorithm: strongly connected components =="]
    try:
        from hal_plugins import graph_algorithm
    except ImportError as exc:
        lines.append("plugin unavailable: {}".format(exc))
        return emit("06_scc.txt", lines)

    graph = graph_algorithm.NetlistGraph.from_netlist(netlist)
    # graph_algorithm exposes this as a module-level function taking a `strong`
    # flag, not as a method -- `strong=True` is the strongly connected version.
    components = graph_algorithm.get_connected_components(graph, True)
    if components is None:
        lines.append("get_connected_components returned None; the plugin logged why")
        return emit("06_scc.txt", lines)
    sized = []
    for component in components:
        gates = graph.get_gates_from_vertices(component)
        sized.append(sorted(g.get_name() for g in gates))
    sized.sort(key=lambda names: (-len(names), names))

    lines.append("{} strongly connected component(s)".format(len(sized)))
    lines.append("components with more than one gate:")
    multi = [names for names in sized if len(names) > 1]
    if not multi:
        lines.append("  (none)")
    for names in multi:
        lines.append("  size {}: {}".format(len(names), ", ".join(names)))
    lines.append("")
    lines.append("self-loops (a gate that feeds back to itself through the graph):")
    self_loops = []
    for gate in netlist.get_gates():
        if gate.get_type().get_name() != "tennm_ff":
            continue
        reached = set()
        frontier = [gate]
        while frontier:
            current = frontier.pop()
            for net in current.get_fan_out_nets():
                for endpoint in net.get_destinations():
                    successor = endpoint.get_gate()
                    if successor.get_id() in reached:
                        continue
                    reached.add(successor.get_id())
                    if successor.get_type().get_name() != "tennm_ff":
                        frontier.append(successor)
        if gate.get_id() in reached:
            self_loops.append(gate.get_name())
    for name in sorted(self_loops):
        lines.append("  {}".format(name))
    return emit("06_scc.txt", lines)


# ---------------------------------------------------------------------------
# step 7 -- DANA
# ---------------------------------------------------------------------------


def step_dataflow(hal_py, netlist):
    lines = ["== dataflow_analysis (DANA): recovered register groups =="]
    try:
        from hal_plugins import dataflow
    except ImportError as exc:
        lines.append("plugin unavailable: {}".format(exc))
        return emit("07_dataflow.txt", lines), None

    configuration = dataflow.Configuration(netlist).with_flip_flops()
    result = dataflow.analyze(configuration)
    if result is None:
        lines.append("dataflow.analyze returned None")
        return emit("07_dataflow.txt", lines), None

    groups = result.get_groups()
    lines.append("{} group(s)".format(len(groups)))
    for group_id in sorted(groups, key=int):
        names = sorted(g.get_name() for g in groups[group_id])
        lines.append("  group {:>3} ({:>2} ff): {}".format(group_id, len(names),
                                                           ", ".join(names)))
    return emit("07_dataflow.txt", lines), result


# ---------------------------------------------------------------------------
# step 8 -- module_identification
# ---------------------------------------------------------------------------


def step_module_identification(hal_py, netlist, dataflow_result):
    lines = ["== module_identification =="]
    try:
        from hal_plugins import module_identification
    except ImportError as exc:
        lines.append("plugin unavailable: {}".format(exc))
        return emit("08_module_identification.txt", lines)

    registers = []
    if dataflow_result is not None:
        groups = dataflow_result.get_groups()
        for group_id in sorted(groups, key=int):
            gates = sorted(groups[group_id], key=lambda g: g.get_id())
            if gates:
                registers.append(list(gates))
    lines.append("known registers handed to the plugin: {}".format(len(registers)))

    configuration = module_identification.Configuration(netlist)
    if registers:
        configuration = configuration.with_known_registers(registers)
    try:
        result = module_identification.execute(configuration)
    except Exception as exc:
        lines.append("module_identification.execute raised: {}".format(exc))
        return emit("08_module_identification.txt", lines)

    if result is None:
        lines.append("module_identification.execute returned None")
        lines.append("(the plugin logged the reason; this is NOT evidence that the")
        lines.append(" netlist contains no arithmetic)")
        return emit("08_module_identification.txt", lines)

    # Binding names per plugins/module_identification/python/python_bindings.cpp.
    def safe(obj, name, default=None):
        try:
            attribute = getattr(obj, name)
        except AttributeError:
            return default
        try:
            return attribute() if callable(attribute) else attribute
        except Exception as exc:
            return "<raised {}>".format(exc)

    all_candidates = safe(result, "get_candidates", {}) or {}
    verified = safe(result, "get_verified_candidates", {}) or {}
    lines.append("candidates enumerated : {}".format(len(all_candidates)))
    lines.append("candidates VERIFIED   : {}".format(len(verified)))
    lines.append("")
    lines.append("A verified candidate is an SMT equivalence result about a gate cone.")
    lines.append("An enumerated-but-unverified candidate means 'not proved', never")
    lines.append("'no arithmetic here'.")
    lines.append("")

    for label, group in (("VERIFIED", verified),
                         ("all enumerated", all_candidates)):
        lines.append("-- {} --".format(label))
        if not group:
            lines.append("  (none)")
        for key in sorted(group, key=lambda k: int(k)):
            candidate = group[key]
            types = safe(candidate, "types", set()) or set()
            gates = safe(candidate, "gates", []) or []
            operands = safe(candidate, "operands", []) or []
            outputs = safe(candidate, "output_nets", []) or []
            controls = safe(candidate, "control_signals", []) or []
            lines.append("  candidate {}: name={} verified={}".format(
                key, safe(candidate, "get_name", "?"),
                safe(candidate, "is_verified", "?")))
            lines.append("    types      : {}".format(
                ", ".join(sorted(str(t) for t in types)) or "-"))
            lines.append("    gates ({:>2}) : {}".format(
                len(gates), ", ".join(sorted(g.get_name() for g in gates))))
            for index, operand in enumerate(operands):
                lines.append("    operand {}  : {}".format(
                    index, ", ".join(n.get_name() for n in operand)))
            lines.append("    outputs    : {}".format(
                ", ".join(n.get_name() for n in outputs) or "-"))
            lines.append("    controls   : {}".format(
                ", ".join(n.get_name() for n in controls) or "-"))
        lines.append("")
        if label == "VERIFIED" and len(verified) == len(all_candidates):
            lines.append("(every enumerated candidate was verified; the second list "
                         "would repeat the first)")
            break
    return emit("08_module_identification.txt", lines)


# ---------------------------------------------------------------------------


def main():
    hal_py, netlist, report = load()
    step_stats(hal_py, netlist, report)
    _, chains = step_carry_chain(hal_py, netlist)
    step_chain_diagram(netlist, chains)
    step_slice_truth_table(hal_py, netlist, chains)
    step_control(hal_py, netlist)
    step_opcode_sweep()
    step_graph_algorithm(hal_py, netlist)
    _, dataflow_result = step_dataflow(hal_py, netlist)
    step_module_identification(hal_py, netlist, dataflow_result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
