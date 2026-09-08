"""The deterministic block diagram, emitted through :mod:`hal_viz.dot`.

The diagram has one job the report cannot do: make the *strength* of each label
visible at a glance.  So the encoding is fixed and stated on the drawing itself,
in a legend that is part of the graph rather than a caption somebody has to be
told about:

===================== ==========================================================
verified              solid double border, green -- a decision procedure proved
                      this; the assumptions are in the report
verified (bounded)    as above, plus the cycle bound in the label
heuristic             dashed border, amber -- structural evidence, a guess
unknown / inconclusive dotted border, grey -- checked and undecided
refuted               red border -- an analysis found a counterexample
unknown region        grey filled note with a "?" -- gates *no* analysis claimed
===================== ==========================================================

Nothing is dropped to make the picture nicer.  Unknown regions are drawn, edges
that were truncated say so, and a block whose claims disagree is marked
``contested``.  Layout is fully determined by the document (which
:mod:`hal_explain.serialize` already sorted), so two runs produce byte-identical
``.dot`` files.
"""

import os
import sys

if __package__ in (None, ""):  # pragma: no cover - direct execution
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_viz.dot import DotGraph, truncate

from . import model

__all__ = ["CONFIDENCE_STYLE", "block_diagram", "write_block_diagram"]

#: confidence -> node attributes.  The single source of the visual encoding.
CONFIDENCE_STYLE = {
    model.CONFIDENCE_VERIFIED: {
        "shape": "box",
        "peripheries": "2",
        "style": "filled",
        "fillcolor": "#dff3df",
        "color": "#1d7a1d",
        "penwidth": "1.6",
    },
    model.CONFIDENCE_HEURISTIC: {
        "shape": "box",
        "style": "filled,dashed",
        "fillcolor": "#fdf3d8",
        "color": "#b07d1a",
    },
    model.CONFIDENCE_UNKNOWN: {
        "shape": "box",
        "style": "filled,dotted",
        "fillcolor": "#eeeeee",
        "color": "#808080",
        "fontcolor": "#505050",
    },
    model.CONFIDENCE_REFUTED: {
        "shape": "box",
        "style": "filled",
        "fillcolor": "#ffe0e0",
        "color": "#b30000",
        "penwidth": "1.6",
    },
}

_REGION_STYLE = {
    "shape": "note",
    "style": "filled,dashed",
    "fillcolor": "#e8e8e8",
    "color": "#707070",
    "fontcolor": "#404040",
}

_PORT_STYLE = {"shape": "invhouse", "style": "filled", "fillcolor": "#e6eefc",
               "color": "#3c5a99"}

_CONTROL_EDGE = {"style": "dashed", "color": "#707070", "fontcolor": "#707070"}

#: Drawn between blocks that claim some of the same gates.  Without it a block
#: whose gates are all owned by a stronger one has no edges at all and looks
#: disconnected, which is the one thing it is not.
_OVERLAP_EDGE = {
    "style": "dotted",
    "color": "#7a3ba8",
    "fontcolor": "#7a3ba8",
    "dir": "none",
    "constraint": "false",
}


def _node_id(identifier):
    """Node ids are the document's own ids; hal_viz quotes them on emission."""
    return str(identifier)


def _block_label(block, max_gates):
    lines = [block["label"]]
    lines.append("{} [{}]".format(block["kind"].replace("_", " "), block["confidence"]))
    if block.get("bounded"):
        bound = None
        for claim in block.get("claims", []):
            if claim.get("cycle_bound") is not None:
                bound = claim["cycle_bound"]
                break
        lines.append(
            "bounded{}".format("" if bound is None else " (<= {} cycles)".format(bound))
        )
    if block.get("contested"):
        lines.append("CONTESTED: a claim about this block was refuted")
    lines.append("{} gate(s)".format(len(block.get("gates", []))))
    attributes = block.get("attributes") or {}
    for key in ("operation", "state_count", "width", "output_width"):
        if attributes.get(key) is not None:
            lines.append("{}: {}".format(key.replace("_", " "), attributes[key]))
    names = [ref.get("name", "") for ref in block.get("gates", [])]
    if names:
        shown = names[:max_gates]
        suffix = "" if len(shown) == len(names) else ", +{} more".format(len(names) - len(shown))
        lines.append(truncate(", ".join(shown) + suffix, 60))
    return "\n".join(lines)


def _region_label(region, max_gates):
    lines = ["? {}".format(region["label"]), "no analysis claimed these gates"]
    if region.get("sequential_gate_count"):
        lines.append("{} sequential".format(region["sequential_gate_count"]))
    histogram = region.get("gate_types") or {}
    if histogram:
        parts = ["{}x{}".format(count, name) for name, count in sorted(histogram.items())]
        lines.append(truncate(", ".join(parts), 60))
    names = [ref.get("name", "") for ref in region.get("gates", [])]
    if names:
        shown = names[:max_gates]
        suffix = "" if len(shown) == len(names) else ", +{} more".format(len(names) - len(shown))
        lines.append(truncate(", ".join(shown) + suffix, 60))
    return "\n".join(lines)


def _legend(graph):
    legend = graph.add_cluster("legend", label="how to read this diagram")
    legend.graph_attrs.update({"style": "dotted", "color": "#999999", "fontsize": "10"})
    legend.node_defaults.update({"fontsize": "9"})
    for confidence, caption in (
        (model.CONFIDENCE_VERIFIED, "verified\n(a decision procedure proved it)"),
        (model.CONFIDENCE_HEURISTIC, "heuristic\n(structural evidence, a guess)"),
        (model.CONFIDENCE_UNKNOWN, "unknown\n(checked, undecided)"),
        (model.CONFIDENCE_REFUTED, "refuted\n(a counterexample exists)"),
    ):
        legend.add_node(
            "legend/" + confidence, label=caption, **CONFIDENCE_STYLE[confidence]
        )
    legend.add_node(
        "legend/unknown-region",
        label="unclassified region\n(gates no analysis claimed)",
        **_REGION_STYLE
    )
    return legend


def block_diagram(document, title=None, max_gate_names=6, legend=True):
    """Build a :class:`hal_viz.dot.DotGraph` for a recovered-block document."""
    design = document.get("design") or {}
    coverage = document.get("coverage") or {}
    name = design.get("design_name") or design.get("artifact_id") or "design"

    comment_lines = [
        "generated by tools/hal_explain",
        "design: {}".format(name),
        "gates: {} total, {} in blocks, {} unclassified".format(
            coverage.get("gates_total", "?"),
            coverage.get("gates_in_blocks", "?"),
            coverage.get("gates_unclassified", "?"),
        ),
        "sources: {}".format(
            ", ".join(
                sorted(entry.get("source_id", "?") for entry in document.get("sources", []))
            )
            or "none"
        ),
        "block borders encode claim strength; see the legend cluster",
    ]

    graph = DotGraph(name="recovered_blocks", directed=True,
                     comment="\n".join(comment_lines))
    graph.graph_attrs.update(
        {
            "rankdir": "LR",
            "labelloc": "t",
            "label": title
            or "{} - recovered block model ({}/{} gates classified)".format(
                name, coverage.get("gates_in_blocks", 0), coverage.get("gates_total", 0)
            ),
            "fontname": "Helvetica",
            "compound": "true",
        }
    )
    graph.node_defaults.update({"fontname": "Helvetica", "fontsize": "10"})
    graph.edge_defaults.update({"fontname": "Helvetica", "fontsize": "9"})

    for port in document.get("ports", []):
        graph.add_node(
            _node_id(port["port_id"]),
            label="{}\n{} net(s)".format(
                port.get("label") or port["port_id"], len(port.get("nets", []))
            ),
            **_PORT_STYLE
        )

    for block in document.get("blocks", []):
        style = dict(CONFIDENCE_STYLE.get(block["confidence"], CONFIDENCE_STYLE["unknown"]))
        if block.get("contested"):
            style["color"] = "#b30000"
            style["penwidth"] = "2.0"
        graph.add_node(
            _node_id(block["block_id"]),
            label=_block_label(block, max_gate_names),
            **style
        )

    for region in document.get("unknown_regions", []):
        graph.add_node(
            _node_id(region["region_id"]),
            label=_region_label(region, max_gate_names),
            **_REGION_STYLE
        )

    for edge in document.get("edges", []):
        attrs = dict(_CONTROL_EDGE) if edge.get("kind") == "control" else {}
        label = str(edge.get("net_count", ""))
        if edge.get("truncated"):
            label += "*"
        graph.add_edge(
            _node_id(edge["source"]), _node_id(edge["target"]), label=label, **attrs
        )

    shared = {}
    for overlap in document.get("overlaps") or []:
        block_ids = sorted(overlap.get("block_ids", []))
        for index, left in enumerate(block_ids):
            for right in block_ids[index + 1 :]:
                shared[(left, right)] = shared.get((left, right), 0) + 1
    for (left, right), count in sorted(shared.items()):
        graph.add_edge(
            _node_id(left),
            _node_id(right),
            label="shares {} gate(s)".format(count),
            **_OVERLAP_EDGE
        )

    if legend:
        _legend(graph)
    return graph


def write_block_diagram(document, path, **kwargs):
    """Write the block diagram and return ``(path, node_count, edge_count)``."""
    graph = block_diagram(document, **kwargs)
    graph.write(path)
    return str(path), graph.node_count, graph.edge_count
