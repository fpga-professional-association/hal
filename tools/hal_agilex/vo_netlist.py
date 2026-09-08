"""A reader for the structural Verilog (``.vo``) that ``quartus_eda`` writes.

This is *not* a general Verilog parser and does not try to be one.  It reads
exactly the subset the Quartus EDA netlist writer emits for a post-synthesis
export -- port and net declarations, continuous assignments, and primitive
instances with a parameter list and named port connections -- and raises
:class:`VerilogSubsetError` on anything else rather than skipping it.  A reader
that silently ignores what it does not understand would quietly drop gates, and
a dropped gate is a wrong analysis.

The result is a plain data model (:class:`Netlist`) that the reference
simulator, the coverage inventory, the recognition adapter and the HAL import
all share, so none of them needs HAL or the vendor tool to run.
"""

import re

__all__ = [
    "VerilogSubsetError",
    "Bit",
    "Const",
    "Instance",
    "Netlist",
    "parse_file",
    "parse_text",
]

# `timescale, `define, ... -- compiler directives carry no netlist content.
_DIRECTIVE_RE = re.compile(r"^\s*`.*$", re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")

_TOKEN_RE = re.compile(
    r"""
    (?P<escaped>\\\S+)                                  # \escaped~identifier[3]
  | (?P<number>\d*'[bBoOdDhH][0-9a-fA-FxXzZ_]+)         # 64'h0F, 1'b0
  | (?P<string>"[^"\n]*")
  | (?P<ident>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<integer>\d+)
  | (?P<punct>[(){}\[\],;=~#.:])
    """,
    re.VERBOSE,
)

_NET_KEYWORDS = ("wire", "tri", "tri0", "tri1", "supply0", "supply1", "reg", "logic")
_PORT_KEYWORDS = ("input", "output", "inout")


class VerilogSubsetError(Exception):
    """The input uses Verilog this reader deliberately does not accept."""


class Bit(object):
    """One bit of a signal: a net name, an optional index, an optional ``~``."""

    __slots__ = ("name", "index", "inverted")

    def __init__(self, name, index=None, inverted=False):
        self.name = name
        self.index = index
        self.inverted = inverted

    @property
    def key(self):
        """Identity of the underlying net, ignoring inversion."""
        return self.name if self.index is None else "{}[{}]".format(self.name, self.index)

    def __repr__(self):
        return "Bit({}{})".format("~" if self.inverted else "", self.key)

    def __eq__(self, other):
        return (
            isinstance(other, Bit)
            and (self.name, self.index, self.inverted) == (other.name, other.index, other.inverted)
        )

    def __hash__(self):
        return hash((self.name, self.index, self.inverted))


class Const(object):
    """A literal bit (``1'b0``/``1'b1``) or an unknown one (``1'bx``)."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value  # 0, 1 or None for x/z

    @property
    def key(self):
        return "'{}'".format("x" if self.value is None else self.value)

    def __repr__(self):
        return "Const({})".format(self.key)

    def __eq__(self, other):
        return isinstance(other, Const) and self.value == other.value

    def __hash__(self):
        return hash(("const", self.value))


class Instance(object):
    """One primitive instance with its parameters and named connections."""

    def __init__(self, type_name, name, parameters, connections, line=None):
        self.type = type_name
        self.name = name
        #: parameter name -> int (sized literals and integers) or str (quoted)
        self.parameters = parameters
        #: pin name -> list of :class:`Bit`/:class:`Const` (msb first, empty when open)
        self.connections = connections
        self.line = line

    def single(self, pin):
        """The one bit connected to *pin*, or ``None`` when the pin is open."""
        bits = self.connections.get(pin)
        if not bits:
            return None
        if len(bits) != 1:
            raise VerilogSubsetError(
                "pin {} of instance {} is {} bits wide; this reader only models "
                "single-bit primitive pins".format(pin, self.name, len(bits))
            )
        return bits[0]

    def __repr__(self):
        return "Instance({} {})".format(self.type, self.name)


class Netlist(object):
    """The parsed contents of one ``.vo`` module."""

    def __init__(self, name):
        self.name = name
        #: net name -> (direction, msb, lsb); direction is input/output/inout/None
        self.declarations = {}
        self.port_order = []
        #: list of (target bits, source bits)
        self.assignments = []
        self.instances = []

    # -- convenience ------------------------------------------------------
    def ports(self, direction):
        return [
            name
            for name in self.port_order
            if self.declarations.get(name, (None, None, None))[0] == direction
        ]

    def width(self, name):
        entry = self.declarations.get(name)
        if entry is None or entry[1] is None:
            return 1
        return abs(entry[1] - entry[2]) + 1

    def bits_of(self, name):
        """All bits of a declared signal, least significant first."""
        entry = self.declarations.get(name)
        if entry is None or entry[1] is None:
            return [Bit(name)]
        msb, lsb = entry[1], entry[2]
        low, high = min(msb, lsb), max(msb, lsb)
        return [Bit(name, index) for index in range(low, high + 1)]

    def instances_of_type(self, type_name):
        return [instance for instance in self.instances if instance.type == type_name]

    def type_histogram(self):
        histogram = {}
        for instance in self.instances:
            histogram[instance.type] = histogram.get(instance.type, 0) + 1
        return histogram

    def __repr__(self):
        return "Netlist({}, {} instances)".format(self.name, len(self.instances))


# ---------------------------------------------------------------------------
# tokenizer
# ---------------------------------------------------------------------------


class _Tokens(object):
    def __init__(self, text):
        self.items = []
        stripped = _BLOCK_COMMENT_RE.sub(" ", text)
        stripped = _LINE_COMMENT_RE.sub(" ", stripped)
        stripped = _DIRECTIVE_RE.sub(" ", stripped)
        position = 0
        length = len(stripped)
        line = 1
        while position < length:
            character = stripped[position]
            if character == "\n":
                line += 1
                position += 1
                continue
            if character.isspace():
                position += 1
                continue
            match = _TOKEN_RE.match(stripped, position)
            if match is None:
                raise VerilogSubsetError(
                    "unexpected character {!r} on line {}".format(character, line)
                )
            kind = match.lastgroup
            value = match.group()
            if kind == "escaped":
                # An escaped identifier runs to the next whitespace and the
                # backslash is not part of the name.
                value = value[1:]
                kind = "ident"
            self.items.append((kind, value, line))
            position = match.end()
        self.position = 0

    def peek(self, offset=0):
        index = self.position + offset
        return self.items[index] if index < len(self.items) else (None, None, None)

    def next(self):
        item = self.peek()
        if item[0] is None:
            raise VerilogSubsetError("unexpected end of file")
        self.position += 1
        return item

    def expect(self, value):
        kind, text, line = self.next()
        if text != value:
            raise VerilogSubsetError(
                "expected {!r} but found {!r} on line {}".format(value, text, line)
            )
        return text

    def accept(self, value):
        if self.peek()[1] == value:
            self.position += 1
            return True
        return False

    def at_end(self):
        return self.peek()[0] is None


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def _parse_literal(text):
    """Return (value, width) of a sized Verilog literal, value ``None`` if x/z."""
    width_text, _, rest = text.partition("'")
    base_char = rest[0].lower()
    digits = rest[1:].replace("_", "")
    width = int(width_text) if width_text else None
    bases = {"b": 2, "o": 8, "d": 10, "h": 16}
    if base_char not in bases:
        raise VerilogSubsetError("unsupported literal base in {!r}".format(text))
    if any(character in "xXzZ" for character in digits):
        return None, width
    return int(digits, bases[base_char]), width


def _parse_range(tokens):
    """Parse an optional ``[msb:lsb]`` and return it, or ``(None, None)``."""
    if tokens.peek()[1] != "[":
        return None, None
    tokens.expect("[")
    msb = int(tokens.next()[1])
    tokens.expect(":")
    lsb = int(tokens.next()[1])
    tokens.expect("]")
    return msb, lsb


def _parse_expression(tokens):
    """Parse a connection expression into a list of bits, most significant first."""
    kind, text, line = tokens.peek()
    if text == "{":
        tokens.expect("{")
        bits = []
        while True:
            bits.extend(_parse_expression(tokens))
            if tokens.accept(","):
                continue
            tokens.expect("}")
            break
        return bits

    inverted = False
    if text == "~":
        tokens.expect("~")
        inverted = True
        kind, text, line = tokens.peek()

    if kind == "number":
        tokens.next()
        value, width = _parse_literal(text)
        width = width or 1
        if value is None:
            return [Const(None)] * width
        bits = [Const((value >> index) & 1) for index in range(width)]
        bits.reverse()
        if inverted:
            bits = [Const(None if bit.value is None else 1 - bit.value) for bit in bits]
        return bits

    if kind == "integer":
        tokens.next()
        value = int(text)
        if value not in (0, 1):
            raise VerilogSubsetError(
                "unsized integer {} used as a connection on line {}".format(value, line)
            )
        return [Const(1 - value if inverted else value)]

    if kind != "ident":
        raise VerilogSubsetError(
            "unsupported connection expression starting with {!r} on line {}".format(text, line)
        )

    tokens.next()
    name = text
    index = None
    if tokens.peek()[1] == "[":
        tokens.expect("[")
        first = int(tokens.next()[1])
        if tokens.accept(":"):
            second = int(tokens.next()[1])
            tokens.expect("]")
            low, high = min(first, second), max(first, second)
            bits = [Bit(name, position, inverted) for position in range(high, low - 1, -1)]
            return bits
        tokens.expect("]")
        index = first
    return [Bit(name, index, inverted)]


def _parse_parameters(tokens):
    parameters = {}
    tokens.expect("#")
    tokens.expect("(")
    while True:
        tokens.expect(".")
        name = tokens.next()[1]
        tokens.expect("(")
        kind, text, line = tokens.next()
        if kind == "string":
            value = text[1:-1]
        elif kind == "number":
            value, _ = _parse_literal(text)
            if value is None:
                raise VerilogSubsetError(
                    "parameter {} on line {} has an x/z value".format(name, line)
                )
        elif kind == "integer":
            value = int(text)
        elif kind == "ident":
            value = text
        else:
            raise VerilogSubsetError(
                "unsupported parameter value {!r} for {} on line {}".format(text, name, line)
            )
        tokens.expect(")")
        parameters[name] = value
        if tokens.accept(","):
            continue
        tokens.expect(")")
        break
    return parameters


def _parse_connections(tokens):
    connections = {}
    tokens.expect("(")
    if tokens.accept(")"):
        return connections
    while True:
        tokens.expect(".")
        pin = tokens.next()[1]
        tokens.expect("(")
        if tokens.accept(")"):
            connections[pin] = []
        else:
            connections[pin] = _parse_expression(tokens)
            tokens.expect(")")
        if tokens.accept(","):
            continue
        tokens.expect(")")
        break
    return connections


def parse_text(text):
    """Parse the first module of a Quartus ``.vo`` netlist."""
    tokens = _Tokens(text)

    while not tokens.at_end() and tokens.peek()[1] != "module":
        tokens.next()
    if tokens.at_end():
        raise VerilogSubsetError("no 'module' declaration found")

    tokens.expect("module")
    netlist = Netlist(tokens.next()[1])

    if tokens.accept("("):
        while not tokens.accept(")"):
            kind, text_value, line = tokens.next()
            if text_value == ",":
                continue
            if kind != "ident":
                raise VerilogSubsetError(
                    "unsupported module port list entry {!r} on line {}".format(text_value, line)
                )
            netlist.port_order.append(text_value)
            netlist.declarations.setdefault(text_value, (None, None, None))
    tokens.expect(";")

    while True:
        kind, text_value, line = tokens.peek()
        if text_value is None:
            raise VerilogSubsetError("module {} is not terminated".format(netlist.name))
        if text_value == "endmodule":
            tokens.next()
            break

        if text_value in _PORT_KEYWORDS or text_value in _NET_KEYWORDS:
            tokens.next()
            direction = text_value if text_value in _PORT_KEYWORDS else None
            net_kind = None if text_value in _PORT_KEYWORDS else text_value
            if tokens.peek()[1] in _NET_KEYWORDS:
                net_kind = tokens.next()[1]
            msb, lsb = _parse_range(tokens)
            while True:
                name = tokens.next()[1]
                previous = netlist.declarations.get(name)
                if previous is not None and previous[0] is not None and direction is None:
                    direction = previous[0]
                if previous is not None and previous[1] is not None and msb is None:
                    msb, lsb = previous[1], previous[2]
                netlist.declarations[name] = (direction, msb, lsb)
                if net_kind in ("tri1", "supply1"):
                    netlist.assignments.append(([Bit(name)], [Const(1)]))
                elif net_kind in ("tri0", "supply0"):
                    netlist.assignments.append(([Bit(name)], [Const(0)]))
                if tokens.accept(","):
                    continue
                tokens.expect(";")
                break
            continue

        if text_value == "assign":
            tokens.next()
            target = _parse_expression(tokens)
            tokens.expect("=")
            source = _parse_expression(tokens)
            tokens.expect(";")
            if len(target) != len(source):
                raise VerilogSubsetError(
                    "width mismatch in assignment on line {}: {} vs {}".format(
                        line, len(target), len(source)
                    )
                )
            netlist.assignments.append((target, source))
            continue

        if kind != "ident":
            raise VerilogSubsetError(
                "unsupported statement starting with {!r} on line {}".format(text_value, line)
            )

        type_name = tokens.next()[1]
        parameters = _parse_parameters(tokens) if tokens.peek()[1] == "#" else {}
        instance_name = tokens.next()[1]
        connections = _parse_connections(tokens)
        tokens.expect(";")
        netlist.instances.append(
            Instance(type_name, instance_name, parameters, connections, line=line)
        )

    return netlist


def parse_file(path):
    with open(str(path), "r", encoding="utf-8", errors="replace") as handle:
        return parse_text(handle.read())
