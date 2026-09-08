"""Turn HAL netlist objects into :class:`~hal_viz.dot.DotGraph` instances.

Nothing in this module imports ``hal_py``.  Every netlist object is used
purely through duck-typed accessors that match the ``hal_py`` bindings:

* ``Gate``   -- ``get_id``, ``get_name``, ``get_type``, ``get_module``,
  ``get_fan_out_endpoints``, ``get_unique_successors``,
  ``get_unique_predecessors``, ``is_gnd_gate``, ``is_vcc_gate``
* ``GateType``  -- ``get_name``
* ``Endpoint``  -- ``get_gate``, ``get_net``, ``get_pin``
* ``Net``    -- ``get_id``, ``get_name``, ``get_destinations``,
  ``is_global_input_net``, ``is_global_output_net``
* ``Module`` -- ``get_id``, ``get_name``, ``get_type``, ``get_gates``,
  ``get_submodules``, ``get_parent_module``

That keeps the whole graph-building layer testable with plain stub objects on
a machine where HAL cannot be built.
"""

from .dot import DotGraph, truncate

__all__ = [
    "ScopeTooLarge",
    "collect_module_gates",
    "collect_neighborhood",
    "build_netlist_graph",
    "build_module_tree_graph",
]

# Palette kept intentionally small and colour-blind safe-ish; these are pastel
# fills that still read on a white background in both SVG and PNG.
_MODULE_FILLS = (
    "#dbe9f6",
    "#e6f2dc",
    "#fdece2",
    "#efe4f3",
    "#fdf6d8",
    "#e0f1f0",
    "#f7e0e6",
    "#e9e9e9",
)


class ScopeTooLarge(Exception):
    """Raised when a requested scope exceeds the configured node budget."""

    def __init__(self, actual, limit, hint=None):
        self.actual = actual
        self.limit = limit
        message = "scope contains {} gates, which exceeds the limit of {}".format(
            actual, limit
        )
        if hint:
            message += " ({})".format(hint)
        Exception.__init__(self, message)


def _name_of(obj, default="<unnamed>"):
    if obj is None:
        return default
    getter = getattr(obj, "get_name", None)
    if getter is None:
        return str(obj)
    try:
        return getter() or default
    except Exception:  # pragma: no cover - defensive against binding quirks
        return default


def _gate_type_name(gate):
    try:
        return _name_of(gate.get_type(), "<untyped>")
    except Exception:  # pragma: no cover
        return "<untyped>"


def _module_of(gate):
    try:
        return gate.get_module()
    except Exception:  # pragma: no cover
        return None


def _bool_call(obj, method):
    fn = getattr(obj, method, None)
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:  # pragma: no cover
        return False


def gate_node_id(gate):
    return "g{}".format(gate.get_id())


def module_node_id(module):
    return "m{}".format(module.get_id())


def net_node_id(net):
    return "net{}".format(net.get_id())


# ---------------------------------------------------------------------------
# scope selection
# ---------------------------------------------------------------------------


def collect_module_gates(module, recursive=False):
    """Return the gates of ``module``, optionally descending into submodules.

    Recursion is implemented here rather than via ``Module.get_gates(filter,
    recursive)`` so that behaviour does not depend on pybind11 overload
    resolution for a ``None`` filter.
    """
    gates = []
    seen = set()
    pending = [module]
    while pending:
        current = pending.pop()
        for gate in current.get_gates():
            key = gate.get_id()
            if key not in seen:
                seen.add(key)
                gates.append(gate)
        if recursive:
            pending.extend(current.get_submodules())
    return gates


def collect_neighborhood(seed_gates, depth, direction="both", max_gates=None):
    """Breadth-first expansion around ``seed_gates`` up to ``depth`` hops.

    ``direction`` is one of ``"both"``, ``"successors"`` or ``"predecessors"``.
    Raises :class:`ScopeTooLarge` as soon as ``max_gates`` would be exceeded.
    """
    if direction not in ("both", "successors", "predecessors"):
        raise ValueError("unknown direction: {!r}".format(direction))

    result = []
    seen = set()
    frontier = []
    for gate in seed_gates:
        key = gate.get_id()
        if key not in seen:
            seen.add(key)
            result.append(gate)
            frontier.append(gate)

    if max_gates is not None and len(result) > max_gates:
        raise ScopeTooLarge(len(result), max_gates, "reduce the number of seed gates")

    for _hop in range(max(0, int(depth))):
        if not frontier:
            break
        next_frontier = []
        for gate in frontier:
            neighbors = []
            if direction in ("both", "successors"):
                neighbors.extend(gate.get_unique_successors())
            if direction in ("both", "predecessors"):
                neighbors.extend(gate.get_unique_predecessors())
            for neighbor in neighbors:
                key = neighbor.get_id()
                if key in seen:
                    continue
                seen.add(key)
                result.append(neighbor)
                next_frontier.append(neighbor)
                if max_gates is not None and len(result) > max_gates:
                    raise ScopeTooLarge(
                        len(result), max_gates, "lower --depth or raise --max-gates"
                    )
        frontier = next_frontier

    return result


# ---------------------------------------------------------------------------
# gate-level graph
# ---------------------------------------------------------------------------


def build_netlist_graph(
    gates,
    title="netlist",
    rankdir="LR",
    net_labels=True,
    pin_labels=False,
    cluster_modules=False,
    show_boundary=False,
    label_limit=48,
    comment=None,
):
    """Build a gate-level DOT graph.

    Gates become nodes, nets become edges between a net's source gate and each
    of its destination gates.  Only gates in ``gates`` are drawn; edges leaving
    the scope are either dropped or, with ``show_boundary``, terminated in a
    small point node so a truncated view is visibly truncated.
    """
    gates = list(gates)
    in_scope = {gate.get_id(): gate for gate in gates}

    graph = DotGraph(title, directed=True, comment=comment)
    graph.graph_attrs.update(
        {
            "rankdir": rankdir,
            "label": title,
            "labelloc": "t",
            "fontname": "Helvetica",
            "fontsize": "16",
            "splines": "spline",
            "overlap": "false",
        }
    )
    graph.node_defaults.update(
        {
            "shape": "box",
            "style": "rounded,filled",
            "fillcolor": "#f4f4f4",
            "color": "#555555",
            "fontname": "Helvetica",
            "fontsize": "10",
        }
    )
    graph.edge_defaults.update(
        {"fontname": "Helvetica", "fontsize": "8", "color": "#777777"}
    )

    # -- nodes, optionally grouped into per-module clusters ------------------
    containers = {}
    if cluster_modules:
        buckets = {}
        for gate in gates:
            module = _module_of(gate)
            key = module.get_id() if module is not None else None
            buckets.setdefault(key, (module, []))[1].append(gate)
        for index, key in enumerate(sorted(buckets, key=lambda k: (k is None, k))):
            module, bucket = buckets[key]
            if module is None:
                container = graph
            else:
                container = graph.add_cluster(
                    "mod_{}".format(key),
                    label="{} (module {})".format(_name_of(module), key),
                    style="rounded",
                    color="#9aa0a6",
                    fontname="Helvetica",
                    fontsize="11",
                    bgcolor=_MODULE_FILLS[index % len(_MODULE_FILLS)],
                )
            for gate in bucket:
                containers[gate.get_id()] = container
    else:
        for gate in gates:
            containers[gate.get_id()] = graph

    for gate in gates:
        attrs = {}
        if _bool_call(gate, "is_gnd_gate"):
            attrs.update({"fillcolor": "#d9d9d9", "shape": "invtriangle"})
        elif _bool_call(gate, "is_vcc_gate"):
            attrs.update({"fillcolor": "#fff2cc", "shape": "triangle"})
        label = "{}\n[{}]\nid {}".format(
            truncate(_name_of(gate), label_limit),
            truncate(_gate_type_name(gate), label_limit),
            gate.get_id(),
        )
        containers[gate.get_id()].add_node(gate_node_id(gate), label=label, **attrs)

    # -- edges ---------------------------------------------------------------
    boundary_index = [0]

    def _add_boundary(node_id_prefix):
        boundary_index[0] += 1
        node_id = "{}_{}".format(node_id_prefix, boundary_index[0])
        graph.add_node(
            node_id,
            label="",
            shape="point",
            width="0.08",
            color="#bbbbbb",
            fillcolor="#bbbbbb",
            style="filled",
        )
        return node_id

    seen_edges = set()
    for gate in gates:
        for source_ep in gate.get_fan_out_endpoints():
            net = source_ep.get_net()
            if net is None:
                continue
            source_pin = _name_of(source_ep.get_pin(), "")
            destinations = list(net.get_destinations())
            drawn_any = False
            for dest_ep in destinations:
                dest_gate = dest_ep.get_gate()
                if dest_gate is None or dest_gate.get_id() not in in_scope:
                    continue
                drawn_any = True
                key = (gate.get_id(), dest_gate.get_id(), net.get_id())
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                attrs = {}
                if net_labels or pin_labels:
                    pieces = []
                    if net_labels:
                        pieces.append(truncate(_name_of(net), label_limit))
                    if pin_labels:
                        pieces.append(
                            "{} -> {}".format(
                                source_pin or "?", _name_of(dest_ep.get_pin(), "?")
                            )
                        )
                    attrs["label"] = "\n".join(pieces)
                if _bool_call(net, "is_global_input_net") or _bool_call(
                    net, "is_global_output_net"
                ):
                    attrs["color"] = "#1a73e8"
                graph.add_edge(
                    gate_node_id(gate), gate_node_id(dest_gate), **attrs
                )
            if show_boundary and (destinations and not drawn_any):
                sink = _add_boundary("out")
                graph.add_edge(
                    gate_node_id(gate),
                    sink,
                    label=truncate(_name_of(net), label_limit) if net_labels else None,
                    style="dashed",
                    color="#bbbbbb",
                )

    if show_boundary:
        for gate in gates:
            fan_in = getattr(gate, "get_fan_in_endpoints", None)
            if fan_in is None:
                continue
            for dest_ep in fan_in():
                net = dest_ep.get_net()
                if net is None:
                    continue
                sources = list(net.get_sources())
                if not sources:
                    continue
                if any(
                    ep.get_gate() is not None and ep.get_gate().get_id() in in_scope
                    for ep in sources
                ):
                    continue
                source = _add_boundary("in")
                graph.add_edge(
                    source,
                    gate_node_id(gate),
                    label=truncate(_name_of(net), label_limit) if net_labels else None,
                    style="dashed",
                    color="#bbbbbb",
                )

    return graph


# ---------------------------------------------------------------------------
# module hierarchy
# ---------------------------------------------------------------------------


def build_module_tree_graph(
    root_module,
    max_depth=None,
    rankdir="TB",
    label_limit=48,
    show_gate_counts=True,
    comment=None,
):
    """Build a DOT tree of ``root_module`` and its submodules.

    ``max_depth`` limits how many levels below the root are drawn; modules that
    are cut off are marked with an ellipsis node so the truncation is visible.
    """
    title = "module hierarchy of {}".format(_name_of(root_module))
    graph = DotGraph(title, directed=True, comment=comment)
    graph.graph_attrs.update(
        {
            "rankdir": rankdir,
            "label": title,
            "labelloc": "t",
            "fontname": "Helvetica",
            "fontsize": "16",
        }
    )
    graph.node_defaults.update(
        {
            "shape": "box",
            "style": "rounded,filled",
            "fillcolor": "#dbe9f6",
            "color": "#4a6d8c",
            "fontname": "Helvetica",
            "fontsize": "10",
        }
    )
    graph.edge_defaults.update({"color": "#4a6d8c", "arrowsize": "0.7"})

    stack = [(root_module, 0)]
    while stack:
        module, depth = stack.pop()
        pieces = [truncate(_name_of(module), label_limit)]
        module_type = None
        try:
            module_type = module.get_type()
        except Exception:  # pragma: no cover
            module_type = None
        if module_type:
            pieces.append("<{}>".format(truncate(str(module_type), label_limit)))
        pieces.append("id {}".format(module.get_id()))
        if show_gate_counts:
            direct = len(module.get_gates())
            total = len(collect_module_gates(module, recursive=True))
            noun = "gate" if direct == 1 else "gates"
            if total == direct:
                pieces.append("{} {}".format(direct, noun))
            else:
                pieces.append("{} {} ({} total)".format(direct, noun, total))
        attrs = {}
        if depth == 0:
            attrs.update({"fillcolor": "#c5dcef", "penwidth": "2"})
        graph.add_node(module_node_id(module), label="\n".join(pieces), **attrs)

        submodules = list(module.get_submodules())
        if not submodules:
            continue
        if max_depth is not None and depth >= max_depth:
            cutoff_id = "{}_more".format(module_node_id(module))
            graph.add_node(
                cutoff_id,
                label="... {} submodule(s) hidden\n(raise --depth)".format(
                    len(submodules)
                ),
                shape="note",
                fillcolor="#f4f4f4",
                color="#999999",
                fontsize="9",
            )
            graph.add_edge(module_node_id(module), cutoff_id, style="dashed")
            continue
        for submodule in submodules:
            graph.add_edge(module_node_id(module), module_node_id(submodule))
            stack.append((submodule, depth + 1))

    return graph
