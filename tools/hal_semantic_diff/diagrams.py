"""Side-by-side diagrams of the two cones behind a changed observation point.

The picture that helps is not the netlist: it is the *pair* of cones feeding
one point, drawn in the same frame, with the boundary variables they share
drawn once so the eye can follow them into both sides. That is what this module
builds, on top of ``hal_viz.dot`` -- the DOT emitter the repository already has,
with its quoting and cluster handling -- and rendered with ``hal_viz.render``,
which already knows how to find and drive Graphviz and how to survive its
absence.

Gates whose sub-cone signature does not occur in the other build are the
localization result, and they are the ones highlighted.
"""

import os

from hal_findings.adapters.common import call
from hal_viz.dot import DotGraph, truncate

__all__ = ["cone_pair_graph", "write_cone_pair", "render_if_possible"]

_SHARED_FILL = "#e8eef6"
_CHANGED_FILL = "#f7c7c7"
_UNCHANGED_FILL = "#eef3ea"
_OUTPUT_FILL = "#fdf0cd"


def _gate_label(gate):
    # A real newline: hal_viz.dot.escape() folds it into the DOT "\n" line break.
    return "{}\n{}".format(
        truncate(call(gate, "get_name", default=""), 28),
        call(call(gate, "get_type"), "get_name", default="<untyped>"),
    )


def _boundary_node_id(name):
    return "b_" + "".join(
        character if character.isalnum() or character == "_" else "_" for character in name
    )


def _add_side(graph, cone, side, changed_ids, boundary_nodes):
    cluster = graph.add_cluster(
        "cluster_{}".format(side),
        label="build {}  ({} gates, signature {})".format(
            side.upper(), len(cone.gates), cone.signature[:12]
        ),
        style="rounded",
        color="#8899aa",
        bgcolor="#fbfbfd",
    )
    for gate in cone.gates:
        gate_id = call(gate, "get_id")
        node_id = "{}_g{}".format(side, gate_id)
        changed = gate_id in changed_ids
        cluster.add_node(
            node_id,
            label=_gate_label(gate),
            shape="box",
            style="filled,rounded",
            fillcolor=_CHANGED_FILL if changed else _UNCHANGED_FILL,
            color="#b03030" if changed else "#7a8a7a",
            penwidth="2" if changed else "1",
        )
    output_id = "{}_out".format(side)
    cluster.add_node(
        output_id,
        label=truncate(call(cone.output_net, "get_name", default="<net>"), 32),
        shape="ellipse",
        style="filled",
        fillcolor=_OUTPUT_FILL,
    )

    net_to_node = {}
    for gate in cone.gates:
        gate_id = call(gate, "get_id")
        for endpoint in call(gate, "get_fan_out_endpoints", default=[]) or []:
            net = call(endpoint, "get_net")
            if net is not None:
                net_to_node[call(net, "get_id")] = (
                    "{}_g{}".format(side, gate_id),
                    call(call(endpoint, "get_pin"), "get_name", default=""),
                )

    for boundary in cone.boundaries:
        node_id = _boundary_node_id(boundary.name)
        if node_id not in boundary_nodes:
            graph.add_node(
                node_id,
                label="{}\n({})".format(truncate(boundary.name, 30), boundary.kind),
                shape="ellipse",
                style="filled",
                fillcolor=_SHARED_FILL,
                color="#5577aa",
            )
            boundary_nodes.add(node_id)
        net_to_node[boundary.net_id] = (node_id, "")

    for gate in cone.gates:
        gate_id = call(gate, "get_id")
        target = "{}_g{}".format(side, gate_id)
        for endpoint in call(gate, "get_fan_in_endpoints", default=[]) or []:
            net = call(endpoint, "get_net")
            if net is None:
                continue
            source = net_to_node.get(call(net, "get_id"))
            if source is None:
                continue
            graph.add_edge(
                source[0],
                target,
                label=truncate(call(call(endpoint, "get_pin"), "get_name", default=""), 10),
                fontsize="9",
                color="#666666",
            )

    root = net_to_node.get(call(cone.output_net, "get_id"))
    if root is not None:
        graph.add_edge(root[0], output_id, color="#666666")


def cone_pair_graph(outcome, label_a="A", label_b="B"):
    """Build a :class:`hal_viz.dot.DotGraph` for one comparison outcome."""
    changed_a, changed_b = outcome.changed_gates()
    graph = DotGraph(
        name="cone_pair",
        comment=(
            "hal_semantic_diff: cones feeding {} in build {} and build {} "
            "(status: {})".format(outcome.point.label, label_a, label_b, outcome.status)
        ),
    )
    graph.graph_attrs.update(
        {
            "rankdir": "LR",
            "labelloc": "t",
            "label": "{}  --  {}".format(outcome.point.label, outcome.status),
            "fontname": "Helvetica",
        }
    )
    graph.node_defaults.update({"fontname": "Helvetica", "fontsize": "10"})
    graph.edge_defaults.update({"fontname": "Helvetica", "fontsize": "9"})

    boundary_nodes = set()
    if outcome.cone_a is not None:
        _add_side(
            graph,
            outcome.cone_a,
            "a",
            {call(gate, "get_id") for gate in changed_a},
            boundary_nodes,
        )
    if outcome.cone_b is not None:
        _add_side(
            graph,
            outcome.cone_b,
            "b",
            {call(gate, "get_id") for gate in changed_b},
            boundary_nodes,
        )
    return graph


def write_cone_pair(outcome, directory, label_a="A", label_b="B"):
    """Write the cone pair of ``outcome`` as a ``.dot`` file; return its path."""
    safe = "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in outcome.point.key
    )
    path = os.path.join(directory, "cone_{}.dot".format(safe))
    cone_pair_graph(outcome, label_a=label_a, label_b=label_b).write(path)
    return path


def render_if_possible(dot_path, fmt="svg", dot_binary=None, timeout=120):
    """Render ``dot_path`` with Graphviz if it is available; else return ``None``.

    A missing ``dot`` binary is not an error here for the same reason it is not
    one in ``hal_viz``: the ``.dot`` file is the artifact, the picture is a
    convenience.
    """
    try:
        from hal_viz import render
    except ImportError:  # pragma: no cover - broken checkout
        return None
    binary = render.find_dot_binary(dot_binary)
    if not binary:
        return None
    out_path = os.path.splitext(dot_path)[0] + "." + fmt
    try:
        return render.render_dot(dot_path, out_path, fmt, dot_binary=binary, timeout=timeout)
    except Exception:  # noqa: BLE001 - a failed render must not fail the analysis
        return None
