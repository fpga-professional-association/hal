"""Build a :class:`circuit.Circuit` from a structural Verilog netlist, offline.

This is **not** a Verilog parser in the HAL sense and must never be used as
one.  It reads exactly the subset a gate-level netlist needs -- port and wire
declarations with optional ranges, named-port cell instances, ``assign``
aliases and the ``1'b0``/``1'b1`` literals -- so that the recovery can be
developed, tested and demonstrated on a machine without a HAL build.  Anything
it does not understand is an error, never a silent omission.

Net naming follows HAL: ``a[3]`` becomes ``a(3)`` (see
``VerilogParser::expand_ranges_recursively``), so a mapping file written
against this reader also resolves against a netlist loaded through ``hal_py``.
"""

import re

from . import circuit as circuit_model
from .circuit import Circuit, CircuitError, FlipFlop, Gate
from .hgl_library import GateLibraryError, expression_function

__all__ = ["VerilogError", "read_netlist"]


class VerilogError(RuntimeError):
    """The Verilog netlist uses something this reader deliberately refuses."""


_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"//[^\n]*")
_ATTRIBUTE = re.compile(r"\(\*.*?\*\)", re.DOTALL)
_MODULE = re.compile(r"\bmodule\s+(\\?[A-Za-z_][\w$.\\]*)\s*(\([^;]*?\))?\s*;(.*?)\bendmodule\b", re.DOTALL)
_DECLARATION = re.compile(
    r"\b(input|output|inout|wire|reg)\b\s*(?:\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*)?([^;]+);"
)
_INSTANCE = re.compile(
    r"\b([A-Za-z_][\w$]*)\s+(\\?[A-Za-z_][\w$.\\]*)\s*\(\s*((?:\.[^;]*?))\)\s*;", re.DOTALL
)
_PORT_CONNECTION = re.compile(r"\.\s*([A-Za-z_][\w$]*)\s*\(\s*([^)]*?)\s*\)")
_ASSIGN = re.compile(r"\bassign\b\s+([^=;]+?)\s*=\s*([^;]+?)\s*;")
_LITERAL = re.compile(r"^\d*'[bB]([01xzXZ])$")
_BIT_SELECT = re.compile(r"^(\S+?)\s*\[\s*(\d+)\s*\]$")


def _strip(text):
    text = _COMMENT_BLOCK.sub(" ", text)
    text = _COMMENT_LINE.sub(" ", text)
    text = _ATTRIBUTE.sub(" ", text)
    return text


def _expand(name, high, low):
    if high is None:
        return [name]
    high, low = int(high), int(low)
    step = 1 if low <= high else -1
    indices = list(range(low, high + step, step))
    return ["{}({})".format(name, index) for index in sorted(indices)]


class _Signal(object):
    def __init__(self, name, nets, direction):
        self.name = name
        self.nets = nets
        self.direction = direction


class _Union(object):
    """Union-find over net names, used to collapse ``assign`` aliases."""

    def __init__(self):
        self.parent = {}

    def add(self, key):
        self.parent.setdefault(key, key)

    def find(self, key):
        self.add(key)
        root = key
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[key] != root:
            self.parent[key], key = root, self.parent[key]
        return root

    def union(self, left, right):
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def read_netlist(path, library, top_module=None):
    """Read ``path`` against ``library`` and return a :class:`Circuit`."""
    with open(path, "r", encoding="utf-8") as handle:
        text = _strip(handle.read())

    modules = {}
    for match in _MODULE.finditer(text):
        modules[match.group(1)] = match.group(3)
    if not modules:
        raise VerilogError("{} contains no module".format(path))
    if top_module is None:
        if len(modules) > 1:
            raise VerilogError(
                "{} contains {} modules ({}); this reader only handles a flattened "
                "netlist, pass the top module explicitly".format(
                    path, len(modules), ", ".join(sorted(modules))
                )
            )
        top_module = next(iter(modules))
    if top_module not in modules:
        raise VerilogError("{} has no module {!r}".format(path, top_module))
    body = modules[top_module]

    signals = {}
    directions = {}
    for match in _DECLARATION.finditer(body):
        keyword, high, low = match.group(1), match.group(2), match.group(3)
        for raw_name in match.group(4).split(","):
            name = raw_name.strip()
            if not name:
                continue
            if "[" in name or "=" in name:
                raise VerilogError(
                    "unsupported declaration {!r} in module {}".format(name, top_module)
                )
            nets = _expand(name, high, low)
            if name not in signals:
                signals[name] = _Signal(name, nets, None)
            elif signals[name].nets != nets:
                raise VerilogError(
                    "signal {!r} declared twice with different ranges".format(name)
                )
            if keyword in ("input", "output", "inout"):
                directions[name] = keyword

    union = _Union()
    net_keys = []
    for signal in signals.values():
        for net in signal.nets:
            union.add(net)
            net_keys.append(net)

    constant_nets = {}

    def resolve(expression, where):
        expression = expression.strip()
        literal = _LITERAL.match(expression)
        if literal:
            digit = literal.group(1).lower()
            if digit not in ("0", "1"):
                raise VerilogError(
                    "literal {!r} in {} is neither 0 nor 1".format(expression, where)
                )
            key = "__const{}".format(digit)
            constant_nets[key] = int(digit)
            union.add(key)
            return key
        select = _BIT_SELECT.match(expression)
        if select:
            base, index = select.group(1), int(select.group(2))
            key = "{}({})".format(base, index)
        else:
            key = expression
        if key not in union.parent:
            raise VerilogError("undeclared net {!r} used in {}".format(expression, where))
        return key

    for match in _ASSIGN.finditer(body):
        left = resolve(match.group(1), "an assign statement")
        right = resolve(match.group(2), "an assign statement")
        union.union(left, right)

    instances = []
    for match in _INSTANCE.finditer(body):
        cell_type, instance_name, connections = match.group(1), match.group(2), match.group(3)
        if cell_type in ("module", "assign", "input", "output", "inout", "wire", "reg", "endmodule"):
            continue
        cell = library.get(cell_type)
        if cell is None:
            raise VerilogError(
                "gate type {!r} (instance {!r}) is not in gate library {!r}".format(
                    cell_type, instance_name, library.name
                )
            )
        ports = {}
        for connection in _PORT_CONNECTION.finditer(connections):
            pin, expression = connection.group(1), connection.group(2).strip()
            if not expression:
                continue
            if pin not in cell.pin_by_name:
                raise VerilogError(
                    "gate type {} has no pin {!r} (instance {!r})".format(
                        cell_type, pin, instance_name
                    )
                )
            ports[pin] = resolve(expression, "instance {!r}".format(instance_name))
        instances.append((cell_type, instance_name.lstrip("\\"), cell, ports))

    # collapse aliases
    canonical = {}
    for key in list(union.parent):
        canonical[key] = union.find(key)

    net_names = {}
    for key in net_keys:
        root = canonical[key]
        # prefer the shortest, then lexicographically smallest, alias as the name
        current = net_names.get(root)
        if current is None or (len(key), key) < (len(current), current):
            net_names[root] = key
    for key, value in constant_nets.items():
        net_names.setdefault(canonical[key], "1'b{}".format(value))

    gates = []
    for cell_type, instance_name, cell, ports in instances:
        inputs, outputs = {}, {}
        for pin_name, net in ports.items():
            pin = cell.pin_by_name[pin_name]
            if pin.is_supply:
                continue
            if pin.direction == "input":
                inputs[pin_name] = canonical[net]
            elif pin.direction == "output":
                outputs[pin_name] = canonical[net]
            else:
                raise VerilogError(
                    "pin {}.{} has unsupported direction {!r}".format(
                        cell_type, pin_name, pin.direction
                    )
                )
        gates.append(_build_gate(instance_name, cell, inputs, outputs))

    input_nets, output_nets = [], []
    for name, direction in sorted(directions.items()):
        for net in signals[name].nets:
            if direction == "input":
                input_nets.append(canonical[net])
            elif direction == "output":
                output_nets.append(canonical[net])
            else:
                input_nets.append(canonical[net])
                output_nets.append(canonical[net])

    # constant nets behave like driven nets: model them as zero-input gates
    for key, value in sorted(constant_nets.items()):
        gates.append(
            Gate(
                "const_{}".format(value),
                "const_{}".format(value),
                "<literal>",
                circuit_model.KIND_COMBINATIONAL,
                {},
                {"Z": canonical[key]},
                functions={"Z": (lambda _values, _value=value: _value)},
            )
        )

    return Circuit(
        top_module,
        gates,
        net_names,
        input_nets,
        output_nets,
        source=str(path),
        gate_library=library.name,
    )


def _build_gate(instance_name, cell, inputs, outputs):
    reason = cell.unsupported_reason()
    if reason is not None:
        return Gate(
            instance_name,
            instance_name,
            cell.name,
            circuit_model.KIND_UNSUPPORTED,
            inputs,
            outputs,
            unsupported_reason=reason,
            properties=cell.types,
        )

    try:
        if cell.is_ff:
            config = cell.ff_config
            ff = FlipFlop(
                expression_function(config["next_state"]),
                clock=expression_function(config["clocked_on"]) if config.get("clocked_on") else None,
                async_reset=expression_function(config["clear_on"]) if config.get("clear_on") else None,
                async_set=expression_function(config["preset_on"]) if config.get("preset_on") else None,
                state_pins=cell.pins_of_type("state"),
                neg_state_pins=cell.pins_of_type("neg_state"),
                clock_pins=cell.pins_of_type("clock"),
                clear_preset_state=_clear_preset_state(config.get("state_clear_preset")),
            )
            return Gate(
                instance_name,
                instance_name,
                cell.name,
                circuit_model.KIND_FF,
                inputs,
                outputs,
                ff=ff,
                properties=cell.types,
            )

        functions = {}
        for pin in cell.signal_pins("output"):
            functions[pin.name] = expression_function(pin.function)
        return Gate(
            instance_name,
            instance_name,
            cell.name,
            circuit_model.KIND_COMBINATIONAL,
            inputs,
            outputs,
            functions=functions,
            properties=cell.types,
        )
    except GateLibraryError as exc:
        raise VerilogError(
            "cannot model gate type {} (instance {}): {}".format(cell.name, instance_name, exc)
        )
    except CircuitError as exc:
        raise VerilogError(str(exc))


def _clear_preset_state(behaviour):
    if behaviour in ("L", "low", 0):
        return 0
    if behaviour in ("H", "high", 1):
        return 1
    return None
