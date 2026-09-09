"""Every HAL-side analysis step of walkthrough 04, in the order the guide tells it.

Run inside the build container:

    export HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib
    python3 examples/agilex3_walkthroughs/04_pwm_generator/artifacts/analyze.py \
        --repo /guide_04

It writes the text artifacts this walkthrough's guide.html quotes, under
<example>/artifacts/.  Images are produced separately by tools/hal_viz; the
exact commands are in the guide.
"""

import argparse
import collections
import json
import os
import sys

EXAMPLE_REL = "examples/agilex3_walkthroughs/04_pwm_generator"
GATE_LIBRARY_REL = "plugins/gate_libraries/definitions/AGILEX_TENNM.hgl"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")))
    ap.add_argument("--hal-lib", default=os.environ.get("HAL_PY_PATH", "/work/build/lib"))
    ap.add_argument("--only", choices=["all", "scc"], default="all",
                    help="'scc' runs only the graph_algorithm step (it is the slow one)")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    example = os.path.join(repo, EXAMPLE_REL)
    artifacts = os.path.join(example, "artifacts")
    os.makedirs(artifacts, exist_ok=True)

    sys.path.insert(0, args.hal_lib)
    sys.path.insert(0, os.path.join(repo, "tools"))

    import hal_py

    hal_py.plugin_manager.load_all_plugins([os.path.join(args.hal_lib, "hal_plugins")])

    from hal_agilex import hal_adapter

    netlist = hal_py.NetlistFactory.load_netlist(
        os.path.join(example, "netlist", "netlist.hal.v"),
        os.path.join(repo, GATE_LIBRARY_REL),
    )
    if netlist is None:
        raise SystemExit("could not load the netlist")
    report = hal_adapter.elaborate(hal_py, netlist)

    def write(name, lines):
        path = os.path.join(artifacts, name)
        with open(path, "w") as handle:
            handle.write("\n".join(lines) + "\n")
        print("wrote", path)

    counts = collections.Counter(g.get_type().get_name() for g in netlist.get_gates())

    if args.only == "scc":
        from hal_plugins import graph_algorithm

        graph = graph_algorithm.NetlistGraph.from_netlist(netlist)
        components = graph_algorithm.get_connected_components(graph, True, 2)
        lines = ["strongly connected components with at least 2 vertices: %d" % len(components)]
        for index, component in enumerate(components):
            gates = graph.get_gates_from_vertices(component)
            names = sorted(g.get_name() for g in gates)
            types = collections.Counter(g.get_type().get_name() for g in gates)
            lines.append("")
            lines.append("SCC %d: %d gates  %s" % (index, len(names), dict(types)))
            lines += ["    %s" % name for name in names]
        write("05_scc.txt", lines)
        return

    # ---------------------------------------------------------------- 01 stats
    lines = [
        "design name : %s" % netlist.get_design_name(),
        "gates       : %d" % len(netlist.get_gates()),
        "nets        : %d" % len(netlist.get_nets()),
        "modules     : %d" % len(netlist.get_modules()),
    ]
    lines += ["  %-18s %d" % (k, v) for k, v in sorted(counts.items())]
    lines += [
        "top module  : %s" % netlist.get_top_module().get_name(),
        "submodules  : %d" % len(netlist.get_top_module().get_submodules(recursive=True)),
        "inputs      : %s" % sorted(n.get_name() for n in netlist.get_global_input_nets()),
        "outputs     : %s" % sorted(n.get_name() for n in netlist.get_global_output_nets()),
        "elaborated  : %d lcell gates given boolean functions" % report["elaborated"],
        "checked ff  : %d" % report["checked_ff"],
        "refused     : %s" % report["refused"],
    ]
    write("01_stats.txt", lines)

    # ------------------------------------------------- 02 flip-flop signatures
    def net_name(gate, pin):
        net = gate.get_fan_in_net(pin)
        return net.get_name() if net is not None else "-"

    signatures = collections.defaultdict(list)
    for gate in netlist.get_gates():
        if gate.get_type().get_name() != "tennm_ff":
            continue
        key = (net_name(gate, "clk"), net_name(gate, "ena"), net_name(gate, "clrn"))
        signatures[key].append(gate)

    lines = ["%d flip-flops, %d distinct (clk, ena, clrn) signatures" % (
        sum(len(v) for v in signatures.values()), len(signatures))]
    for key, gates in sorted(signatures.items(), key=lambda kv: -len(kv[1])):
        lines.append("")
        lines.append("clk=%s  ena=%s  clrn=%s   -> %d flip-flops" % (key + (len(gates),)))
        for gate in sorted(gates, key=lambda g: g.get_name()):
            d_net = gate.get_fan_in_net("d")
            q_net = gate.get_fan_out_net("q")
            lines.append("    %-12s d <- %-26s q -> %s" % (
                gate.get_name(),
                d_net.get_name() if d_net else "-",
                q_net.get_name() if q_net else "-",
            ))
    write("02_ff_signatures.txt", lines)

    # ------------------------------------------------------- 03 carry chains
    lcells = [g for g in netlist.get_gates() if g.get_type().get_name() == "tennm_lcell_comb"]
    by_name = {g.get_name(): g for g in netlist.get_gates()}

    def carry_successor(gate):
        net = gate.get_fan_out_net("cout")
        if net is None:
            return None
        for endpoint in net.get_destinations():
            if endpoint.get_pin().get_name() == "cin":
                return endpoint.get_gate()
        return None

    def has_carry_in(gate):
        net = gate.get_fan_in_net("cin")
        if net is None:
            return False
        return not (net.is_gnd_net() or net.is_vcc_net())

    heads = [g for g in lcells if not has_carry_in(g) and (
        g.get_fan_out_net("cout") is not None or g.get_fan_out_net("sumout") is not None)]
    chains = []
    for head in sorted(heads, key=lambda g: g.get_name()):
        chain, cur = [], head
        while cur is not None:
            chain.append(cur)
            cur = carry_successor(cur)
        if len(chain) > 1:
            chains.append(chain)

    lines = ["%d carry chain(s) found by following cout -> cin" % len(chains)]
    for index, chain in enumerate(chains):
        lines.append("")
        lines.append("chain %d: %d cells" % (index, len(chain)))
        for position, gate in enumerate(chain):
            data = []
            for pin in ("dataa", "datab", "datac", "datad", "datae", "dataf"):
                net = gate.get_fan_in_net(pin)
                if net is not None and not (net.is_gnd_net() or net.is_vcc_net()):
                    data.append("%s=%s" % (pin, net.get_name()))
            sinks = []
            for pin in ("sumout", "cout", "combout"):
                net = gate.get_fan_out_net(pin)
                if net is None:
                    continue
                for endpoint in net.get_destinations():
                    sinks.append("%s->%s.%s" % (pin, endpoint.get_gate().get_name(), endpoint.get_pin().get_name()))
                if net.is_global_output_net():
                    sinks.append("%s->PORT %s" % (pin, net.get_name()))
            mask = gate.get_data("generic", "lut_mask")
            lines.append("  [%d] %-24s mask=%s" % (position, gate.get_name(), mask[1] if mask else "?"))
            lines.append("        inputs : %s" % (", ".join(data) if data else "(all constant)"))
            lines.append("        drives : %s" % (", ".join(sinks) if sinks else "(nothing)"))
    write("03_carry_chains.txt", lines)

    # -------------------------------------------- 04 boolean functions per gate
    lines = ["Boolean functions attached by hal_agilex.hal_adapter.elaborate()",
             "(the gate library carries none for tennm_lcell_comb -- see the guide)", ""]
    for gate in sorted(lcells, key=lambda g: g.get_name()):
        functions = gate.get_boolean_functions()
        if not functions:
            lines.append("%-26s (no function attached)" % gate.get_name())
            continue
        for pin, function in sorted(functions.items()):
            lines.append("%-26s %-8s = %s" % (gate.get_name(), pin, function))
    write("04_boolean_functions.txt", lines)

    # 05_scc.txt is produced by a separate `--only scc` run: the graph_algorithm
    # plugin pulls in igraph and is by far the heaviest step here, so it is kept
    # out of the way of the cheap structural passes above.

    # -------------------------------------------------- 06 what drives a port
    lines = []
    for net in sorted(netlist.get_global_output_nets(), key=lambda n: n.get_name()):
        lines.append("output %s" % net.get_name())
        for endpoint in net.get_sources():
            gate = endpoint.get_gate()
            functions = gate.get_boolean_functions()
            lines.append("    driven by %s.%s (%s)" % (gate.get_name(), endpoint.get_pin().get_name(), gate.get_type().get_name()))
            for pin, function in sorted(functions.items()):
                lines.append("        %s = %s" % (pin, function))
    write("06_output_cones.txt", lines)

    summary = {
        "gates": len(netlist.get_gates()),
        "nets": len(netlist.get_nets()),
        "modules": len(netlist.get_modules()),
        "gate_types": dict(counts),
        "ff_signatures": {
            "clk=%s|ena=%s|clrn=%s" % key: len(gates) for key, gates in signatures.items()
        },
        "carry_chains": [[g.get_name() for g in chain] for chain in chains],
    }
    with open(os.path.join(artifacts, "07_summary.json"), "w") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
    print("wrote", os.path.join(artifacts, "07_summary.json"))


if __name__ == "__main__":
    main()
