"""Contributions + inventory -> the recovered-block model.

This module is where the honesty rules of the whole tool are actually enforced,
and each one is a decision that could easily have gone the other way:

* **Identical gate sets become one block, not two.**  When DANA groups four
  flip-flops and hal_fsm recovers a transition relation for the same four, that
  is one block carrying two claims of two different strengths -- not two blocks
  that a reader has to notice are the same thing.
* **Different-but-overlapping gate sets stay separate, and the overlap is
  recorded.**  Two analyses disagreeing about where a boundary is *is* the
  result; resolving it silently would delete it.  One of them owns the gate for
  connectivity purposes (the stronger confidence wins, then the lower block id),
  and ``overlaps`` says so.
* **Every unclaimed gate ends up in an unknown region.**  :func:`compose` asserts
  the arithmetic: blocks + unknown regions = every gate in the netlist.
  ``model.coverage`` raises if it ever fails to add up.
* **Region clustering never merges through a shared control net.**  Grouping
  unclaimed gates by "shares a net" would put every flip-flop in one region via
  the clock.  Clock pins and constant nets are excluded, and a net with more
  loads than ``max_net_loads`` is excluded and *reported*, because a hidden
  clustering threshold is a magic number.
"""

import os

from . import model
from .adapters import common as adapter_common

__all__ = [
    "ComposeError",
    "ComposeOptions",
    "PORT_INPUTS",
    "PORT_OUTPUTS",
    "compose",
]

#: Diagram node ids for the design boundary.
PORT_INPUTS = "port/inputs"
PORT_OUTPUTS = "port/outputs"

_KIND_ORDER = {kind: index for index, kind in enumerate(model.BLOCK_KINDS)}


class ComposeError(ValueError):
    """The block model could not be composed."""


class ComposeOptions(object):
    """Every threshold the composition uses, in one place and all reported."""

    def __init__(
        self,
        max_net_loads=8,
        max_edge_nets=8,
        max_block_ports=32,
        max_port_nets=64,
    ):
        #: nets with more loads than this do not cluster unknown gates together
        self.max_net_loads = int(max_net_loads)
        #: how many net references an edge lists before it is marked truncated
        self.max_edge_nets = int(max_edge_nets)
        #: how many nets a block's port list carries
        self.max_block_ports = int(max_block_ports)
        #: how many nets a boundary port node carries
        self.max_port_nets = int(max_port_nets)

    def to_json(self):
        return {
            "max_net_loads": self.max_net_loads,
            "max_edge_nets": self.max_edge_nets,
            "max_block_ports": self.max_block_ports,
            "max_port_nets": self.max_port_nets,
        }


# ---------------------------------------------------------------------------
# merging
# ---------------------------------------------------------------------------


def _merge_identical(contributions):
    """Fold contributions with an identical gate set into one."""
    merged = {}
    order = []
    for contribution in contributions:
        key = tuple(contribution.gate_ids)
        if key in merged:
            merged[key].merge(contribution)
        else:
            merged[key] = contribution
            order.append(key)
    return [merged[key] for key in order]


def _merge_kind(existing, incoming):
    """Keep the more specific kind when two analyses claim the same gate set.

    A gate set that is both DANA's ``register`` and hal_fsm's ``state_machine``
    is a state machine, and a verified ``counter`` beats a plain ``register``.
    """
    priority = {
        "other": 0,
        "register": 1,
        "datapath": 2,
        "multiplexer": 3,
        "comparator": 4,
        "arithmetic": 5,
        "counter": 6,
        "state_machine": 7,
    }
    return incoming if priority.get(incoming, 0) > priority.get(existing, 0) else existing


# ---------------------------------------------------------------------------
# composition
# ---------------------------------------------------------------------------


def _block_id(kind, index):
    return "block/{}/{:04d}".format(kind, index)


def _assign_blocks(contributions, inventory):
    """Turn merged contributions into finished block dicts, deterministically."""
    ordered = sorted(
        contributions,
        key=lambda entry: (
            _KIND_ORDER.get(entry.kind, len(_KIND_ORDER)),
            entry.gate_ids[0] if entry.gate_ids else 0,
            entry.key,
        ),
    )
    counters = {}
    blocks = []
    for contribution in ordered:
        kind = contribution.kind
        counters[kind] = counters.get(kind, 0) + 1
        block_id = _block_id(kind, counters[kind])
        blocks.append(
            {
                "contribution": contribution,
                "block_id": block_id,
                "kind": kind,
                "label": contribution.label,
                "gate_ids": list(contribution.gate_ids),
            }
        )
    return blocks


def _owner_map(blocks):
    """gate id -> owning block id, plus the overlaps that had to be broken."""
    claims = {}
    for entry in blocks:
        for gate_id in entry["gate_ids"]:
            claims.setdefault(gate_id, []).append(entry)

    owners = {}
    overlaps = []
    for gate_id in sorted(claims):
        candidates = claims[gate_id]
        best = sorted(
            candidates,
            key=lambda entry: (
                -model.CONFIDENCE_RANK.get(entry["confidence"], 0),
                entry["block_id"],
            ),
        )[0]
        owners[gate_id] = best["block_id"]
        if len(candidates) > 1:
            overlaps.append(
                (gate_id, sorted(entry["block_id"] for entry in candidates), best["block_id"])
            )
    return owners, overlaps


def _region_components(inventory, gate_ids, options, excluded_nets):
    """Union-find over unclaimed gates, connected by data nets only."""
    parent = {gate_id: gate_id for gate_id in gate_ids}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    members = set(gate_ids)
    for net_id in sorted(inventory.nets):
        if inventory.is_constant_net(net_id):
            continue
        endpoints = [gid for gid in inventory.net_drivers(net_id) if gid in members]
        loads = [
            gid
            for gid, pin in inventory.net_loads(net_id)
            if gid in members and not inventory.is_control_pin(gid, pin)
        ]
        endpoints.extend(loads)
        endpoints = sorted(set(endpoints))
        if len(endpoints) < 2:
            continue
        total_loads = len(inventory.net_loads(net_id))
        if total_loads > options.max_net_loads:
            excluded_nets.append((net_id, total_loads))
            continue
        for other in endpoints[1:]:
            union(endpoints[0], other)

    groups = {}
    for gate_id in sorted(gate_ids):
        groups.setdefault(find(gate_id), []).append(gate_id)
    return [groups[key] for key in sorted(groups, key=lambda key: groups[key][0])]


def _block_ports(inventory, gate_ids, options):
    """The nets crossing this block's boundary, split into in/out/control."""
    members = set(gate_ids)
    inputs, outputs, control = {}, {}, {}

    for gate_id in sorted(members):
        for pin, net_id in inventory.input_nets(gate_id):
            if inventory.is_constant_net(net_id):
                continue
            drivers = inventory.net_drivers(net_id)
            external = any(driver not in members for driver in drivers) or not drivers
            net = inventory.net(net_id) or {}
            if net.get("is_global_input"):
                external = True
            if not external:
                continue
            if inventory.is_control_pin(gate_id, pin):
                control.setdefault(net_id, pin)
            else:
                inputs.setdefault(net_id, pin)
        for net_id in inventory.output_nets(gate_id):
            if inventory.is_constant_net(net_id):
                continue
            net = inventory.net(net_id) or {}
            loads = inventory.net_loads(net_id)
            if net.get("is_global_output") or any(gid not in members for gid, _pin in loads):
                outputs.setdefault(net_id, None)

    def refs(mapping, role):
        keys = sorted(mapping)[: options.max_block_ports]
        return [inventory.net_ref(net_id, role=role) for net_id in keys]

    ports = {}
    if inputs:
        ports["inputs"] = refs(inputs, "data_in")
    if outputs:
        ports["outputs"] = refs(outputs, "data_out")
    if control:
        ports["control"] = refs(control, "control")
    return ports or None


def _edges(inventory, owners, options):
    """Structural edges between owning nodes, aggregated per node pair."""
    aggregated = {}
    for net_id in sorted(inventory.nets):
        if inventory.is_constant_net(net_id):
            continue
        net = inventory.net(net_id) or {}
        drivers = [owners.get(gid) for gid in inventory.net_drivers(net_id)]
        drivers = [owner for owner in drivers if owner]
        if net.get("is_global_input"):
            drivers.append(PORT_INPUTS)
        loads = []
        for gid, pin in inventory.net_loads(net_id):
            owner = owners.get(gid)
            if owner:
                loads.append((owner, inventory.is_control_pin(gid, pin)))
        if net.get("is_global_output"):
            loads.append((PORT_OUTPUTS, False))
        for source in sorted(set(drivers)):
            for target, is_control in sorted(set(loads)):
                if source == target:
                    continue
                key = (source, target)
                entry = aggregated.setdefault(key, {"nets": set(), "control": True})
                entry["nets"].add(net_id)
                if not is_control:
                    entry["control"] = False

    edges = []
    for (source, target), entry in sorted(aggregated.items()):
        net_ids = sorted(entry["nets"])
        shown = net_ids[: options.max_edge_nets]
        edges.append(
            model.edge(
                source,
                target,
                len(net_ids),
                nets=[inventory.net_ref(net_id) for net_id in shown],
                kind="control" if entry["control"] else "data",
                truncated=True if len(shown) < len(net_ids) else None,
            )
        )
    return edges


def compose(
    inventory,
    sources,
    producer=None,
    options=None,
    generated_at=None,
    extra_notes=(),
):
    """Compose a recovered-block document.

    :param inventory: the :class:`hal_explain.inventory.Inventory` every claim is
        resolved against.
    :param sources: an iterable of ``(adapter_name, adapter_module,
        SourceDocument)`` triples, in the order the user gave them.
    :returns: a recovered-block document (validate it with
        :func:`hal_explain.validate.validate_document`).
    """
    from . import __version__

    options = options or ComposeOptions()
    notes = list(extra_notes)
    contributions = []
    source_entries = []

    for adapter_name, adapter_module, source in sources:
        produced, adapter_notes = adapter_module.contributions(source, inventory)
        contributions.extend(produced)
        notes.extend(adapter_notes)
        source_entries.append(source.to_source(adapter_name))
        if source.unresolved:
            notes.append(
                "{} gate reference(s) in source {!r} could not be resolved against this "
                "netlist; the blocks built from it are incomplete (see "
                "sources[].unresolved_gates)".format(len(source.unresolved), source.source_id)
            )

    if not contributions:
        notes.append(
            "no analysis claimed any gate of this netlist: the model is one big "
            "unknown region, which is a true statement about what is known"
        )

    # -- merge identical gate sets, keeping the more specific kind ----------
    merged = _merge_identical(contributions)
    by_gates = {}
    for contribution in contributions:
        by_gates.setdefault(tuple(contribution.gate_ids), []).append(contribution)
    for contribution in merged:
        group = by_gates[tuple(contribution.gate_ids)]
        kind = contribution.kind
        for other in group[1:]:
            kind = _merge_kind(kind, other.kind)
        if kind != contribution.kind:
            # Take the label from the contribution that supplied the winning kind,
            # before overwriting the kind -- otherwise the block keeps the weaker
            # analysis's wording ("candidate register") for a state machine.
            label = next(
                (other.label for other in group if other.kind == kind), contribution.label
            )
            contribution.kind = kind
            contribution.label = label
        if len(group) > 1:
            contribution.notes.append(
                "the same gate set was claimed by {} analyses; each claim is listed "
                "separately below".format(len(group))
            )

    # -- lay out the blocks -------------------------------------------------
    staged = _assign_blocks(merged, inventory)
    for entry in staged:
        claims = entry["contribution"].claims
        entry["confidence"] = model.strongest_confidence(
            claim["confidence"] for claim in claims
        )

    owners, overlap_records = _owner_map(staged)

    blocks = []
    for entry in staged:
        contribution = entry["contribution"]
        gate_refs = [inventory.gate_ref(gate_id) for gate_id in entry["gate_ids"]]
        blocks.append(
            model.block(
                entry["block_id"],
                entry["kind"],
                entry["label"],
                gate_refs,
                contribution.claims,
                ports=_block_ports(inventory, entry["gate_ids"], options),
                attributes=dict(contribution.attributes) or None,
                notes=contribution.notes or None,
            )
        )

    # -- unknown regions: everything nobody claimed -------------------------
    claimed = set(owners)
    unclaimed = [gid for gid in inventory.gate_ids() if gid not in claimed]
    excluded_nets = []
    regions = []
    for index, component in enumerate(
        _region_components(inventory, unclaimed, options, excluded_nets), start=1
    ):
        region_id = "unknown/{:04d}".format(index)
        histogram = inventory.gate_type_histogram(component)
        sequential = sum(1 for gate_id in component if inventory.is_sequential(gate_id))
        regions.append(
            model.unknown_region(
                region_id,
                "unclassified region [{} gates]".format(len(component)),
                "no analysis in this model claimed these gates; they are shown "
                "because a block diagram that hides them would misrepresent what "
                "was recovered",
                [inventory.gate_ref(gate_id) for gate_id in component],
                gate_types=histogram,
                sequential_gate_count=sequential,
            )
        )
        for gate_id in component:
            owners[gate_id] = region_id

    if excluded_nets:
        worst = sorted(excluded_nets, key=lambda item: -item[1])[:3]
        notes.append(
            "{} net(s) with more than {} loads were excluded from unknown-region "
            "clustering, so gates that share only such a net are separate regions "
            "(largest: {}). Raise --max-net-loads to cluster through them.".format(
                len(excluded_nets),
                options.max_net_loads,
                ", ".join(
                    "{} ({} loads)".format(
                        (inventory.net(net_id) or {}).get("name", net_id), count
                    )
                    for net_id, count in worst
                ),
            )
        )

    # -- boundary ports and edges ------------------------------------------
    input_nets, output_nets = inventory.boundary_nets()
    ports = []
    if input_nets:
        shown = input_nets[: options.max_port_nets]
        ports.append(
            model.port(
                PORT_INPUTS,
                "input",
                [inventory.net_ref(net_id) for net_id in shown],
                label="primary inputs ({})".format(len(input_nets)),
            )
        )
        if len(shown) < len(input_nets):
            notes.append(
                "the primary-input port lists {} of {} nets".format(
                    len(shown), len(input_nets)
                )
            )
    if output_nets:
        shown = output_nets[: options.max_port_nets]
        ports.append(
            model.port(
                PORT_OUTPUTS,
                "output",
                [inventory.net_ref(net_id) for net_id in shown],
                label="primary outputs ({})".format(len(output_nets)),
            )
        )
        if len(shown) < len(output_nets):
            notes.append(
                "the primary-output port lists {} of {} nets".format(
                    len(shown), len(output_nets)
                )
            )

    edges = _edges(inventory, owners, options)

    overlaps = [
        model.overlap(inventory.gate_ref(gate_id), block_ids, owner)
        for gate_id, block_ids, owner in overlap_records
    ]
    if overlaps:
        notes.append(
            "{} gate(s) are claimed by more than one block; each block still lists "
            "them, and 'overlaps' records which block owns them in the "
            "diagram".format(len(overlaps))
        )

    # -- coverage -----------------------------------------------------------
    gates_by_confidence = {}
    for entry in staged:
        for gate_id in entry["gate_ids"]:
            current = gates_by_confidence.get(gate_id)
            if current is None or model.CONFIDENCE_RANK.get(
                entry["confidence"], 0
            ) > model.CONFIDENCE_RANK.get(current, 0):
                gates_by_confidence[gate_id] = entry["confidence"]

    sequential_total = len(inventory.sequential_gate_ids())
    sequential_unclassified = sum(
        1 for gate_id in unclaimed if inventory.is_sequential(gate_id)
    )
    coverage = model.coverage(
        len(inventory.gates),
        len(claimed),
        len(unclaimed),
        gates_verified=sum(
            1
            for value in gates_by_confidence.values()
            if value == model.CONFIDENCE_VERIFIED
        ),
        gates_heuristic=sum(
            1
            for value in gates_by_confidence.values()
            if value == model.CONFIDENCE_HEURISTIC
        ),
        gates_unknown_confidence=sum(
            1
            for value in gates_by_confidence.values()
            if value in (model.CONFIDENCE_UNKNOWN, model.CONFIDENCE_REFUTED)
        ),
        gates_overlapping=len(overlaps),
        sequential_gates_total=sequential_total,
        sequential_gates_unclassified=sequential_unclassified,
        block_count=len(blocks),
        unknown_region_count=len(regions),
        claim_count=sum(len(block["claims"]) for block in blocks),
    )

    if sequential_unclassified:
        notes.append(
            "{} of {} sequential gate(s) are in no block: no analysis in this model "
            "explains what they hold".format(sequential_unclassified, sequential_total)
        )

    notes.append(
        "composition thresholds: {}".format(
            ", ".join(
                "{}={}".format(key, value)
                for key, value in sorted(options.to_json().items())
            )
        )
    )

    producer = producer or {"name": "hal_explain.compose", "version": __version__}
    return model.document(
        producer,
        inventory.design,
        source_entries,
        blocks,
        regions,
        coverage,
        ports=ports,
        edges=edges,
        overlaps=overlaps,
        generated_at=generated_at,
        notes=notes,
    )


def load_sources(paths, adapter_names=None, validate=True):
    """Load ``(adapter_name, module, SourceDocument)`` triples for ``paths``.

    ``adapter_names`` is a parallel list; ``None`` in a slot means auto-detect
    from ``analysis.plugin.name``.  Source IDs come from the file's base name and
    are made unique by suffixing, never by silently overwriting.
    """
    from .adapters import select_adapter

    adapter_names = list(adapter_names or [None] * len(paths))
    if len(adapter_names) != len(paths):
        raise ComposeError(
            "got {} --adapter values for {} findings documents; pass one per "
            "document or none at all".format(len(adapter_names), len(paths))
        )

    seen = {}
    triples = []
    for path, requested in zip(paths, adapter_names):
        base = os.path.splitext(os.path.basename(str(path)))[0]
        seen[base] = seen.get(base, 0) + 1
        source_id = base if seen[base] == 1 else "{}~{}".format(base, seen[base])
        source = adapter_common.load_source(path, source_id=source_id, validate=validate)
        try:
            name, module = select_adapter(source.document, requested)
        except KeyError as exc:
            raise ComposeError(str(exc))
        triples.append((name, module, source))
    return triples
