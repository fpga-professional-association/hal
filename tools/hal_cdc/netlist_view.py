"""A plain-data view of a netlist.

Every other module in ``hal_cdc`` works on a :class:`NetlistView`, never on
``hal_py`` objects.  That buys three things:

* the analysis is unit-testable on a machine that cannot build HAL (the
  fixtures are read by :mod:`hal_cdc.fixture_netlist` into the same view);
* one place has to be audited when a binding is renamed;
* the view is immutable and hashable-by-id, so the analysis is deterministic.

Gate-type semantics are never guessed.  ``properties`` comes straight from
``GateType.get_property_list()`` and pin types straight from
``GatePin.get_type()``; the predicates below only rename them.
"""

__all__ = [
    "PinView",
    "GateTypeView",
    "GateView",
    "NetView",
    "NetlistView",
    "CLOCK_TRANSPARENT_PROPERTIES",
    "CONTROL_PIN_TYPES",
    "from_hal_netlist",
]

#: Gate-type properties whose gates pass a clock through unchanged (up to
#: inversion).  Anything else on a clock path is a *generated* clock.
CLOCK_TRANSPARENT_PROPERTIES = frozenset(("c_buffer", "c_inverter"))

#: Pin types that carry control rather than data into a sequential gate.
CONTROL_PIN_TYPES = frozenset(("enable", "set", "reset", "select", "control"))


class PinView(object):
    """One pin of a gate type: name, direction and pin type, as HAL reports them."""

    __slots__ = ("name", "direction", "type")

    def __init__(self, name, direction, pin_type):
        self.name = str(name)
        self.direction = str(direction)
        self.type = str(pin_type)

    def __repr__(self):  # pragma: no cover - debugging aid
        return "PinView({!r}, {!r}, {!r})".format(self.name, self.direction, self.type)


class GateTypeView(object):
    """A gate type reduced to what a structural CDC screen needs."""

    __slots__ = ("name", "properties", "pins", "_by_name")

    def __init__(self, name, properties, pins):
        self.name = str(name)
        self.properties = frozenset(str(entry) for entry in properties)
        self.pins = tuple(pins)
        self._by_name = {pin.name: pin for pin in self.pins}

    # -- predicates ------------------------------------------------------

    @property
    def is_sequential(self):
        return "sequential" in self.properties

    @property
    def is_ff(self):
        return "ff" in self.properties

    @property
    def is_latch(self):
        return "latch" in self.properties

    @property
    def is_combinational(self):
        return "combinational" in self.properties

    @property
    def is_constant(self):
        return bool(self.properties & {"power", "ground"})

    @property
    def is_clock_transparent(self):
        return bool(self.properties & CLOCK_TRANSPARENT_PROPERTIES)

    @property
    def is_black_box(self):
        """True for a gate type HAL classifies as neither combinational nor sequential."""
        return not (self.is_combinational or self.is_sequential or self.is_constant)

    # -- pins ------------------------------------------------------------

    def pin(self, name):
        return self._by_name.get(name)

    def pins_of_type(self, pin_type, direction="input"):
        return tuple(
            pin for pin in self.pins if pin.type == pin_type and pin.direction == direction
        )

    def input_pins(self):
        return tuple(pin for pin in self.pins if pin.direction == "input")

    def output_pins(self):
        return tuple(pin for pin in self.pins if pin.direction == "output")

    def __repr__(self):  # pragma: no cover - debugging aid
        return "GateTypeView({!r})".format(self.name)


class GateView(object):
    """A gate instance with its pin-to-net wiring resolved in both directions."""

    __slots__ = ("id", "name", "type", "fan_in", "fan_out", "module_id", "module_name")

    def __init__(self, gate_id, name, gate_type, fan_in, fan_out, module_id=None,
                 module_name=None):
        self.id = int(gate_id)
        self.name = str(name)
        self.type = gate_type
        #: pin name -> net id
        self.fan_in = dict(fan_in)
        #: pin name -> net id
        self.fan_out = dict(fan_out)
        self.module_id = module_id
        self.module_name = module_name

    def fan_in_net(self, pin_name):
        return self.fan_in.get(pin_name)

    def clock_pins(self):
        return self.type.pins_of_type("clock")

    def data_input_nets(self):
        """(pin, net id) for every connected input pin that is not the clock."""
        result = []
        for pin in self.type.input_pins():
            if pin.type == "clock":
                continue
            net_id = self.fan_in.get(pin.name)
            if net_id is not None:
                result.append((pin, net_id))
        return result

    def output_nets(self):
        return sorted({net for net in self.fan_out.values() if net is not None})

    def __repr__(self):  # pragma: no cover - debugging aid
        return "GateView({}, {!r}, {!r})".format(self.id, self.name, self.type.name)


class NetView(object):
    """A net with its driver and load endpoints as ``(gate id, pin name)`` pairs."""

    __slots__ = ("id", "name", "sources", "destinations", "is_global_input",
                 "is_global_output", "is_gnd", "is_vcc")

    def __init__(self, net_id, name, sources=(), destinations=(), is_global_input=False,
                 is_global_output=False, is_gnd=False, is_vcc=False):
        self.id = int(net_id)
        self.name = str(name)
        self.sources = tuple(sources)
        self.destinations = tuple(destinations)
        self.is_global_input = bool(is_global_input)
        self.is_global_output = bool(is_global_output)
        self.is_gnd = bool(is_gnd)
        self.is_vcc = bool(is_vcc)

    @property
    def is_constant(self):
        return self.is_gnd or self.is_vcc or self.name in ("'0'", "'1'")

    def __repr__(self):  # pragma: no cover - debugging aid
        return "NetView({}, {!r})".format(self.id, self.name)


class NetlistView(object):
    """Immutable snapshot of the netlist the audit runs on."""

    def __init__(self, gates, nets, design_name=None, input_filename=None,
                 gate_library_name=None, gate_library_path=None, netlist_id=None,
                 device_name=None):
        self.gates = {gate.id: gate for gate in gates}
        self.nets = {net.id: net for net in nets}
        self.design_name = design_name
        self.input_filename = input_filename
        self.gate_library_name = gate_library_name
        self.gate_library_path = gate_library_path
        self.netlist_id = netlist_id
        self.device_name = device_name

        self._gates_by_name = {}
        for gate in self.gates.values():
            self._gates_by_name.setdefault(gate.name, []).append(gate)
        self._nets_by_name = {}
        for net in self.nets.values():
            self._nets_by_name.setdefault(net.name, []).append(net)

    # -- lookup ----------------------------------------------------------

    def gate(self, gate_id):
        return self.gates.get(gate_id)

    def net(self, net_id):
        return self.nets.get(net_id)

    def gate_by_name(self, name):
        matches = self._gates_by_name.get(name, [])
        return matches[0] if len(matches) == 1 else None

    def net_by_name(self, name):
        matches = self._nets_by_name.get(name, [])
        return matches[0] if len(matches) == 1 else None

    def net_names(self):
        return sorted(self._nets_by_name)

    def is_constant_net(self, net_id):
        """True for a net that carries a constant and therefore no clock domain.

        Both spellings count: a net HAL marked as the GND/VCC net, and a net
        driven only by gates whose type has the ``power``/``ground`` property.
        Structural parsers differ on which one they produce, and treating a tie
        cell's output as a domain source would invent crossings that are not
        there.
        """
        net = self.nets.get(net_id)
        if net is None:
            return True
        if net.is_constant:
            return True
        if not net.sources:
            return False
        for gate_id, _pin in net.sources:
            gate = self.gates.get(gate_id)
            if gate is None or not gate.type.is_constant:
                return False
        return True

    # -- ordered iteration (determinism) ---------------------------------

    def sorted_gates(self):
        return [self.gates[key] for key in sorted(self.gates)]

    def sorted_nets(self):
        return [self.nets[key] for key in sorted(self.nets)]

    def sequential_gates(self):
        return [gate for gate in self.sorted_gates() if gate.type.is_sequential]

    def gate_type_histogram(self):
        histogram = {}
        for gate in self.gates.values():
            histogram[gate.type.name] = histogram.get(gate.type.name, 0) + 1
        return histogram

    def gate_types(self):
        seen = {}
        for gate in self.sorted_gates():
            seen.setdefault(gate.type.name, gate.type)
        return seen

    def __repr__(self):  # pragma: no cover - debugging aid
        return "NetlistView({} gates, {} nets)".format(len(self.gates), len(self.nets))


# ---------------------------------------------------------------------------
# hal_py -> NetlistView
# ---------------------------------------------------------------------------


def _enum_name(value):
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value).rsplit(".", 1)[-1]


def _call(obj, name, *args, **kwargs):
    default = kwargs.pop("default", None)
    method = getattr(obj, name, None)
    if method is None:
        return default
    try:
        value = method(*args, **kwargs)
    except Exception:
        return default
    return default if value is None else value


def _gate_type_view(gate_type, cache):
    name = _call(gate_type, "get_name", default="<unnamed>")
    cached = cache.get(name)
    if cached is not None:
        return cached

    properties = _call(gate_type, "get_property_list", default=None)
    if properties is None:
        properties = _call(gate_type, "get_properties", default=[])
    pins = []
    for pin in _call(gate_type, "get_pins", default=[]) or []:
        pins.append(
            PinView(
                _call(pin, "get_name", default=""),
                _enum_name(_call(pin, "get_direction", default="none")),
                _enum_name(_call(pin, "get_type", default="none")),
            )
        )
    view = GateTypeView(name, (_enum_name(entry) for entry in properties), pins)
    cache[name] = view
    return view


def from_hal_netlist(netlist):
    """Build a :class:`NetlistView` from a ``hal_py.Netlist``.

    Only the accessors listed in the module docstring of ``hal_cdc`` are used,
    and each one goes through :func:`_call`, so a binding that disappears
    degrades into a missing field rather than a traceback in the middle of an
    analysis.
    """
    type_cache = {}

    gates = []
    for gate in _call(netlist, "get_gates", default=[]) or []:
        gate_type = _gate_type_view(_call(gate, "get_type"), type_cache)
        fan_in, fan_out = {}, {}
        for endpoint in _call(gate, "get_fan_in_endpoints", default=[]) or []:
            pin = _call(endpoint, "get_pin")
            net = _call(endpoint, "get_net")
            if pin is None or net is None:
                continue
            fan_in[_call(pin, "get_name", default="")] = _call(net, "get_id")
        for endpoint in _call(gate, "get_fan_out_endpoints", default=[]) or []:
            pin = _call(endpoint, "get_pin")
            net = _call(endpoint, "get_net")
            if pin is None or net is None:
                continue
            fan_out[_call(pin, "get_name", default="")] = _call(net, "get_id")
        module = _call(gate, "get_module")
        gates.append(
            GateView(
                _call(gate, "get_id", default=0),
                _call(gate, "get_name", default=""),
                gate_type,
                fan_in,
                fan_out,
                module_id=_call(module, "get_id") if module is not None else None,
                module_name=_call(module, "get_name") if module is not None else None,
            )
        )

    nets = []
    for net in _call(netlist, "get_nets", default=[]) or []:
        sources, destinations = [], []
        for endpoint in _call(net, "get_sources", default=[]) or []:
            gate = _call(endpoint, "get_gate")
            pin = _call(endpoint, "get_pin")
            if gate is None or pin is None:
                continue
            sources.append((_call(gate, "get_id"), _call(pin, "get_name", default="")))
        for endpoint in _call(net, "get_destinations", default=[]) or []:
            gate = _call(endpoint, "get_gate")
            pin = _call(endpoint, "get_pin")
            if gate is None or pin is None:
                continue
            destinations.append((_call(gate, "get_id"), _call(pin, "get_name", default="")))
        nets.append(
            NetView(
                _call(net, "get_id", default=0),
                _call(net, "get_name", default=""),
                sources=sources,
                destinations=destinations,
                is_global_input=bool(_call(net, "is_global_input_net", default=False)),
                is_global_output=bool(_call(net, "is_global_output_net", default=False)),
                is_gnd=bool(_call(net, "is_gnd_net", default=False)),
                is_vcc=bool(_call(net, "is_vcc_net", default=False)),
            )
        )

    library = _call(netlist, "get_gate_library")
    return NetlistView(
        gates,
        nets,
        design_name=_call(netlist, "get_design_name"),
        input_filename=_call(netlist, "get_input_filename"),
        gate_library_name=_call(library, "get_name") if library is not None else None,
        gate_library_path=(
            str(_call(library, "get_path", default="")) or None if library is not None else None
        ),
        netlist_id=_call(netlist, "get_id"),
        device_name=_call(netlist, "get_device_name"),
    )
