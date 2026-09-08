"""Build a :class:`circuit.Circuit` from a live ``hal_py`` netlist.

Everything HAL-specific about the recovery lives here.  The gate semantics come
from HAL itself -- ``Gate.get_resolved_boolean_function`` for combinational
cells and the ``FFComponent`` functions for flip-flops -- so the analysis never
re-implements primitive behaviour or guesses at a cell from its name.  A gate
type HAL models as something other than an edge-triggered flip-flop (a latch, a
RAM, a tristate cell) becomes an explicitly *unsupported* gate whose outputs
stay ``X``; it is reported, not approximated.

Import this module only when ``hal_py`` is importable; :mod:`verilog_source`
covers the offline case.
"""

from . import circuit as circuit_model
from .circuit import Circuit, FlipFlop, Gate

__all__ = ["HalSourceError", "build_circuit", "load_netlist"]


class HalSourceError(RuntimeError):
    """A binding the recovery needs is missing, or a netlist cannot be modelled."""


def _enum_name(value):
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value).rsplit(".", 1)[-1]


def _net_key(net):
    return "n{}".format(net.get_id())


def _gate_key(gate):
    return "g{}".format(gate.get_id())


def _make_combinational_function(hal_py, boolean_function):
    zero = hal_py.BooleanFunction.Value.ZERO
    one = hal_py.BooleanFunction.Value.ONE

    def evaluate(pin_values, _bf=boolean_function, _zero=zero, _one=one):
        inputs = {}
        for pin, value in pin_values.items():
            if value == 0:
                inputs[pin] = _zero
            elif value == 1:
                inputs[pin] = _one
        result = _bf.evaluate(inputs)
        if result == _zero:
            return 0
        if result == _one:
            return 1
        return None

    return evaluate


def _optional_function(hal_py, boolean_function):
    if boolean_function is None:
        return None
    try:
        if boolean_function.is_empty():
            return None
    except Exception:  # pragma: no cover - older bindings
        pass
    return _make_combinational_function(hal_py, boolean_function)


def _clear_preset_state(behaviour):
    name = _enum_name(behaviour)
    if name == "L":
        return 0
    if name == "H":
        return 1
    return None


def _pin_names_of_type(gate_type, wanted):
    names = []
    for pin in gate_type.get_pins():
        if _enum_name(pin.get_type()) == wanted:
            names.append(pin.get_name())
    return names


def _ff_component(hal_py, gate_type):
    component_class = getattr(hal_py, "FFComponent", None)
    if component_class is None:
        raise HalSourceError(
            "hal_py has no FFComponent; the recovery cannot read flip-flop semantics "
            "from this build"
        )
    component = gate_type.get_component(
        lambda candidate: component_class.is_class_of(candidate)
    )
    if component is None:
        return None
    for accessor in (
        "get_next_state_function",
        "get_clock_function",
        "get_async_reset_function",
        "get_async_set_function",
    ):
        if not hasattr(component, accessor):
            raise HalSourceError(
                "gate type {} returned a component without {}(); the bindings did not "
                "down-cast it to FFComponent, so flip-flop semantics cannot be "
                "read".format(gate_type.get_name(), accessor)
            )
    return component


def build_circuit(hal_py, netlist, name=None):
    """Translate a ``hal_py.Netlist`` into the analysis circuit model."""
    gates = []
    net_names = {}
    net_uids = {}

    for net in netlist.get_nets():
        net_names[_net_key(net)] = net.get_name()
        net_uids[_net_key(net)] = net.get_id()

    for gate in netlist.get_gates():
        gate_type = gate.get_type()
        properties = sorted(_enum_name(entry) for entry in gate_type.get_property_list())

        inputs, outputs = {}, {}
        for endpoint in gate.get_fan_in_endpoints():
            inputs[endpoint.get_pin().get_name()] = _net_key(endpoint.get_net())
        for endpoint in gate.get_fan_out_endpoints():
            outputs[endpoint.get_pin().get_name()] = _net_key(endpoint.get_net())

        key = _gate_key(gate)
        if "ff" in properties:
            component = _ff_component(hal_py, gate_type)
            if component is None:
                gates.append(
                    Gate(
                        key,
                        gate.get_name(),
                        gate_type.get_name(),
                        circuit_model.KIND_UNSUPPORTED,
                        inputs,
                        outputs,
                        unsupported_reason=(
                            "gate type {} carries the 'ff' property but exposes no "
                            "FFComponent, so its next-state function is unknown".format(
                                gate_type.get_name()
                            )
                        ),
                        properties=properties,
                        uid=gate.get_id(),
                    )
                )
                continue
            next_state = _optional_function(hal_py, component.get_next_state_function())
            if next_state is None:
                gates.append(
                    Gate(
                        key,
                        gate.get_name(),
                        gate_type.get_name(),
                        circuit_model.KIND_UNSUPPORTED,
                        inputs,
                        outputs,
                        unsupported_reason=(
                            "flip-flop type {} has an empty next-state function".format(
                                gate_type.get_name()
                            )
                        ),
                        properties=properties,
                        uid=gate.get_id(),
                    )
                )
                continue
            behaviour = None
            try:
                behaviour = component.get_async_set_reset_behavior()[0]
            except Exception:  # pragma: no cover - optional in older builds
                behaviour = None
            ff = FlipFlop(
                next_state,
                clock=_optional_function(hal_py, component.get_clock_function()),
                async_reset=_optional_function(hal_py, component.get_async_reset_function()),
                async_set=_optional_function(hal_py, component.get_async_set_function()),
                state_pins=_pin_names_of_type(gate_type, "state"),
                neg_state_pins=_pin_names_of_type(gate_type, "neg_state"),
                clock_pins=_pin_names_of_type(gate_type, "clock"),
                clear_preset_state=_clear_preset_state(behaviour),
            )
            gates.append(
                Gate(
                    key,
                    gate.get_name(),
                    gate_type.get_name(),
                    circuit_model.KIND_FF,
                    inputs,
                    outputs,
                    ff=ff,
                    properties=properties,
                    uid=gate.get_id(),
                )
            )
            continue

        if "sequential" in properties:
            gates.append(
                Gate(
                    key,
                    gate.get_name(),
                    gate_type.get_name(),
                    circuit_model.KIND_UNSUPPORTED,
                    inputs,
                    outputs,
                    unsupported_reason=(
                        "sequential gate type {} ({}) is not an edge-triggered "
                        "flip-flop; the APB recovery only models edge-triggered "
                        "state".format(gate_type.get_name(), ", ".join(properties))
                    ),
                    properties=properties,
                    uid=gate.get_id(),
                )
            )
            continue

        functions = {}
        missing = []
        for pin in gate_type.get_pins():
            if _enum_name(pin.get_direction()) != "output":
                continue
            pin_name = pin.get_name()
            if pin_name not in outputs:
                continue
            boolean_function = gate.get_resolved_boolean_function(pin, False)
            if boolean_function is None:
                boolean_function = gate.get_boolean_function(pin)
            compiled = _optional_function(hal_py, boolean_function)
            if compiled is None:
                missing.append(pin_name)
            else:
                functions[pin_name] = compiled
        if missing:
            gates.append(
                Gate(
                    key,
                    gate.get_name(),
                    gate_type.get_name(),
                    circuit_model.KIND_UNSUPPORTED,
                    inputs,
                    outputs,
                    unsupported_reason=(
                        "gate type {} has no resolvable Boolean function for output "
                        "pin(s) {}".format(gate_type.get_name(), ", ".join(sorted(missing)))
                    ),
                    properties=properties,
                    uid=gate.get_id(),
                )
            )
            continue
        gates.append(
            Gate(
                key,
                gate.get_name(),
                gate_type.get_name(),
                circuit_model.KIND_COMBINATIONAL,
                inputs,
                outputs,
                functions=functions,
                properties=properties,
                uid=gate.get_id(),
            )
        )

    input_nets = [_net_key(net) for net in netlist.get_global_input_nets()]
    output_nets = [_net_key(net) for net in netlist.get_global_output_nets()]

    library = netlist.get_gate_library()
    library_name = library.get_name() if library is not None else None

    return Circuit(
        name or netlist.get_design_name() or "netlist",
        gates,
        net_names,
        input_nets,
        output_nets,
        source=netlist.get_input_filename() or None,
        gate_library=library_name,
        net_uids=net_uids,
    )


def load_netlist(hal_py, path, gate_library=None):
    """Load a HAL project directory, a ``.hal`` file or a netlist file."""
    import os

    factory = hal_py.NetlistFactory
    if os.path.isdir(path):
        netlist = factory.load_hal_project(str(path))
    elif gate_library:
        netlist = factory.load_netlist(str(path), str(gate_library))
    else:
        netlist = factory.load_netlist(str(path))
    if netlist is None:
        raise HalSourceError(
            "hal_py could not load {!r}{}; see the HAL log above".format(
                str(path),
                "" if gate_library is None else " with gate library {!r}".format(gate_library),
            )
        )
    return netlist
