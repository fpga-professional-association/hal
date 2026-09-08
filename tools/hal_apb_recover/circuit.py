"""A three-valued netlist model shared by the offline and the ``hal_py`` path.

Values are ``0``, ``1`` and :data:`X` (``None``) for *unconstrained*.  ``X`` is
not "unknown because we were lazy": it is the sound abstraction the recovery
leans on.  When a next-state function evaluates to a definite value while every
other input is ``X``, the value holds for **every** assignment of those inputs,
which is exactly the difference between a claim that needs an environment
assumption and one that does not.

The model carries no HAL types.  A gate is a key, a type name, a pin->net map
per direction and either

* ``functions``: one callable per output pin, or
* ``ff``: the four flip-flop functions (next state, clock, async reset, async
  set), or
* nothing at all, in which case the gate is explicitly *unsupported* and its
  outputs stay ``X`` -- a latch or a RAM is reported, never guessed at.
"""

X = None

KIND_COMBINATIONAL = "combinational"
KIND_FF = "ff"
KIND_UNSUPPORTED = "unsupported"

__all__ = [
    "X",
    "KIND_COMBINATIONAL",
    "KIND_FF",
    "KIND_UNSUPPORTED",
    "FlipFlop",
    "Gate",
    "Circuit",
    "StateElement",
    "CircuitError",
    "value_to_str",
]


class CircuitError(RuntimeError):
    """The circuit model could not be built or is inconsistent."""


def value_to_str(value):
    return "x" if value is None else str(value)


class FlipFlop(object):
    """The four functions of an edge-triggered flip-flop, over input pin names."""

    __slots__ = (
        "next_state",
        "clock",
        "async_reset",
        "async_set",
        "state_pins",
        "neg_state_pins",
        "clock_pins",
        "clear_preset_state",
    )

    def __init__(
        self,
        next_state,
        clock=None,
        async_reset=None,
        async_set=None,
        state_pins=(),
        neg_state_pins=(),
        clock_pins=(),
        clear_preset_state=None,
    ):
        self.next_state = next_state
        self.clock = clock
        self.async_reset = async_reset
        self.async_set = async_set
        self.state_pins = tuple(state_pins)
        self.neg_state_pins = tuple(neg_state_pins)
        self.clock_pins = tuple(clock_pins)
        self.clear_preset_state = clear_preset_state


class Gate(object):
    """One netlist gate together with the way its outputs are computed."""

    __slots__ = (
        "key",
        "uid",
        "name",
        "type_name",
        "kind",
        "inputs",
        "outputs",
        "functions",
        "ff",
        "unsupported_reason",
        "properties",
        "_input_pins",
        "_cache",
        "_ff_cache",
    )

    def __init__(
        self,
        key,
        name,
        type_name,
        kind,
        inputs,
        outputs,
        functions=None,
        ff=None,
        unsupported_reason=None,
        properties=(),
        uid=None,
    ):
        self.key = key
        self.uid = uid
        self.name = name
        self.type_name = type_name
        self.kind = kind
        self.inputs = dict(inputs)
        self.outputs = dict(outputs)
        self.functions = dict(functions or {})
        self.ff = ff
        self.unsupported_reason = unsupported_reason
        self.properties = tuple(sorted(properties))
        self._input_pins = tuple(sorted(self.inputs))
        self._cache = {}
        self._ff_cache = {}

        if kind == KIND_FF and ff is None:
            raise CircuitError("flip-flop gate {!r} has no FF description".format(key))
        if kind == KIND_UNSUPPORTED and not unsupported_reason:
            raise CircuitError("unsupported gate {!r} needs an explicit reason".format(key))

    def _key(self, pin_values):
        return tuple(pin_values.get(pin) for pin in self._input_pins)

    def evaluate(self, pin_values):
        """Return ``{output_pin: value}`` for a combinational gate."""
        cache_key = self._key(pin_values)
        cached = self._cache.get(cache_key)
        if cached is None:
            cached = {pin: function(pin_values) for pin, function in self.functions.items()}
            self._cache[cache_key] = cached
        return cached

    def evaluate_ff(self, pin_values):
        """Return ``{next_state, clock, async_reset, async_set}`` for a flip-flop."""
        cache_key = self._key(pin_values)
        cached = self._ff_cache.get(cache_key)
        if cached is None:
            ff = self.ff
            cached = {
                "next_state": ff.next_state(pin_values) if ff.next_state else X,
                "clock": ff.clock(pin_values) if ff.clock else X,
                "async_reset": ff.async_reset(pin_values) if ff.async_reset else 0,
                "async_set": ff.async_set(pin_values) if ff.async_set else 0,
            }
            self._ff_cache[cache_key] = cached
        return cached

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<Gate {} {} {}>".format(self.key, self.type_name, self.kind)


class StateElement(object):
    """A flip-flop seen as one bit of netlist state."""

    __slots__ = ("gate", "state_net", "neg_state_net")

    def __init__(self, gate, state_net, neg_state_net=None):
        self.gate = gate
        self.state_net = state_net
        self.neg_state_net = neg_state_net

    @property
    def key(self):
        return self.gate.key

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<StateElement {} -> {}>".format(self.gate.key, self.state_net)


def _invert(value):
    return X if value is None else 1 - value


class Circuit(object):
    """A flattened, combinationally acyclic netlist with state elements."""

    def __init__(self, name, gates, net_names, input_nets, output_nets, source=None,
                 gate_library=None, net_uids=None):
        self.name = name
        self.source = source
        self.gate_library = gate_library
        self.gates = list(gates)
        self.gate_by_key = {gate.key: gate for gate in self.gates}
        if len(self.gate_by_key) != len(self.gates):
            raise CircuitError("duplicate gate keys in circuit {!r}".format(name))
        # Gate and net ids are what the findings schema references objects by.
        # A reader that has no ids of its own (the offline Verilog reader) gets
        # reader-local ones, which the findings document says so explicitly.
        for index, gate in enumerate(self.gates, start=1):
            if gate.uid is None:
                gate.uid = index
        self.net_names = dict(net_names)
        self.net_uids = dict(net_uids or {})
        for index, net in enumerate(sorted(self.net_names), start=1):
            self.net_uids.setdefault(net, index)
        self.input_nets = list(input_nets)
        self.output_nets = list(output_nets)

        self.name_to_net = {}
        for net, net_name in self.net_names.items():
            self.name_to_net.setdefault(net_name, net)

        self.state_elements = self._collect_state_elements()
        self.state_by_key = {element.key: element for element in self.state_elements}
        self.unsupported_gates = [
            gate for gate in self.gates if gate.kind == KIND_UNSUPPORTED
        ]
        self.driver_of = {}
        for gate in self.gates:
            for pin, net in gate.outputs.items():
                self.driver_of.setdefault(net, (gate.key, pin))

        self.eval_order, self.combinational_loop = self._build_order()

    # -- construction -------------------------------------------------------

    def _collect_state_elements(self):
        elements = []
        for gate in self.gates:
            if gate.kind != KIND_FF:
                continue
            state_net = None
            neg_state_net = None
            for pin in gate.ff.state_pins:
                if pin in gate.outputs:
                    state_net = gate.outputs[pin]
                    break
            for pin in gate.ff.neg_state_pins:
                if pin in gate.outputs:
                    neg_state_net = gate.outputs[pin]
                    break
            if state_net is None and neg_state_net is None:
                # A flip-flop whose outputs are all dangling still holds state,
                # but it cannot be observed; keep it out of the state vector.
                continue
            elements.append(StateElement(gate, state_net, neg_state_net))
        elements.sort(key=lambda element: element.key)
        return elements

    def _build_order(self):
        """Levelize the combinational gates; report the ones inside loops."""
        source_nets = set(self.input_nets)
        for element in self.state_elements:
            if element.state_net is not None:
                source_nets.add(element.state_net)
            if element.neg_state_net is not None:
                source_nets.add(element.neg_state_net)
        for gate in self.gates:
            if gate.kind in (KIND_FF, KIND_UNSUPPORTED):
                source_nets.update(gate.outputs.values())

        pending = [gate for gate in self.gates if gate.kind == KIND_COMBINATIONAL]
        ready = set(source_nets)
        # Nets nobody drives are sources too (dangling inputs evaluate to X).
        driven = set()
        for gate in pending:
            driven.update(gate.outputs.values())
        for gate in self.gates:
            for net in gate.inputs.values():
                if net not in driven:
                    ready.add(net)

        order = []
        remaining = list(pending)
        while remaining:
            progressed = []
            blocked = []
            for gate in remaining:
                if all(net in ready for net in gate.inputs.values()):
                    progressed.append(gate)
                else:
                    blocked.append(gate)
            if not progressed:
                # every remaining gate sits on (or behind) a combinational loop
                return order, sorted(gate.key for gate in blocked)
            for gate in progressed:
                order.append(gate)
                ready.update(gate.outputs.values())
            remaining = blocked
        return order, []

    # -- lookup -------------------------------------------------------------

    def resolve_net(self, reference):
        """Resolve a mapping reference (``"name"`` or ``"id:7"``) to a net key."""
        if reference is None:
            return None
        text = str(reference)
        if text in self.net_names:
            return text
        if text.startswith("id:"):
            candidate = "n" + text[3:]
            if candidate in self.net_names:
                return candidate
            return None
        return self.name_to_net.get(text)

    def net_name(self, net):
        return self.net_names.get(net, str(net))

    # -- evaluation ---------------------------------------------------------

    def evaluate(self, net_values):
        """Propagate ``net_values`` through the combinational cone.

        ``net_values`` holds the primary inputs and the state nets; every net
        that is absent (or mapped to :data:`X`) is unconstrained.
        """
        values = dict(net_values)
        for gate in self.eval_order:
            pin_values = {pin: values.get(net) for pin, net in gate.inputs.items()}
            outputs = gate.evaluate(pin_values)
            for pin, value in outputs.items():
                net = gate.outputs.get(pin)
                if net is not None:
                    values[net] = value
        return values

    def state_vector_nets(self, state_bits):
        """Expand ``{gate_key: value}`` into ``{net: value}`` for Q and QN."""
        nets = {}
        for key, value in state_bits.items():
            element = self.state_by_key.get(key)
            if element is None:
                raise CircuitError("no state element {!r}".format(key))
            if element.state_net is not None:
                nets[element.state_net] = value
            if element.neg_state_net is not None:
                nets[element.neg_state_net] = _invert(value)
        return nets

    def next_state(self, values, elements=None):
        """Next value of each state element given already-propagated ``values``."""
        result = {}
        for element in elements if elements is not None else self.state_elements:
            gate = element.gate
            pin_values = {pin: values.get(net) for pin, net in gate.inputs.items()}
            result[element.key] = gate.evaluate_ff(pin_values)["next_state"]
        return result

    def asynchronous_values(self, values, elements=None):
        """Async reset/set verdict per state element: ``0``, ``1`` or :data:`X`."""
        result = {}
        for element in elements if elements is not None else self.state_elements:
            gate = element.gate
            pin_values = {pin: values.get(net) for pin, net in gate.inputs.items()}
            evaluated = gate.evaluate_ff(pin_values)
            reset, preset = evaluated["async_reset"], evaluated["async_set"]
            if reset == 1 and preset == 1:
                result[element.key] = gate.ff.clear_preset_state
            elif reset == 1:
                result[element.key] = 0
            elif preset == 1:
                result[element.key] = 1
            elif reset == 0 and preset == 0:
                result[element.key] = "none"
            else:
                result[element.key] = X
        return result

    def clock_values(self, values, elements=None):
        result = {}
        for element in elements if elements is not None else self.state_elements:
            gate = element.gate
            pin_values = {pin: values.get(net) for pin, net in gate.inputs.items()}
            result[element.key] = gate.evaluate_ff(pin_values)["clock"]
        return result

    def clock_net(self, element):
        """Net feeding the clock pin of ``element`` (``None`` if not identifiable).

        The clock pin is taken from the gate type's pin *types*, never guessed
        from a pin name: a design whose clock pin is called something else must
        still be analysed correctly, and a gate type without a typed clock pin
        must be reported rather than assumed.
        """
        gate = element.gate
        nets = [gate.inputs[pin] for pin in gate.ff.clock_pins if pin in gate.inputs]
        if len(nets) == 1:
            return nets[0]
        return None

    def statistics(self):
        return {
            "gates": len(self.gates),
            "nets": len(self.net_names),
            "inputs": len(self.input_nets),
            "outputs": len(self.output_nets),
            "flip_flops": len(self.state_elements),
            "unsupported_gates": len(self.unsupported_gates),
            "combinational_loop_gates": len(self.combinational_loop),
        }
