"""Offline: a structural Verilog netlist -> a cycle-accurate transition system.

There are two front ends in this tool and they must produce the *same* model, or
the offline tests prove nothing about the run that matters. So neither of them
is written from scratch:

* this module reuses ``hal_apb_recover.verilog_source`` (the structural Verilog
  reader) and ``hal_apb_recover.hgl_library`` (the ``.hgl`` reader), then lifts
  the resulting :class:`~hal_apb_recover.circuit.Circuit` into a
  :class:`hal_apb_check.system.TransitionSystem` **symbolically** -- the circuit
  model evaluates three-valued over concrete pins, which is right for register
  recovery and useless for a SAT query, so the gate functions are re-read from
  the same library expressions and turned into Boolean terms;
* :mod:`hal_secprop.halsource` calls ``hal_apb_check.netlist`` unchanged.

Both end up in the same ``TransitionSystem`` with the same signal names (net
names), which is what ``test_hal_secprop_hal.py`` checks by requiring identical
verdicts from both paths on the same fixture.

Deliberate refusals, all raised rather than approximated:

* a sequential gate that is not an edge-triggered flip-flop (a latch, a RAM)
  -- :class:`~hal_secprop.errors.UnsupportedPrimitives`, which the CLI turns
  into ``unsupported`` findings, never into a silent model;
* a flip-flop with *both* an asynchronous set and an asynchronous clear (the
  library's precedence would have to be guessed);
* a connected ``QN`` pin (see :func:`_refuse_neg_state`);
* a clock net that is also driven by logic -- a gated or derived clock breaks
  the one-step-per-edge abstraction the whole analysis is scoped to;
* a multi-driven net, an unconnected function input, a combinational loop.
"""

import os

from hal_apb_check import expr
from hal_apb_check.system import TransitionSystem, ff_next
from hal_apb_recover import circuit as circuit_model
from hal_apb_recover import verilog_source
from hal_apb_recover.hgl_library import GateLibrary, GateLibraryError, parse_expression

from .errors import DesignError, UnsupportedPrimitives

__all__ = ["load", "build_transition_system", "term_from_expression"]


def term_from_expression(node, variables, where):
    """``.hgl`` expression AST -> :mod:`hal_apb_check.expr` term.

    ``variables`` maps a *pin name* to a term. A pin the expression reads but
    which is not connected has no defined value in this model, so it is an
    error rather than a free variable: an unconnected flip-flop data pin that
    silently became an input would make every verdict about that register
    meaningless.
    """
    kind = node[0]
    if kind == "const":
        return expr.const(bool(node[1]))
    if kind == "var":
        term = variables.get(node[1])
        if term is None:
            raise DesignError(
                "{}: the expression reads pin {!r}, which is not connected to a net".format(
                    where, node[1]
                )
            )
        return term
    if kind == "!":
        return expr.not_(term_from_expression(node[1], variables, where))
    left = term_from_expression(node[1], variables, where)
    right = term_from_expression(node[2], variables, where)
    if kind == "&":
        return expr.and_(left, right)
    if kind == "|":
        return expr.or_(left, right)
    if kind == "^":
        return expr.xor_(left, right)
    raise DesignError("{}: unknown operator {!r} in a gate library expression".format(where, kind))


def _compile(text, variables, where):
    try:
        node = parse_expression(text)
    except GateLibraryError as error:
        raise DesignError("{}: {}".format(where, error))
    return term_from_expression(node, variables, where)


def _refuse_neg_state(element, circuit):
    """A connected ``QN`` output is refused rather than modelled.

    ``hal_apb_check``'s ``hal_py`` front end derives one state signal per
    flip-flop from the ``state`` pin and leaves a connected ``neg_state`` net
    looking like a free input. Defining it as ``!Q`` here would make the offline
    model *stronger* than the ``hal_py`` one, and a design that checks clean
    offline and differently in the container is worse than one that is refused
    in both. Neither fixture uses ``QN``.
    """
    if element.neg_state_net is None:
        return
    raise DesignError(
        "flip-flop {!r} has its inverted output ({}) connected. This analysis models one "
        "state signal per flip-flop; an inverted output would be a second name for the "
        "same bit and is refused rather than modelled inconsistently with the hal_py "
        "front end.".format(element.gate.name, circuit.net_name(element.neg_state_net))
    )


def build_transition_system(circuit, library, design_name=None):
    """Lift ``circuit`` into a :class:`TransitionSystem`.

    Returns ``(system, statistics)``.
    """
    unsupported = {}
    for gate in circuit.unsupported_gates:
        entry = unsupported.setdefault(
            gate.type_name,
            {
                "gate_type": gate.type_name,
                "reason": gate.unsupported_reason,
                "count": 0,
                "example_gates": [],
            },
        )
        entry["count"] += 1
        if len(entry["example_gates"]) < 3:
            entry["example_gates"].append({"id": gate.uid, "name": gate.name})
    if unsupported:
        raise UnsupportedPrimitives(
            "the netlist uses primitives this analysis does not model: {}".format(
                ", ".join(sorted(unsupported))
            ),
            unsupported.values(),
        )
    if circuit.combinational_loop:
        raise DesignError(
            "combinational loop through gate(s) {}; a design with a loop has no "
            "single-valued next state".format(", ".join(circuit.combinational_loop))
        )

    # ---- multi-driven nets have no single Boolean function ----------------
    drivers = {}
    for gate in circuit.gates:
        for pin, net in gate.outputs.items():
            drivers.setdefault(net, []).append("{}.{}".format(gate.name, pin))
    multi = sorted(net for net, sources in drivers.items() if len(sources) > 1)
    if multi:
        raise DesignError(
            "net(s) {} have more than one driver; a multi-driven net has no single "
            "Boolean function".format(", ".join(circuit.net_name(net) for net in multi))
        )

    # ---- clocks are abstracted away ---------------------------------------
    clock_nets = set()
    for element in circuit.state_elements:
        _refuse_neg_state(element, circuit)
        net = circuit.clock_net(element)
        if net is None:
            raise DesignError(
                "flip-flop {!r} ({}) has no single net on a clock pin; the one-step-per-"
                "edge abstraction cannot be applied to it".format(
                    element.gate.name, element.gate.type_name
                )
            )
        clock_nets.add(net)

    state_nets = {element.state_net: element for element in circuit.state_elements}
    combinational_nets = []
    for gate in circuit.eval_order:
        for pin, net in sorted(gate.outputs.items()):
            combinational_nets.append(net)
    combinational_set = set(combinational_nets)

    gated = sorted(combinational_set & clock_nets)
    if gated:
        raise DesignError(
            "net(s) {} drive a flip-flop clock pin *and* are produced by combinational "
            "logic; gated or derived clocks break the one-step-per-edge abstraction this "
            "analysis relies on".format(", ".join(circuit.net_name(net) for net in gated))
        )
    if set(state_nets) & clock_nets:
        raise DesignError("a flip-flop output is used as a clock; this is not modelled")

    # ---- inputs: nets with destinations that nothing drives ----------------
    used = set()
    for gate in circuit.gates:
        used.update(gate.inputs.values())
    driven = set(state_nets) | combinational_set
    input_nets = sorted(
        net for net in used if net not in driven and net not in clock_nets
    )

    system = TransitionSystem(design_name or circuit.name)
    terms = {}
    for net in input_nets:
        terms[net] = system.add_input(circuit.net_name(net))
    for net in sorted(state_nets):
        terms[net] = system.add_state(circuit.net_name(net), initial=None)

    # ---- combinational definitions, in levelized order --------------------
    for gate in circuit.eval_order:
        pin_terms = {}
        for pin, net in gate.inputs.items():
            if net in terms:
                pin_terms[pin] = terms[net]
        if gate.type_name == "<literal>":
            # verilog_source models 1'b0/1'b1 as zero-input gates.
            value = gate.key.endswith("_1")
            for pin, net in sorted(gate.outputs.items()):
                terms[net] = system.define(circuit.net_name(net), expr.const(value))
            continue
        cell = library.get(gate.type_name)
        if cell is None:
            raise DesignError(
                "gate type {!r} (instance {!r}) is not in gate library {!r}".format(
                    gate.type_name, gate.name, library.name
                )
            )
        for pin, net in sorted(gate.outputs.items()):
            function = cell.pin_by_name[pin].function
            if not function:
                raise DesignError(
                    "gate type {} has no Boolean function for output pin {}".format(
                        cell.name, pin
                    )
                )
            terms[net] = system.define(
                circuit.net_name(net),
                _compile(
                    function,
                    pin_terms,
                    "gate {!r} ({}) pin {}".format(gate.name, gate.type_name, pin),
                ),
            )

    # ---- next-state terms --------------------------------------------------
    for net in sorted(state_nets):
        element = state_nets[net]
        gate = element.gate
        cell = library.get(gate.type_name)
        config = cell.ff_config
        where = "flip-flop {!r} ({})".format(gate.name, gate.type_name)
        pin_terms = {}
        for pin, wire in gate.inputs.items():
            if wire in terms:
                pin_terms[pin] = terms[wire]
        if config.get("clear_on") and config.get("preset_on"):
            raise DesignError(
                "{}: the gate type has both an asynchronous clear and an asynchronous "
                "set; the library's precedence is not modelled and must not be "
                "guessed".format(where)
            )
        data = _compile(config["next_state"], pin_terms, "{} next_state".format(where))
        clear = (
            _compile(config["clear_on"], pin_terms, "{} clear_on".format(where))
            if config.get("clear_on")
            else None
        )
        preset = (
            _compile(config["preset_on"], pin_terms, "{} preset_on".format(where))
            if config.get("preset_on")
            else None
        )
        system.set_next(circuit.net_name(net), ff_next(data, clear=clear, preset=preset))

    problems = system.check()
    if problems:
        raise DesignError(
            "the extracted transition system is not sound:\n  - " + "\n  - ".join(problems)
        )

    statistics = {
        "gates": len(circuit.gates),
        "registers": len(circuit.state_elements),
        "combinational_gates": len(circuit.eval_order),
        "inputs": len(system.inputs),
        "states": len(system.states),
        "definitions": len(system.defines),
        "clock_nets": sorted(circuit.net_name(net) for net in clock_nets),
        "duplicate_net_names": [],
    }
    return system, statistics


def load(netlist_path, gate_library_path, top_module=None):
    """Read a structural Verilog netlist offline. Returns ``(system, artifact)``."""
    if not netlist_path or not os.path.isfile(netlist_path):
        raise DesignError("no such netlist file: {}".format(netlist_path))
    if not gate_library_path or not os.path.isfile(gate_library_path):
        raise DesignError(
            "the gate library {!r} does not exist; a structural netlist cannot be read "
            "without one".format(gate_library_path)
        )
    try:
        library = GateLibrary.from_file(gate_library_path)
    except GateLibraryError as error:
        raise DesignError(str(error))
    try:
        circuit = verilog_source.read_netlist(netlist_path, library, top_module=top_module)
    except (verilog_source.VerilogError, circuit_model.CircuitError) as error:
        raise DesignError(str(error))

    system, statistics = build_transition_system(circuit, library)
    artifact = {
        "kind": "netlist",
        "path": os.path.abspath(netlist_path),
        "design_name": circuit.name,
        "gate_count": len(circuit.gates),
        "net_count": len(circuit.net_names),
        "gate_library": {"path": os.path.abspath(gate_library_path), "name": library.name},
        "description": (
            "gate-level design read offline by hal_apb_recover's structural Verilog "
            "reader; gate and net identifiers are reader-local, not HAL ids"
        ),
        "net_refs": {},
        "statistics": statistics,
        "front_end": "offline",
    }
    return system, artifact
