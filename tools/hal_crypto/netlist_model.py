"""A combinational-cone view of a parsed Agilex ``.vo`` export.

Every recognition pass in this package asks the same two questions about a
netlist -- *which registers and inputs does this net depend on*, and *what
function of them is it* -- so both are answered once, here, on top of the
reader and the primitive semantics that ``tools/hal_agilex`` already validates.
This module imports that package; it deliberately does not re-implement any of
it, because a second ``.vo`` reader is a second chance to be wrong about what
the vendor wrote.

The evaluation is exact, not sampled.  A cone over *n* sources is held as one
Python integer with ``2**n`` bits -- bit *j* is the cone's output under
assignment *j* of the sources -- so a whole truth table is produced with a
handful of big-integer operations per cell instead of ``2**n`` interpreted
passes.  :data:`MAX_CONE_SOURCES` bounds *n*; a wider cone is reported as
out of reach rather than approximated.

What counts as a *source*:

* the ``q`` output of a ``tennm_ff`` (a register boundary cuts the cone),
* a bit of a module input port,
* any net with no driver at all (an open input is a free variable, never 0).

Constants -- literals, ``gnd``/``vcc`` aliases, and ``tri0``/``tri1`` nets --
are folded into the cone, not treated as sources.
"""

from hal_agilex import primitives
from hal_agilex.vo_netlist import Bit, Const

from .boolfunc import TruthTable

__all__ = [
    "MAX_CONE_SOURCES",
    "ConeTooWide",
    "UnsupportedCell",
    "Cone",
    "NetlistModel",
]

#: Widest cone the exact evaluation will build.  ``2**16`` columns is a 64 kbit
#: integer per net, which is fast; every doubling beyond that costs memory and
#: time for no analysis this package performs.
MAX_CONE_SOURCES = 16

_DATA_PINS = primitives.LCELL_DATA_PINS


class ConeTooWide(Exception):
    """The cone has more sources than :data:`MAX_CONE_SOURCES`."""


class UnsupportedCell(Exception):
    """A cell cannot be evaluated with the modelled primitive semantics."""


def _column(position, source_count):
    """Bit mask over ``2**source_count`` assignments where source *position* is 1."""
    block = 1 << position
    period = block << 1
    total = 1 << source_count
    mask = ((1 << block) - 1) << block
    size = period
    while size < total:
        mask |= mask << size
        size <<= 1
    return mask


class Cone(object):
    """One net's exact function of an ordered list of sources."""

    __slots__ = ("order", "mask", "net")

    def __init__(self, net, order, mask):
        self.net = net
        self.order = tuple(order)
        self.mask = mask

    @property
    def source_count(self):
        return len(self.order)

    def values(self):
        total = 1 << self.source_count
        return [(self.mask >> index) & 1 for index in range(total)]

    def truth_table(self):
        return TruthTable(self.order, self.values())

    def restricted(self):
        """A :class:`TruthTable` over the sources the net actually depends on."""
        return self.truth_table().restricted()

    def is_constant(self):
        total = 1 << self.source_count
        return self.mask == 0 or self.mask == (1 << total) - 1

    def constant_value(self):
        if not self.is_constant():
            return None
        return 1 if self.mask else 0

    def __repr__(self):
        return "Cone({}, {} sources)".format(self.net, self.source_count)


class NetlistModel(object):
    """Driver/source structure plus exact cone evaluation for one netlist."""

    def __init__(self, netlist):
        self.netlist = netlist
        self._alias = {}
        self._constants = {}
        self._prepare_assignments()
        self._drivers = {}
        self.ff_instances = []
        self.lcells = []
        self._index_instances()
        self.input_keys = self._input_keys()
        self._support_cache = {}
        self._cut_caches = {}

    # -- preparation ------------------------------------------------------

    def _prepare_assignments(self):
        for target, source in self.netlist.assignments:
            for target_bit, source_bit in zip(target, source):
                if not isinstance(target_bit, Bit):
                    continue
                if isinstance(source_bit, Const):
                    self._constants[target_bit.key] = source_bit.value
                else:
                    self._alias[target_bit.key] = source_bit

    def _index_instances(self):
        for instance in self.netlist.instances:
            if instance.type == primitives.FF:
                self.ff_instances.append(instance)
                pins = ("q",)
            elif instance.type == primitives.LCELL:
                self.lcells.append(instance)
                pins = primitives.LCELL_OUTPUT_PINS
            else:
                continue
            for pin in pins:
                bits = instance.connections.get(pin)
                if not bits:
                    continue
                for bit in bits:
                    if isinstance(bit, Bit):
                        self._drivers[bit.key] = (instance, pin)

    def _input_keys(self):
        keys = set()
        for name in self.netlist.ports("input"):
            for bit in self.netlist.bits_of(name):
                keys.add(bit.key)
        return keys

    # -- net resolution ---------------------------------------------------

    def resolve(self, bit, depth=0):
        """Follow aliases to ``('const', v)`` or ``('net', key, inverted)``.

        ``v`` is ``None`` for an x/z literal, which callers must refuse rather
        than treat as either value.
        """
        if isinstance(bit, Const):
            return ("const", bit.value)
        if depth > 64:
            raise UnsupportedCell("assignment loop while resolving {}".format(bit.key))
        key = bit.key
        if key in self._constants:
            value = self._constants[key]
            if value is None:
                return ("const", None)
            return ("const", (1 - value) if bit.inverted else value)
        alias = self._alias.get(key)
        if alias is not None:
            resolved = self.resolve(alias, depth + 1)
            if resolved[0] == "const":
                if resolved[1] is None:
                    return resolved
                return ("const", (1 - resolved[1]) if bit.inverted else resolved[1])
            return ("net", resolved[1], resolved[2] ^ bit.inverted)
        return ("net", key, bool(bit.inverted))

    def is_source(self, key):
        driver = self._drivers.get(key)
        if driver is None:
            return True
        return driver[1] == "q"

    def driver(self, key):
        return self._drivers.get(key)

    def register_of(self, key):
        """The ``tennm_ff`` whose ``q`` drives *key*, or ``None``."""
        driver = self._drivers.get(key)
        if driver is not None and driver[1] == "q":
            return driver[0]
        return None

    def peel(self, key, inverted=False, depth=0):
        """Follow buffers and inverters back to the net that carries the value.

        A synthesiser spends ALM cells on plain ``q``/``!q`` copies all the
        time -- Quartus emits one per output bit of an inverted-storage
        register, and a rotation between two banks often arrives through one.
        Every pass here wants to see through those, and none of them wants to
        see through anything else, so the peel stops at the first cell that is
        not a one-input affine function.
        """
        if depth > 64:
            return key, inverted
        alias = self._alias.get(key)
        if alias is not None:
            resolved = self.resolve(alias)
            if resolved[0] == "net":
                return self.peel(resolved[1], inverted ^ resolved[2], depth + 1)
            return key, inverted
        driver = self._drivers.get(key)
        if driver is None or driver[1] != "combout":
            return key, inverted
        try:
            keys, table, _, _, _ = self._lut_inputs(driver[0])
        except UnsupportedCell:
            return key, inverted
        # The test is on the cell itself, not on its cone: a buffer is a cell
        # with one live input and a two-entry table, and looking any further
        # than that would mean evaluating the whole cone behind every operand
        # of every adder just to discover it is not a buffer.
        if len(keys) != 1:
            return key, inverted
        if table == [0, 1]:
            return self.peel(keys[0], inverted, depth + 1)
        if table == [1, 0]:
            return self.peel(keys[0], not inverted, depth + 1)
        return key, inverted

    def peel_bit(self, bit):
        """:meth:`peel` starting from a possibly inverted connection."""
        resolved = self.resolve(bit)
        if resolved[0] == "const":
            return ("const", resolved[1])
        key, inverted = self.peel(resolved[1], resolved[2])
        return ("net", key, inverted)

    # -- cell decomposition ----------------------------------------------

    def _lut_inputs(self, instance):
        """``(active, table_bits)`` for the six data pins of one ALM cell.

        ``active`` is the list of ``(pin_position, net_key, inverted)`` for pins
        driven by a real net; the constant pins are folded away by rewriting the
        mask, so the returned table is over the active pins only, least
        significant first.
        """
        mask = instance.parameters.get("lut_mask")
        if mask is None:
            raise UnsupportedCell("{} has no lut_mask".format(instance.name))
        for pin in primitives.LCELL_EXTENDED_PINS:
            if not instance.connections.get(pin):
                continue
            resolved = self.resolve(instance.single(pin))
            if resolved[0] != "const" or resolved[1] != 0:
                raise UnsupportedCell(
                    "{}: {} carries a signal; the 7-input mode is not "
                    "modelled".format(instance.name, pin)
                )
        if str(instance.parameters.get("shared_arith", "off")).lower() != "off":
            raise UnsupportedCell("{}: shared_arith is on".format(instance.name))
        if str(instance.parameters.get("extended_lut", "off")).lower() != "off":
            raise UnsupportedCell("{}: extended_lut is on".format(instance.name))

        active = []
        fixed = {}
        inverted_pins = []
        for position, pin in enumerate(_DATA_PINS):
            bits = instance.connections.get(pin)
            if not bits:
                fixed[position] = 0
                continue
            bit = instance.single(pin)
            resolved = self.resolve(bit)
            if resolved[0] == "const":
                if resolved[1] is None:
                    raise UnsupportedCell(
                        "{}: {} is driven by an x/z literal".format(instance.name, pin)
                    )
                fixed[position] = resolved[1]
                continue
            if resolved[2]:
                inverted_pins.append(pin)
            active.append((position, resolved[1]))

        mask = primitives.absorb_input_inversions(mask, inverted_pins)
        positions = [position for position, _ in active]
        table = []
        for index in range(1 << len(positions)):
            address = 0
            for slot, position in enumerate(positions):
                address |= ((index >> slot) & 1) << position
            for position, value in fixed.items():
                address |= value << position
            table.append(primitives.lut_mask_bit(mask, address))
        return [key for _, key in active], table, mask, positions, fixed

    def _arithmetic_tables(self, instance):
        """``(keys, f0_table, f1_table)`` for the two arithmetic mask halves."""
        keys, _, mask, positions, fixed = self._lut_inputs(instance)
        for position in (4, 5):
            if position in positions:
                raise UnsupportedCell(
                    "{}: datae/dataf are driven while the arithmetic outputs are "
                    "used".format(instance.name)
                )
            if fixed.get(position, 0) != 0:
                raise UnsupportedCell(
                    "{}: datae/dataf are tied high while the arithmetic outputs are "
                    "used".format(instance.name)
                )
        low, high = [], []
        for index in range(1 << len(positions)):
            address = 0
            for slot, position in enumerate(positions):
                address |= ((index >> slot) & 1) << position
            for position, value in fixed.items():
                address |= value << position
            low.append(primitives.lut_mask_bit(mask, address & 0xF))
            high.append(primitives.lut_mask_bit(mask, 16 + (address & 0xF)))
        return keys, low, high

    # -- support ----------------------------------------------------------

    def support(self, key, _stack=None):
        """The source net keys *key* depends on, syntactically."""
        return self._support(key, frozenset(), self._support_cache, _stack or set())

    def cut_support(self, key, cut, expand_root=False):
        """:meth:`support`, stopping at every net in *cut* as if it were a source.

        The register boundary is not the only cut worth taking.  A round-key
        XOR or a load multiplexer between two layers hides which bit of the
        upstream layer reaches which register, and the upstream layer is
        usually still a *named vector* in the export -- so cutting the walk
        there answers "which bit of that vector does this register read" in one
        pass, where :meth:`support` can only answer "which registers".  The
        cut is the caller's claim about where a meaningful boundary is, and
        :func:`hal_crypto.permutation.cone_support_maps` is what makes it.

        *cut* must be hashable (a frozenset); one cache is kept per cut.

        ``expand_root`` steps through *key* itself when it happens to be in the
        cut, instead of answering "it depends on itself".  The caller wants the
        cut to name the layer *behind* the net it is asking about -- a register's
        ``d`` pin is often a bit of a ``nxt``-style vector, and stopping there
        would answer nothing.
        """
        if expand_root and key in cut and not self.is_source(key):
            cut = cut - {key}
            cache_key = (cut, key)
        else:
            cache_key = cut
        cache = self._cut_caches.get(cache_key)
        if cache is None:
            cache = self._cut_caches[cache_key] = {}
        return self._support(key, cut, cache, set())

    def _support(self, key, cut, cache, stack):
        cached = cache.get(key)
        if cached is not None:
            return cached
        if key in stack:
            raise UnsupportedCell("combinational loop through {}".format(key))
        if key in cut or self.is_source(key):
            result = frozenset([key])
            cache[key] = result
            return result
        instance, pin = self._drivers[key]
        stack = stack | {key}
        result = set()
        if pin in ("combout", "sumout", "cout"):
            keys, _, _, _, _ = self._lut_inputs(instance)
            for child in keys:
                result |= self._support(child, cut, cache, stack)
            if pin in ("sumout", "cout"):
                carry = instance.connections.get("cin")
                if carry:
                    resolved = self.resolve(instance.single("cin"))
                    if resolved[0] == "net":
                        result |= self._support(resolved[1], cut, cache, stack)
        else:
            raise UnsupportedCell(
                "net {} is driven by pin {} of {}, which is not modelled".format(
                    key, pin, instance.name
                )
            )
        frozen = frozenset(result)
        cache[key] = frozen
        return frozen

    def support_of_bit(self, bit):
        resolved = self.resolve(bit)
        if resolved[0] == "const":
            return frozenset()
        return self.support(resolved[1])

    # -- exact evaluation -------------------------------------------------

    def cone(self, key, order=None):
        """The exact function of *key* over ``order`` (default: its own support)."""
        if order is None:
            order = tuple(sorted(self.support(key)))
        else:
            order = tuple(order)
        if len(order) > MAX_CONE_SOURCES:
            raise ConeTooWide(
                "{} depends on {} sources, above the {} the exact evaluation "
                "covers".format(key, len(order), MAX_CONE_SOURCES)
            )
        columns = {
            name: _column(position, len(order)) for position, name in enumerate(order)
        }
        mask = self._evaluate(key, 1 << len(order), columns, {})
        return Cone(key, order, mask)

    def evaluate_samples(self, keys, samples):
        """Evaluate *keys* under an explicit list of source assignments.

        ``samples`` is a list of ``{source key: 0/1}`` dicts.  The same
        column-mask evaluation is used as for a full truth table, with one
        column per sample instead of one per assignment, so a net whose cone is
        far too wide to enumerate can still be checked against a model on as
        many vectors as the caller is willing to pay for.  The result is a dict
        mapping each key to a list of bits, one per sample.
        """
        samples = list(samples)
        width = len(samples)
        names = set()
        for sample in samples:
            names.update(sample)
        columns = {}
        for name in names:
            column = 0
            for index, sample in enumerate(samples):
                if sample.get(name):
                    column |= 1 << index
            columns[name] = column
        cache = {}
        result = {}
        for key in keys:
            mask = self._evaluate(key, width, columns, cache)
            result[key] = [(mask >> index) & 1 for index in range(width)]
        return result

    def evaluate_bit_samples(self, bit, samples):
        """:meth:`evaluate_samples` for one possibly inverted connection."""
        resolved = self.resolve(bit)
        if resolved[0] == "const":
            if resolved[1] is None:
                raise UnsupportedCell("an x/z literal was used as a cone input")
            return [resolved[1]] * len(samples)
        values = self.evaluate_samples([resolved[1]], samples)[resolved[1]]
        if resolved[2]:
            return [1 - value for value in values]
        return values

    def cone_of_bit(self, bit, order=None):
        """Like :meth:`cone`, honouring an inverted or constant connection."""
        resolved = self.resolve(bit)
        if resolved[0] == "const":
            if resolved[1] is None:
                raise UnsupportedCell("an x/z literal was used as a cone input")
            order = tuple(order or ())
            total = 1 << len(order)
            return Cone("'{}'".format(resolved[1]), order,
                        ((1 << total) - 1) if resolved[1] else 0)
        cone = self.cone(resolved[1], order=order)
        if resolved[2]:
            total = 1 << cone.source_count
            return Cone(cone.net, cone.order, cone.mask ^ ((1 << total) - 1))
        return cone

    def _evaluate(self, key, width, columns, cache):
        cached = cache.get(key)
        if cached is not None:
            return cached
        if key in columns:
            cache[key] = columns[key]
            return columns[key]
        if self.is_source(key):
            raise UnsupportedCell(
                "source {} is not part of the evaluation order".format(key)
            )
        instance, pin = self._drivers[key]
        all_ones = (1 << width) - 1
        if pin == "combout":
            keys, table, _, _, _ = self._lut_inputs(instance)
            operands = [self._evaluate(child, width, columns, cache) for child in keys]
            value = _apply_table(table, operands, all_ones)
        elif pin in ("sumout", "cout"):
            keys, low, high = self._arithmetic_tables(instance)
            operands = [self._evaluate(child, width, columns, cache) for child in keys]
            f0 = _apply_table(low, operands, all_ones)
            f1 = _apply_table(high, operands, all_ones)
            carry_bits = instance.connections.get("cin")
            if not carry_bits:
                carry = 0
            else:
                resolved = self.resolve(instance.single("cin"))
                if resolved[0] == "const":
                    if resolved[1] is None:
                        raise UnsupportedCell("{}: cin is x/z".format(instance.name))
                    carry = all_ones if resolved[1] else 0
                else:
                    carry = self._evaluate(resolved[1], width, columns, cache)
                    if resolved[2]:
                        carry ^= all_ones
            if pin == "sumout":
                value = f0 ^ carry
            else:
                value = (f0 & carry) | ((f0 ^ all_ones) & f1)
        else:
            raise UnsupportedCell(
                "net {} is driven by pin {} of {}".format(key, pin, instance.name)
            )
        cache[key] = value
        return value

    # -- register helpers -------------------------------------------------

    def ff_pin(self, instance, pin):
        """Resolved connection of a register pin, as :meth:`resolve` returns it."""
        bits = instance.connections.get(pin)
        if not bits:
            return ("const", 0 if pin in primitives.FF_MUST_BE_ZERO_PINS else None)
        return self.resolve(instance.single(pin))

    def ff_configuration_problem(self, instance):
        """A reason string when the register is outside the modelled subset."""
        driven = set()
        constants = {}
        for pin in primitives.FF_INPUT_PINS:
            bits = instance.connections.get(pin)
            if not bits:
                continue
            resolved = self.resolve(instance.single(pin))
            if resolved[0] == "const" and resolved[1] is not None:
                constants[pin] = resolved[1]
            else:
                driven.add(pin)
        try:
            primitives.check_ff_configuration(driven, constants)
        except primitives.UnsupportedConfiguration as exc:
            return str(exc)
        return None


def _apply_table(table, operands, all_ones):
    """Evaluate a small truth ``table`` over column-mask ``operands``.

    ``minterms[j]`` ends up as the set of assignments on which the operands
    read exactly *j*, with operand *i* at address bit *i*; appending the
    ``operand`` half after the ``~operand`` half is what keeps that indexing.
    """
    if not operands:
        return all_ones if table[0] else 0
    minterms = [all_ones]
    for operand in operands:
        negated = operand ^ all_ones
        minterms = [term & negated for term in minterms] + [
            term & operand for term in minterms
        ]
    result = 0
    for index, value in enumerate(table):
        if value:
            result |= minterms[index]
    return result
