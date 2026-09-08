"""The read-only netlist queries -- and nothing that imports ``hal_py``.

Every netlist object is used through the duck-typed accessors the ``hal_py``
bindings expose, exactly as :mod:`hal_viz.extract` does:

* ``Netlist`` -- ``get_gates``, ``get_nets``, ``get_modules``,
  ``get_gate_by_id``, ``get_net_by_id``, ``get_module_by_id``,
  ``get_design_name``, ``get_top_module``, ``get_gate_library``
* ``Gate``    -- ``get_id``, ``get_name``, ``get_type``, ``get_module``,
  ``get_fan_in_endpoints``, ``get_fan_out_endpoints``,
  ``get_unique_successors``, ``get_unique_predecessors``, ``is_gnd_gate``,
  ``is_vcc_gate``
* ``Net``     -- ``get_id``, ``get_name``, ``get_sources``,
  ``get_destinations``, ``is_global_input_net``, ``is_global_output_net``
* ``Module``  -- ``get_id``, ``get_name``, ``get_type``, ``get_gates``,
  ``get_submodules``, ``get_parent_module``
* ``Endpoint``-- ``get_gate``, ``get_net``, ``get_pin``

Keeping the query layer free of ``hal_py`` is not tidiness: it is what lets the
pagination, filtering, cone and truncation logic be tested on a machine that
cannot build HAL, which is where this code was written.  The in-HAL side
(:mod:`hal_analysis_api.inhal`) does nothing but load a netlist and call these
functions.

Two invariants hold everywhere here:

* **Order is deterministic.**  Everything is sorted by object id before it is
  paginated or cut, so page 2 of a listing follows page 1 and two runs of the
  same query return the same answer.
* **A cut is reported.**  No function silently shortens a list; it returns a
  ``truncation`` record alongside.

Nothing in this module writes to the netlist.  It calls no setter, no
``create_*``, no ``delete_*`` and no ``ProjectManager`` -- the process it runs
in exits without saving, so a read cannot modify a project even by accident.
"""

from . import limits as limit_module
from .errors import LimitExceeded, UnknownObject

__all__ = [
    "DIRECTIONS",
    "summary",
    "gates",
    "nets",
    "modules",
    "gate",
    "net",
    "cone",
]

#: Cone directions. ``fan_in`` walks towards the drivers of the seeds,
#: ``fan_out`` towards what they drive.
DIRECTIONS = ("fan_in", "fan_out", "both")


# ---------------------------------------------------------------------------
# defensive accessors
# ---------------------------------------------------------------------------


def _name(obj, default=""):
    if obj is None:
        return default
    getter = getattr(obj, "get_name", None)
    if getter is None:
        return str(obj)
    try:
        return getter() or default
    except Exception:  # pragma: no cover - defensive against binding quirks
        return default


def _call(obj, method, default=None):
    function = getattr(obj, method, None)
    if function is None:
        return default
    try:
        return function()
    except Exception:  # pragma: no cover - defensive
        return default


def _type_name(gate_object):
    return _name(_call(gate_object, "get_type"), "<untyped>")


def _properties(gate_object):
    """Gate-type properties as strings (``ff``, ``combinational``, ...).

    The bindings return an enum set; ``str()`` of a pybind11 enum is
    ``GateTypeProperty.ff``, so the qualifier is stripped.  Anything that does
    not behave like that is reported as its own ``str``, never dropped.
    """
    gate_type = _call(gate_object, "get_type")
    if gate_type is None:
        return []
    raw = _call(gate_type, "get_properties", []) or []
    names = []
    for entry in raw:
        text = getattr(entry, "name", None) or str(entry)
        names.append(text.split(".")[-1])
    return sorted(set(names))


def _module_of(gate_object):
    return _call(gate_object, "get_module")


def _gate_ref(gate_object, with_module=True, with_properties=False):
    reference = {
        "id": gate_object.get_id(),
        "name": _name(gate_object),
        "type": _type_name(gate_object),
    }
    if with_module:
        module = _module_of(gate_object)
        reference["module_id"] = module.get_id() if module is not None else None
        reference["module_name"] = _name(module, None) if module is not None else None
    if with_properties:
        reference["properties"] = _properties(gate_object)
    return reference


def _net_ref(net_object, with_counts=True):
    reference = {"id": net_object.get_id(), "name": _name(net_object)}
    reference["is_global_input"] = bool(_call(net_object, "is_global_input_net", False))
    reference["is_global_output"] = bool(_call(net_object, "is_global_output_net", False))
    if with_counts:
        reference["source_count"] = len(_call(net_object, "get_sources", []) or [])
        reference["destination_count"] = len(_call(net_object, "get_destinations", []) or [])
    return reference


def _module_ref(module_object):
    parent = _call(module_object, "get_parent_module")
    module_type = _call(module_object, "get_type")
    return {
        "id": module_object.get_id(),
        "name": _name(module_object),
        "type": str(module_type) if module_type else None,
        "parent_id": parent.get_id() if parent is not None else None,
        "gate_count": len(_call(module_object, "get_gates", []) or []),
        "submodule_count": len(_call(module_object, "get_submodules", []) or []),
    }


def _endpoint(endpoint_object, direction, peer=None, peer_known=False):
    """One endpoint as the API reports it.

    ``gate`` is the gate *on the other side*: for a net's sources and
    destinations that is simply the endpoint's own gate, and for a gate's own
    fan-in/fan-out it is the neighbour the pin is connected to (``None`` when
    the pin leaves the netlist, which is information -- an undriven input looks
    different from one driven by a gate).
    """
    net_object = _call(endpoint_object, "get_net")
    gate_object = peer if peer_known else _call(endpoint_object, "get_gate")
    return {
        "pin": _name(_call(endpoint_object, "get_pin"), "?"),
        "direction": direction,
        "net": {"id": net_object.get_id(), "name": _name(net_object)} if net_object else None,
        "gate": _gate_ref(gate_object, with_module=False) if gate_object else None,
    }


def _pin_view(gate_object, side):
    """A gate's pins expanded to one entry per connected neighbour.

    ``get_fan_in_endpoints()`` answers "which pins does this gate have"; an
    agent asking about a gate wants "what drives each input pin and what does
    each output pin drive".  So each endpoint is expanded across the peers on
    its net, and a pin with no peer still produces one entry (with ``gate:
    null``) rather than disappearing.
    """
    if side == "fan_in":
        endpoints = _call(gate_object, "get_fan_in_endpoints", []) or []
        peers_of = "get_sources"
        direction = "input"
    else:
        endpoints = _call(gate_object, "get_fan_out_endpoints", []) or []
        peers_of = "get_destinations"
        direction = "output"

    entries = []
    for endpoint_object in endpoints:
        net_object = _call(endpoint_object, "get_net")
        peers = []
        if net_object is not None:
            for peer_endpoint in _call(net_object, peers_of, []) or []:
                peer_gate = _call(peer_endpoint, "get_gate")
                if peer_gate is not None and peer_gate.get_id() != gate_object.get_id():
                    peers.append(peer_gate)
        if not peers:
            entries.append(_endpoint(endpoint_object, direction, peer=None, peer_known=True))
            continue
        for peer_gate in sorted(peers, key=lambda item: item.get_id()):
            entries.append(
                _endpoint(endpoint_object, direction, peer=peer_gate, peer_known=True)
            )
    return entries


def _by_id(collection):
    """Deterministic order for anything with ``get_id``: gates, nets, modules."""
    return sorted(collection or [], key=lambda item: item.get_id())


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def summary(netlist, max_gate_types=limit_module.DEFAULT_GATE_TYPES):
    """Counts, design identity and the gate-type histogram.

    The histogram is the cheapest thing that tells an agent what kind of design
    it is looking at, so it is here rather than behind another call -- but it is
    cut at ``max_gate_types`` (largest first) with the total kept in
    ``counts.gate_types``, so a library with 900 cell types cannot flood a
    context window.
    """
    all_gates = list(netlist.get_gates() or [])
    all_nets = list(netlist.get_nets() or [])
    all_modules = list(netlist.get_modules() or [])

    histogram = {}
    for gate_object in all_gates:
        name = _type_name(gate_object)
        histogram[name] = histogram.get(name, 0) + 1
    ordered = sorted(histogram.items(), key=lambda item: (-item[1], item[0]))
    shown = ordered[: max(1, int(max_gate_types))]

    top_module = _call(netlist, "get_top_module")
    library = _call(netlist, "get_gate_library")

    return {
        "design_name": _call(netlist, "get_design_name") or None,
        "netlist_id": _call(netlist, "get_id"),
        "device_name": _call(netlist, "get_device_name") or None,
        "gate_library": _name(library, None) if library is not None else None,
        "top_module": _module_ref(top_module) if top_module is not None else None,
        "counts": {
            "gates": len(all_gates),
            "nets": len(all_nets),
            "modules": len(all_modules),
            "gate_types": len(ordered),
            "global_inputs": sum(
                1 for item in all_nets if _call(item, "is_global_input_net", False)
            ),
            "global_outputs": sum(
                1 for item in all_nets if _call(item, "is_global_output_net", False)
            ),
        },
        "gate_types": [{"type": name, "count": count} for name, count in shown],
        "truncation": limit_module.truncation(
            len(shown) < len(ordered),
            reason="{} gate types in this netlist; the {} most common are "
            "listed".format(len(ordered), len(shown)),
            limit=int(max_gate_types),
            kind="gate_types",
        ),
    }


def gates(
    netlist,
    name_contains=None,
    gate_type=None,
    module_id=None,
    offset=0,
    limit=limit_module.DEFAULT_PAGE_LIMIT,
):
    """A page of gates, filtered before it is paginated.

    ``page.total`` counts the gates that matched the *filter*, not the netlist,
    so a caller can tell "there are 258 flip-flops, you have seen 50" from "the
    netlist has 407 gates".
    """
    if module_id is not None:
        module_object = netlist.get_module_by_id(int(module_id))
        if module_object is None:
            raise UnknownObject(
                "no module with id {} in this netlist".format(module_id),
                hint="list the hierarchy with netlist.modules",
                data={"kind": "module", "id": module_id},
            )
        candidates = list(_call(module_object, "get_gates", []) or [])
    else:
        candidates = list(netlist.get_gates() or [])

    needle = (name_contains or "").lower()
    matched = []
    for gate_object in candidates:
        if needle and needle not in _name(gate_object).lower():
            continue
        if gate_type and _type_name(gate_object) != gate_type:
            continue
        matched.append(gate_object)

    window, page = limit_module.paginate(_by_id(matched), offset, limit)
    return {
        "gates": [_gate_ref(item, with_properties=True) for item in window],
        "page": page,
        "filters": {
            "name_contains": name_contains,
            "gate_type": gate_type,
            "module_id": module_id,
        },
    }


def nets(
    netlist,
    name_contains=None,
    global_only=False,
    offset=0,
    limit=limit_module.DEFAULT_PAGE_LIMIT,
):
    """A page of nets, optionally only the netlist's global inputs and outputs."""
    needle = (name_contains or "").lower()
    matched = []
    for net_object in netlist.get_nets() or []:
        if needle and needle not in _name(net_object).lower():
            continue
        if global_only and not (
            _call(net_object, "is_global_input_net", False)
            or _call(net_object, "is_global_output_net", False)
        ):
            continue
        matched.append(net_object)

    window, page = limit_module.paginate(_by_id(matched), offset, limit)
    return {
        "nets": [_net_ref(item) for item in window],
        "page": page,
        "filters": {"name_contains": name_contains, "global_only": bool(global_only)},
    }


def modules(
    netlist, name_contains=None, offset=0, limit=limit_module.DEFAULT_PAGE_LIMIT
):
    """A page of modules."""
    needle = (name_contains or "").lower()
    matched = [
        item
        for item in (netlist.get_modules() or [])
        if not needle or needle in _name(item).lower()
    ]
    window, page = limit_module.paginate(_by_id(matched), offset, limit)
    return {
        "modules": [_module_ref(item) for item in window],
        "page": page,
        "filters": {"name_contains": name_contains},
    }


def gate(netlist, gate_id, max_endpoints=limit_module.DEFAULT_ENDPOINTS):
    """One gate with its endpoints.

    A missing id is ``unknown_object`` and never an empty gate: an agent that
    reads an empty fan-in would conclude the gate is undriven.
    """
    gate_object = netlist.get_gate_by_id(int(gate_id))
    if gate_object is None:
        raise UnknownObject(
            "no gate with id {} in this netlist".format(gate_id),
            hint="find one with netlist.gates (filter by name_contains or gate_type)",
            data={"kind": "gate", "id": int(gate_id)},
        )

    fan_in = _pin_view(gate_object, "fan_in")
    fan_out = _pin_view(gate_object, "fan_out")
    budget = max(1, int(max_endpoints))
    shown_in = fan_in[:budget]
    shown_out = fan_out[:budget]

    detail = _gate_ref(gate_object, with_properties=True)
    detail.update(
        {
            "is_gnd": bool(_call(gate_object, "is_gnd_gate", False)),
            "is_vcc": bool(_call(gate_object, "is_vcc_gate", False)),
            "fan_in": shown_in,
            "fan_out": shown_out,
            "fan_in_total": len(fan_in),
            "fan_out_total": len(fan_out),
            "truncation": limit_module.truncation(
                len(shown_in) < len(fan_in) or len(shown_out) < len(fan_out),
                reason="the gate has {} fan-in and {} fan-out pin connections; at most {} of "
                "each are listed".format(len(fan_in), len(fan_out), budget),
                limit=budget,
                kind="endpoints",
            ),
        }
    )
    return {"gate": detail}


def net(netlist, net_id, max_endpoints=limit_module.DEFAULT_ENDPOINTS):
    """One net with its sources and destinations."""
    net_object = netlist.get_net_by_id(int(net_id))
    if net_object is None:
        raise UnknownObject(
            "no net with id {} in this netlist".format(net_id),
            hint="find one with netlist.nets, or from a gate's endpoints via netlist.gate",
            data={"kind": "net", "id": int(net_id)},
        )

    sources = list(_call(net_object, "get_sources", []) or [])
    destinations = list(_call(net_object, "get_destinations", []) or [])
    budget = max(1, int(max_endpoints))
    shown_sources = sources[:budget]
    shown_destinations = destinations[:budget]

    detail = _net_ref(net_object)
    detail.update(
        {
            "sources": [_endpoint(item, "output") for item in shown_sources],
            "destinations": [_endpoint(item, "input") for item in shown_destinations],
            "truncation": limit_module.truncation(
                len(shown_sources) < len(sources)
                or len(shown_destinations) < len(destinations),
                reason="the net has {} sources and {} destinations; at most {} of each "
                "are listed".format(len(sources), len(destinations), budget),
                limit=budget,
                kind="endpoints",
            ),
        }
    )
    return {"net": detail}


def cone(
    netlist,
    seed_gate_ids,
    direction="both",
    depth=limit_module.DEFAULT_CONE_DEPTH,
    max_gates=limit_module.DEFAULT_CONE_GATES,
    include_edges=True,
    max_edges=limit_module.MAX_CONE_EDGES,
):
    """The bounded cone around ``seed_gate_ids``.

    This is the operation the whole API exists for: an investigation is about a
    handful of gates and what reaches them, not about a netlist dump.

    Bounded breadth-first, hop by hop, deterministic (frontier and neighbours
    are visited in id order), and it *stops at the budget instead of raising*:
    ``hal_viz.extract.collect_neighborhood`` raises ``ScopeTooLarge`` when a
    scope exceeds its node limit, which is right for a renderer -- a half-drawn
    graph is a lie -- and wrong here, where a caller needs the part of the cone
    that fits plus an explicit statement that there is more.  ``truncation``
    carries which limit cut it, and ``counts.frontier`` how many gates were
    reachable but not expanded.

    A seed id that does not exist is ``unknown_object``: an agent asking about
    gate 9999 must not receive a plausible-looking cone of the gates that do
    exist.
    """
    if direction not in DIRECTIONS:
        raise ValueError("unknown direction {!r}; expected one of {}".format(direction, DIRECTIONS))

    seeds = []
    seen = set()
    for raw_id in seed_gate_ids:
        gate_object = netlist.get_gate_by_id(int(raw_id))
        if gate_object is None:
            raise UnknownObject(
                "no gate with id {} in this netlist".format(raw_id),
                hint="every seed of a cone must exist; find gates with netlist.gates",
                data={"kind": "gate", "id": int(raw_id)},
            )
        if gate_object.get_id() not in seen:
            seen.add(gate_object.get_id())
            seeds.append(gate_object)

    budget = max(1, int(max_gates))
    if len(seeds) > budget:
        raise LimitExceeded(
            "{} distinct seed gates exceed max_gates={}".format(len(seeds), budget),
            hint="raise max_gates or ask for fewer seeds; a cone that cannot hold its "
            "own seeds would not be a cone",
            data={"seeds": len(seeds), "max_gates": budget},
        )

    collected = {item.get_id(): item for item in seeds}
    frontier = list(seeds)
    truncated = False
    reason = None
    limit_hit = None
    kind = None
    frontier_left = 0
    reached_depth = 0

    for hop in range(max(0, int(depth))):
        if not frontier:
            break
        next_frontier = []
        for gate_object in sorted(frontier, key=lambda item: item.get_id()):
            neighbours = []
            if direction in ("both", "fan_out"):
                neighbours.extend(_call(gate_object, "get_unique_successors", []) or [])
            if direction in ("both", "fan_in"):
                neighbours.extend(_call(gate_object, "get_unique_predecessors", []) or [])
            for neighbour in sorted(neighbours, key=lambda item: item.get_id()):
                key = neighbour.get_id()
                if key in collected:
                    continue
                if len(collected) >= budget:
                    truncated = True
                    frontier_left += 1
                    continue
                collected[key] = neighbour
                next_frontier.append(neighbour)
        if truncated:
            reason = (
                "the cone reached the {}-gate budget after {} hop(s); {} further gate(s) "
                "were adjacent to it and were not expanded".format(
                    budget, hop + 1, frontier_left
                )
            )
            limit_hit = budget
            kind = "gates"
            reached_depth = hop + 1
            break
        reached_depth = hop + 1 if next_frontier else reached_depth
        frontier = next_frontier

    if not truncated and frontier:
        # The depth ran out while the cone was still growing; say so, because
        # "no more gates" and "no more hops" are different answers.
        remaining = 0
        for gate_object in frontier:
            neighbours = []
            if direction in ("both", "fan_out"):
                neighbours.extend(_call(gate_object, "get_unique_successors", []) or [])
            if direction in ("both", "fan_in"):
                neighbours.extend(_call(gate_object, "get_unique_predecessors", []) or [])
            remaining += sum(1 for item in neighbours if item.get_id() not in collected)
        if remaining:
            truncated = True
            frontier_left = remaining
            reason = "the cone stopped at depth {}; {} adjacent gate(s) were not " "visited".format(
                int(depth), remaining
            )
            limit_hit = int(depth)
            kind = "depth"

    in_scope = collected
    edges = []
    edges_truncated = False
    if include_edges:
        for gate_object in sorted(in_scope.values(), key=lambda item: item.get_id()):
            for endpoint_object in _call(gate_object, "get_fan_out_endpoints", []) or []:
                net_object = _call(endpoint_object, "get_net")
                if net_object is None:
                    continue
                for destination in _call(net_object, "get_destinations", []) or []:
                    target = _call(destination, "get_gate")
                    if target is None or target.get_id() not in in_scope:
                        continue
                    if len(edges) >= max_edges:
                        edges_truncated = True
                        break
                    edges.append(
                        {
                            "from_gate_id": gate_object.get_id(),
                            "to_gate_id": target.get_id(),
                            "net_id": net_object.get_id(),
                            "net_name": _name(net_object),
                            "from_pin": _name(_call(endpoint_object, "get_pin"), None) or None,
                            "to_pin": _name(_call(destination, "get_pin"), None) or None,
                        }
                    )
                if edges_truncated:
                    break
            if edges_truncated:
                break
        edges.sort(key=lambda item: (item["from_gate_id"], item["to_gate_id"], item["net_id"]))

    if edges_truncated and not truncated:
        truncated = True
        reason = "the cone has more than {} edges; the listing stops there".format(max_edges)
        limit_hit = max_edges
        kind = "edges"

    result = {
        "seed_gate_ids": [item.get_id() for item in seeds],
        "direction": direction,
        "depth": int(depth),
        "reached_depth": reached_depth,
        "gates": [
            _gate_ref(item, with_properties=True)
            for item in sorted(in_scope.values(), key=lambda entry: entry.get_id())
        ],
        "counts": {"gates": len(in_scope), "edges": len(edges), "frontier": frontier_left},
        "truncation": limit_module.truncation(truncated, reason=reason, limit=limit_hit, kind=kind),
    }
    if include_edges:
        result["edges"] = edges
    return result
