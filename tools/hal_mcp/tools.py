"""The tool surface: one JSON Schema and one handler per MCP tool.

Three rules hold for every tool in this file.

**Read only.**  Nothing here modifies a netlist, a module, a net or a project,
and nothing writes a file except ``hal_render_graph``, which writes only to the
output path its caller names (plus the sibling ``.dot`` it renders from).

**Bounded.**  Any tool that can return many items takes ``limit`` (default 100,
hard maximum 1000) and ``offset``, and reports the true ``total`` next to the
window it returned.  A 100k-gate netlist must never land in one response.

**Failure is data.**  A handler signals a problem by raising
:class:`~hal_mcp.session.ToolError` with a message that says what to do
instead; the server turns that into an ``isError`` result, not a JSON-RPC
error.
"""

import os
import shutil
import tempfile

from .session import ToolError, halenv

__all__ = ["TOOLS", "TOOLS_BY_NAME", "list_tools", "call_tool"]

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

#: Depth bounds. Deep enough for any real traversal, shallow enough that a
#: mistyped argument cannot walk a whole chip.
MAX_DEPTH = 64


# ---------------------------------------------------------------------------
# argument plumbing
# ---------------------------------------------------------------------------


def _string(arguments, key, required=True, default=None):
    value = arguments.get(key, default)
    if value is None or value == "":
        if required:
            raise ToolError("missing required argument {!r}.".format(key))
        return default
    if not isinstance(value, str):
        raise ToolError("argument {!r} must be a string, got {}.".format(key, type(value).__name__))
    return value


def _integer(arguments, key, default, minimum=None, maximum=None):
    value = arguments.get(key, default)
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ToolError("argument {!r} must be an integer, got {!r}.".format(key, value))
    if minimum is not None and value < minimum:
        raise ToolError("argument {!r} must be >= {}, got {}.".format(key, minimum, value))
    if maximum is not None and value > maximum:
        raise ToolError(
            "argument {!r} must be <= {}, got {}. Page through the result with "
            "offset instead of raising it.".format(key, maximum, value)
        )
    return value


def _boolean(arguments, key, default=False):
    value = arguments.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ToolError("argument {!r} must be a boolean, got {!r}.".format(key, value))


def _page(arguments):
    limit = _integer(arguments, "limit", DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)
    offset = _integer(arguments, "offset", 0, minimum=0)
    return limit, offset


def _window(items, limit, offset):
    """Return ``(window, page_metadata)`` -- the total is always the real one."""
    total = len(items)
    window = list(items[offset : offset + limit])
    return window, {
        "total": total,
        "offset": offset,
        "limit": limit,
        "returned": len(window),
        "truncated": offset + len(window) < total,
    }


def _cap(names, limit):
    """Cap a nested name list, reporting how many there really were."""
    names = list(names)
    return {"total": len(names), "names": names[:limit], "truncated": len(names) > limit}


def _enum_name(value):
    """``PinDirection.input`` -> ``"input"``, for any pybind11 enum."""
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value).rsplit(".", 1)[-1]


# ---------------------------------------------------------------------------
# netlist lookups
# ---------------------------------------------------------------------------


def _find_gate(session, spec):
    env = halenv()
    try:
        return env.find_gate(session.netlist, spec)
    except env.NetlistLoadError as exc:
        raise ToolError(
            "{} Use hal_list_gates to find the exact name, or pass the numeric "
            "gate id.".format(exc)
        )


def _find_module(session, spec):
    env = halenv()
    try:
        return env.find_module(session.netlist, spec)
    except env.NetlistLoadError as exc:
        raise ToolError("{} Use hal_module_tree to list the modules.".format(exc))


def _find_net(session, spec):
    """Resolve a net by numeric id, exact name, or unique substring.

    The same resolution rules as ``halenv.find_gate`` / ``find_module``; HAL has
    no ``get_net_by_name`` either, so the scan is unavoidable.
    """
    netlist = session.netlist
    spec = str(spec)
    if spec.isdigit():
        net = netlist.get_net_by_id(int(spec))
        if net is None:
            raise ToolError("no net with id {} in this netlist.".format(spec))
        return net

    nets = list(netlist.get_nets())
    exact = [net for net in nets if net.get_name() == spec]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ToolError(
            "net name {!r} is ambiguous ({} matches: ids {}). Pass the numeric "
            "net id instead.".format(
                spec, len(exact), ", ".join(str(net.get_id()) for net in exact[:10])
            )
        )
    partial = [net for net in nets if spec in net.get_name()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise ToolError(
            "no net named exactly {!r}; {} names contain it (e.g. {}). Use an "
            "exact name or a numeric net id.".format(
                spec, len(partial), ", ".join(repr(net.get_name()) for net in partial[:5])
            )
        )
    raise ToolError(
        "no net named {!r} in this netlist. hal_gate_info lists the nets "
        "connected to a gate; hal_netlist_stats lists the global I/O nets.".format(spec)
    )


def _is_sequential(session, gate):
    hal_py = session.hal_py
    try:
        return bool(gate.get_type().has_property(hal_py.GateTypeProperty.sequential))
    except Exception:  # pragma: no cover - depends on the gate library
        return False


def _gate_row(session, gate):
    module = gate.get_module()
    return {
        "id": gate.get_id(),
        "name": gate.get_name(),
        "type": gate.get_type().get_name(),
        "module": module.get_name() if module is not None else None,
        "is_sequential": _is_sequential(session, gate),
    }


def _endpoint(endpoint):
    gate = endpoint.get_gate()
    pin = endpoint.get_pin()
    return {
        "gate": gate.get_name() if gate is not None else None,
        "gate_id": gate.get_id() if gate is not None else None,
        "pin": pin.get_name() if pin is not None else None,
    }


def _import_plugin(name, why):
    env = halenv()
    try:
        return env.import_plugin(name)
    except env.HalUnavailable as exc:
        raise ToolError(
            "{} {} Build HAL with -DBUILD_ALL_PLUGINS=ON (or enable just this "
            "plugin) and restart the server.".format(exc, why)
        )


# ---------------------------------------------------------------------------
# session tools
# ---------------------------------------------------------------------------


def _tool_load_netlist(store, arguments):
    path = _string(arguments, "netlist_path")
    gate_library = _string(arguments, "gate_library", required=False)
    session = store.open_netlist(path, gate_library)
    summary = session.describe()
    summary["next"] = (
        "Reuse this session_id for every other hal_* tool; the netlist stays "
        "loaded until hal_close_session. hal_netlist_stats is the usual first call."
    )
    return summary


def _tool_load_project(store, arguments):
    path = _string(arguments, "project_dir")
    session = store.open_project(path)
    summary = session.describe()
    summary["next"] = (
        "Reuse this session_id for every other hal_* tool; the project stays "
        "loaded until hal_close_session."
    )
    return summary


def _tool_list_sessions(store, arguments):
    limit, offset = _page(arguments)
    sessions = store.all()
    window, page = _window(sessions, limit, offset)
    payload = {"sessions": [session.describe() for session in window]}
    payload.update(page)
    return payload


def _tool_close_session(store, arguments):
    session_id = _string(arguments, "session_id")
    session = store.close(session_id)
    return {
        "closed": session.id,
        "source": session.source,
        "open_sessions": [other.id for other in store.all()],
    }


# ---------------------------------------------------------------------------
# inspection tools
# ---------------------------------------------------------------------------


def _tool_netlist_stats(store, arguments):
    session = store.get(_string(arguments, "session_id"))
    limit, offset = _page(arguments)
    netlist = session.netlist

    histogram = {}
    sequential = 0
    for gate in netlist.get_gates():
        name = gate.get_type().get_name()
        histogram[name] = histogram.get(name, 0) + 1
        if _is_sequential(session, gate):
            sequential += 1

    ordered = sorted(histogram.items(), key=lambda item: (-item[1], item[0]))
    window, page = _window(ordered, limit, offset)

    top = netlist.get_top_module()
    library = netlist.get_gate_library()
    return {
        "session_id": session.id,
        "design_name": netlist.get_design_name(),
        "source": session.source,
        "gate_library": library.get_name() if library is not None else None,
        "counts": {
            "gates": len(netlist.get_gates()),
            "nets": len(netlist.get_nets()),
            "modules": len(netlist.get_modules()),
            "sequential_gates": sequential,
            "distinct_gate_types": len(histogram),
        },
        "gate_types": [{"type": name, "count": count} for name, count in window],
        "gate_types_page": page,
        "top_module": None
        if top is None
        else {
            "id": top.get_id(),
            "name": top.get_name(),
            "type": top.get_type(),
            "direct_gates": len(top.get_gates()),
            "submodules": len(top.get_submodules()),
        },
        "global_inputs": _cap(
            sorted(net.get_name() for net in netlist.get_global_input_nets()), limit
        ),
        "global_outputs": _cap(
            sorted(net.get_name() for net in netlist.get_global_output_nets()), limit
        ),
        "gnd_nets": _cap(sorted(net.get_name() for net in netlist.get_gnd_nets()), limit),
        "vcc_nets": _cap(sorted(net.get_name() for net in netlist.get_vcc_nets()), limit),
    }


def _tool_list_gates(store, arguments):
    session = store.get(_string(arguments, "session_id"))
    limit, offset = _page(arguments)
    type_contains = _string(arguments, "type_contains", required=False)
    name_contains = _string(arguments, "name_contains", required=False)
    sequential_only = _boolean(arguments, "sequential_only", False)

    type_needle = type_contains.lower() if type_contains else None
    name_needle = name_contains.lower() if name_contains else None

    matched = []
    for gate in session.netlist.get_gates():
        if type_needle and type_needle not in gate.get_type().get_name().lower():
            continue
        if name_needle and name_needle not in gate.get_name().lower():
            continue
        if sequential_only and not _is_sequential(session, gate):
            continue
        matched.append(gate)
    matched.sort(key=lambda gate: (gate.get_name(), gate.get_id()))

    window, page = _window(matched, limit, offset)
    payload = {
        "session_id": session.id,
        "filters": {
            "type_contains": type_contains,
            "name_contains": name_contains,
            "sequential_only": sequential_only,
        },
        "gates": [_gate_row(session, gate) for gate in window],
    }
    payload.update(page)
    return payload


def _tool_gate_info(store, arguments):
    session = store.get(_string(arguments, "session_id"))
    gate = _find_gate(session, _string(arguments, "gate"))
    limit, _ = _page(arguments)

    gate_type = gate.get_type()
    inputs = []
    outputs = []
    for pin in gate_type.get_pins():
        direction = _enum_name(pin.get_direction())
        entry = {
            "pin": pin.get_name(),
            "direction": direction,
            "pin_type": _enum_name(pin.get_type()),
        }
        if direction in ("input", "inout"):
            net = gate.get_fan_in_net(pin)
            row = dict(entry)
            row["net"] = net.get_name() if net is not None else None
            row["net_id"] = net.get_id() if net is not None else None
            if net is not None:
                row["is_gnd"] = net.is_gnd_net()
                row["is_vcc"] = net.is_vcc_net()
            inputs.append(row)
        if direction in ("output", "inout"):
            net = gate.get_fan_out_net(pin)
            row = dict(entry)
            row["net"] = net.get_name() if net is not None else None
            row["net_id"] = net.get_id() if net is not None else None
            outputs.append(row)

    module = gate.get_module()
    payload = {
        "session_id": session.id,
        "id": gate.get_id(),
        "name": gate.get_name(),
        "type": gate_type.get_name(),
        "is_sequential": _is_sequential(session, gate),
        "is_gnd_gate": gate.is_gnd_gate(),
        "is_vcc_gate": gate.is_vcc_gate(),
        "properties": sorted(_enum_name(prop) for prop in gate_type.get_properties()),
        "module": None
        if module is None
        else {"id": module.get_id(), "name": module.get_name()},
        "location": None
        if not gate.has_location()
        else {"x": gate.get_location_x(), "y": gate.get_location_y()},
        "input_pins": inputs[:limit],
        "input_pin_count": len(inputs),
        "output_pins": outputs[:limit],
        "output_pin_count": len(outputs),
        "predecessor_gates": len(gate.get_unique_predecessors()),
        "successor_gates": len(gate.get_unique_successors()),
    }
    if len(inputs) > limit or len(outputs) > limit:
        payload["truncated"] = True
    return payload


def _tool_net_info(store, arguments):
    session = store.get(_string(arguments, "session_id"))
    net = _find_net(session, _string(arguments, "net"))
    limit, offset = _page(arguments)

    sources = [_endpoint(endpoint) for endpoint in net.get_sources()]
    destinations = [_endpoint(endpoint) for endpoint in net.get_destinations()]
    destinations.sort(key=lambda entry: (entry["gate"] or "", entry["pin"] or ""))
    window, page = _window(destinations, limit, offset)

    return {
        "session_id": session.id,
        "id": net.get_id(),
        "name": net.get_name(),
        "is_global_input": net.is_global_input_net(),
        "is_global_output": net.is_global_output_net(),
        "is_gnd": net.is_gnd_net(),
        "is_vcc": net.is_vcc_net(),
        "is_unrouted": net.is_unrouted(),
        "source_count": len(sources),
        "sources": sources[:limit],
        "destination_count": len(destinations),
        "destinations": window,
        "destinations_page": page,
    }


def _tool_module_tree(store, arguments):
    session = store.get(_string(arguments, "session_id"))
    limit, offset = _page(arguments)
    depth = _integer(arguments, "depth", 3, minimum=0, maximum=MAX_DEPTH)

    spec = _string(arguments, "module", required=False)
    if spec:
        root = _find_module(session, spec)
    else:
        root = session.netlist.get_top_module()
    if root is None:
        raise ToolError(
            "this netlist has no top module, so there is no hierarchy to walk. "
            "hal_netlist_stats reports the flat gate and net counts."
        )

    # Depth-first pre-order: 'parent_id' plus 'depth' is the hierarchy, and a
    # flat list is what pages cleanly.
    rows = []

    def walk(module, level, parent_id):
        rows.append(
            {
                "id": module.get_id(),
                "name": module.get_name(),
                "type": module.get_type(),
                "depth": level,
                "parent_id": parent_id,
                "direct_gates": len(module.get_gates()),
                "submodules": len(module.get_submodules()),
                "truncated_here": level >= depth and len(module.get_submodules()) > 0,
            }
        )
        if level >= depth:
            return
        for child in sorted(module.get_submodules(), key=lambda m: (m.get_name(), m.get_id())):
            walk(child, level + 1, module.get_id())

    walk(root, 0, None)
    window, page = _window(rows, limit, offset)
    payload = {
        "session_id": session.id,
        "root": {"id": root.get_id(), "name": root.get_name()},
        "depth": depth,
        "modules_in_netlist": len(session.netlist.get_modules()),
        "modules": window,
    }
    payload.update(page)
    return payload


# ---------------------------------------------------------------------------
# traversal tools
# ---------------------------------------------------------------------------


def _neighbors(gate, direction):
    """Gates one hop away, structurally (no plugin, no decorator)."""
    result = []
    if direction == "in":
        for net in gate.get_fan_in_nets():
            for endpoint in net.get_sources():
                neighbor = endpoint.get_gate()
                if neighbor is not None:
                    result.append(neighbor)
    else:
        for net in gate.get_fan_out_nets():
            for endpoint in net.get_destinations():
                neighbor = endpoint.get_gate()
                if neighbor is not None:
                    result.append(neighbor)
    return result


def _seed_gates(session, arguments, direction):
    gate_spec = _string(arguments, "gate", required=False)
    net_spec = _string(arguments, "net", required=False)
    if bool(gate_spec) == bool(net_spec):
        raise ToolError(
            "give exactly one of 'gate' or 'net' as the starting point "
            "(gate name/id, or net name/id)."
        )
    if gate_spec:
        gate = _find_gate(session, gate_spec)
        return [gate], {"gate": gate.get_name(), "gate_id": gate.get_id()}, False

    net = _find_net(session, net_spec)
    endpoints = net.get_sources() if direction == "in" else net.get_destinations()
    gates = [endpoint.get_gate() for endpoint in endpoints]
    return (
        [gate for gate in gates if gate is not None],
        {"net": net.get_name(), "net_id": net.get_id()},
        True,
    )


def _traverse(store, arguments, direction):
    session = store.get(_string(arguments, "session_id"))
    limit, offset = _page(arguments)
    depth = _integer(arguments, "depth", 1, minimum=1, maximum=MAX_DEPTH)
    stop_at_sequential = _boolean(arguments, "stop_at_sequential", False)

    seeds, origin, from_net = _seed_gates(session, arguments, direction)

    # A net seed already spent one hop reaching its endpoint gates, so those
    # gates sit at distance 1; a gate seed sits at distance 0 and is excluded
    # from the answer.
    distance = {}
    frontier = []
    start_distance = 1 if from_net else 0
    for gate in seeds:
        if gate.get_id() not in distance:
            distance[gate.get_id()] = (gate, start_distance)
            frontier.append(gate)

    level = start_distance
    while frontier and level < depth:
        level += 1
        following = []
        for gate in frontier:
            if stop_at_sequential and distance[gate.get_id()][1] > start_distance:
                if _is_sequential(session, gate):
                    continue
            for neighbor in _neighbors(gate, direction):
                if neighbor.get_id() in distance:
                    continue
                distance[neighbor.get_id()] = (neighbor, level)
                following.append(neighbor)
        frontier = following

    rows = []
    for gate, hops in distance.values():
        if not from_net and hops == 0:
            continue  # the seed gate itself
        row = _gate_row(session, gate)
        row["distance"] = hops
        rows.append(row)
    rows.sort(key=lambda row: (row["distance"], row["name"]))

    window, page = _window(rows, limit, offset)
    payload = {
        "session_id": session.id,
        "direction": "fan_in" if direction == "in" else "fan_out",
        "origin": origin,
        "depth": depth,
        "stop_at_sequential": stop_at_sequential,
        "gates": window,
    }
    payload.update(page)
    return payload


def _tool_fan_in(store, arguments):
    return _traverse(store, arguments, "in")


def _tool_fan_out(store, arguments):
    return _traverse(store, arguments, "out")


def _tool_shortest_path(store, arguments):
    session = store.get(_string(arguments, "session_id"))
    start = _find_gate(session, _string(arguments, "from_gate"))
    end = _find_gate(session, _string(arguments, "to_gate"))
    both = _boolean(arguments, "search_both_directions", False)
    limit, offset = _page(arguments)

    # The submodule is bound as ``hal_py.NetlistUtils`` -- capitalised, unlike the
    # C++ namespace and unlike what .claude/skills/using-hal/SKILL.md says.
    try:
        path = session.hal_py.NetlistUtils.get_shortest_path(start, end, both)
    except Exception as exc:
        raise ToolError("NetlistUtils.get_shortest_path failed: {}".format(exc))

    rows = [_gate_row(session, gate) for gate in path or []]
    for index, row in enumerate(rows):
        row["position"] = index
    window, page = _window(rows, limit, offset)

    payload = {
        "session_id": session.id,
        "from_gate": start.get_name(),
        "to_gate": end.get_name(),
        "search_both_directions": both,
        "found": bool(rows),
        "length": len(rows),
        "path": window,
    }
    if not rows:
        payload["note"] = (
            "no forward path from {} to {}. Try search_both_directions=true, or "
            "check the direction with hal_fan_out from the start gate.".format(
                start.get_name(), end.get_name()
            )
        )
    payload.update(page)
    return payload


# ---------------------------------------------------------------------------
# analysis tools
# ---------------------------------------------------------------------------


def _tool_boolean_function(store, arguments):
    session = store.get(_string(arguments, "session_id"))
    gate = _find_gate(session, _string(arguments, "gate"))
    pin_name = _string(arguments, "pin", required=False)
    use_net_variables = _boolean(arguments, "use_net_variables", False)

    gate_type = gate.get_type()
    output_pins = [pin for pin in gate_type.get_pins() if _enum_name(pin.get_direction()) in ("output", "inout")]
    if pin_name:
        selected = [pin for pin in output_pins if pin.get_name() == pin_name]
        if not selected:
            raise ToolError(
                "gate {!r} (type {}) has no output pin {!r}. Output pins: {}.".format(
                    gate.get_name(),
                    gate_type.get_name(),
                    pin_name,
                    ", ".join(pin.get_name() for pin in output_pins) or "<none>",
                )
            )
    else:
        selected = output_pins
    if not selected:
        raise ToolError(
            "gate type {} declares no output pins, so it has no Boolean "
            "function to resolve.".format(gate_type.get_name())
        )

    functions = []
    for pin in selected:
        try:
            resolved = gate.get_resolved_boolean_function(pin, use_net_variables)
        except Exception as exc:
            functions.append({"pin": pin.get_name(), "function": None, "error": str(exc)})
            continue
        if resolved is None:
            functions.append(
                {
                    "pin": pin.get_name(),
                    "function": None,
                    "error": "HAL could not resolve this pin (see the server's stderr "
                    "log). A gate library that carries no function for the pin behaves "
                    "this way: black boxes, and FPGA LUT/ALM cells whose function lives "
                    "in a configuration mask rather than in the library. For Agilex "
                    "tennm_lcell_comb cells the mask is in the gate's 'lut_mask' data "
                    "(hal_gate_info does not decode it); "
                    "tools/hal_agilex/hal_adapter.elaborate() attaches the real "
                    "functions, but it modifies the netlist, which this read-only "
                    "server does not do.",
                }
            )
            continue
        entry = {"pin": pin.get_name(), "function": str(resolved)}
        variables = getattr(resolved, "get_variable_names", None)
        if callable(variables):
            try:
                entry["variables"] = sorted(variables())
            except Exception:
                pass
        functions.append(entry)

    return {
        "session_id": session.id,
        "gate": gate.get_name(),
        "gate_id": gate.get_id(),
        "type": gate_type.get_name(),
        "use_net_variables": use_net_variables,
        "variable_kind": "net (net_<id>)" if use_net_variables else "input pin name",
        "functions": functions,
    }


def _tool_sccs(store, arguments):
    session = store.get(_string(arguments, "session_id"))
    limit, offset = _page(arguments)
    min_size = _integer(arguments, "min_size", 2, minimum=1)
    max_gates = _integer(arguments, "max_gates_per_component", 100, minimum=1, maximum=MAX_LIMIT)

    graph_algorithm = _import_plugin(
        "graph_algorithm", "SCC extraction needs the graph_algorithm plugin (igraph)."
    )
    graph = graph_algorithm.NetlistGraph.from_netlist(session.netlist)
    if graph is None:
        raise ToolError(
            "graph_algorithm.NetlistGraph.from_netlist returned None; the netlist "
            "could not be converted into a graph (see the server's stderr log)."
        )
    components = graph_algorithm.get_connected_components(graph, True, min_size)
    if components is None:
        raise ToolError(
            "graph_algorithm.get_connected_components failed (see the server's "
            "stderr log)."
        )

    described = []
    for vertices in components:
        gates = graph.get_gates_from_vertices(list(vertices))
        histogram = {}
        for gate in gates:
            key = gate.get_type().get_name()
            histogram[key] = histogram.get(key, 0) + 1
        names = sorted(gate.get_name() for gate in gates)
        described.append(
            {
                "size": len(gates),
                "gate_types": dict(sorted(histogram.items())),
                "gates": _cap(names, max_gates),
            }
        )
    described.sort(key=lambda entry: (-entry["size"], entry["gates"]["names"][:1]))

    window, page = _window(described, limit, offset)
    payload = {
        "session_id": session.id,
        "vertices": graph.get_num_vertices(),
        "edges": graph.get_num_edges(),
        "min_size": min_size,
        "components": window,
        "note": "Every strongly connected component larger than one vertex is a "
        "feedback loop, and in a synchronous design that means state.",
    }
    payload.update(page)
    return payload


def _dataflow_result(session, arguments):
    dataflow = _import_plugin(
        "dataflow", "Register-group recovery needs the dataflow_analysis (DANA) plugin."
    )
    config = dataflow.Configuration(session.netlist).with_flip_flops()
    min_group_size = arguments.get("min_group_size")
    if min_group_size is not None:
        config = config.with_min_group_size(
            _integer(arguments, "min_group_size", 2, minimum=1)
        )
    expected = arguments.get("expected_sizes")
    if expected:
        if not isinstance(expected, list) or not all(isinstance(v, int) for v in expected):
            raise ToolError("argument 'expected_sizes' must be a list of integers.")
        config = config.with_expected_sizes(list(expected))
    if _boolean(arguments, "stage_identification", False):
        config = config.with_stage_identification(True)
    if _boolean(arguments, "type_consistency", False):
        config = config.with_type_consistency(True)

    # DANA is loud. Its logging goes to file descriptor 1, which the server
    # redirected to stderr at startup, so none of it can reach the protocol
    # stream -- see hal_mcp.server.install_stdout_guard.
    result = dataflow.analyze(config)
    if result is None:
        raise ToolError(
            "DANA (dataflow_analysis) returned no result; see the server's stderr "
            "log for the reason. Designs with no flip-flops have nothing to group."
        )
    return result


def _tool_dataflow_groups(store, arguments):
    session = store.get(_string(arguments, "session_id"))
    limit, offset = _page(arguments)
    max_gates = _integer(arguments, "max_gates_per_group", 100, minimum=1, maximum=MAX_LIMIT)

    result = _dataflow_result(session, arguments)
    groups = result.get_groups()

    described = []
    for group_id, gates in groups.items():
        names = sorted(gate.get_name() for gate in gates)
        described.append({"group_id": group_id, "size": len(names), "gates": _cap(names, max_gates)})
    described.sort(key=lambda entry: (-entry["size"], entry["group_id"]))

    window, page = _window(described, limit, offset)
    payload = {
        "session_id": session.id,
        "groups": window,
        "grouped_gates": sum(entry["size"] for entry in described),
        "note": "DANA groups flip-flops that behave like one word. A group is a "
        "hypothesis about a register, not proof of one.",
    }
    payload.update(page)
    return payload


def _control_nets(session, gate):
    """The clock/reset/set/enable nets of one gate, read from its pin types."""
    buckets = {"clock": [], "reset": [], "set": [], "enable": []}
    for pin in gate.get_type().get_pins():
        if _enum_name(pin.get_direction()) not in ("input", "inout"):
            continue
        kind = _enum_name(pin.get_type())
        if kind not in buckets:
            continue
        net = gate.get_fan_in_net(pin)
        if net is None:
            continue
        if net.is_gnd_net() or net.is_vcc_net():
            continue  # a tied-off control pin is not a domain
        buckets[kind].append(net.get_name())
    return {kind: sorted(set(names)) for kind, names in buckets.items()}


def _tool_clock_domains(store, arguments):
    session = store.get(_string(arguments, "session_id"))
    limit, offset = _page(arguments)
    max_gates = _integer(arguments, "max_gates_per_domain", 100, minimum=1, maximum=MAX_LIMIT)

    buckets = {}
    sequential = 0
    unclocked = []
    for gate in session.netlist.get_gates():
        if not _is_sequential(session, gate):
            continue
        sequential += 1
        control = _control_nets(session, gate)
        if not control["clock"]:
            unclocked.append(gate.get_name())
        key = (
            tuple(control["clock"]),
            tuple(control["reset"]),
            tuple(control["set"]),
            tuple(control["enable"]),
        )
        buckets.setdefault(key, []).append(gate.get_name())

    described = []
    for key, names in buckets.items():
        described.append(
            {
                "clock": list(key[0]),
                "reset": list(key[1]),
                "set": list(key[2]),
                "enable": list(key[3]),
                "size": len(names),
                "gates": _cap(sorted(names), max_gates),
            }
        )
    described.sort(key=lambda entry: (-entry["size"], entry["clock"], entry["reset"]))

    window, page = _window(described, limit, offset)
    payload = {
        "session_id": session.id,
        "sequential_gates": sequential,
        "domains": window,
        "unclocked_sequential_gates": _cap(sorted(unclocked), max_gates),
        "method": "structural: every sequential gate's clock/reset/set/enable "
        "pins are read from the gate library's pin types and the nets attached "
        "to them form the grouping key. Constant-tied control pins are ignored. "
        "The clock_tree_extractor plugin is deliberately not used -- it returns "
        "vertices it was asked to exclude (issue #63).",
        "caveat": "Sharing a clock, a reset and an enable is necessary for two "
        "flip-flops to be one register, never sufficient. Cross-check with "
        "hal_dataflow_groups.",
    }
    payload.update(page)
    return payload


# ---------------------------------------------------------------------------
# visualization
# ---------------------------------------------------------------------------

_RENDER_KINDS = ("module_tree", "netlist_graph", "dataflow")
_RENDER_FORMATS = ("svg", "png", "pdf")


def _split_output(output_path):
    """``/tmp/x/top.svg`` -> ``("/tmp/x/top", "svg")``; default format is svg."""
    output_path = os.path.abspath(os.path.expanduser(str(output_path)))
    base, suffix = os.path.splitext(output_path)
    fmt = suffix.lstrip(".").lower()
    if fmt not in _RENDER_FORMATS:
        base = output_path
        fmt = "svg"
        output_path = base + ".svg"
    parent = os.path.dirname(output_path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise ToolError("cannot create the output directory {}: {}".format(parent, exc))
    return output_path, base, fmt


def _render(dot_path, output_path, fmt, engine):
    from hal_viz.render import RenderError, find_dot_binary, render_dot

    try:
        binary = find_dot_binary()
        if not binary:
            return None, (
                "Graphviz 'dot' was not found on PATH, so only the .dot file was "
                "written. Install Graphviz or render the .dot elsewhere."
            )
        render_dot(dot_path, output_path, fmt, dot_binary=binary, engine=engine, timeout=600)
    except RenderError as exc:
        return None, "{} The .dot file was still written.".format(exc)
    return output_path, None


def _tool_render_graph(store, arguments):
    session = store.get(_string(arguments, "session_id"))
    kind = _string(arguments, "kind")
    if kind not in _RENDER_KINDS:
        raise ToolError(
            "unknown kind {!r}; expected one of: {}.".format(kind, ", ".join(_RENDER_KINDS))
        )
    engine = _string(arguments, "engine", required=False, default="dot")
    output_path, base, fmt = _split_output(_string(arguments, "output_path"))
    dot_path = base + ".dot"

    from hal_viz import __version__ as viz_version
    from hal_viz.extract import (
        ScopeTooLarge,
        build_module_tree_graph,
        build_netlist_graph,
        collect_module_gates,
        collect_neighborhood,
    )

    nodes = edges = None
    if kind == "module_tree":
        depth = arguments.get("depth")
        spec = _string(arguments, "module", required=False)
        root = _find_module(session, spec) if spec else session.netlist.get_top_module()
        if root is None:
            raise ToolError("this netlist has no top module, so there is no tree to draw.")
        graph = build_module_tree_graph(
            root,
            max_depth=None if depth is None else _integer(arguments, "depth", 3, minimum=0, maximum=MAX_DEPTH),
            comment="generated by hal_mcp via hal_viz {}".format(viz_version),
        )
        scope = "module hierarchy below {!r}".format(root.get_name())
        graph.write(dot_path)
        nodes, edges = graph.node_count, graph.edge_count

    elif kind == "netlist_graph":
        max_gates = _integer(arguments, "max_gates", 400, minimum=1, maximum=100000)
        depth = _integer(arguments, "depth", 1, minimum=0, maximum=MAX_DEPTH)
        module_spec = _string(arguments, "module", required=False)
        gate_spec = _string(arguments, "gate", required=False)
        if module_spec and gate_spec:
            raise ToolError("give at most one of 'module' or 'gate' to scope the graph.")
        if module_spec:
            module = _find_module(session, module_spec)
            gates = collect_module_gates(module, recursive=_boolean(arguments, "recursive", False))
            scope = "module {!r}".format(module.get_name())
            if depth:
                gates = collect_neighborhood(gates, depth, "both", max_gates)
                scope += " + {} hop(s)".format(depth)
        elif gate_spec:
            seed = _find_gate(session, gate_spec)
            gates = collect_neighborhood([seed], depth, "both", max_gates)
            scope = "gate {!r}, depth {}".format(seed.get_name(), depth)
        else:
            gates = session.netlist.get_gates()
            scope = "whole netlist"
        gates = list(gates)
        if len(gates) > max_gates:
            raise ToolError(
                "the selected scope holds {} gates, more than max_gates={}. Scope "
                "the view with 'module' or 'gate' plus 'depth', or raise "
                "max_gates.".format(len(gates), max_gates)
            )
        title = "{} - {} - {} gates".format(
            session.netlist.get_design_name() or os.path.basename(session.source),
            scope,
            len(gates),
        )
        graph = build_netlist_graph(
            gates,
            title=title,
            cluster_modules=_boolean(arguments, "cluster_modules", False),
            comment="generated by hal_mcp via hal_viz {}".format(viz_version),
        )
        graph.write(dot_path)
        nodes, edges = graph.node_count, graph.edge_count

    else:  # dataflow
        result = _dataflow_result(session, arguments)
        # DANA writes a fixed 'graph.dot' into a directory of its choosing, so
        # give it a scratch one and move the file to the caller's base name.
        scratch = tempfile.mkdtemp(prefix="hal_mcp_dataflow_")
        try:
            if not result.write_dot(scratch):
                raise ToolError(
                    "the dataflow result could not be written as DOT; see the "
                    "server's stderr log."
                )
            produced = os.path.join(scratch, "graph.dot")
            if not os.path.isfile(produced):
                raise ToolError(
                    "DANA reported success but wrote no graph.dot into {}.".format(scratch)
                )
            shutil.copyfile(produced, dot_path)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        scope = "{} DANA register group(s)".format(len(result.get_groups()))

    rendered, note = _render(dot_path, output_path, fmt, engine)
    payload = {
        "session_id": session.id,
        "kind": kind,
        "rendered": rendered,
        "dot_path": dot_path,
        "format": fmt,
        "engine": engine,
        "scope": scope,
    }
    if nodes is not None:
        payload["nodes"] = nodes
        payload["edges"] = edges
    if note:
        payload["note"] = note
    return payload


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


def _schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_SESSION_PROP = {
    "session_id": {
        "type": "string",
        "description": "Session id returned by hal_load_netlist or hal_load_project. "
        "The netlist behind it stays parsed in the server process, so repeated "
        "calls cost nothing extra.",
    }
}


def _paged(properties, extra_note=""):
    merged = dict(properties)
    merged["limit"] = {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_LIMIT,
        "default": DEFAULT_LIMIT,
        "description": "Maximum number of items to return (default {}, hard maximum "
        "{}). The true total is always reported alongside.{}".format(
            DEFAULT_LIMIT, MAX_LIMIT, extra_note
        ),
    }
    merged["offset"] = {
        "type": "integer",
        "minimum": 0,
        "default": 0,
        "description": "Number of items to skip before returning; page through a "
        "large result by advancing offset by limit.",
    }
    return merged


class Tool(object):
    def __init__(self, name, description, schema, handler):
        self.name = name
        self.description = description
        self.schema = schema
        self.handler = handler

    def to_json(self):
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
        }


TOOLS = [
    Tool(
        "hal_load_netlist",
        "Load a gate-level netlist into a new server-side session and return its "
        "session_id plus a summary (design name, gate/net/module counts, top "
        "module). This is the expensive call -- it imports hal_py, loads all HAL "
        "plugins and parses the design -- and it happens once; every other hal_* "
        "tool then answers from the loaded netlist in milliseconds. HDL netlists "
        "(.v/.vhd) need a gate_library; a .hal file carries its own.",
        _schema(
            {
                "netlist_path": {
                    "type": "string",
                    "description": "Path to the netlist: a .hal file, an HDL netlist "
                    "(.v/.vhd, needs gate_library), or a HAL project directory.",
                },
                "gate_library": {
                    "type": "string",
                    "description": "Path to the gate library (.hgl or .lib) the netlist "
                    "was mapped to. Required for HDL netlists. Bundled libraries live "
                    "in plugins/gate_libraries/definitions.",
                },
            },
            required=["netlist_path"],
        ),
        _tool_load_netlist,
    ),
    Tool(
        "hal_load_project",
        "Load a HAL project directory (the kind produced by 'hal --import-netlist "
        "... --project-dir ...' or by unzipping an examples/*.zip archive) into a "
        "new session. Use hal_load_netlist for a bare netlist file.",
        _schema(
            {
                "project_dir": {
                    "type": "string",
                    "description": "Path to the HAL project directory.",
                }
            },
            required=["project_dir"],
        ),
        _tool_load_project,
    ),
    Tool(
        "hal_list_sessions",
        "List the netlists currently loaded in this server, with their session ids, "
        "sources and sizes. Use it to find a session_id you have lost track of.",
        _schema(_paged({})),
        _tool_list_sessions,
    ),
    Tool(
        "hal_close_session",
        "Close a session and release its netlist. Nothing is written to disk; the "
        "netlist file itself is untouched.",
        _schema(dict(_SESSION_PROP), required=["session_id"]),
        _tool_close_session,
    ),
    Tool(
        "hal_netlist_stats",
        "First look at a loaded design: gate/net/module counts, how many gates are "
        "sequential, the gate-type histogram (most frequent first, paginated), the "
        "top module, and the global input/output/GND/VCC nets.",
        _schema(_paged(dict(_SESSION_PROP), " Applies to the gate-type histogram."), required=["session_id"]),
        _tool_netlist_stats,
    ),
    Tool(
        "hal_list_gates",
        "List gates, optionally filtered by a case-insensitive substring of the "
        "gate type and/or of the gate name, and optionally restricted to sequential "
        "gates. Always paginated, always reports the true total.",
        _schema(
            _paged(
                dict(
                    _SESSION_PROP,
                    type_contains={
                        "type": "string",
                        "description": "Case-insensitive substring of the gate type name, "
                        "e.g. 'ff' to find flip-flops or 'lcell' for ALM cells.",
                    },
                    name_contains={
                        "type": "string",
                        "description": "Case-insensitive substring of the gate instance name.",
                    },
                    sequential_only={
                        "type": "boolean",
                        "default": False,
                        "description": "Keep only gates whose type carries the 'sequential' "
                        "property (flip-flops, latches).",
                    },
                )
            ),
            required=["session_id"],
        ),
        _tool_list_gates,
    ),
    Tool(
        "hal_gate_info",
        "Everything about one gate: its type and type properties, whether it is "
        "sequential, its module, its placement location when the netlist carries "
        "one, and every pin with the net attached to it (including which pins sit "
        "on GND/VCC).",
        _schema(
            _paged(
                dict(
                    _SESSION_PROP,
                    gate={
                        "type": "string",
                        "description": "Gate to describe: numeric gate id, exact name, or a "
                        "substring that matches exactly one gate name.",
                    },
                ),
                " Caps the number of pins listed per direction.",
            ),
            required=["session_id", "gate"],
        ),
        _tool_gate_info,
    ),
    Tool(
        "hal_net_info",
        "Everything about one net: its driver(s), its destinations (gate and pin, "
        "paginated), and whether it is a global input, a global output, GND or VCC.",
        _schema(
            _paged(
                dict(
                    _SESSION_PROP,
                    net={
                        "type": "string",
                        "description": "Net to describe: numeric net id, exact name, or a "
                        "substring that matches exactly one net name.",
                    },
                ),
                " Applies to the destination list.",
            ),
            required=["session_id", "net"],
        ),
        _tool_net_info,
    ),
    Tool(
        "hal_module_tree",
        "Walk the module hierarchy from the top module (or from a named module) "
        "down to a bounded depth. Returns modules in depth-first pre-order; each "
        "row carries its depth and parent_id, which together are the tree, plus "
        "its direct gate and submodule counts.",
        _schema(
            _paged(
                dict(
                    _SESSION_PROP,
                    module={
                        "type": "string",
                        "description": "Root of the walk: module id, exact name, or a unique "
                        "substring. Defaults to the netlist's top module.",
                    },
                    depth={
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_DEPTH,
                        "default": 3,
                        "description": "How many levels below the root to walk. 0 returns only "
                        "the root. Modules cut off are flagged with truncated_here.",
                    },
                )
            ),
            required=["session_id"],
        ),
        _tool_module_tree,
    ),
    Tool(
        "hal_fan_in",
        "Walk backwards from a gate or a net and report every gate reachable "
        "within 'depth' hops, with its distance. Set stop_at_sequential=true to "
        "stop at flip-flops and latches, which bounds the walk to one clock cycle "
        "of combinational logic.",
        _schema(
            _paged(
                dict(
                    _SESSION_PROP,
                    gate={
                        "type": "string",
                        "description": "Start gate: id, exact name, or unique substring. Give "
                        "either gate or net, not both.",
                    },
                    net={
                        "type": "string",
                        "description": "Start net: id, exact name, or unique substring. Its "
                        "driving gates are the first hop.",
                    },
                    depth={
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_DEPTH,
                        "default": 1,
                        "description": "Maximum number of hops to walk.",
                    },
                    stop_at_sequential={
                        "type": "boolean",
                        "default": False,
                        "description": "Report sequential gates but do not traverse through "
                        "them, so the result is one cycle of combinational cone.",
                    },
                )
            ),
            required=["session_id"],
        ),
        _tool_fan_in,
    ),
    Tool(
        "hal_fan_out",
        "Walk forwards from a gate or a net and report every gate reachable within "
        "'depth' hops, with its distance. The forward twin of hal_fan_in; "
        "stop_at_sequential=true bounds the walk at the next register stage.",
        _schema(
            _paged(
                dict(
                    _SESSION_PROP,
                    gate={
                        "type": "string",
                        "description": "Start gate: id, exact name, or unique substring. Give "
                        "either gate or net, not both.",
                    },
                    net={
                        "type": "string",
                        "description": "Start net: id, exact name, or unique substring. Its "
                        "sink gates are the first hop.",
                    },
                    depth={
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_DEPTH,
                        "default": 1,
                        "description": "Maximum number of hops to walk.",
                    },
                    stop_at_sequential={
                        "type": "boolean",
                        "default": False,
                        "description": "Report sequential gates but do not traverse through "
                        "them, so the result stops at the next register stage.",
                    },
                )
            ),
            required=["session_id"],
        ),
        _tool_fan_out,
    ),
    Tool(
        "hal_shortest_path",
        "Find the shortest gate path connecting two gates (hal_py's "
        "NetlistUtils.get_shortest_path). Returns the gates in traversal order, "
        "or found=false with a hint when there is no such path.",
        _schema(
            _paged(
                dict(
                    _SESSION_PROP,
                    from_gate={
                        "type": "string",
                        "description": "Start gate: id, exact name, or unique substring.",
                    },
                    to_gate={
                        "type": "string",
                        "description": "End gate: id, exact name, or unique substring.",
                    },
                    search_both_directions={
                        "type": "boolean",
                        "default": False,
                        "description": "Also look for a shorter path from end to start; the "
                        "result may then be in reverse order.",
                    },
                ),
                " Applies to the returned path.",
            ),
            required=["session_id", "from_gate", "to_gate"],
        ),
        _tool_shortest_path,
    ),
    Tool(
        "hal_boolean_function",
        "Resolve the Boolean function of a gate's output pin so that it depends "
        "only on the gate's inputs (Gate.get_resolved_boolean_function). With "
        "use_net_variables=false (the default) the variables are the gate's input "
        "pin names; with true they are net variables named net_<id>, which is what "
        "you want when composing functions across gates.",
        _schema(
            dict(
                _SESSION_PROP,
                gate={
                    "type": "string",
                    "description": "Gate: id, exact name, or unique substring.",
                },
                pin={
                    "type": "string",
                    "description": "Output pin name. Omit to resolve every output pin of "
                    "the gate.",
                },
                use_net_variables={
                    "type": "boolean",
                    "default": False,
                    "description": "False: variables are input pin names. True: variables "
                    "are net_<id> names derived from the fan-in nets.",
                },
            ),
            required=["session_id", "gate"],
        ),
        _tool_boolean_function,
    ),
    Tool(
        "hal_sccs",
        "Strongly connected components of the gate graph, via the graph_algorithm "
        "plugin (igraph). Purely combinational logic is a DAG, so every component "
        "bigger than one vertex is a feedback loop -- the cheapest first cut "
        "between logic and state on an unknown netlist.",
        _schema(
            _paged(
                dict(
                    _SESSION_PROP,
                    min_size={
                        "type": "integer",
                        "minimum": 1,
                        "default": 2,
                        "description": "Ignore components smaller than this. 2 (the default) "
                        "drops the trivial single-gate components.",
                    },
                    max_gates_per_component={
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LIMIT,
                        "default": 100,
                        "description": "Cap on the gate names listed inside one component; "
                        "the component's true size is reported regardless.",
                    },
                ),
                " Applies to the component list.",
            ),
            required=["session_id"],
        ),
        _tool_sccs,
    ),
    Tool(
        "hal_dataflow_groups",
        "Run DANA (the dataflow_analysis plugin) over the flip-flops and return "
        "the register groups it recovers, largest first. Each group is a "
        "hypothesis that those flip-flops form one word. This is the slow tool -- "
        "seconds to minutes on a large design.",
        _schema(
            _paged(
                dict(
                    _SESSION_PROP,
                    min_group_size={
                        "type": "integer",
                        "minimum": 1,
                        "description": "Discard groups smaller than this (DANA's "
                        "with_min_group_size).",
                    },
                    expected_sizes={
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Word sizes you expect to find, e.g. [8, 32]; DANA "
                        "favours groupings that produce them.",
                    },
                    stage_identification={
                        "type": "boolean",
                        "default": False,
                        "description": "Enable DANA's stage identification.",
                    },
                    type_consistency={
                        "type": "boolean",
                        "default": False,
                        "description": "Require the gates of a group to share a gate type.",
                    },
                    max_gates_per_group={
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LIMIT,
                        "default": 100,
                        "description": "Cap on the gate names listed inside one group; the "
                        "group's true size is reported regardless.",
                    },
                ),
                " Applies to the group list.",
            ),
            required=["session_id"],
        ),
        _tool_dataflow_groups,
    ),
    Tool(
        "hal_clock_domains",
        "Group every sequential gate by the nets on its clock, reset, set and "
        "enable pins, read structurally from the gate library's pin types. Two "
        "flip-flops in the same group at least *could* belong to one register; two "
        "in different groups cannot. Constant-tied control pins are ignored, and "
        "flip-flops with no clock net are called out separately.",
        _schema(
            _paged(
                dict(
                    _SESSION_PROP,
                    max_gates_per_domain={
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LIMIT,
                        "default": 100,
                        "description": "Cap on the gate names listed inside one domain; the "
                        "domain's true size is reported regardless.",
                    },
                ),
                " Applies to the domain list.",
            ),
            required=["session_id"],
        ),
        _tool_clock_domains,
    ),
    Tool(
        "hal_render_graph",
        "Draw the loaded design and write the picture to a path you choose. "
        "kind=module_tree draws the module hierarchy; kind=netlist_graph draws a "
        "gate-level graph (scope it with module or gate plus depth -- a whole "
        "netlist is rarely renderable); kind=dataflow draws DANA's register "
        "groups. The Graphviz .dot source is always written next to the image, so "
        "a machine without Graphviz still gets a usable artifact.",
        _schema(
            dict(
                _SESSION_PROP,
                kind={
                    "type": "string",
                    "enum": list(_RENDER_KINDS),
                    "description": "What to draw: module_tree, netlist_graph or dataflow.",
                },
                output_path={
                    "type": "string",
                    "description": "Where to write the image. The extension picks the format "
                    "(.svg, .png, .pdf); anything else is treated as a base name and .svg "
                    "is appended. The .dot source is written next to it.",
                },
                module={
                    "type": "string",
                    "description": "Scope for module_tree (root module) or netlist_graph "
                    "(draw this module's gates). Module id, exact name, or unique substring.",
                },
                gate={
                    "type": "string",
                    "description": "Scope for netlist_graph: draw the neighborhood of this "
                    "gate. Gate id, exact name, or unique substring.",
                },
                depth={
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_DEPTH,
                    "description": "netlist_graph: how many hops around the scope to include "
                    "(default 1). module_tree: how many hierarchy levels to draw.",
                },
                max_gates={
                    "type": "integer",
                    "minimum": 1,
                    "default": 400,
                    "description": "netlist_graph: refuse to draw more gates than this "
                    "(default 400). A bigger graph is unreadable, not just slow.",
                },
                recursive={
                    "type": "boolean",
                    "default": False,
                    "description": "netlist_graph with 'module': also include the gates of "
                    "its submodules.",
                },
                cluster_modules={
                    "type": "boolean",
                    "default": False,
                    "description": "netlist_graph: draw a box around the gates of each module.",
                },
                engine={
                    "type": "string",
                    "enum": ["dot", "neato", "fdp", "sfdp", "circo", "twopi", "osage"],
                    "default": "dot",
                    "description": "Graphviz layout engine. Use sfdp for very large graphs.",
                },
                min_group_size={
                    "type": "integer",
                    "minimum": 1,
                    "description": "kind=dataflow: DANA's minimum group size.",
                },
            ),
            required=["session_id", "kind", "output_path"],
        ),
        _tool_render_graph,
    ),
]

TOOLS_BY_NAME = dict((tool.name, tool) for tool in TOOLS)


def list_tools():
    """The ``tools/list`` payload."""
    return [tool.to_json() for tool in TOOLS]


def call_tool(store, name, arguments):
    """Run one tool; raises :class:`ToolError` for anything the caller can fix."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise ToolError(
            "unknown tool {!r}. Available tools: {}.".format(
                name, ", ".join(sorted(TOOLS_BY_NAME))
            )
        )
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolError("the 'arguments' member of tools/call must be an object.")
    unknown = sorted(set(arguments) - set(tool.schema["properties"]))
    if unknown:
        raise ToolError(
            "unknown argument(s) for {}: {}. Accepted: {}.".format(
                name, ", ".join(unknown), ", ".join(sorted(tool.schema["properties"]))
            )
        )
    return tool.handler(store, arguments)
