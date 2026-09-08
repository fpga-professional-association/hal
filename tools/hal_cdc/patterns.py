"""Conservative classification of a crossing against known-good structures.

Only one structure is *recognised* here: the two-flop synchroniser, and only in
its unambiguous form -- the crossing signal is captured directly (through
buffers and inverters at most) by a flip-flop in the destination domain, whose
output goes to exactly one load, which is the data input of a second flip-flop
in the same domain.

Every deviation is reported rather than tolerated, because the whole value of
this pass is that "recognised" means something.  A first stage whose output
fans out to two loads is not a synchroniser: the two loads can sample different
values of the same metastable event.  Combinational logic in front of the first
stage is not a synchroniser either.
"""

__all__ = [
    "CLASS_TWO_FLOP",
    "CLASS_MULTI_FLOP",
    "CLASS_UNSYNCHRONIZED",
    "CLASS_UNSYNCHRONIZED_CONTROL",
    "CLASS_UNSUPPORTED_DESTINATION",
    "CLASS_WAIVED",
    "RECOGNIZED_CLASSES",
    "Classification",
    "classify_crossing",
    "follow_single_load",
]

CLASS_TWO_FLOP = "two_flop_synchronizer"
CLASS_MULTI_FLOP = "multi_flop_synchronizer"
CLASS_UNSYNCHRONIZED = "unsynchronized"
CLASS_UNSYNCHRONIZED_CONTROL = "unsynchronized_control_path"
CLASS_UNSUPPORTED_DESTINATION = "unsupported_destination"
CLASS_WAIVED = "waived"

#: Classifications that describe a structure this pass accepts.
RECOGNIZED_CLASSES = (CLASS_TWO_FLOP, CLASS_MULTI_FLOP)


class Classification(object):
    """The verdict on one crossing."""

    __slots__ = ("name", "reason", "stage_gate_ids", "stages", "caveats")

    def __init__(self, name, reason, stage_gate_ids=(), stages=0, caveats=()):
        self.name = name
        self.reason = reason
        self.stage_gate_ids = tuple(stage_gate_ids)
        self.stages = int(stages)
        self.caveats = tuple(caveats)

    @property
    def is_recognized(self):
        return self.name in RECOGNIZED_CLASSES

    def to_json(self, view):
        data = {"classification": self.name, "reason": self.reason}
        if self.stage_gate_ids:
            data["synchronizer_stages"] = [
                view.gate(gate_id).name
                for gate_id in self.stage_gate_ids
                if view.gate(gate_id) is not None
            ]
            data["stage_count"] = self.stages
        if self.caveats:
            data["caveats"] = list(self.caveats)
        return data

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Classification({!r}, stages={})".format(self.name, self.stages)


def follow_single_load(view, net_id, limits):
    """Follow a net forward while it has exactly one load, through buffers only.

    :returns: ``(gate, pin_name, transparent_gate_ids, reason)``.  ``gate`` is
        ``None`` when the chain cannot be followed, and ``reason`` then says why.
    """
    chain = []
    current = net_id
    seen = set()
    budget = limits.max_path_gates

    while True:
        if budget <= 0:
            return None, None, chain, "buffer chain longer than {} gates".format(
                limits.max_path_gates
            )
        budget -= 1
        if current in seen:
            return None, None, chain, "combinational loop on the fan-out path"
        seen.add(current)

        net = view.net(current)
        if net is None:
            return None, None, chain, "net {} is not in the netlist".format(current)
        loads = list(net.destinations)
        if net.is_global_output:
            return None, None, chain, "net {!r} also leaves the design as a primary output".format(
                net.name
            )
        if len(loads) != 1:
            return None, None, chain, (
                "net {!r} drives {} loads; a synchroniser's first stage must drive exactly "
                "one".format(net.name, len(loads))
            )

        gate_id, pin_name = loads[0]
        gate = view.gate(gate_id)
        if gate is None:
            return None, None, chain, "net {!r} has an unresolvable load".format(net.name)
        if not gate.type.is_clock_transparent:
            return gate, pin_name, chain, None

        outputs = gate.output_nets()
        if len(outputs) != 1:
            return None, None, chain, "{!r} has {} output nets".format(gate.name, len(outputs))
        chain.append(gate_id)
        current = outputs[0]


def _next_stage(view, gate, domain, clock_resolutions, limits):
    """The flip-flop that a synchroniser stage feeds, if there is exactly one."""
    outputs = gate.output_nets()
    if len(outputs) != 1:
        return None, "{!r} drives {} output nets; a synchroniser stage must drive one".format(
            gate.name, len(outputs)
        )
    load, pin_name, _chain, reason = follow_single_load(view, outputs[0], limits)
    if load is None:
        return None, reason
    pin = load.type.pin(pin_name)
    if pin is None:
        return None, "load pin {!r} of {!r} is unknown".format(pin_name, load.name)
    if not load.type.is_ff:
        return None, (
            "the single load of {!r} is {!r} ({}), not a flip-flop".format(
                gate.name, load.name, load.type.name
            )
        )
    if pin.type == "clock":
        return None, "{!r} drives the clock pin of {!r}".format(gate.name, load.name)
    if pin.type not in ("data", "none"):
        return None, (
            "{!r} drives the {!r} ({}) pin of {!r}, not its data pin".format(
                gate.name, pin.name, pin.type, load.name
            )
        )
    resolution = clock_resolutions.get(load.id)
    load_domain = resolution.domain if resolution is not None else None
    if load_domain != domain:
        return None, (
            "the next stage {!r} is clocked by {} rather than {}".format(
                load.name, load_domain if load_domain else "an unresolved clock", domain
            )
        )
    return load, None


def _control_caveats(view, gate, domain, net_domains):
    """Note control pins of a synchroniser stage that are not domain-clean."""
    caveats = []
    for pin in gate.type.input_pins():
        if pin.type not in ("enable", "set", "reset", "select", "control"):
            continue
        net_id = gate.fan_in.get(pin.name)
        if net_id is None:
            continue
        if view.is_constant_net(net_id):
            continue
        domains = net_domains.get(net_id)
        if domains is None:
            continue
        foreign = sorted(domains.known - {domain})
        if foreign or domains.unknown:
            caveats.append(
                "the {!r} pin of {!r} is driven by {}; the synchroniser only covers the "
                "data path".format(
                    pin.name,
                    gate.name,
                    ", ".join(foreign + sorted(domains.unknown)) or "an unknown source",
                )
            )
    return caveats


def classify_crossing(view, crossing, clock_resolutions, net_domains, limits):
    """Classify one :class:`~hal_cdc.crossings.Crossing`."""
    from .crossings import ROLE_CONTROL

    destination = view.gate(crossing.destination_gate_id)
    if destination is None:
        return Classification(
            CLASS_UNSYNCHRONIZED, "the capturing gate is not in the netlist"
        )

    if destination.type.is_latch:
        return Classification(
            CLASS_UNSUPPORTED_DESTINATION,
            "the crossing is captured by the latch {!r} ({}); hal_cdc does not model "
            "level-sensitive capture and makes no claim about it".format(
                destination.name, destination.type.name
            ),
        )
    if not destination.type.is_ff:
        return Classification(
            CLASS_UNSUPPORTED_DESTINATION,
            "the crossing is captured by {!r} ({}), a sequential primitive that is neither "
            "a flip-flop nor a latch; its capture semantics are not modelled".format(
                destination.name, destination.type.name
            ),
        )

    if crossing.role == ROLE_CONTROL:
        return Classification(
            CLASS_UNSYNCHRONIZED_CONTROL,
            "the crossing reaches the {!r} ({}) pin of {!r}. A control pin is never the "
            "first stage of a synchroniser, so this path enters {} unsynchronised".format(
                crossing.destination_pin.name,
                crossing.destination_pin.type,
                destination.name,
                crossing.destination_domain,
            ),
        )

    if not crossing.direct:
        gates = ", ".join(
            sorted(
                view.gate(gid).name
                for gid in crossing.path_gate_ids
                if view.gate(gid) is not None and not view.gate(gid).type.is_clock_transparent
            )
        )
        return Classification(
            CLASS_UNSYNCHRONIZED,
            "combinational logic ({}) lies between domain {} and the capturing register "
            "{!r}; the first synchronising stage must be fed directly".format(
                gates or "unnamed gates", crossing.source_domain, destination.name
            ),
        )

    if crossing.mixed:
        mixed = sorted(net_domains.get(crossing.net_id).known) if crossing.net_id in net_domains else []
        return Classification(
            CLASS_UNSYNCHRONIZED,
            "the data input of {!r} is fed by more than one source ({}); a synchroniser "
            "stage must see exactly one source domain".format(
                destination.name,
                ", ".join(mixed + list(crossing.unknown_sources)) or "several sources",
            ),
        )

    stages = [destination.id]
    stage_gate = destination
    reason = None
    while len(stages) < 8:
        nxt, reason = _next_stage(
            view, stage_gate, crossing.destination_domain, clock_resolutions, limits
        )
        if nxt is None:
            break
        stages.append(nxt.id)
        stage_gate = nxt

    if len(stages) < 2:
        return Classification(
            CLASS_UNSYNCHRONIZED,
            "{!r} captures the crossing but is not followed by a second synchronising "
            "stage: {}".format(destination.name, reason or "no second flip-flop found"),
        )

    caveats = []
    for gate_id in stages:
        gate = view.gate(gate_id)
        if gate is not None:
            caveats.extend(
                _control_caveats(view, gate, crossing.destination_domain, net_domains)
            )

    name = CLASS_TWO_FLOP if len(stages) == 2 else CLASS_MULTI_FLOP
    return Classification(
        name,
        "the crossing is captured directly by {!r} and re-registered by {} in domain {}; "
        "this matches the {}-stage synchroniser pattern. Structural recognition only: no "
        "MTBF, settling-time or physical-timing claim is made".format(
            destination.name,
            ", ".join(view.gate(gid).name for gid in stages[1:] if view.gate(gid)),
            crossing.destination_domain,
            len(stages),
        ),
        stage_gate_ids=stages,
        stages=len(stages),
        caveats=caveats,
    )
