"""A dependency-free loader for the ``fixtures/`` netlists.

HAL's ``verilog_parser`` is the authority on what a fixture means; this module
exists so the *analysis* can be tested on a machine that cannot build HAL.  It
reads the same two files HAL reads -- the ``.hgl`` gate library (which is plain
JSON) and a structural Verilog netlist -- and produces the same
:class:`~hal_cdc.netlist_view.NetlistView` that :func:`hal_cdc.netlist_view.
from_hal_netlist` produces from ``hal_py``.

It deliberately understands only the subset the fixtures use: a single module,
``input``/``output``/``wire`` declarations, and cell instantiations with named
port connections.  Anything else raises, because a fixture that silently parses
into something other than what it says is worse than no fixture.

``tests/`` in the container run the *same* assertions through HAL's parser, so
a divergence between this reader and HAL's shows up as a failing check rather
than as a quietly different netlist.
"""

import json
import os
import re

from .netlist_view import GateTypeView, GateView, NetlistView, NetView, PinView

__all__ = ["FixtureError", "load_gate_library", "load_verilog", "load_fixture"]


class FixtureError(ValueError):
    """The fixture (or the library it needs) is not in the supported subset."""


_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_MODULE_RE = re.compile(r"\bmodule\s+([\w$\\]+)\s*\((.*?)\)\s*;", re.DOTALL)
_DECL_RE = re.compile(r"\b(input|output|inout|wire)\b([^;]*);")
_INSTANCE_RE = re.compile(
    r"([A-Za-z_][\w$]*)\s+([A-Za-z_][\w$]*)\s*\(\s*((?:\.\s*[\w$]+\s*\([^)]*\)\s*,?\s*)+)\)\s*;"
)
_CONNECTION_RE = re.compile(r"\.\s*([\w$]+)\s*\(\s*([^)]*?)\s*\)")
_CONSTANT_RE = re.compile(r"^\d*'[bB]([01])$")


def load_gate_library(path):
    """Read a ``.hgl`` gate library into ``{cell name: GateTypeView}``."""
    with open(str(path), "r", encoding="utf-8") as handle:
        document = json.load(handle)
    cells = document.get("cells")
    if not isinstance(cells, list):
        raise FixtureError("{} has no 'cells' array".format(path))

    library = {}
    for cell in cells:
        pins = []
        for group in cell.get("pin_groups", []) or []:
            for pin in group.get("pins", []) or []:
                pins.append(
                    PinView(
                        pin.get("name", ""),
                        pin.get("direction", "none"),
                        pin.get("type", "none"),
                    )
                )
        for pin in cell.get("pins", []) or []:
            pins.append(
                PinView(pin.get("name", ""), pin.get("direction", "none"), pin.get("type", "none"))
            )
        library[cell["name"]] = GateTypeView(cell["name"], cell.get("types", []), pins)
    return library, document.get("library") or os.path.basename(str(path))


def _split_identifiers(text):
    names = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if not re.match(r"^[A-Za-z_][\w$]*$", chunk):
            raise FixtureError(
                "the fixture reader only supports plain scalar signal names, got "
                "{!r}. Write the fixture bit by bit instead of using a vector.".format(chunk)
            )
        names.append(chunk)
    return names


def load_verilog(path, library, library_name=None, library_path=None):
    """Parse a structural Verilog fixture into a :class:`NetlistView`."""
    with open(str(path), "r", encoding="utf-8") as handle:
        text = handle.read()
    text = _COMMENT_RE.sub(" ", text)

    module = _MODULE_RE.search(text)
    if module is None:
        raise FixtureError("{}: no 'module <name> (...);' header found".format(path))
    design_name = module.group(1)
    body = text[module.end():]
    end = body.find("endmodule")
    if end < 0:
        raise FixtureError("{}: no 'endmodule'".format(path))
    body = body[:end]

    inputs, outputs = [], []
    declared = []
    for match in _DECL_RE.finditer(body):
        kind = match.group(1)
        names = _split_identifiers(match.group(2))
        declared.extend(names)
        if kind in ("input", "inout"):
            inputs.extend(names)
        elif kind == "output":
            outputs.extend(names)

    nets = {}
    net_order = []

    def net_id(name):
        if name not in nets:
            nets[name] = len(nets) + 1
            net_order.append(name)
        return nets[name]

    for name in declared:
        net_id(name)

    input_set, output_set = set(inputs), set(outputs)
    sources, destinations = {}, {}
    gates = []
    constants = set()

    declaration_body = _DECL_RE.sub(" ", body)
    for index, match in enumerate(_INSTANCE_RE.finditer(declaration_body), start=1):
        type_name, instance_name, connections = match.group(1), match.group(2), match.group(3)
        if type_name in ("input", "output", "wire", "inout"):
            continue
        gate_type = library.get(type_name)
        if gate_type is None:
            raise FixtureError(
                "{}: instance {!r} uses cell {!r}, which the gate library does not "
                "define".format(path, instance_name, type_name)
            )
        fan_in, fan_out = {}, {}
        for pin_name, signal in _CONNECTION_RE.findall(connections):
            pin = gate_type.pin(pin_name)
            if pin is None:
                raise FixtureError(
                    "{}: cell {!r} has no pin {!r} (instance {!r})".format(
                        path, type_name, pin_name, instance_name
                    )
                )
            signal = signal.strip()
            if not signal:
                continue
            constant = _CONSTANT_RE.match(signal)
            if constant is not None:
                signal = "'{}'".format(constant.group(1))
                constants.add(signal)
            elif signal not in nets:
                raise FixtureError(
                    "{}: instance {!r} connects pin {!r} to the undeclared signal {!r}; "
                    "declare every wire so the fixture is unambiguous".format(
                        path, instance_name, pin_name, signal
                    )
                )
            identifier = net_id(signal)
            if pin.direction == "input":
                fan_in[pin_name] = identifier
                destinations.setdefault(identifier, []).append((index, pin_name))
            elif pin.direction == "output":
                fan_out[pin_name] = identifier
                sources.setdefault(identifier, []).append((index, pin_name))
            else:
                raise FixtureError(
                    "{}: pin {!r} of {!r} has direction {!r}, which the fixture reader "
                    "does not support".format(path, pin_name, type_name, pin.direction)
                )
        gates.append(GateView(index, instance_name, gate_type, fan_in, fan_out,
                              module_id=1, module_name="top_module"))

    net_views = []
    for name in net_order:
        identifier = nets[name]
        net_views.append(
            NetView(
                identifier,
                name,
                sources=sorted(sources.get(identifier, [])),
                destinations=sorted(destinations.get(identifier, [])),
                is_global_input=name in input_set and identifier not in sources,
                is_global_output=name in output_set,
                is_gnd=name == "'0'",
                is_vcc=name == "'1'",
            )
        )

    return NetlistView(
        gates,
        net_views,
        design_name=design_name,
        input_filename=os.path.abspath(str(path)),
        gate_library_name=library_name,
        gate_library_path=library_path,
        netlist_id=1,
    )


def load_fixture(verilog_path, library_path):
    """Read a fixture netlist and the gate library it is written against."""
    library, library_name = load_gate_library(library_path)
    return load_verilog(
        verilog_path,
        library,
        library_name=library_name,
        library_path=os.path.abspath(str(library_path)),
    )
