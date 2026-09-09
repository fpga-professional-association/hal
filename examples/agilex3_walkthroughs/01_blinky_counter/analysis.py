#!/usr/bin/env python3
"""The reverse-engineering steps of walkthrough 01, one function per step.

Every step reads *only* the imported netlist and the gate library.  Nothing in
this file looks at ``design.v`` or ``spec.md``: that is the whole point.

Usage (from the repository root, inside a container with a built HAL):

    HAL_BASE_PATH=/work/build PYTHONPATH=/work/build/lib:tools \\
        python3 examples/agilex3_walkthroughs/01_blinky_counter/analysis.py all \\
            -o examples/agilex3_walkthroughs/01_blinky_counter/artifacts

Subcommands: ``stats``, ``registers``, ``scc``, ``chain``, ``increment``,
``all``.  ``all`` runs every step in order and also writes the DOT/SVG files
the guide embeds.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))

NETLIST = os.path.join(HERE, "netlist.hal.v")
GATE_LIBRARY = os.path.join(
    REPO, "plugins", "gate_libraries", "definitions", "AGILEX_TENNM.hgl"
)


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------


def load(netlist_path=NETLIST, gate_library=GATE_LIBRARY):
    """Import hal_py, register the parser plugins, load the netlist."""
    for entry in os.environ.get("HAL_PY_PATH", "").split(os.pathsep):
        if entry and entry not in sys.path:
            sys.path.insert(0, entry)
    import hal_py

    # Without this the .hgl and .v parsers are not registered and every load
    # silently returns None.
    hal_py.plugin_manager.load_all_plugins()
    netlist = hal_py.NetlistFactory.load_netlist(netlist_path, gate_library)
    if netlist is None:
        raise SystemExit("could not load {}".format(netlist_path))
    return hal_py, netlist


def elaborate(hal_py, netlist):
    """Attach the ALM Boolean functions that the gate library cannot carry."""
    tools = os.path.join(REPO, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from hal_agilex import hal_adapter

    return hal_adapter.elaborate(hal_py, netlist)


# ---------------------------------------------------------------------------
# step 1 -- first contact
# ---------------------------------------------------------------------------


def step_stats(hal_py, netlist):
    """What is in the box: types, nets, boundary."""
    histogram = {}
    for gate in netlist.get_gates():
        name = gate.get_type().get_name()
        histogram[name] = histogram.get(name, 0) + 1

    top = netlist.get_top_module()
    return {
        "design_name": netlist.get_design_name(),
        "gates": len(netlist.get_gates()),
        "nets": len(netlist.get_nets()),
        "modules": len(netlist.get_modules()),
        "gate_types": dict(sorted(histogram.items())),
        "top_module": top.get_name(),
        "global_inputs": sorted(n.get_name() for n in netlist.get_global_input_nets()),
        "global_outputs": sorted(n.get_name() for n in netlist.get_global_output_nets()),
        "gnd_nets": sorted(n.get_name() for n in netlist.get_gnd_nets()),
        "vcc_nets": sorted(n.get_name() for n in netlist.get_vcc_nets()),
    }


# ---------------------------------------------------------------------------
# step 2 -- the register bank
# ---------------------------------------------------------------------------


def _fan_in_name(gate, pin):
    net = gate.get_fan_in_net(pin)
    return net.get_name() if net is not None else None


def step_registers(hal_py, netlist):
    """Group the flip-flops by (clock, async clear, enable).

    This is the cheapest structural grouping there is and it needs no plugin:
    two registers that share a clock, a reset and an enable are, at minimum,
    candidates for being one word.  It is a *necessary* condition, never a
    sufficient one -- two unrelated 12-bit registers on the same clock would
    land in the same bucket here.
    """
    buckets = {}
    for gate in netlist.get_gates():
        if gate.get_type().get_name() != "tennm_ff":
            continue
        key = (
            _fan_in_name(gate, "clk"),
            _fan_in_name(gate, "clrn"),
            _fan_in_name(gate, "ena"),
        )
        buckets.setdefault(key, []).append(gate.get_name())

    return {
        "flip_flops": sum(len(v) for v in buckets.values()),
        "groups": [
            {
                "clk": key[0],
                "clrn": key[1],
                "ena": key[2],
                "size": len(names),
                "members": sorted(names),
            }
            for key, names in sorted(buckets.items(), key=lambda kv: -len(kv[1]))
        ],
    }


# ---------------------------------------------------------------------------
# step 3 -- feedback loops (strongly connected components)
# ---------------------------------------------------------------------------


def step_scc(hal_py, netlist, min_size=2):
    """Strongly connected components of the gate graph, via graph_algorithm.

    A combinational-only circuit is a DAG: it has no SCC bigger than one
    vertex.  Every SCC of size > 1 is therefore a *feedback loop* and, in a
    synchronous design, is state.  This is the single most useful first cut on
    an unknown netlist: it splits "logic" from "memory" without knowing
    anything about either.
    """
    # The plugin's bindings live in their own module, not in hal_py.
    graph_algorithm = __import__(
        "hal_plugins.graph_algorithm", fromlist=["graph_algorithm"]
    )

    graph = graph_algorithm.NetlistGraph.from_netlist(netlist)
    if graph is None:
        raise SystemExit("NetlistGraph.from_netlist returned None")
    components = graph_algorithm.get_connected_components(graph, True, min_size)
    if components is None:
        raise SystemExit("graph_algorithm.get_connected_components failed")

    result = []
    for vertices in components:
        gates = graph.get_gates_from_vertices(list(vertices))
        types = {}
        for gate in gates:
            key = gate.get_type().get_name()
            types[key] = types.get(key, 0) + 1
        result.append(
            {
                "size": len(gates),
                "gate_types": dict(sorted(types.items())),
                "gates": sorted(g.get_name() for g in gates),
            }
        )
    result.sort(key=lambda entry: -entry["size"])
    return {
        "vertices": graph.get_num_vertices(),
        "edges": graph.get_num_edges(),
        "min_size": min_size,
        "components": result,
    }


# ---------------------------------------------------------------------------
# step 4 -- the carry chain
# ---------------------------------------------------------------------------


def step_chain(hal_py, netlist):
    """Follow every cout -> cin link and report the chains in order.

    On an Altera ALM the carry is a *dedicated* connection: a net that leaves a
    ``tennm_lcell_comb.cout`` and enters another cell's ``cin`` can be nothing
    but a carry.  Chains are therefore recoverable exactly, with no heuristic.
    """
    successor = {}
    has_predecessor = set()
    cells = [
        g for g in netlist.get_gates() if g.get_type().get_name() == "tennm_lcell_comb"
    ]
    by_name = {g.get_name(): g for g in cells}

    for gate in cells:
        cout = gate.get_fan_out_net("cout")
        if cout is None:
            continue
        for endpoint in cout.get_destinations():
            if endpoint.get_pin().get_name() != "cin":
                continue
            successor[gate.get_name()] = endpoint.get_gate().get_name()
            has_predecessor.add(endpoint.get_gate().get_name())

    chains = []
    for gate in cells:
        name = gate.get_name()
        if name in has_predecessor:
            continue
        if name not in successor:
            continue  # a lone cell is not a chain
        walk = [name]
        while walk[-1] in successor:
            walk.append(successor[walk[-1]])
        chains.append(walk)

    described = []
    for walk in chains:
        slices = []
        for position, name in enumerate(walk):
            gate = by_name[name]
            slices.append(
                {
                    "position": position,
                    "cell": name,
                    "lut_mask": (gate.get_data("generic", "lut_mask") or ("", ""))[1],
                    "data_inputs": {
                        pin: _fan_in_name(gate, pin)
                        for pin in ("dataa", "datab", "datac", "datad", "datae", "dataf")
                        if _is_signal(gate, pin)
                    },
                    "sumout": gate.get_fan_out_net("sumout").get_name()
                    if gate.get_fan_out_net("sumout") is not None
                    else None,
                    "sumout_sink": _sequential_sinks(gate, "sumout"),
                }
            )
        described.append({"length": len(walk), "slices": slices})
    described.sort(key=lambda entry: -entry["length"])
    return {"chains": described}


def _is_signal(gate, pin):
    net = gate.get_fan_in_net(pin)
    return net is not None and not net.is_gnd_net() and not net.is_vcc_net()


def _sequential_sinks(gate, pin):
    net = gate.get_fan_out_net(pin)
    if net is None:
        return []
    return sorted(
        "{}.{}".format(ep.get_gate().get_name(), ep.get_pin().get_name())
        for ep in net.get_destinations()
    )


# ---------------------------------------------------------------------------
# step 5 -- read the increment out of the chain
# ---------------------------------------------------------------------------


def step_increment(hal_py, netlist):
    """Decide, per chain slice, what the second addend bit is.

    A ripple-carry slice computes ``sum = a ^ b ^ cin`` and
    ``cout = maj(a, b, cin)``.  Given the elaborated Boolean functions this
    step asks a much narrower question: with the *register* input of the slice
    called ``a``, is the remaining operand ``b`` a constant, and which one?
    ``b = 0`` for every bit above the LSB plus a carry-in of 1 at the bottom is
    an increment by one; a second register bank on ``b`` would be an adder.
    """
    report = []
    for gate in netlist.get_gates():
        if gate.get_type().get_name() != "tennm_lcell_comb":
            continue
        functions = {
            pin: str(func) for pin, func in gate.get_boolean_functions().items()
        }
        if not functions:
            continue
        signals = {
            pin: _fan_in_name(gate, pin)
            for pin in ("dataa", "datab", "datac", "datad", "datae", "dataf", "cin")
            if _is_signal(gate, pin)
        }
        constants = {
            pin: (1 if gate.get_fan_in_net(pin).is_vcc_net() else 0)
            for pin in ("dataa", "datab", "datac", "datad", "datae", "dataf", "cin")
            if gate.get_fan_in_net(pin) is not None and not _is_signal(gate, pin)
        }
        report.append(
            {
                "cell": gate.get_name(),
                "mode": "arithmetic"
                if gate.get_fan_out_net("sumout") is not None
                else "normal",
                "lut_mask": (gate.get_data("generic", "lut_mask") or ("", ""))[1],
                "signal_inputs": signals,
                "constant_inputs": constants,
                "functions": functions,
            }
        )
    report.sort(key=lambda entry: entry["cell"])
    return {"cells": report}


# ---------------------------------------------------------------------------
# step 6 -- which register bit leaves the design
# ---------------------------------------------------------------------------


def step_output(hal_py, netlist):
    """Trace every module output back to the register that drives it."""
    result = []
    for net in netlist.get_global_output_nets():
        sources = [
            "{}.{}".format(ep.get_gate().get_name(), ep.get_pin().get_name())
            for ep in net.get_sources()
        ]
        result.append({"net": net.get_name(), "driven_by": sorted(sources)})
    return {"outputs": sorted(result, key=lambda entry: entry["net"])}


# ---------------------------------------------------------------------------
# ordering the bank without the names
# ---------------------------------------------------------------------------


def step_order(hal_py, netlist):
    """Recover the bit order of the register bank from the carry chain alone.

    The names in this netlist happen to be readable (``count[7]``), which is a
    luxury a stripped netlist does not give you.  This step therefore ignores
    them: it orders the registers by their position along the carry chain,
    which is a property of the wiring.
    """
    chains = step_chain(hal_py, netlist)["chains"]
    if not chains:
        return {"order": [], "note": "no carry chain found"}
    order = []
    for entry in chains[0]["slices"]:
        for sink in entry["sumout_sink"]:
            gate_name, pin = sink.rsplit(".", 1)
            if pin != "d":
                continue
            order.append({"chain_position": entry["position"], "register": gate_name})
    return {"order": order, "chain_length": chains[0]["length"]}


# ---------------------------------------------------------------------------
# a picture of the chain that hal_viz does not draw
# ---------------------------------------------------------------------------


def chain_dot(hal_py, netlist):
    """Graphviz DOT of the carry chain with the register it feeds, per bit.

    hal_viz draws the netlist faithfully, which for this design means a hairball
    of 50 boxes.  The *understanding* is one dimensional -- a ladder -- so this
    draws the ladder: one rank per chain position, the ALM slice on the left,
    the register it clocks on the right, the carry running down the middle.
    """
    chains = step_chain(hal_py, netlist)["chains"]
    lines = [
        "// generated by examples/agilex3_walkthroughs/01_blinky_counter/analysis.py",
        'digraph carry_chain {',
        '  graph [rankdir="BT", fontname="Helvetica", fontsize="14", '
        'label="blinky_counter - carry chain and the register bank it drives", labelloc="t"];',
        '  node [shape="box", style="rounded,filled", fontname="Helvetica", fontsize="10"];',
        '  edge [fontname="Helvetica", fontsize="9"];',
    ]
    if not chains:
        lines.append('  empty [label="no carry chain found"];')
        lines.append("}")
        return "\n".join(lines) + "\n"

    slices = chains[0]["slices"]
    for entry in slices:
        cell = entry["cell"]
        operands = ", ".join(sorted(entry["data_inputs"].values()))
        lines.append(
            '  "alm_{p}" [fillcolor="#dbeafe", color="#2563eb", '
            'label="{cell}\\nmask {mask}\\noperand: {ops}"];'.format(
                p=entry["position"],
                cell=_esc(cell),
                mask=_esc(entry["lut_mask"]),
                ops=_esc(operands) or "-",
            )
        )
        for sink in entry["sumout_sink"]:
            gate_name, pin = sink.rsplit(".", 1)
            if pin != "d":
                continue
            lines.append(
                '  "ff_{p}" [fillcolor="#fef3c7", color="#d97706", '
                'label="{name}\\ntennm_ff"];'.format(p=entry["position"], name=_esc(gate_name))
            )
            lines.append(
                '  "alm_{p}" -> "ff_{p}" [label="sumout->d", color="#d97706"];'.format(
                    p=entry["position"]
                )
            )
            lines.append(
                '  "ff_{p}" -> "alm_{p}" [label="q", style="dashed", '
                'color="#9ca3af", constraint=false];'.format(p=entry["position"])
            )
        lines.append('  {{ rank=same; "alm_{p}"; "ff_{p}"; }}'.format(p=entry["position"]))

    for entry in slices[:-1]:
        lines.append(
            '  "alm_{a}" -> "alm_{b}" [label="cout->cin", color="#2563eb", '
            'penwidth="2"];'.format(a=entry["position"], b=entry["position"] + 1)
        )

    lines.append(
        '  "cin0" [shape="plaintext", style="", label="cin = 0"];'
    )
    lines.append('  "cin0" -> "alm_0" [color="#2563eb", penwidth="2"];')
    lines.append(
        '  "coutN" [shape="plaintext", style="", '
        'label="cout of the top slice is unconnected\\n=> the counter wraps"];'
    )
    lines.append(
        '  "alm_{p}" -> "coutN" [style="dotted", color="#9ca3af"];'.format(
            p=slices[-1]["position"]
        )
    )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _esc(text):
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

STEPS = {
    "stats": step_stats,
    "registers": step_registers,
    "scc": step_scc,
    "chain": step_chain,
    "increment": step_increment,
    "output": step_output,
    "order": step_order,
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=sorted(STEPS) + ["all"])
    parser.add_argument("-o", "--output", help="directory to write JSON artifacts into")
    parser.add_argument("--netlist", default=NETLIST)
    parser.add_argument("--gate-library", default=GATE_LIBRARY)
    parser.add_argument("--dot", help="write the carry-chain ladder diagram here")
    args = parser.parse_args(argv)

    hal_py, netlist = load(args.netlist, args.gate_library)
    elaborate(hal_py, netlist)

    if args.dot:
        os.makedirs(os.path.dirname(os.path.abspath(args.dot)), exist_ok=True)
        with open(args.dot, "w") as handle:
            handle.write(chain_dot(hal_py, netlist))
        print(args.dot)

    names = sorted(STEPS) if args.step == "all" else [args.step]
    # a fixed, pedagogical order rather than alphabetical
    order = ["stats", "registers", "scc", "chain", "increment", "output", "order"]
    names = [n for n in order if n in names]

    for name in names:
        result = STEPS[name](hal_py, netlist)
        text = json.dumps(result, indent=2, sort_keys=True)
        print("=== {} ===".format(name))
        print(text)
        if args.output:
            os.makedirs(args.output, exist_ok=True)
            path = os.path.join(args.output, "step_{}.json".format(name))
            with open(path, "w") as handle:
                handle.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
