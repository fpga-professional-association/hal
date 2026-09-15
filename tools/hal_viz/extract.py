"""Turn HAL netlist objects into :class:`~hal_viz.dot.DotGraph` instances.

Nothing in this module imports ``hal_py``.  Every netlist object is used
purely through duck-typed accessors that match the ``hal_py`` bindings:

* ``Gate``   -- ``get_id``, ``get_name``, ``get_type``, ``get_module``,
  ``get_fan_in_endpoints``, ``get_fan_out_endpoints``,
  ``get_unique_successors``, ``get_unique_predecessors``, ``is_gnd_gate``,
  ``is_vcc_gate``
* ``GateType``  -- ``get_name``, ``get_properties`` (optional; the ``dag`` view
  falls back to the type name when the bindings do not offer it)
* ``Endpoint``  -- ``get_gate``, ``get_net``, ``get_pin``
* ``Net``    -- ``get_id``, ``get_name``, ``get_destinations``,
  ``is_global_input_net``, ``is_global_output_net``
* ``Module`` -- ``get_id``, ``get_name``, ``get_type``, ``get_gates``,
  ``get_submodules``, ``get_parent_module``

That keeps the whole graph-building layer testable with plain stub objects on
a machine where HAL cannot be built.
"""

import re

from .dot import DotGraph, truncate
from .levels import compute_levels

__all__ = [
    "ScopeTooLarge",
    "DagView",
    "LEGEND_ROWS",
    "collect_module_gates",
    "collect_neighborhood",
    "constant_value",
    "is_sequential_gate",
    "is_io_gate",
    "add_legend",
    "build_netlist_graph",
    "build_dag_graph",
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


#: Gate-type properties (``str(GateTypeProperty.ff)`` is ``"GateTypeProperty.ff"``,
#: so only the part behind the last dot is compared) that make a gate a register.
_SEQUENTIAL_PROPERTIES = frozenset(("ff", "latch", "sequential", "ram"))

#: Fallback for bindings or stubs that expose no gate-type properties at all.
#: Anchored so that ``BUF``/``BUFF`` and ``MUX`` are not mistaken for registers.
_SEQUENTIAL_NAME_RE = re.compile(r"(^|[^A-Z])(S?D?FF|LATCH|REG)", re.IGNORECASE)


def _type_properties(gate):
    """The gate type's properties as bare lower-case strings, or ``None``.

    ``None`` means "this object does not report properties", which is the
    signal to fall back to the type name; an empty set means "reports none".
    """
    try:
        gate_type = gate.get_type()
    except Exception:  # pragma: no cover - defensive against binding quirks
        return None
    if gate_type is None:
        return None
    getter = getattr(gate_type, "get_properties", None) or getattr(
        gate_type, "get_property_list", None
    )
    if getter is None:
        return None
    try:
        raw = getter()
    except Exception:  # pragma: no cover
        return None
    if raw is None:
        return None
    names = set()
    for entry in raw:
        text = getattr(entry, "name", None) or str(entry)
        names.add(text.rsplit(".", 1)[-1].lower())
    return names


def is_sequential_gate(gate):
    """True for a flip-flop, latch or other state-holding gate.

    Real ``hal_py`` gate types carry the ``ff``/``latch``/``sequential``
    properties, which is what is used when they are available.  Bindings (or
    test stubs) that do not report properties fall back to the type name.
    """
    properties = _type_properties(gate)
    if properties:
        return bool(properties & _SEQUENTIAL_PROPERTIES)
    return bool(_SEQUENTIAL_NAME_RE.search(_gate_type_name(gate)))


def constant_value(gate):
    """``"0"`` for a GND gate, ``"1"`` for a VCC gate, ``None`` otherwise."""
    if _bool_call(gate, "is_gnd_gate"):
        return "0"
    if _bool_call(gate, "is_vcc_gate"):
        return "1"
    properties = _type_properties(gate)
    if properties:
        if "ground" in properties:
            return "0"
        if "power" in properties:
            return "1"
    return None


def _endpoint_nets(gate, method):
    getter = getattr(gate, method, None)
    if getter is None:
        return []
    try:
        endpoints = list(getter())
    except Exception:  # pragma: no cover
        return []
    return [endpoint.get_net() for endpoint in endpoints if endpoint.get_net() is not None]


def is_io_gate(gate):
    """True when the gate touches a global input or output net of the netlist."""
    for net in _endpoint_nets(gate, "get_fan_in_endpoints"):
        if _bool_call(net, "is_global_input_net"):
            return True
    for net in _endpoint_nets(gate, "get_fan_out_endpoints"):
        if _bool_call(net, "is_global_output_net"):
            return True
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
# shared visual vocabulary
# ---------------------------------------------------------------------------

#: Every drawing uses these four node styles and two edge styles, and every
#: drawing carries the legend below that decodes them.  Keeping the styles in
#: one place is what keeps the legend honest.
_COMB_STYLE = {"shape": "box", "style": "rounded,filled", "fillcolor": "#f4f4f4",
               "color": "#555555"}
_SEQ_STYLE = {"shape": "box", "style": "filled", "fillcolor": "#dbe9f6",
              "color": "#2a5d8f", "penwidth": "2"}
_IO_STYLE = {"shape": "octagon", "style": "filled", "fillcolor": "#d6ecff",
             "color": "#2a5d8f"}
_TIE_STYLE = {
    "0": {"shape": "circle", "style": "filled", "fillcolor": "#e8e8e8",
          "color": "#8a8a8a", "width": "0.28", "height": "0.28",
          "fixedsize": "true", "fontsize": "10", "margin": "0"},
    "1": {"shape": "circle", "style": "filled", "fillcolor": "#fff2cc",
          "color": "#bf9000", "width": "0.28", "height": "0.28",
          "fixedsize": "true", "fontsize": "10", "margin": "0"},
}
_CUT_EDGE_STYLE = {"style": "dashed", "color": "#c0392b", "constraint": "false"}
_CYCLE_STYLE = {"color": "#c0392b", "penwidth": "3", "fillcolor": "#fdecea"}


#: The same vocabulary as :func:`add_legend`, spelled out for an HTML page.
#: One source for both keeps the drawing and the page from drifting apart.
LEGEND_ROWS = (
    ("rounded grey box", "combinational gate"),
    ("blue square box", "flip-flop or latch"),
    ("blue octagon", "gate on a global (primary) input or output net"),
    (
        "small 0 / 1 circle",
        "constant tie-off: one stub per consuming edge, so the GND/VCC gate "
        "itself is never drawn",
    ),
    ("solid grey arrow", "net, from its driver to one sink"),
    (
        "dashed red arrow",
        "edge cut at a register: it ends at a flip-flop or latch and is "
        "ignored when levelling, which is what makes the rest a DAG",
    ),
    ("red outlined gate", "gate on a real combinational loop"),
    (
        "level N column",
        "topological level: level 0 (primary inputs, tie-offs, register "
        "outputs) on the left, depth increasing to the right",
    ),
)


class _TieOffs(object):
    """Hands out one ``0``/``1`` source stub per constant-driven edge.

    The stubs replace the GND/VCC gate itself: the gate is never drawn, so no
    single node collects the fan-out of every constant in the design.  Which
    gate a stub stands for is kept in its tooltip, which SVG shows on hover and
    which costs nothing in the other formats.
    """

    def __init__(self, label_limit=48):
        self.count = 0
        self.ids = []
        self._label_limit = label_limit

    def reserve(self, value):
        """Claim the id of the next stub without declaring the node yet."""
        self.count += 1
        node_id = "tie{}_{}".format(value, self.count)
        self.ids.append(node_id)
        return node_id

    def declare(self, container, node_id, value, driver=None, net=None):
        where = []
        if driver is not None:
            where.append(
                "{} [{}]".format(
                    truncate(_name_of(driver), self._label_limit), _gate_type_name(driver)
                )
            )
        if net is not None:
            where.append("net {}".format(truncate(_name_of(net), self._label_limit)))
        container.add_node(
            node_id,
            label=value,
            tooltip="constant {}{}".format(
                value, " from " + ", ".join(where) if where else ""
            ),
            **_TIE_STYLE[value]
        )
        return node_id

    def stub(self, container, value, driver=None, net=None):
        """Reserve and declare a stub in one go (the ``netlist_graph`` case)."""
        return self.declare(
            container, self.reserve(value), value, driver=driver, net=net
        )


def add_legend(graph, cut_edges=False, levels=False, name="legend"):
    """Add the cluster that decodes the drawing's visual vocabulary.

    Every node id inside starts with ``legend`` so that a consumer parsing the
    emitted ``.dot`` can tell the key apart from the circuit (see
    ``tests/headless_smoke/real_netlist_smoke.py``).  ``cut_edges`` and
    ``levels`` add the two entries that only the ``dag`` view needs.
    """
    box = graph.add_cluster(
        name,
        label="legend",
        counted=False,  # a key is not part of the circuit being counted
        style="rounded",
        color="#bbbbbb",
        bgcolor="#fcfcfc",
        fontname="Helvetica",
        fontsize="11",
        labelloc="t",
    )
    box.add_node("legend_comb", label="combinational\ngate", fontsize="9", **_COMB_STYLE)
    box.add_node("legend_seq", label="flip-flop /\nlatch", fontsize="9", **_SEQ_STYLE)
    box.add_node("legend_io", label="primary I/O", fontsize="9", **_IO_STYLE)
    box.add_node("legend_tie0", label="0", **_TIE_STYLE["0"])
    box.add_node("legend_tie1", label="1", **_TIE_STYLE["1"])
    box.add_node(
        "legend_tie_note",
        label="constant tie-off,\none stub per sink",
        shape="plaintext",
        style="",
        fillcolor=None,
        fontsize="9",
        fontcolor="#555555",
    )
    for point in ("legend_net_a", "legend_net_b"):
        box.add_node(point, label="", shape="point", width="0.06",
                     color="#777777", fillcolor="#777777", style="filled")
    box.add_edge("legend_net_a", "legend_net_b", label="net", fontsize="9",
                 color="#777777")
    if cut_edges:
        for point in ("legend_cut_a", "legend_cut_b"):
            box.add_node(point, label="", shape="point", width="0.06",
                         color="#c0392b", fillcolor="#c0392b", style="filled")
        box.add_edge(
            "legend_cut_a",
            "legend_cut_b",
            label="cut at register",
            fontsize="9",
            style="dashed",
            color="#c0392b",
        )
        box.add_node(
            "legend_cycle",
            label="combinational\nloop",
            fontsize="9",
            shape="box",
            style="rounded,filled",
            **_CYCLE_STYLE
        )
    if levels:
        box.add_node(
            "legend_levels",
            label="level 0 (inputs, tie-offs, register outputs)\n"
            "on the left; depth increases to the right",
            shape="plaintext",
            style="",
            fillcolor=None,
            fontsize="9",
            fontcolor="#555555",
        )
    return box


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
    legend=True,
    const_hub=False,
    comment=None,
):
    """Build a gate-level DOT graph.

    Gates become nodes, nets become edges between a net's source gate and each
    of its destination gates.  Only gates in ``gates`` are drawn; edges leaving
    the scope are either dropped or, with ``show_boundary``, terminated in a
    small point node so a truncated view is visibly truncated.

    GND and VCC gates are *not* drawn as gates.  A single constant gate drives
    hundreds of pins on a real netlist, and one node with that fan-out is a
    spider that hides the circuit, so every constant-driven edge gets its own
    little source stub labelled ``0`` or ``1`` instead.  ``const_hub=True``
    restores the old shared-node drawing.
    """
    gates = list(gates)
    in_scope = {gate.get_id(): gate for gate in gates}
    constants = {}
    if not const_hub:
        for gate in gates:
            value = constant_value(gate)
            if value is not None:
                constants[gate.get_id()] = value
    tie_offs = _TieOffs(label_limit)

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
        if gate.get_id() in constants:
            continue  # drawn as one 0/1 stub per consuming edge, see below
        attrs = {}
        if const_hub and _bool_call(gate, "is_gnd_gate"):
            attrs.update({"fillcolor": "#d9d9d9", "shape": "invtriangle"})
        elif const_hub and _bool_call(gate, "is_vcc_gate"):
            attrs.update({"fillcolor": "#fff2cc", "shape": "triangle"})
        elif is_sequential_gate(gate):
            attrs.update(_SEQ_STYLE)
        elif is_io_gate(gate):
            attrs.update(_IO_STYLE)
        label = "{}\n[{}]\nid {}".format(
            truncate(_name_of(gate), label_limit),
            truncate(_gate_type_name(gate), label_limit),
            gate.get_id(),
        )
        containers[gate.get_id()].add_node(gate_node_id(gate), label=label, **attrs)

    # -- edges ---------------------------------------------------------------
    boundary_index = [0]
    tie_rank = []

    def _tie_stub(value, driver, net):
        """A 0/1 stub, pinned to the source rank so tie-offs stay on the left."""
        if not tie_rank:
            tie_rank.append(graph.add_subgraph("tieoffs", rank="source"))
        return tie_offs.stub(tie_rank[0], value, driver=driver, net=net)

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
        constant = constants.get(gate.get_id())
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
                if dest_gate.get_id() in constants:
                    continue  # a constant gate has no drawn node to point at
                drawn_any = True
                if constant is not None:
                    # one stub per consuming edge, never a shared hub
                    graph.add_edge(
                        _tie_stub(constant, gate, net),
                        gate_node_id(dest_gate),
                        color="#8a8a8a",
                    )
                    continue
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
            if show_boundary and constant is None and (destinations and not drawn_any):
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
            if gate.get_id() in constants:
                continue
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
                # A constant driver just outside the scope is still a tie-off,
                # not an anonymous boundary stub.
                driver = sources[0].get_gate()
                value = None if const_hub or driver is None else constant_value(driver)
                if value is not None:
                    graph.add_edge(
                        _tie_stub(value, driver, net),
                        gate_node_id(gate),
                        color="#8a8a8a",
                    )
                    continue
                source = _add_boundary("in")
                graph.add_edge(
                    source,
                    gate_node_id(gate),
                    label=truncate(_name_of(net), label_limit) if net_labels else None,
                    style="dashed",
                    color="#bbbbbb",
                )

    if legend:
        add_legend(graph)
    graph.comment = _with_tie_off_note(comment, tie_offs.count)
    return graph


def _with_tie_off_note(comment, count):
    """Record the number of emitted tie-off stubs in the .dot comment header."""
    if not count:
        return comment
    note = "{} constant tie-off stub(s) emitted (one per consuming edge; the " \
           "GND/VCC gate itself is not drawn)".format(count)
    return note if not comment else "{}\n{}".format(comment, note)


# ---------------------------------------------------------------------------
# levelled directed graph (the "dag" view)
# ---------------------------------------------------------------------------


class DagView(object):
    """A levelled gate graph plus the numbers a caption or a warning needs.

    ``graph`` is the :class:`~hal_viz.dot.DotGraph`; the rest describes what was
    drawn: ``levels`` (the :class:`~hal_viz.levels.Levels` result),
    ``gate_levels`` (gate id -> level), ``cycle_gates`` (``(id, name)`` of every
    gate on a real combinational loop), and the counts.
    """

    def __init__(self, graph, levels, gate_levels, cycle_gates, gate_count,
                 edge_count, cut_count, tie_off_count):
        self.graph = graph
        self.levels = levels
        self.gate_levels = gate_levels
        self.cycle_gates = cycle_gates
        self.gate_count = gate_count
        self.edge_count = edge_count
        self.cut_count = cut_count
        self.tie_off_count = tie_off_count

    @property
    def level_count(self):
        return self.levels.level_count

    @property
    def has_cycles(self):
        return bool(self.cycle_gates)

    def cycle_summary(self, limit=8):
        """A one-line, human-readable list of the looping gates."""
        names = ["{} (id {})".format(name, gate_id) for gate_id, name in self.cycle_gates]
        if len(names) > limit:
            names = names[:limit] + ["... and {} more".format(len(self.cycle_gates) - limit)]
        return ", ".join(names)


def build_dag_graph(
    gates,
    title="dag",
    rankdir="LR",
    net_labels=False,
    pin_labels=False,
    label_limit=48,
    legend=True,
    level_labels=True,
    const_hub=False,
    comment=None,
):
    """Build the topologically levelled view of ``gates``.

    The combinational core of a netlist is a DAG once the feedback is cut at
    the registers, and this draws exactly that: every edge that *ends* at a
    flip-flop or latch is a cut edge, so a register output is a source of the
    remaining graph and a register input is one of its sinks.  Levels come from
    Kahn's algorithm on the cut graph -- level 0 holds the primary inputs, the
    constant tie-offs and the register outputs, and every other gate sits one
    level behind its deepest driver -- and each level becomes a ``rank=same``
    group, so with the default ``rankdir=LR`` depth grows to the right.

    A real combinational loop survives the cut.  It is neither an error nor a
    reason to drop gates: the loop members are levelled anyway (see
    :func:`hal_viz.levels.compute_levels`), drawn highlighted, and reported
    through :attr:`DagView.cycle_gates` so the caller can warn about them.
    """
    gates = list(gates)
    in_scope = {gate.get_id(): gate for gate in gates}
    constants = {}
    if not const_hub:
        for gate in gates:
            value = constant_value(gate)
            if value is not None:
                constants[gate.get_id()] = value
    drawn = [gate for gate in gates if gate.get_id() not in constants]
    sequential = set(
        gate.get_id() for gate in drawn if is_sequential_gate(gate)
    )
    tie_offs = _TieOffs(label_limit)

    graph = DotGraph(title, directed=True, comment=comment)
    graph.graph_attrs.update(
        {
            "rankdir": rankdir,
            "label": title,
            "labelloc": "t",
            "fontname": "Helvetica",
            "fontsize": "16",
            "splines": "spline",
            "newrank": "true",
            "nodesep": "0.25",
            "ranksep": "0.9",
        }
    )
    # the default node is a combinational gate; registers, I/O, tie-offs and
    # loop members override it below
    graph.node_defaults.update({"fontname": "Helvetica", "fontsize": "10"})
    graph.node_defaults.update(_COMB_STYLE)
    graph.edge_defaults.update(
        {"fontname": "Helvetica", "fontsize": "8", "color": "#777777"}
    )

    # -- the cut graph -------------------------------------------------------
    pending = []  # (source_key, target_key, attrs, is_cut)
    seen_edges = set()
    tie_specs = []  # (node_id, value, driver, net) -- declared with their level
    for gate in gates:
        constant = constants.get(gate.get_id())
        for source_ep in gate.get_fan_out_endpoints():
            net = source_ep.get_net()
            if net is None:
                continue
            source_pin = _name_of(source_ep.get_pin(), "")
            for dest_ep in net.get_destinations():
                dest_gate = dest_ep.get_gate()
                if dest_gate is None or dest_gate.get_id() not in in_scope:
                    continue
                if dest_gate.get_id() in constants:
                    continue
                target = gate_node_id(dest_gate)
                cut = dest_gate.get_id() in sequential
                if constant is not None:
                    source = tie_offs.reserve(constant)
                    tie_specs.append((source, constant, gate, net))
                    pending.append((source, target, {"color": "#8a8a8a"}, cut))
                    continue
                key = (gate.get_id(), dest_gate.get_id(), net.get_id())
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                attrs = {}
                pieces = []
                if net_labels:
                    pieces.append(truncate(_name_of(net), label_limit))
                if pin_labels:
                    pieces.append(
                        "{} -> {}".format(
                            source_pin or "?", _name_of(dest_ep.get_pin(), "?")
                        )
                    )
                if pieces:
                    attrs["label"] = "\n".join(pieces)
                pending.append((gate_node_id(gate), target, attrs, cut))

    node_keys = [spec[0] for spec in tie_specs] + [gate_node_id(gate) for gate in drawn]
    levelling = compute_levels(
        node_keys, [(src, dst) for src, dst, _attrs, cut in pending if not cut]
    )

    cycle_gates = []
    for gate in drawn:
        if gate_node_id(gate) in levelling.cycle_nodes:
            cycle_gates.append((gate.get_id(), _name_of(gate)))

    # -- nodes, one rank=same group per level --------------------------------
    tie_by_id = dict((spec[0], spec) for spec in tie_specs)
    gate_by_node = dict((gate_node_id(gate), gate) for gate in drawn)
    ranks = {}
    for level in sorted(levelling.by_level):
        rank = graph.add_subgraph("level_{}".format(level), rank="same")
        ranks[level] = rank
        if level_labels:
            rank.add_node(
                "lvl_{}".format(level),
                label="level {}".format(level),
                shape="plaintext",
                style="",
                fontsize="11",
                fontcolor="#777777",
            )
        for key in levelling.by_level[level]:
            spec = tie_by_id.get(key)
            if spec is not None:
                node_id, value, driver, net = spec
                tie_offs.declare(rank, node_id, value, driver=driver, net=net)
                continue
            gate = gate_by_node[key]
            attrs = {}
            if gate.get_id() in sequential:
                attrs.update(_SEQ_STYLE)
            elif is_io_gate(gate):
                attrs.update(_IO_STYLE)
            if key in levelling.cycle_nodes:
                attrs.update(_CYCLE_STYLE)
            label = "{}\n[{}]\nid {}".format(
                truncate(_name_of(gate), label_limit),
                truncate(_gate_type_name(gate), label_limit),
                gate.get_id(),
            )
            rank.add_node(key, label=label, **attrs)

    if level_labels and len(ranks) > 1:
        ordered = sorted(ranks)
        for left, right in zip(ordered, ordered[1:]):
            graph.add_edge(
                "lvl_{}".format(left), "lvl_{}".format(right), style="invis"
            )

    # -- edges ---------------------------------------------------------------
    cut_count = 0
    for source, target, attrs, cut in pending:
        attrs = dict(attrs)
        if cut:
            cut_count += 1
            attrs.update(_CUT_EDGE_STYLE)
        elif source in levelling.cycle_nodes and target in levelling.cycle_nodes:
            attrs.update({"color": "#c0392b", "penwidth": "1.8"})
        graph.add_edge(source, target, **attrs)

    if legend:
        add_legend(graph, cut_edges=True, levels=True)

    notes = []
    if tie_offs.count:
        notes.append(
            "{} constant tie-off stub(s) emitted (one per consuming edge; the "
            "GND/VCC gate itself is not drawn)".format(tie_offs.count)
        )
    notes.append(
        "{} level(s), {} edge(s) cut at a register".format(
            levelling.level_count, cut_count
        )
    )
    if cycle_gates:
        notes.append(
            "combinational loop(s) involving {} gate(s): {}".format(
                len(cycle_gates),
                ", ".join("{} (id {})".format(name, gid) for gid, name in cycle_gates),
            )
        )
    graph.comment = "\n".join(([comment] if comment else []) + notes)

    return DagView(
        graph,
        levelling,
        dict(
            (gate.get_id(), levelling.levels[gate_node_id(gate)]) for gate in drawn
        ),
        cycle_gates,
        len(drawn),
        len(pending),
        cut_count,
        tie_offs.count,
    )


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
