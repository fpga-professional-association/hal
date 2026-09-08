"""Build a netlist-shaped stub from a real HAL gate library file.

The inventory extractor never imports ``hal_py``; it calls the bindings
duck-typed. That makes it testable, but only if the stub is built from
something authoritative rather than from what the test author remembers about
a primitive. So this module reads an actual ``.hgl`` gate library -- the same
file HAL parses -- and turns its cells into stub gate types with the same
properties, pins, pin types and components:

* ``types``       -> ``GateType.get_property_list()``
* ``pin_groups``  -> ``GateType.get_pins()`` with name/direction/type
* ``ff_config``   -> an FF component (clock, next state, async clear/preset)
                     plus the nested init and state components
* ``lut_config``  -> a LUT component plus its init component
* ``ram_config``  -> a RAM component (bit size) if the library declares one

A cell that declares no RAM component therefore produces a stub with no bit
size, exactly as HAL does -- which is the point: the "incomplete metadata"
behaviour under test is a property of the shipped gate library, not something
the fixture invents.

The netlist itself is described by an instance list: ``(name, cell, {pin: net})``.
Nets are created on demand; a net named in the top-level port list becomes a
global input or output net.
"""

import json
import os

__all__ = [
    "StubPin",
    "EmptyBooleanFunction",
    "StubComponent",
    "StubGateType",
    "StubGate",
    "StubNet",
    "StubEndpoint",
    "StubNetlist",
    "load_library",
    "build_netlist",
]


class StubPin(object):
    def __init__(self, name, direction, pin_type):
        self._name = name
        self._direction = direction
        self._type = pin_type

    def get_name(self):
        return self._name

    def get_direction(self):
        return self._direction

    def get_type(self):
        return self._type


class EmptyBooleanFunction(object):
    """What HAL returns for a function a gate type does not model.

    ``FFComponent.get_async_reset_function()`` and friends never return
    ``None``; a cell without a ``clear_on`` yields an *empty* BooleanFunction
    whose ``str()`` is the literal ``"<empty>"``. The stub reproduces that so
    the extractor is tested against HAL's actual return value rather than
    against a missing attribute.
    """

    def __str__(self):
        return "<empty>"

    def __repr__(self):  # pragma: no cover - debugging aid
        return "EmptyBooleanFunction()"

    def is_empty(self):
        return True


def _function(expression):
    """A gate library expression, or HAL's empty BooleanFunction if there is none."""
    return expression if expression else EmptyBooleanFunction()


class StubComponent(object):
    """A gate type component: any accessor given as a keyword becomes a method."""

    def __init__(self, component_type, **accessors):
        self._type = component_type
        self._accessors = {key: value for key, value in accessors.items() if value is not None}

    def get_type(self):
        return self._type

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._accessors:
            value = self._accessors[name]
            return lambda: value
        raise AttributeError(name)


class StubGateType(object):
    def __init__(self, name, properties, pins, components):
        self._name = name
        self._properties = list(properties)
        self._pins = list(pins)
        self._components = list(components)

    def get_name(self):
        return self._name

    def get_property_list(self):
        return list(self._properties)

    def get_pins(self):
        return list(self._pins)

    def get_components(self):
        return list(self._components)


class StubModule(object):
    def __init__(self, module_id, name):
        self._id = module_id
        self._name = name

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name


class StubEndpoint(object):
    def __init__(self, gate, pin):
        self._gate = gate
        self._pin = pin

    def get_gate(self):
        return self._gate

    def get_pin(self):
        return self._pin


class StubNet(object):
    def __init__(self, net_id, name):
        self._id = net_id
        self._name = name
        self.sources = []
        self.destinations = []

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_sources(self):
        return list(self.sources)

    def get_destinations(self):
        return list(self.destinations)


class StubGate(object):
    def __init__(self, gate_id, name, gate_type, module, init_data=None, data_map=None):
        self._id = gate_id
        self._name = name
        self._type = gate_type
        self._module = module
        self._init_data = list(init_data or [])
        self._data_map = dict(data_map or {})
        self.fan_in = {}
        self.fan_out = {}

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_type(self):
        return self._type

    def get_module(self):
        return self._module

    def get_init_data(self):
        return list(self._init_data)

    def get_data_map(self):
        return dict(self._data_map)

    def get_fan_in_net(self, pin_name):
        return self.fan_in.get(pin_name)

    def get_fan_out_net(self, pin_name):
        return self.fan_out.get(pin_name)

    def get_fan_in_nets(self):
        return list(self.fan_in.values())

    def get_fan_out_nets(self):
        return list(self.fan_out.values())


class StubGateLibrary(object):
    def __init__(self, name, path):
        self._name = name
        self._path = path

    def get_name(self):
        return self._name

    def get_path(self):
        return self._path


class StubNetlist(object):
    def __init__(self, library, design_name, device_name, input_filename):
        self._library = library
        self._design_name = design_name
        self._device_name = device_name
        self._input_filename = input_filename
        self.gates = []
        self.nets = []
        self.modules = []
        self.global_inputs = []
        self.global_outputs = []

    def get_id(self):
        return 1

    def get_design_name(self):
        return self._design_name

    def get_device_name(self):
        return self._device_name

    def get_input_filename(self):
        return self._input_filename

    def get_gate_library(self):
        return self._library

    def get_gates(self):
        return list(self.gates)

    def get_nets(self):
        return list(self.nets)

    def get_modules(self):
        return list(self.modules)

    def get_global_input_nets(self):
        return list(self.global_inputs)

    def get_global_output_nets(self):
        return list(self.global_outputs)


def _components_for(cell):
    """Translate the cell's config blocks into the components HAL would build."""
    components = []
    ff_config = cell.get("ff_config")
    lut_config = cell.get("lut_config")
    latch_config = cell.get("latch_config")
    ram_config = cell.get("ram_config")

    if ff_config:
        components.append(
            StubComponent(
                "ff",
                get_clock_function=_function(ff_config.get("clocked_on")),
                get_next_state_function=_function(ff_config.get("next_state")),
                get_async_reset_function=_function(ff_config.get("clear_on")),
                get_async_set_function=_function(ff_config.get("preset_on")),
                get_async_set_reset_behavior=(
                    list(ff_config["set_reset_behavior"])
                    if ff_config.get("set_reset_behavior")
                    else ["undef", "undef"]
                ),
            )
        )
        if ff_config.get("state") or ff_config.get("neg_state"):
            components.append(
                StubComponent(
                    "state",
                    get_state_identifier=ff_config.get("state"),
                    get_neg_state_identifier=ff_config.get("neg_state"),
                )
            )
        if ff_config.get("data_identifier"):
            components.append(
                StubComponent(
                    "init",
                    get_init_category=ff_config.get("data_category"),
                    get_init_identifier=[ff_config["data_identifier"]],
                )
            )
    if latch_config:
        components.append(
            StubComponent(
                "latch",
                get_data_in_function=_function(latch_config.get("data_in")),
                get_enable_function=_function(latch_config.get("enable_on")),
                get_async_reset_function=_function(latch_config.get("clear_on")),
                get_async_set_function=_function(latch_config.get("preset_on")),
                get_async_set_reset_behavior=(
                    list(latch_config["set_reset_behavior"])
                    if latch_config.get("set_reset_behavior")
                    else ["undef", "undef"]
                ),
            )
        )
    if lut_config:
        components.append(
            StubComponent(
                "lut",
                is_init_ascending=lut_config.get("bit_order") == "ascending",
            )
        )
        if lut_config.get("data_identifier"):
            components.append(
                StubComponent(
                    "init",
                    get_init_category=lut_config.get("data_category"),
                    get_init_identifier=[lut_config["data_identifier"]],
                )
            )
    if ram_config:
        components.append(
            StubComponent("ram", get_bit_size=ram_config.get("bit_size"))
        )
    return components


def load_library(path):
    """Return ``(library_name, {cell_name: StubGateType})`` for an ``.hgl`` file."""
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    gate_types = {}
    for cell in document.get("cells", []):
        pins = []
        for group in cell.get("pin_groups", []):
            for pin in group.get("pins", []):
                pins.append(
                    StubPin(pin["name"], pin.get("direction", "none"), pin.get("type", "none"))
                )
        gate_types[cell["name"]] = StubGateType(
            cell["name"], cell.get("types", []), pins, _components_for(cell)
        )
    return document.get("library", os.path.basename(path)), gate_types


def build_netlist(
    library_path,
    instances,
    inputs=(),
    outputs=(),
    inouts=(),
    design_name="stub_design",
    device_name="",
    input_filename="",
    init_data=None,
):
    """Assemble a stub netlist.

    :param instances: ``[(instance_name, cell_name, {pin_name: net_name})]``.
    :param inputs/outputs/inouts: net names that are top-level ports.
    :param init_data: ``{instance_name: [init strings]}``.
    """
    library_name, gate_types = load_library(library_path)
    netlist = StubNetlist(
        StubGateLibrary(library_name, library_path), design_name, device_name, input_filename
    )
    module = StubModule(1, "top_module")
    netlist.modules.append(module)

    nets = {}

    def net_for(name):
        if name not in nets:
            net = StubNet(len(nets) + 1, name)
            nets[name] = net
            netlist.nets.append(net)
        return nets[name]

    # create the port nets first so their ids follow the declaration order
    for name in list(inputs) + list(outputs) + list(inouts):
        net_for(name)

    for index, (instance_name, cell_name, connections) in enumerate(instances, start=1):
        if cell_name not in gate_types:
            raise KeyError(
                "cell {!r} is not in {}".format(cell_name, os.path.basename(library_path))
            )
        gate_type = gate_types[cell_name]
        gate = StubGate(
            index,
            instance_name,
            gate_type,
            module,
            init_data=(init_data or {}).get(instance_name),
        )
        directions = {pin.get_name(): pin.get_direction() for pin in gate_type.get_pins()}
        for pin_name, net_name in connections.items():
            if pin_name not in directions:
                raise KeyError(
                    "cell {} has no pin {!r}".format(cell_name, pin_name)
                )
            net = net_for(net_name)
            if directions[pin_name] == "output":
                gate.fan_out[pin_name] = net
                net.sources.append(StubEndpoint(gate, pin_name))
            elif directions[pin_name] == "inout":
                gate.fan_in[pin_name] = net
                gate.fan_out[pin_name] = net
                net.sources.append(StubEndpoint(gate, pin_name))
                net.destinations.append(StubEndpoint(gate, pin_name))
            else:
                gate.fan_in[pin_name] = net
                net.destinations.append(StubEndpoint(gate, pin_name))
        netlist.gates.append(gate)

    for name in list(inputs) + list(inouts):
        netlist.global_inputs.append(net_for(name))
    for name in list(outputs) + list(inouts):
        netlist.global_outputs.append(net_for(name))
    return netlist
