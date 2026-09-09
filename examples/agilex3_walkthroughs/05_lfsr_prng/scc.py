#!/usr/bin/env python3
"""Strongly connected components of the netlist, via the graph_algorithm plugin.

Feedback is what separates a shift *register* from a shift *chain*: an SCC that
contains flip-flops is a loop the state can circulate in.  This script prints
every strongly connected component of size >= 2 with the gates it contains, and
writes a Graphviz file in which the largest one is highlighted.

    HAL_BASE_PATH=/work/build PYTHONPATH=/work/build/lib \\
        python3 examples/agilex3_walkthroughs/05_lfsr_prng/scc.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools"))

from hal_agilex import hal_adapter                                # noqa: E402

NETLIST = os.path.join(HERE, "netlist.hal.v")
GATE_LIBRARY = os.path.join(REPO, "plugins", "gate_libraries", "definitions",
                            "AGILEX_TENNM.hgl")
DOT_PATH = os.path.join(HERE, "artifacts", "scc.dot")


def main():
    hal_lib = [d for d in os.environ.get("HAL_PY_PATH", "").split(os.pathsep) if d]
    hal_py = hal_adapter.import_hal(hal_lib)
    netlist = hal_adapter.load_netlist(hal_py, NETLIST, GATE_LIBRARY)

    module = __import__("hal_plugins.graph_algorithm", fromlist=["graph_algorithm"])
    graph = module.NetlistGraph.from_netlist(netlist)
    if graph is None:
        raise SystemExit("NetlistGraph.from_netlist returned None")

    print("netlist graph: %d vertices, %d edges"
          % (graph.get_num_vertices(), graph.get_num_edges()))

    weak = module.get_connected_components(graph, False, 0)
    print("weakly connected components: %s"
          % sorted((len(c) for c in weak), reverse=True))

    strong = module.get_connected_components(graph, True, 2)
    print("strongly connected components of size >= 2: %s"
          % sorted((len(c) for c in strong), reverse=True))

    largest = []
    for index, component in enumerate(sorted(strong, key=len, reverse=True)):
        gates = graph.get_gates_from_vertices(component)
        types = {}
        for gate in gates:
            name = gate.get_type().get_name()
            types[name] = types.get(name, 0) + 1
        print("\ncomponent %d: %d gates %s" % (index, len(gates), types))
        for gate in sorted(gates, key=lambda g: g.get_id()):
            print("    %4d  %-28s %s" % (gate.get_id(), gate.get_name(),
                                         gate.get_type().get_name()))
        if not largest:
            largest = [g.get_id() for g in gates]

    write_dot(netlist, set(largest))
    print("\nwrote %s" % DOT_PATH)
    print("gates inside the largest SCC: %d of %d"
          % (len(largest), len(netlist.get_gates())))


def write_dot(netlist, highlight):
    lines = ["digraph scc {",
             '  graph [rankdir=LR, fontname="Helvetica", labelloc=t, '
             'label="strongly connected component of lfsr_prng (filled)"];',
             '  node [shape=box, fontname="Helvetica", style=filled, fillcolor=white];']
    for gate in sorted(netlist.get_gates(), key=lambda g: g.get_id()):
        inside = gate.get_id() in highlight
        lines.append('  g%d [label="%s\\n%s"%s];' % (
            gate.get_id(), gate.get_name().replace("\\", ""),
            gate.get_type().get_name(),
            ', fillcolor="#ffe9c9", color="#b3762a", penwidth=2' if inside else ""))
    for net in netlist.get_nets():
        for source in net.get_sources():
            for destination in net.get_destinations():
                src, dst = source.get_gate(), destination.get_gate()
                both = src.get_id() in highlight and dst.get_id() in highlight
                lines.append('  g%d -> g%d [%s];' % (
                    src.get_id(), dst.get_id(),
                    'color="#b3762a", penwidth=2' if both else 'color="#999999"'))
    lines.append("}")
    os.makedirs(os.path.dirname(DOT_PATH), exist_ok=True)
    with open(DOT_PATH, "w") as handle:
        handle.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
