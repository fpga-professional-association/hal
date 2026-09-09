"""Extract a cycle-accurate transition system from a real netlist via ``hal_py``.

This is the only module that imports ``hal_py``. Everything it produces is an
ordinary :class:`~.system.TransitionSystem`, so the property engine, the SAT
back end, the counterexample replay and the findings writer never learn that a
netlist was involved.

How the model is built:

* gates split into combinational and sequential by the gate type's own
  ``sequential`` property -- the same predicate the rest of HAL uses, never a
  name heuristic;
* every sequential gate must carry an ``FFComponent``. Latches, RAM, and
  anything else are refused by name in a
  :class:`UnsupportedPrimitives` error rather than silently modelled as a
  flip-flop;
* each register contributes one state signal (the net on its ``state`` output
  pin) whose next-state term is the gate type's ``next_state`` function with pin
  names replaced by the combinational cone driving them, wrapped by the gate's
  own asynchronous clear/preset functions;
* every other net driven by combinational logic becomes a named definition,
  built with ``SubgraphNetlistDecorator.get_subgraph_function`` over the
  combinational gates only, so the boundary variables are exactly primary inputs
  and register outputs;
* clock nets are removed from the model entirely -- one step of the system *is*
  one active edge. A clock net that also feeds data logic is a gated or derived
  clock and is refused, because the cycle abstraction would be wrong for it.

Signal names are net names, which is what a bus mapping refers to. Duplicate net
names are disambiguated as ``<name>#<id>`` and reported, so a mapping can never
silently bind to the wrong one of two identically named nets.
"""

import os
import sys

from . import expr
from .system import TransitionSystem, ff_next

__all__ = [
    "NetlistFrontendError",
    "UnsupportedPrimitives",
    "import_hal",
    "load_plugins",
    "load",
    "build_transition_system",
]

#: Node types of a HAL Boolean function this checker can translate.
_SUPPORTED_NODES = (
    "And",
    "Or",
    "Not",
    "Xor",
    "Ite",
    "Concat",
    "Slice",
    "Zext",
    "Sext",
    "Variable",
    "Constant",
    "Index",
)

#: ``BooleanFunction::NodeType`` ids, from ``include/hal_core/netlist/boolean_function.h``.
#: The Python bindings expose ``node.type`` as the plain ``u16`` value, so a node's
#: kind has to be recovered from the number (``4098`` is ``Variable``) rather than
#: from ``str(node.type)``. The table is only a fallback: the values are read back
#: from ``hal_py.BooleanFunction.NodeType`` whenever the bindings publish them.
_NODE_TYPE_IDS = {
    "And": 0x0000,
    "Or": 0x0001,
    "Not": 0x0002,
    "Xor": 0x0003,
    "Concat": 0x0100,
    "Slice": 0x0101,
    "Zext": 0x0102,
    "Sext": 0x0103,
    "Ite": 0x0405,
    "Constant": 0x1000,
    "Index": 0x1001,
    "Variable": 0x1002,
}

_NODE_TYPE_NAMES = {}


class NetlistFrontendError(RuntimeError):
    """The netlist could not be loaded or turned into a transition system."""


class UnsupportedPrimitives(NetlistFrontendError):
    """The netlist uses primitives this checker does not model."""

    def __init__(self, message, primitives):
        #: ``[{"gate_type": ..., "reason": ..., "count": ..., "example_gates": [...]}, ...]``
        self.primitives = list(primitives)
        NetlistFrontendError.__init__(self, message)


def import_hal(hal_libs=()):
    """Import ``hal_py``, with an actionable message when it is missing."""
    for entry in hal_libs or ():
        path = os.path.abspath(os.path.expanduser(entry))
        if not os.path.isdir(path):
            raise NetlistFrontendError("--hal-lib directory does not exist: {}".format(entry))
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        import hal_py
    except ImportError as error:
        raise NetlistFrontendError(
            "could not import hal_py ({}).\n"
            "Checking a netlist needs a built HAL. Point at one with --hal-lib <build>/lib or "
            "PYTHONPATH=<build>/lib, and set HAL_BASE_PATH=<build> so the parser plugins and "
            "gate libraries are found. Reference-model checks need none of this.".format(error)
        )
    return hal_py


def load_plugins(hal_py):
    """Register HAL's parser plugins; idempotent, so every loader can just call it."""
    try:
        hal_py.plugin_manager.load_all_plugins()
    except Exception as error:  # noqa: BLE001 - a parser is a plugin; say so plainly
        raise NetlistFrontendError(
            "hal_py.plugin_manager.load_all_plugins() failed ({}). The Verilog/VHDL parsers are "
            "plugins, so a netlist cannot be read without them.".format(error)
        )


def _load_netlist(hal_py, netlist_path, project_path, gate_library):
    load_plugins(hal_py)
    if project_path:
        if not os.path.isdir(project_path):
            raise NetlistFrontendError("no such HAL project directory: {}".format(project_path))
        netlist = hal_py.NetlistFactory.load_hal_project(project_path)
        source = project_path
    else:
        if not netlist_path or not os.path.isfile(netlist_path):
            raise NetlistFrontendError("no such netlist file: {}".format(netlist_path))
        if not gate_library or not os.path.isfile(gate_library):
            raise NetlistFrontendError(
                "the gate library {!r} does not exist; a structural netlist cannot be parsed "
                "without one".format(gate_library)
            )
        netlist = hal_py.NetlistFactory.load_netlist(netlist_path, gate_library)
        source = netlist_path
    if netlist is None:
        raise NetlistFrontendError(
            "HAL returned no netlist for {}; see the HAL log above".format(source)
        )
    return netlist, source


def load(netlist_path=None, project_path=None, gate_library=None, hal_libs=()):
    """Load a design and return ``(TransitionSystem, artifact_spec)``."""
    hal_py = import_hal(hal_libs)
    netlist, source = _load_netlist(hal_py, netlist_path, project_path, gate_library)
    system, net_refs, statistics = build_transition_system(hal_py, netlist)
    artifact = {
        "kind": "hal_project" if project_path else "netlist",
        "path": os.path.abspath(source),
        "design_name": netlist.get_design_name() or system.name,
        "netlist_id": netlist.get_id(),
        "gate_count": len(netlist.get_gates()),
        "net_count": len(netlist.get_nets()),
        "description": "gate-level design checked against a user-supplied APB bus mapping",
        "net_refs": net_refs,
        "statistics": statistics,
    }
    if gate_library and os.path.isfile(gate_library):
        artifact["gate_library"] = {"path": os.path.abspath(gate_library)}
    return system, artifact


# ---------------------------------------------------------------------------
# Boolean function translation
# ---------------------------------------------------------------------------


def _node_type_names(hal_py):
    """``node.type`` value -> node type name, preferring what the bindings publish."""
    key = id(hal_py)
    cached = _NODE_TYPE_NAMES.get(key)
    if cached is None:
        by_name = dict(_NODE_TYPE_IDS)
        node_type = getattr(getattr(hal_py, "BooleanFunction", None), "NodeType", None)
        for attribute in dir(node_type) if node_type is not None else ():
            if attribute.startswith("_"):
                continue
            value = getattr(node_type, attribute, None)
            if isinstance(value, int) and not isinstance(value, bool):
                by_name[attribute] = value
        cached = {value: name for name, value in by_name.items()}
        _NODE_TYPE_NAMES[key] = cached
    return cached


def _node_type_name(hal_py, node):
    raw = getattr(node, "type", None)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return _node_type_names(hal_py).get(raw, str(raw))
    return str(raw).rsplit(".", 1)[-1]


def _constant_bits(node, where):
    """A ``Constant`` node's value as a list of terms, least significant bit first."""
    bits = []
    for value in node.constant:
        name = str(value).rsplit(".", 1)[-1]
        if name not in ("0", "1", "ZERO", "ONE"):
            raise NetlistFrontendError(
                "{}: constant bit {!r} is not 0 or 1 (X/Z are not modelled)".format(where, name)
            )
        bits.append(expr.const(name in ("1", "ONE")))
    return bits


def _translate(hal_py, function, variable_names, where):
    """HAL ``BooleanFunction`` (reverse polish) -> :mod:`hal_apb_check.expr` term.

    HAL Boolean functions are bit vectors, so every stack entry here is a list of
    single-bit terms, least significant bit first -- exactly the ``Value`` vector
    order HAL itself uses -- and ``Index`` nodes push a plain integer. That lets
    the word-level node types a real gate library produces (``Concat``, ``Slice``,
    ``Zext``, ``Sext``, and the ``Ite`` of a multiplexer) be bit-blasted exactly
    instead of being refused.

    The *result* still has to be a single bit, because a signal in the transition
    system is one bit. Anything this checker cannot express -- arithmetic,
    comparisons, shifts, a multi-bit variable -- raises, because a silently
    dropped operator would turn into a confident, wrong verdict.
    """
    stack = []
    for node in function.get_nodes():
        kind = _node_type_name(hal_py, node)
        arity = node.get_arity()
        if len(stack) < arity:
            raise NetlistFrontendError("{}: malformed Boolean function".format(where))
        operands = stack[len(stack) - arity:] if arity else []
        del stack[len(stack) - arity:]

        def bits(position, operands=operands, kind=kind):
            """Operand ``position`` as a bit vector, refusing an ``Index`` there."""
            operand = operands[position]
            if not isinstance(operand, list):
                raise NetlistFrontendError(
                    "{}: {} operand {} is an index, not a value".format(where, kind, position)
                )
            return operand

        def index(position, operands=operands, kind=kind):
            operand = operands[position]
            if not isinstance(operand, int):
                raise NetlistFrontendError(
                    "{}: {} operand {} is not a constant index".format(where, kind, position)
                )
            return operand

        if kind == "Index":
            stack.append(int(node.index))
            continue

        if kind == "Variable":
            if node.size != 1:
                raise NetlistFrontendError(
                    "{}: variable {!r} is {} bits wide; this checker models single-bit signals "
                    "only".format(where, node.variable, node.size)
                )
            name = node.variable
            translated = variable_names.get(name)
            if translated is None:
                raise NetlistFrontendError(
                    "{}: Boolean function refers to unknown variable {!r}".format(where, name)
                )
            result = [translated]
        elif kind == "Constant":
            result = _constant_bits(node, where)
        elif kind == "Not":
            result = [expr.not_(bit) for bit in bits(0)]
        elif kind in ("And", "Or", "Xor"):
            left, right = bits(0), bits(1)
            if len(left) != len(right):
                raise NetlistFrontendError("{}: malformed Boolean function".format(where))
            combine = {"And": expr.and_, "Or": expr.or_, "Xor": expr.xor_}[kind]
            result = [combine(a, b) for a, b in zip(left, right)]
        elif kind == "Ite":
            condition, if_true, if_false = bits(0), bits(1), bits(2)
            if len(condition) != 1 or len(if_true) != len(if_false):
                raise NetlistFrontendError("{}: malformed Boolean function".format(where))
            select = condition[0]
            result = [
                expr.or_(expr.and_(select, a), expr.and_(expr.not_(select), b))
                for a, b in zip(if_true, if_false)
            ]
        elif kind == "Concat":
            # ``Concat(p0, p1)`` puts p0 in the high bits, p1 in the low ones.
            result = list(bits(1)) + list(bits(0))
        elif kind == "Slice":
            value, start, end = bits(0), index(1), index(2)
            if not 0 <= start <= end < len(value):
                raise NetlistFrontendError(
                    "{}: slice [{}:{}] is outside the {}-bit operand".format(
                        where, start, end, len(value)
                    )
                )
            result = value[start:end + 1]
        elif kind in ("Zext", "Sext"):
            value = bits(0)
            if not value or node.size < len(value):
                raise NetlistFrontendError("{}: malformed Boolean function".format(where))
            filler = expr.const(False) if kind == "Zext" else value[-1]
            result = list(value) + [filler] * (node.size - len(value))
        else:
            raise NetlistFrontendError(
                "{}: Boolean function node type {!r} is not modelled; this checker handles "
                "{} only".format(where, kind, ", ".join(_SUPPORTED_NODES))
            )

        if len(result) != node.size:
            raise NetlistFrontendError(
                "{}: {} node produced {} bits but declares {}".format(
                    where, kind, len(result), node.size
                )
            )
        stack.append(result)

    if len(stack) != 1 or not isinstance(stack[0], list):
        raise NetlistFrontendError("{}: malformed Boolean function".format(where))
    if len(stack[0]) != 1:
        raise NetlistFrontendError(
            "{}: {} is {} bits wide; this checker models single-bit APB signals only".format(
                where, function, len(stack[0])
            )
        )
    return stack[0][0]


# ---------------------------------------------------------------------------
# transition system construction
# ---------------------------------------------------------------------------


def _is_sequential(gate):
    properties = {str(entry).rsplit(".", 1)[-1] for entry in gate.get_type().get_properties()}
    return "sequential" in properties


def _signal_names(netlist):
    """Net -> unique signal name, disambiguating duplicate net names."""
    by_name = {}
    for net in netlist.get_nets():
        by_name.setdefault(net.get_name(), []).append(net)
    names = {}
    duplicates = []
    for name, nets in by_name.items():
        if len(nets) == 1:
            names[nets[0].get_id()] = name
            continue
        duplicates.append(name)
        for net in nets:
            names[net.get_id()] = "{}#{}".format(name, net.get_id())
    return names, sorted(duplicates)


def _ff_component(hal_py, gate):
    gate_type = gate.get_type()
    try:
        component = gate_type.get_component(lambda entry: hal_py.FFComponent.is_class_of(entry))
    except Exception:  # noqa: BLE001 - older bindings, treat as absent
        component = None
    return component


def build_transition_system(hal_py, netlist):
    """Return ``(TransitionSystem, net_refs, statistics)`` for ``netlist``."""
    names, duplicate_names = _signal_names(netlist)
    gates = netlist.get_gates()
    sequential = [gate for gate in gates if _is_sequential(gate)]
    combinational = [gate for gate in gates if not _is_sequential(gate)]

    # ---- multi-driven nets have no single Boolean function ----------------
    for net in netlist.get_nets():
        sources = net.get_sources()
        if len(sources) > 1:
            raise NetlistFrontendError(
                "net {!r} has {} drivers; a multi-driven net has no single Boolean function, "
                "so it cannot enter the model (resolve the conflict or exclude the "
                "design)".format(net.get_name(), len(sources))
            )

    # ---- refuse what we do not model, by name ----------------------------
    unsupported = {}
    registers = []

    def refuse(gate, reason):
        entry = unsupported.setdefault(
            gate.get_type().get_name(),
            {
                "gate_type": gate.get_type().get_name(),
                "reason": reason,
                "count": 0,
                "example_gates": [],
            },
        )
        entry["count"] += 1
        if len(entry["example_gates"]) < 3:
            entry["example_gates"].append({"id": gate.get_id(), "name": gate.get_name()})

    for gate in sequential:
        component = _ff_component(hal_py, gate)
        if component is None:
            refuse(
                gate,
                "sequential gate without an FFComponent (latch, RAM or custom primitive); "
                "only edge-triggered flip-flops are modelled",
            )
            continue
        # A flip-flop with *both* an asynchronous set and an asynchronous clear
        # needs its library's set/reset precedence to be modelled exactly.
        # Guessing one would silently change the reset behaviour the whole
        # analysis is scoped to, so it is refused by name instead.
        preset_function = component.get_async_set_function()
        clear_function = component.get_async_reset_function()
        if (
            preset_function is not None
            and clear_function is not None
            and not preset_function.is_empty()
            and not clear_function.is_empty()
        ):
            refuse(
                gate,
                "flip-flop with both an asynchronous set and an asynchronous clear; the "
                "library's set/reset precedence is not modelled and must not be guessed",
            )
            continue
        registers.append((gate, component))
    if unsupported:
        raise UnsupportedPrimitives(
            "the netlist uses sequential primitives this checker does not model: {}".format(
                ", ".join(sorted(unsupported))
            ),
            unsupported.values(),
        )

    # ---- clock nets are abstracted away ----------------------------------
    clock_nets = set()
    for gate, _ in registers:
        for pin in gate.get_type().get_pins():
            if str(pin.get_type()).rsplit(".", 1)[-1] != "clock":
                continue
            net = gate.get_fan_in_net(pin.get_name())
            if net is not None:
                clock_nets.add(net.get_id())

    decorator = hal_py.SubgraphNetlistDecorator(netlist)

    def net_variable(net):
        return expr.var(names[net.get_id()])

    def boundary_names(net_ids):
        return {
            hal_py.BooleanFunctionNetDecorator(net).get_boolean_variable_name(): net_variable(net)
            for net in (netlist.get_net_by_id(net_id) for net_id in net_ids)
        }

    system = TransitionSystem(netlist.get_design_name() or "netlist_{}".format(netlist.get_id()))

    state_net_ids = set()
    for gate, component in registers:
        state_pins = [
            pin
            for pin in gate.get_type().get_pins()
            if str(pin.get_type()).rsplit(".", 1)[-1] == "state"
        ]
        if len(state_pins) != 1:
            raise NetlistFrontendError(
                "flip-flop {} ({}) has {} 'state' output pins; exactly one is required".format(
                    gate.get_name(), gate.get_type().get_name(), len(state_pins)
                )
            )
        net = gate.get_fan_out_net(state_pins[0].get_name())
        if net is None:
            continue  # an unconnected register cannot influence the bus
        if net.get_id() in state_net_ids:
            raise NetlistFrontendError(
                "net {!r} is driven by more than one register".format(net.get_name())
            )
        state_net_ids.add(net.get_id())

    # ---- inputs: nets with no driver, minus the clocks --------------------
    driven = set(state_net_ids)
    for gate in combinational:
        for net in gate.get_fan_out_nets():
            driven.add(net.get_id())

    input_net_ids = []
    for net in netlist.get_nets():
        if net.get_id() in driven or net.get_id() in clock_nets:
            continue
        if not net.get_destinations():
            continue  # a dangling net cannot influence anything
        input_net_ids.append(net.get_id())
    input_net_ids.sort()

    for net_id in input_net_ids:
        system.add_input(names[net_id])
    for net_id in sorted(state_net_ids):
        system.add_state(names[net_id], initial=None)

    # ---- combinational definitions ---------------------------------------
    combinational_net_ids = sorted(
        net_id for net_id in driven if net_id not in state_net_ids
    )
    for net_id in combinational_net_ids:
        net = netlist.get_net_by_id(net_id)
        if net_id in clock_nets:
            raise NetlistFrontendError(
                "net {!r} drives a flip-flop clock pin *and* is produced by combinational "
                "logic; gated or derived clocks break the one-step-per-edge abstraction this "
                "checker relies on".format(net.get_name())
            )
        result = decorator.get_subgraph_function(combinational, net)
        if result is None:
            raise NetlistFrontendError(
                "could not build the Boolean function of net {!r}; see the HAL log (a "
                "combinational loop or a multi-driven net is the usual cause)".format(
                    net.get_name()
                )
            )
        variables = {}
        for variable_name in result.get_variable_names():
            source = hal_py.BooleanFunctionNetDecorator.get_net_from(netlist, variable_name)
            if source is None:
                raise NetlistFrontendError(
                    "the function of net {!r} refers to {!r}, which is not a net".format(
                        net.get_name(), variable_name
                    )
                )
            variables[variable_name] = expr.var(names[source.get_id()])
        system.define(
            names[net_id],
            _translate(hal_py, result, variables, "net {!r}".format(net.get_name())),
        )

    # ---- next-state terms -------------------------------------------------
    for gate, component in registers:
        state_pin = [
            pin
            for pin in gate.get_type().get_pins()
            if str(pin.get_type()).rsplit(".", 1)[-1] == "state"
        ][0]
        net = gate.get_fan_out_net(state_pin.get_name())
        if net is None:
            continue
        where = "flip-flop {!r} ({})".format(gate.get_name(), gate.get_type().get_name())

        pin_terms = {}
        for pin in gate.get_type().get_pins():
            if str(pin.get_direction()).rsplit(".", 1)[-1] != "input":
                continue
            fan_in = gate.get_fan_in_net(pin.get_name())
            if fan_in is None:
                continue
            pin_terms[pin.get_name()] = expr.var(names[fan_in.get_id()])

        def pin_function(function, label, terms=pin_terms, context=where):
            if function is None or function.is_empty():
                return None
            unconnected = sorted(set(function.get_variable_names()) - set(terms))
            if unconnected:
                raise NetlistFrontendError(
                    "{}: the {} function reads pin(s) {} that are not connected to a net; an "
                    "unconnected flip-flop input has no defined value in this model".format(
                        context, label, ", ".join(unconnected)
                    )
                )
            return _translate(hal_py, function, terms, "{} {}".format(context, label))

        next_state = pin_function(component.get_next_state_function(), "next_state")
        if next_state is None:
            raise NetlistFrontendError("{}: gate type has no next_state function".format(where))
        clear = pin_function(component.get_async_reset_function(), "async_reset")
        preset = pin_function(component.get_async_set_function(), "async_set")
        system.set_next(names[net.get_id()], ff_next(next_state, clear=clear, preset=preset))

    if duplicate_names:
        system.notes.append(
            "duplicate net names disambiguated as <name>#<id>: {}".format(
                ", ".join(duplicate_names)
            )
        )

    # Signal name -> HAL net id, so a witness entry can point back at the exact
    # net in the exact artifact rather than at a bare string.
    net_refs = {
        names[net.get_id()]: net.get_id()
        for net in netlist.get_nets()
        if system.has_signal(names[net.get_id()])
    }
    statistics = {
        "gates": len(gates),
        "registers": len(registers),
        "combinational_gates": len(combinational),
        "inputs": len(system.inputs),
        "states": len(system.states),
        "definitions": len(system.defines),
        "clock_nets": sorted(names[net_id] for net_id in clock_nets),
        "duplicate_net_names": duplicate_names,
    }
    return system, net_refs, statistics
