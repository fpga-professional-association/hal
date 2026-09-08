"""Read a HAL ``.hgl`` gate library with the standard library only.

This exists so the fixtures and the offline tests use the *same* primitive
semantics HAL would use -- the Boolean functions and the ``ff_config`` are read
out of ``plugins/gate_libraries/definitions/*.hgl`` rather than re-typed into a
lookup table that could silently disagree with HAL.

Only what the recovery needs is modelled: output-pin functions, the flip-flop
configuration and pin types.  Latches, RAMs and anything else are recognised
and reported as unsupported by :mod:`verilog_source`; they are never
approximated.
"""

import json

from .circuit import X

__all__ = [
    "GateLibraryError",
    "Pin",
    "Cell",
    "GateLibrary",
    "parse_expression",
    "evaluate_expression",
]


class GateLibraryError(RuntimeError):
    """The gate library could not be read, or a cell is not expressible."""


# ---------------------------------------------------------------------------
# three-valued expression evaluation
# ---------------------------------------------------------------------------


def _tokenize(text):
    tokens = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if char in "()&|^":
            tokens.append(char)
            index += 1
            continue
        if char in "!~":
            tokens.append("!")
            index += 1
            continue
        if char.isalnum() or char == "_":
            start = index
            while index < length and (text[index].isalnum() or text[index] == "_"):
                index += 1
            tokens.append(text[start:index])
            continue
        raise GateLibraryError("unexpected character {!r} in expression {!r}".format(char, text))
    return tokens


class _Parser(object):
    """Precedence: ``!`` > ``&`` > ``^`` > ``|`` (HAL's own ordering)."""

    def __init__(self, tokens, text):
        self.tokens = tokens
        self.text = text
        self.position = 0

    def peek(self):
        if self.position < len(self.tokens):
            return self.tokens[self.position]
        return None

    def take(self):
        token = self.peek()
        self.position += 1
        return token

    def parse(self):
        node = self.parse_or()
        if self.position != len(self.tokens):
            raise GateLibraryError(
                "trailing tokens in expression {!r} at {}".format(self.text, self.position)
            )
        return node

    def parse_or(self):
        node = self.parse_xor()
        while self.peek() == "|":
            self.take()
            node = ("|", node, self.parse_xor())
        return node

    def parse_xor(self):
        node = self.parse_and()
        while self.peek() == "^":
            self.take()
            node = ("^", node, self.parse_and())
        return node

    def parse_and(self):
        node = self.parse_unary()
        while self.peek() == "&":
            self.take()
            node = ("&", node, self.parse_unary())
        return node

    def parse_unary(self):
        token = self.peek()
        if token == "!":
            self.take()
            return ("!", self.parse_unary())
        return self.parse_atom()

    def parse_atom(self):
        token = self.take()
        if token is None:
            raise GateLibraryError("unexpected end of expression {!r}".format(self.text))
        if token == "(":
            node = self.parse_or()
            if self.take() != ")":
                raise GateLibraryError("unbalanced parentheses in {!r}".format(self.text))
            return node
        if token in ("0", "0b0"):
            return ("const", 0)
        if token in ("1", "0b1"):
            return ("const", 1)
        if token.startswith("0b"):
            raise GateLibraryError(
                "multi-bit constant {!r} in {!r} is not supported".format(token, self.text)
            )
        return ("var", token)


def parse_expression(text):
    """Parse an ``.hgl`` Boolean expression into a tuple tree."""
    return _Parser(_tokenize(text), text).parse()


def evaluate_expression(node, values):
    """Evaluate a parsed expression three-valued; missing names are ``X``."""
    kind = node[0]
    if kind == "const":
        return node[1]
    if kind == "var":
        return values.get(node[1], X)
    if kind == "!":
        inner = evaluate_expression(node[1], values)
        return X if inner is None else 1 - inner
    left = evaluate_expression(node[1], values)
    right = evaluate_expression(node[2], values)
    if kind == "&":
        if left == 0 or right == 0:
            return 0
        if left == 1 and right == 1:
            return 1
        return X
    if kind == "|":
        if left == 1 or right == 1:
            return 1
        if left == 0 and right == 0:
            return 0
        return X
    if kind == "^":
        if left is None or right is None:
            return X
        return left ^ right
    raise GateLibraryError("unknown operator {!r}".format(kind))


def expression_function(text):
    """Compile ``text`` into ``callable(values) -> 0 | 1 | X``."""
    node = parse_expression(text)
    return lambda values, _node=node: evaluate_expression(_node, values)


# ---------------------------------------------------------------------------
# the library
# ---------------------------------------------------------------------------


class Pin(object):
    __slots__ = ("name", "direction", "type", "function")

    def __init__(self, name, direction, pin_type, function=None):
        self.name = name
        self.direction = direction
        self.type = pin_type
        self.function = function

    @property
    def is_supply(self):
        return self.type in ("power", "ground")


class Cell(object):
    def __init__(self, name, types, pins, ff_config=None, latch_config=None):
        self.name = name
        self.types = tuple(types)
        self.pins = list(pins)
        self.pin_by_name = {pin.name: pin for pin in self.pins}
        self.ff_config = dict(ff_config or {})
        self.latch_config = dict(latch_config or {})

    def signal_pins(self, direction):
        return [
            pin for pin in self.pins if pin.direction == direction and not pin.is_supply
        ]

    def pins_of_type(self, pin_type):
        return [pin.name for pin in self.pins if pin.type == pin_type]

    @property
    def is_ff(self):
        return "ff" in self.types and bool(self.ff_config)

    @property
    def is_sequential(self):
        return "sequential" in self.types

    def unsupported_reason(self):
        """Why the recovery cannot model this cell, or ``None`` if it can."""
        if self.is_ff:
            return None
        if self.is_sequential:
            kinds = [kind for kind in self.types if kind not in ("sequential",)]
            return (
                "sequential gate type {} ({}) is not an edge-triggered flip-flop; the "
                "APB recovery only models edge-triggered state".format(
                    self.name, ", ".join(kinds) or "unclassified"
                )
            )
        missing = [
            pin.name
            for pin in self.signal_pins("output")
            if not pin.function
        ]
        if missing:
            return (
                "combinational gate type {} has no Boolean function for output pin(s) "
                "{}".format(self.name, ", ".join(sorted(missing)))
            )
        return None


class GateLibrary(object):
    """A parsed ``.hgl`` gate library."""

    def __init__(self, name, cells, path=None):
        self.name = name
        self.path = path
        self.cells = {cell.name: cell for cell in cells}

    def __contains__(self, name):
        return name in self.cells

    def get(self, name):
        return self.cells.get(name)

    @classmethod
    def from_file(cls, path):
        with open(path, "r", encoding="utf-8") as handle:
            try:
                document = json.load(handle)
            except ValueError as exc:
                raise GateLibraryError("{} is not valid JSON: {}".format(path, exc))
        return cls.from_document(document, path=path)

    @classmethod
    def from_document(cls, document, path=None):
        if "cells" not in document:
            raise GateLibraryError("gate library document has no 'cells' array")
        cells = []
        for entry in document["cells"]:
            pins = []
            for group in entry.get("pin_groups", []):
                for pin in group.get("pins", []):
                    pins.append(
                        Pin(
                            pin["name"],
                            pin.get("direction", "input"),
                            pin.get("type", "none"),
                            pin.get("function"),
                        )
                    )
            cells.append(
                Cell(
                    entry["name"],
                    entry.get("types", []),
                    pins,
                    entry.get("ff_config"),
                    entry.get("latch_config"),
                )
            )
        return cls(document.get("library", "unknown"), cells, path=path)
