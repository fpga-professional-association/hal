"""Connected components of the netlist graph -- the runner's reference analysis.

Why this one is the first workflow: it is *deterministic*.
``NetlistGraph.from_netlist`` builds one vertex per gate and one directed edge
per ``(source, destination)`` pair of every net, and igraph's component
decomposition of that graph is a function of the graph alone.  Run it twice on
the same netlist and the findings are byte-identical -- which is exactly what a
checkpoint cache and a manifest digest need to be worth anything.
``tests/headless_smoke/real_netlist_smoke.py`` already pins the expected answer
for ``examples/uart.zip`` (407 vertices, 1604 edges, feedback loops of 68/50/15).

What a finding here may claim
-----------------------------
A strongly connected component is a *proved* property -- of the graph.  It is
not a statement about behaviour, and pretending otherwise is precisely the
failure mode the findings schema exists to prevent.  So each component is
recorded as ``proven_under_assumptions`` with a ``structural`` method and two
assumptions spelled out: how the graph was derived from the netlist, and that
the claim is about connectivity rather than function.  Downgrading it to
``heuristic`` would be dishonest in the other direction: nothing here is a
guess.
"""

from hal_findings import model
from hal_findings.adapters import common

from .. import __version__
from .dispatch import StepError, StepOutcome

__all__ = ["NAME", "run"]

NAME = "graph_algorithm.connected_components"

_METHOD_DESCRIPTION = (
    "The netlist is converted into a directed graph by "
    "graph_algorithm.NetlistGraph.from_netlist (one vertex per gate, one edge per "
    "(source, destination) pair of every net, parallel edges kept) and igraph's "
    "connected-component decomposition is applied to it."
)


def _assumptions(config):
    return [
        model.assumption(
            "netlist-graph-construction",
            "The analysed graph is the one graph_algorithm.NetlistGraph.from_netlist "
            "builds with create_dummy_vertices={}: one vertex per gate and one directed "
            "edge for every (source, destination) pair of every net, with parallel edges "
            "kept. A different graph abstraction yields different components.".format(
                bool(config.get("create_dummy_vertices"))
            ),
            kind="tool",
        ),
        model.assumption(
            "structural-claim-only",
            "The claim is about connectivity in that graph and about the netlist exactly "
            "as loaded from the pinned artifact. It says nothing about the design's "
            "behaviour: a strongly connected component is a structural feedback path, "
            "not a proven sequential loop of the implemented function.",
            kind="structural",
        ),
    ]


def _method(config):
    return model.method(
        "connected components of the netlist graph (igraph)",
        "structural",
        False,
        description=_METHOD_DESCRIPTION,
        parameters={
            "strong": bool(config["strong"]),
            "min_size": int(config["min_size"]),
            "create_dummy_vertices": bool(config.get("create_dummy_vertices")),
        },
    )


def _gates_of(graph, vertices):
    """Gates of ``vertices``; returns ``(gates, dummy_count)``.

    ``get_gates_from_vertices`` yields ``None`` for dummy vertices, which exist
    only when the caller asked for them -- counting them beats dropping them
    silently.
    """
    gates = graph.get_gates_from_vertices(sorted(int(vertex) for vertex in vertices))
    if gates is None:
        raise StepError(
            "NetlistGraph.get_gates_from_vertices() failed for a component of {} "
            "vertices; see the HAL log".format(len(vertices)),
            kind="plugin_error",
        )
    real = [gate for gate in gates if gate is not None]
    return real, len(gates) - len(real)


def _component_finding(index, vertices, gates, dummy_count, artifact_id, config, method, assumptions):
    limit = int(config["max_gates_per_component"])
    ordered = sorted(gates, key=lambda gate: common.call(gate, "get_id", default=0))
    truncated = len(ordered) > limit
    listed = ordered[:limit]
    gate_refs = [common.gate_reference(gate, artifact_id) for gate in listed]
    gate_types = sorted({ref["type"] for ref in gate_refs if ref.get("type")})

    kind = "strongly" if config["strong"] else "weakly"
    data = {
        "vertices": sorted(int(vertex) for vertex in vertices),
        "strong": bool(config["strong"]),
        "component_size": len(vertices),
        "gates_listed": len(gate_refs),
        "gates_truncated": truncated,
    }
    if dummy_count:
        data["dummy_vertices"] = dummy_count

    summary = (
        "{} gates form a {} connected component of the netlist graph: every gate in it "
        "reaches every other{}.".format(
            len(vertices),
            kind,
            " through directed paths in both directions" if config["strong"] else "",
        )
    )
    if truncated:
        summary += (
            " Only the first {} gates (by gate ID) are listed here; the component size "
            "above is complete.".format(limit)
        )
    if dummy_count:
        summary += " {} of its vertices are dummy vertices with no gate.".format(dummy_count)

    return model.finding(
        "graph_algorithm/component/{:04d}".format(index),
        "{} connected component of {} gates".format(kind.capitalize(), len(vertices)),
        model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
        method,
        model.scope(
            [artifact_id],
            description="the gates of one {} connected component".format(kind),
            gates=gate_refs,
            gate_types=gate_types or None,
        ),
        summary=summary,
        severity="info",
        assumptions=assumptions,
        bounds_dict=model.unbounded(
            description="a graph-theoretic property; no cycle bound is involved"
        ),
        metrics={"component_size": len(vertices), "gate_count": len(gates)},
        data=data,
        tags=["graph-algorithm", "feedback-loop" if config["strong"] else "connectivity"],
    )


def _summary_finding(
    artifact_id, config, method, assumptions, vertices, edges, sizes, reported, dummy_total
):
    return model.finding(
        "graph_algorithm/graph/summary",
        "Netlist graph: {} vertices, {} edges, {} component(s) of size >= {}".format(
            vertices, edges, len(sizes), config["min_size"]
        ),
        model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
        method,
        model.scope([artifact_id], description="the netlist graph as a whole"),
        summary=(
            "The netlist graph has {} vertices and {} edges. {} {} connected "
            "component(s) of at least {} vertices were found; {} of them are reported "
            "as individual findings, largest first.".format(
                vertices,
                edges,
                len(sizes),
                "strongly" if config["strong"] else "weakly",
                config["min_size"],
                reported,
            )
        ),
        severity="info",
        assumptions=assumptions,
        bounds_dict=model.unbounded(
            description="a graph-theoretic property; no cycle bound is involved"
        ),
        metrics={
            "vertices": vertices,
            "edges": edges,
            "components": len(sizes),
            "components_reported": reported,
            "largest_component": sizes[0] if sizes else 0,
        },
        data={
            "component_sizes": sizes,
            "strong": bool(config["strong"]),
            "min_size": int(config["min_size"]),
            "dummy_vertices": dummy_total,
        },
        tags=["graph-algorithm", "summary"],
    )


def run(hal_py, netlist, config, context):
    """Build the netlist graph, decompose it, and report every component."""
    graph_algorithm = context.plugin_module

    graph = graph_algorithm.NetlistGraph.from_netlist(
        netlist, bool(config.get("create_dummy_vertices", False))
    )
    if graph is None:
        raise StepError(
            "NetlistGraph.from_netlist() returned None; see the HAL log for the reason",
            kind="plugin_error",
        )

    vertices = graph.get_num_vertices()
    edges = graph.get_num_edges()

    components = graph_algorithm.get_connected_components(
        graph, bool(config["strong"]), int(config["min_size"])
    )
    if components is None:
        raise StepError(
            "graph_algorithm.get_connected_components() returned None; see the HAL log",
            kind="plugin_error",
        )

    # Largest first, ties broken by the vertex IDs, so two runs order the findings
    # identically and the document digest is stable.
    ordered = sorted(
        (sorted(int(vertex) for vertex in component) for component in components),
        key=lambda component: (-len(component), component),
    )
    sizes = [len(component) for component in ordered]
    reported = min(len(ordered), int(config["max_components"]))

    artifact_id = context.artifact_id
    method = _method(config)
    assumptions = _assumptions(config)

    findings = []
    dummy_total = 0
    for index, component in enumerate(ordered[:reported], start=1):
        gates, dummy_count = _gates_of(graph, component)
        dummy_total += dummy_count
        findings.append(
            _component_finding(
                index, component, gates, dummy_count, artifact_id, config, method, assumptions
            )
        )

    findings.append(
        _summary_finding(
            artifact_id, config, method, assumptions, vertices, edges, sizes, reported, dummy_total
        )
    )

    notes = [
        "component findings are ordered largest first and numbered accordingly; the "
        "numbering is local to this run and is not a HAL object ID",
    ]
    if reported < len(ordered):
        notes.append(
            "{} of {} components are reported as findings (max_components={}); the "
            "complete list of sizes is on graph_algorithm/graph/summary".format(
                reported, len(ordered), config["max_components"]
            )
        )

    document = model.document(
        {"name": "hal_runner.steps.graph_algorithm_components", "version": __version__},
        [common.netlist_artifact(netlist, artifact_id, path=context.netlist_path)],
        {
            "plugin": {
                "name": context.analysis.plugin,
                "version": context.plugin_version,
                "description": "graph algorithms on the netlist graph (igraph)",
            },
            "entry_point": context.analysis.entry_point,
            "configuration": dict(config),
        },
        findings,
        generated_at=common.utc_now(),
        notes=notes,
    )

    return StepOutcome(
        document,
        metrics={
            "vertices": vertices,
            "edges": edges,
            "components": len(ordered),
            "largest_component": sizes[0] if sizes else 0,
        },
        notes=notes,
    )
