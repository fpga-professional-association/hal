"""Reading the netlist: the first of two modules that touch ``hal_py``.

Every accessor goes through :func:`hal_findings.adapters.common.call`, so this
module works against the real bindings and against the stub objects the unit
tests build.  That is not defensive programming for its own sake: it is what
lets the candidate proposal, the reset classification and the cone check be
tested without a HAL build, which is the only way they get tested on a Windows
checkout at all.

What is extracted, and why each piece is needed:

``depends``
    the flip-flop dependency graph -- the input to candidate proposal.

``cones``
    the combinational gates between the state flip-flops.  ``solve_fsm`` takes
    this as its ``transition_logic`` argument and treats *any* net it cannot
    expand as a free variable, so a cone with a hole in it silently produces
    wrong conditions.  Building it here, by backward traversal, is what makes
    the hole check in :mod:`hal_fsm.solve` possible.

``control``
    the asynchronous set/reset (and enable) pins of every flip-flop, resolved to
    the net that drives them and classified as tied-constant or driven.
    ``solve_fsm`` models only the gate library's ``next_state`` function, so an
    asynchronous clear driven by real logic is invisible in its output.  That is
    an assumption of every recovered transition, and it can only be discharged
    -- or reported -- if the pins are looked at.

``init_value``
    the flip-flop's ``INIT``-style attribute, which is where the initial state
    comes from when the design records one.
"""

import os
import sys

if __package__ in (None, ""):  # pragma: no cover - direct execution
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_findings.adapters.common import call, gate_type_properties

from .candidates import SequentialGate, SequentialGraph

__all__ = ["Extraction", "enum_name", "parse_init_value", "extract", "resolve_gates"]

#: Pin types that carry an asynchronous or gating control signal.
CONTROL_PIN_TYPES = ("set", "reset", "enable", "control")
#: Pin types whose fan-in is *not* part of the next-state cone.
NON_CONE_PIN_TYPES = ("clock",)


def enum_name(value):
    """Name of a pybind enum value (``PinType.reset`` -> ``'reset'``)."""
    if value is None:
        return None
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value).rsplit(".", 1)[-1]


def parse_init_value(raw):
    """Parse an ``INIT`` attribute value into ``0``/``1``, or ``None``.

    Verilog writes it in half a dozen notations and a wrong guess here would
    silently move the initial state, so anything unrecognised becomes ``None``
    and the initial-state assumption stays undischarged.
    """
    if raw is None:
        return None
    if isinstance(raw, (tuple, list)):
        raw = raw[-1] if raw else None
    if raw is None:
        return None
    text = str(raw).strip().strip('"').lower()
    if not text:
        return None
    if "'" in text:  # 1'h0, 1'b1, 4'hA ...
        text = text.split("'", 1)[1]
        if text[:1] in ("h", "b", "d", "o"):
            base = {"h": 16, "b": 2, "d": 10, "o": 8}[text[0]]
            digits = text[1:]
        else:
            base, digits = 2, text
        try:
            value = int(digits, base)
        except ValueError:
            return None
    elif text.startswith("0x"):
        try:
            value = int(text, 16)
        except ValueError:
            return None
    elif text.startswith("0b"):
        try:
            value = int(text, 2)
        except ValueError:
            return None
    else:
        try:
            value = int(text, 10)
        except ValueError:
            return None
    if value in (0, 1):
        return value
    # A multi-bit INIT on a single flip-flop is not something to interpret.
    return None


class Extraction(object):
    """The netlist, reduced to what the rest of hal_fsm needs."""

    def __init__(self, graph, nets, gates_by_id, sequential_ids, combinational_ids, notes):
        self.graph = graph
        #: ``{net_id: {"name", "source_gate", "source_kind"}}``
        self.nets = nets
        self.gates_by_id = gates_by_id
        self.sequential_ids = sequential_ids
        self.combinational_ids = combinational_ids
        self.notes = list(notes)

    def gate(self, gate_id):
        return self.gates_by_id.get(int(gate_id))

    def net_name(self, net_id):
        entry = self.nets.get(int(net_id))
        return entry["name"] if entry else "net_{}".format(net_id)

    def net_role(self, net_id, member_ids=()):
        """``input`` | ``external_state`` | ``internal`` -- for witness reporting."""
        entry = self.nets.get(int(net_id))
        if entry is None:
            return "unknown"
        source = entry.get("source_gate")
        if source is None:
            return "input"
        if entry.get("source_kind") == "sequential":
            return "external_state" if source not in set(member_ids) else "state"
        return "internal"


def _pin_records(gate):
    """``[{'name','direction','type'}]`` for a gate's type, or ``[]``."""
    gate_type = call(gate, "get_type")
    if gate_type is None:
        return []
    pins = call(gate_type, "get_pins", default=None)
    if pins is None:
        return []
    records = []
    for pin in pins:
        records.append(
            {
                "name": call(pin, "get_name", default=""),
                "direction": enum_name(call(pin, "get_direction")),
                "type": enum_name(call(pin, "get_type")),
            }
        )
    return records


def _net_id(net):
    return call(net, "get_id")


def _index_nets(netlist, sequential_ids):
    nets = {}
    for net in call(netlist, "get_nets", default=[]) or []:
        net_id = _net_id(net)
        if net_id is None:
            continue
        source_gate = None
        source_kind = None
        for endpoint in call(net, "get_sources", default=[]) or []:
            gate = call(endpoint, "get_gate")
            gate_id = call(gate, "get_id")
            if gate_id is None:
                continue
            source_gate = gate_id
            source_kind = "sequential" if gate_id in sequential_ids else "combinational"
            break
        nets[int(net_id)] = {
            "name": call(net, "get_name", default=""),
            "source_gate": source_gate,
            "source_kind": source_kind,
        }
    return nets


def _backward_cone(netlist_nets, gates_by_id, sequential_ids, start_net_ids, max_gates=100000):
    """Walk backwards from ``start_net_ids`` through combinational gates only.

    Returns ``(cone_gate_ids, boundaries)`` where ``boundaries`` maps every net
    that enters the cone from outside to the sequential gate driving it (or
    ``None`` for a primary input / undriven net).
    """
    cone = set()
    boundaries = {}
    seen_nets = set()
    stack = [int(net_id) for net_id in start_net_ids if net_id is not None]
    while stack:
        net_id = stack.pop()
        if net_id in seen_nets:
            continue
        seen_nets.add(net_id)
        entry = netlist_nets.get(net_id)
        if entry is None:
            boundaries[net_id] = None
            continue
        source_gate = entry.get("source_gate")
        if source_gate is None:
            boundaries[net_id] = None
            continue
        if source_gate in sequential_ids:
            boundaries[net_id] = source_gate
            continue
        if source_gate in cone:
            continue
        if len(cone) >= max_gates:
            boundaries[net_id] = None
            continue
        cone.add(source_gate)
        gate = gates_by_id.get(source_gate)
        for fan_in in call(gate, "get_fan_in_nets", default=[]) or []:
            fan_in_id = _net_id(fan_in)
            if fan_in_id is not None:
                stack.append(int(fan_in_id))
    return cone, boundaries


def extract(netlist, init_category="generic", init_key="INIT"):
    """Build a :class:`~hal_fsm.candidates.SequentialGraph` from a netlist."""
    notes = []
    gates = call(netlist, "get_gates", default=[]) or []
    gates_by_id = {}
    sequential_ids = set()
    combinational_ids = set()

    for gate in gates:
        gate_id = call(gate, "get_id")
        if gate_id is None:
            continue
        gates_by_id[int(gate_id)] = gate
        properties = gate_type_properties(call(gate, "get_type"))
        if "sequential" in properties:
            sequential_ids.add(int(gate_id))
        else:
            combinational_ids.add(int(gate_id))

    nets = _index_nets(netlist, sequential_ids)
    graph = SequentialGraph()

    for gate_id in sorted(sequential_ids):
        gate = gates_by_id[gate_id]
        gate_type = call(gate, "get_type")
        properties = gate_type_properties(gate_type)
        pins = _pin_records(gate)

        data_pins = [
            pin for pin in pins if pin["direction"] == "input" and pin["type"] == "data"
        ]
        clock_nets = set()
        for pin in pins:
            if pin["direction"] == "input" and pin["type"] == "clock":
                net = call(gate, "get_fan_in_net", pin["name"])
                net_id = _net_id(net)
                if net_id is not None:
                    clock_nets.add(int(net_id))

        control = []
        for pin in pins:
            if pin["direction"] != "input" or pin["type"] not in CONTROL_PIN_TYPES:
                continue
            net = call(gate, "get_fan_in_net", pin["name"])
            net_id = _net_id(net)
            constant = None
            if net_id is not None:
                entry = nets.get(int(net_id), {})
                source = entry.get("source_gate")
                if source is not None:
                    source_properties = gate_type_properties(
                        call(gates_by_id.get(source), "get_type")
                    )
                    if "ground" in source_properties:
                        constant = 0
                    elif "power" in source_properties:
                        constant = 1
            control.append(
                {
                    "pin": pin["name"],
                    "pin_type": pin["type"],
                    "net_id": int(net_id) if net_id is not None else None,
                    "net_name": nets.get(int(net_id), {}).get("name") if net_id else None,
                    "constant": constant,
                }
            )

        init_raw = call(gate, "get_data", init_category, init_key)
        init_value = parse_init_value(init_raw)

        module_object = call(gate, "get_module")
        module = None
        if module_object is not None:
            module_id = call(module_object, "get_id")
            module_name = call(module_object, "get_name")
            module = {}
            if module_id is not None:
                module["id"] = module_id
            if module_name is not None:
                module["name"] = module_name
            module = module or None

        data_net_id = None
        if len(data_pins) == 1:
            data_net = call(gate, "get_fan_in_net", data_pins[0]["name"])
            data_net_id = _net_id(data_net)

        sequential_gate = SequentialGate(
            gate_id,
            call(gate, "get_name", default=""),
            gate_type=call(gate_type, "get_name"),
            properties=properties,
            clock_nets=sorted(clock_nets),
            data_net=int(data_net_id) if data_net_id is not None else None,
            control=control,
            init_value=init_value,
            module=module,
        )

        # The cone feeds every input pin that is not the clock: solve_fsm builds
        # the flip-flop's complete next-state function, which for this library
        # includes the clock-enable pin.
        cone_start = []
        for pin in pins:
            if pin["direction"] != "input" or pin["type"] in NON_CONE_PIN_TYPES:
                continue
            if pin["type"] in ("set", "reset"):
                # Not part of next_state; including it would enlarge the cone
                # with logic solve_fsm never looks at.
                continue
            net = call(gate, "get_fan_in_net", pin["name"])
            net_id = _net_id(net)
            if net_id is not None:
                cone_start.append(int(net_id))

        cone, boundaries = _backward_cone(nets, gates_by_id, sequential_ids, cone_start)
        depends = {source for source in boundaries.values() if source is not None}

        fanout = set()
        for net in call(gate, "get_fan_out_nets", default=[]) or []:
            for endpoint in call(net, "get_destinations", default=[]) or []:
                destination = call(call(endpoint, "get_gate"), "get_id")
                if destination is not None:
                    fanout.add(int(destination))

        graph.add(
            sequential_gate,
            depends=depends,
            cone=cone,
            free_inputs=[net_id for net_id, source in boundaries.items() if source is None],
            fanout=fanout,
        )
        graph.boundaries[gate_id] = dict(boundaries)

        if len(data_pins) != 1:
            graph.unusable[gate_id] = (
                "solve_fsm requires exactly one data pin per state flip-flop; gate type "
                "{!r} has {}".format(sequential_gate.type, len(data_pins))
            )
        elif data_net_id is None:
            graph.unusable[gate_id] = (
                "the data pin {!r} of this flip-flop is not driven; solve_fsm cannot "
                "build a next-state function for it".format(data_pins[0]["name"])
            )

    if graph.unusable:
        notes.append(
            "{} sequential gate(s) cannot be part of a state register solve_fsm can "
            "handle: {}".format(
                len(graph.unusable),
                "; ".join(
                    "{} ({})".format(graph.gates[gate_id].name, reason)
                    for gate_id, reason in sorted(graph.unusable.items())[:4]
                ),
            )
        )

    return Extraction(graph, nets, gates_by_id, sequential_ids, combinational_ids, notes)


def resolve_gates(extraction, specs):
    """Resolve configuration entries (gate names or numeric IDs) to gate IDs.

    Ambiguity is an error, never a silent pick: two gates with the same name is
    exactly the situation a user override exists to disentangle.
    """
    by_name = {}
    for gate_id, gate in extraction.gates_by_id.items():
        by_name.setdefault(call(gate, "get_name", default=""), []).append(int(gate_id))
    resolved = []
    problems = []
    for spec in specs:
        text = str(spec)
        if text.isdigit() and int(text) in extraction.gates_by_id:
            resolved.append(int(text))
            continue
        matches = by_name.get(text, [])
        if len(matches) == 1:
            resolved.append(matches[0])
        elif not matches:
            problems.append("no gate named {!r} (and no gate with that ID)".format(text))
        else:
            problems.append(
                "gate name {!r} is ambiguous: IDs {}".format(text, sorted(matches))
            )
    return sorted(set(resolved)), problems
