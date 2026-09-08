"""A reference simulator for the covered Agilex primitives.

The point of this module is validation, not speed.  It evaluates a parsed
Quartus export (:mod:`vo_netlist`) using only the semantics stated in
:mod:`primitives`, so simulating an exported netlist and comparing it against
the behaviour of the RTL it was synthesised from is a direct test of those
semantics: if the ``lut_mask`` decoding or the flip-flop model were wrong, the
comparison would fail.

Only ``tennm_lcell_comb`` and ``tennm_ff`` are executable.  Any other instance
type, and any instance outside the validated configuration, makes
:func:`build` raise -- the simulator never invents behaviour for a primitive it
does not model.
"""

from . import primitives
from .vo_netlist import Bit, Const, VerilogSubsetError

__all__ = ["SimulationError", "Simulator", "build"]


class SimulationError(Exception):
    """The netlist cannot be simulated with the covered primitive set."""


def _key(bit):
    return bit.key


class Simulator(object):
    """Evaluates one parsed netlist cycle by cycle.

    ``inputs`` are driven by name (``"addend"`` for a vector, ``"clk"`` for a
    scalar); ``state`` holds one bit per ``tennm_ff``.  Combinational
    evaluation is a fixed-point iteration over the instances, which terminates
    because the covered cells form an acyclic network once the registers are
    cut.
    """

    def __init__(self, netlist):
        self.netlist = netlist
        self.state = {}
        self.values = {}
        self._alias = {}
        self._constants = {}
        self._inputs = {}
        self._settled = False
        self._prepare()

    # -- preparation ------------------------------------------------------

    def _prepare(self):
        for target, source in self.netlist.assignments:
            for target_bit, source_bit in zip(target, source):
                if not isinstance(target_bit, Bit):
                    raise SimulationError("assignment to a constant is not valid Verilog")
                if isinstance(source_bit, Const):
                    if source_bit.value is None:
                        self._constants[_key(target_bit)] = None
                    else:
                        self._constants[_key(target_bit)] = source_bit.value
                else:
                    self._alias[_key(target_bit)] = source_bit

        for instance in self.netlist.instances:
            if instance.type not in primitives.COVERED_PRIMITIVES:
                raise SimulationError(
                    "instance {} is of type {}, which this package does not model; "
                    "run the coverage inventory instead of simulating".format(
                        instance.name, instance.type
                    )
                )
            self._check(instance)

        self.reset()

    def _driven_and_constant(self, instance, pins):
        driven = set()
        constants = {}
        for pin in pins:
            bits = instance.connections.get(pin)
            if not bits:
                continue
            bit = instance.single(pin)
            if isinstance(bit, Const):
                constants[pin] = bit.value
                continue
            resolved = self._resolve_constant(bit)
            if resolved is not None:
                constants[pin] = resolved
            else:
                driven.add(pin)
        return driven, constants

    def _resolve_constant(self, bit, depth=0):
        """Follow ``assign`` aliases to a constant, or return ``None``."""
        if depth > 32:
            return None
        key = _key(bit)
        if key in self._constants:
            value = self._constants[key]
            if value is None:
                return None
            return 1 - value if bit.inverted else value
        alias = self._alias.get(key)
        if alias is None:
            return None
        value = self._resolve_constant(alias, depth + 1)
        if value is None:
            return None
        if alias.inverted:
            value = 1 - value
        return 1 - value if bit.inverted else value

    def _check(self, instance):
        if instance.type == primitives.LCELL:
            driven, constants = self._driven_and_constant(
                instance, primitives.LCELL_INPUT_PINS
            )
            uses_arithmetic = any(
                instance.connections.get(pin) for pin in ("sumout", "cout")
            )
            try:
                primitives.check_lcell_configuration(
                    instance.parameters, driven, uses_arithmetic, constants
                )
            except primitives.UnsupportedConfiguration as exc:
                raise SimulationError("{}: {}".format(instance.name, exc))
            if instance.connections.get("shareout"):
                raise SimulationError(
                    "{}: shareout is connected; the shared arithmetic mode is not "
                    "modelled".format(instance.name)
                )
            if "lut_mask" not in instance.parameters:
                raise SimulationError("{}: no lut_mask parameter".format(instance.name))
        else:
            driven, constants = self._driven_and_constant(instance, primitives.FF_INPUT_PINS)
            try:
                primitives.check_ff_configuration(driven, constants)
            except primitives.UnsupportedConfiguration as exc:
                raise SimulationError("{}: {}".format(instance.name, exc))

    # -- driving ----------------------------------------------------------

    def reset(self):
        """Clear every register and every evaluated value."""
        self.state = {instance.name: 0 for instance in self.netlist.instances_of_type(primitives.FF)}
        self.values = {}
        self._settled = False

    def set_input(self, name, value, width=None):
        """Drive a declared input by name; vectors take an integer value."""
        if name not in self.netlist.declarations:
            raise SimulationError("no signal named {!r} in {}".format(name, self.netlist.name))
        bits = self.netlist.bits_of(name)
        if len(bits) == 1 and self.netlist.declarations[name][1] is None:
            self._inputs[bits[0].key] = int(value) & 1
        else:
            span = width if width is not None else len(bits)
            for position, bit in enumerate(bits):
                self._inputs[bit.key] = (int(value) >> position) & 1 if position < span else 0
        self._settled = False

    def get_output(self, name):
        """Read a declared signal by name; vectors come back as an integer."""
        if not self._settled:
            self.settle()
        bits = self.netlist.bits_of(name)
        if len(bits) == 1 and self.netlist.declarations.get(name, (None, None, None))[1] is None:
            return self._read(bits[0])
        value = 0
        for position, bit in enumerate(bits):
            value |= self._read(bit) << position
        return value

    # -- evaluation -------------------------------------------------------

    def _read(self, bit, depth=0):
        if isinstance(bit, Const):
            if bit.value is None:
                raise SimulationError("an x/z literal reached the simulator")
            return bit.value
        key = _key(bit)
        value = self.values.get(key)
        if value is None:
            if key in self._constants:
                value = self._constants[key]
                if value is None:
                    raise SimulationError("net {} is driven by an x/z literal".format(key))
            elif key in self._alias:
                if depth > 64:
                    raise SimulationError("assignment loop while reading {}".format(key))
                alias = self._alias[key]
                value = self._read(alias, depth + 1)
                if alias.inverted:
                    value = 1 - value
            else:
                raise SimulationError(
                    "net {} has no driver and was not set as an input".format(key)
                )
            self.values[key] = value
        return 1 - value if bit.inverted else value

    def _input_values(self, instance, pins):
        values = {}
        for pin in pins:
            bits = instance.connections.get(pin)
            if not bits:
                values[pin] = 0
                continue
            values[pin] = self._read(instance.single(pin))
        return values

    def _evaluate_combinational(self):
        pending = list(self.netlist.instances_of_type(primitives.LCELL))
        progress = True
        while pending and progress:
            progress = False
            still_pending = []
            for instance in pending:
                try:
                    values = self._input_values(instance, primitives.LCELL_DATA_PINS)
                    cin_bit = instance.single("cin")
                    cin = self._read(cin_bit) if cin_bit is not None else 0
                except SimulationError:
                    still_pending.append(instance)
                    continue
                mask = instance.parameters["lut_mask"]
                if instance.connections.get("combout"):
                    self._write(instance, "combout", primitives.combout(mask, values))
                if instance.connections.get("sumout") or instance.connections.get("cout"):
                    sumout, cout = primitives.arithmetic_outputs(mask, values, cin)
                    if instance.connections.get("sumout"):
                        self._write(instance, "sumout", sumout)
                    if instance.connections.get("cout"):
                        self._write(instance, "cout", cout)
                progress = True
            pending = still_pending
        if pending:
            raise SimulationError(
                "could not evaluate {} combinational cells; the netlist has a "
                "combinational loop or an undriven input: {}".format(
                    len(pending), ", ".join(instance.name for instance in pending[:5])
                )
            )

    def _write(self, instance, pin, value):
        bit = instance.single(pin)
        if bit is None:
            return
        if isinstance(bit, Const):
            raise SimulationError(
                "{}.{} drives a constant".format(instance.name, pin)
            )
        if bit.inverted:
            raise SimulationError(
                "{}.{} drives an inverted expression, which is not valid "
                "Verilog".format(instance.name, pin)
            )
        self.values[_key(bit)] = value

    def _drive_registers(self):
        for instance in self.netlist.instances_of_type(primitives.FF):
            self._write(instance, "q", self.state[instance.name])

    def settle(self):
        """Propagate the current inputs and register state through the logic.

        Every derived value is discarded first, so a settle after new inputs
        can never return a value left over from the previous vector.
        """
        self.values = dict(self._inputs)
        self._drive_registers()
        self._evaluate_combinational()
        self._settled = True

    def clock(self):
        """One rising clock edge: settle, then update every register."""
        if not self._settled:
            self.settle()
        next_state = {}
        for instance in self.netlist.instances_of_type(primitives.FF):
            clrn_bit = instance.single("clrn")
            clrn = self._read(clrn_bit) if clrn_bit is not None else 1
            if clrn == 0:
                next_state[instance.name] = 0
                continue
            data_bit = instance.single("d")
            data = self._read(data_bit) if data_bit is not None else 0
            enable_bit = instance.single("ena")
            enable = self._read(enable_bit) if enable_bit is not None else 1
            next_state[instance.name] = primitives.ff_next_state(
                self.state[instance.name], data, enable
            )
        self.state = next_state
        self._settled = False

    def apply_async_clear(self):
        """Apply the asynchronous, active-low clear of every register now."""
        if not self._settled:
            self.settle()
        for instance in self.netlist.instances_of_type(primitives.FF):
            clrn_bit = instance.single("clrn")
            if clrn_bit is None:
                continue
            if self._read(clrn_bit) == 0:
                self.state[instance.name] = 0
        self._settled = False


def build(netlist):
    """Create a :class:`Simulator`, raising on anything outside the coverage."""
    try:
        return Simulator(netlist)
    except VerilogSubsetError as exc:
        raise SimulationError(str(exc))
