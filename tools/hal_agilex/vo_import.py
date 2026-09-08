"""Rewrite a Quartus ``.vo`` export into Verilog that HAL can read.

HAL's Verilog parser is a structural netlist reader.  Three things the Quartus
EDA netlist writer emits are outside what it accepts, and each is handled here
in a way that is either exact or refused:

``tri1``/``tri0`` net declarations
    ``devclrn``, ``devpor`` and ``devoe`` are declared as ``tri1``.  They become
    an ordinary ``wire`` with a continuous assignment to ``1'b1``, which is the
    same value the pull-up gives them in a simulation where nothing drives them.

inverted port connections (``.datac(~x)``)
    HAL has no notion of an inverted connection.  For the six ALM data inputs
    the inversion is *absorbed into the LUT mask* (see
    :func:`primitives.absorb_input_inversions`): mask bit ``j`` moves to bit
    ``j ^ (1 << i)``, which relabels the truth table exactly.  An inversion on
    any other pin -- ``cin``, ``sharein``, or any pin of a primitive whose
    semantics this package does not model -- is **refused**, because absorbing
    it would require inventing an inverter that the vendor netlist does not
    contain.

unconnected pins (``.combout()``)
    dropped, which is what an unconnected pin means.

Everything else is passed through unchanged, and any instance of a primitive
outside :data:`primitives.COVERED_PRIMITIVES` makes the conversion fail with the
list of offending types: an import that silently dropped a RAM or a DSP block
would produce a netlist that analyses cleanly and means nothing.
"""

import re

from . import primitives
from .vo_netlist import Bit, Const, parse_file

__all__ = ["ImportRefused", "GATE_LIBRARY_NAME", "convert", "convert_file"]

#: The gate library the emitted netlist must be loaded with.
GATE_LIBRARY_NAME = "AGILEX_TENNM"

_PLAIN_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class ImportRefused(Exception):
    """The export cannot be converted without inventing semantics."""


def _escape(name):
    if _PLAIN_IDENTIFIER_RE.match(name):
        return name
    return "\\{} ".format(name)


def _bit_text(bit):
    if isinstance(bit, Const):
        if bit.value is None:
            raise ImportRefused("an x/z literal cannot be represented in the imported netlist")
        return "1'b{}".format(bit.value)
    if bit.inverted:
        raise ImportRefused("internal error: an inversion survived the rewrite")
    if bit.index is None:
        return _escape(bit.name)
    return "{}[{}]".format(_escape(bit.name), bit.index)


def _expression_text(bits):
    if len(bits) == 1:
        return _bit_text(bits[0])
    return "{{{}}}".format(", ".join(_bit_text(bit) for bit in bits))


def _parameter_text(name, value):
    if isinstance(value, str):
        return '.{} ("{}")'.format(name, value)
    if name == "lut_mask":
        return ".{} (64'h{:016X})".format(name, value)
    return ".{} ({})".format(name, value)


def _rewrite_instance(instance):
    """Return (connections, parameters) with every inversion removed."""
    connections = {}
    inverted_pins = []
    for pin, bits in instance.connections.items():
        if not bits:
            continue
        clean = []
        for bit in bits:
            if isinstance(bit, Bit) and bit.inverted:
                inverted_pins.append(pin)
                clean.append(Bit(bit.name, bit.index, False))
            else:
                clean.append(bit)
        connections[pin] = clean

    if not inverted_pins:
        return connections, dict(instance.parameters)

    if instance.type != primitives.LCELL:
        raise ImportRefused(
            "instance {} of type {} has inverted connections on {}; only the data "
            "inputs of {} can absorb an inversion".format(
                instance.name,
                instance.type,
                ", ".join(sorted(set(inverted_pins))),
                primitives.LCELL,
            )
        )

    offending = sorted(set(inverted_pins) - set(primitives.LCELL_DATA_PINS))
    if offending:
        raise ImportRefused(
            "instance {} has inverted connections on {}, which do not address the "
            "LUT mask and cannot be absorbed".format(instance.name, ", ".join(offending))
        )

    uses_arithmetic = any(instance.connections.get(pin) for pin in ("sumout", "cout"))
    select_pins = sorted(set(inverted_pins) & {"datae", "dataf"})
    if uses_arithmetic and select_pins:
        # In arithmetic mode datae/dataf choose between the propagate and the
        # generate half of the mask, so absorbing an inversion there would swap
        # the halves rather than relabel one input.
        raise ImportRefused(
            "instance {} uses sumout/cout and has an inverted connection on {}; "
            "absorbing it would exchange the arithmetic mask halves".format(
                instance.name, ", ".join(select_pins)
            )
        )

    parameters = dict(instance.parameters)
    mask = parameters.get("lut_mask")
    if mask is None:
        raise ImportRefused(
            "instance {} has inverted data inputs but no lut_mask".format(instance.name)
        )
    parameters["lut_mask"] = primitives.absorb_input_inversions(mask, inverted_pins)
    return connections, parameters


def _consumed_nets(netlist):
    """Keys of every net that is read by an instance or a continuous assignment."""
    consumed = set()
    for instance in netlist.instances:
        for bits in instance.connections.values():
            for bit in bits:
                if isinstance(bit, Bit):
                    consumed.add(bit.key)
    for _, source in netlist.assignments:
        for bit in source:
            if isinstance(bit, Bit):
                consumed.add(bit.key)
    return consumed


def convert(netlist, source_note=None):
    """Return HAL-readable Verilog text for a parsed ``.vo`` netlist."""
    uncovered = sorted(
        {
            instance.type
            for instance in netlist.instances
            if instance.type not in primitives.COVERED_PRIMITIVES
        }
    )
    if uncovered:
        raise ImportRefused(
            "netlist {} instantiates {} which this package does not model: {}. "
            "Run the coverage inventory to get a findings document naming every "
            "one of them; this importer will not emit a netlist that looks "
            "analysable but is not.".format(
                netlist.name,
                "a primitive" if len(uncovered) == 1 else "primitives",
                ", ".join(uncovered),
            )
        )

    lines = []
    lines.append("// Generated by tools/hal_agilex from a Quartus Prime Pro EDA netlist export.")
    lines.append("// Load with the {} gate library.".format(GATE_LIBRARY_NAME))
    if source_note:
        lines.append("// source: {}".format(source_note))
    lines.append(
        "// Inversions on ALM data inputs were absorbed into lut_mask; tri1 nets "
        "became wires tied to 1'b1."
    )
    lines.append("")

    port_list = ", ".join(_escape(name) for name in netlist.port_order)
    lines.append("module {} ({});".format(_escape(netlist.name), port_list))

    for name in netlist.port_order:
        direction, msb, lsb = netlist.declarations[name]
        if direction is None:
            raise ImportRefused("port {} has no direction declaration".format(name))
        span = "" if msb is None else "[{}:{}] ".format(msb, lsb)
        lines.append("{} {}{};".format(direction, span, _escape(name)))

    for name, (direction, msb, lsb) in sorted(netlist.declarations.items()):
        if direction is not None:
            continue
        span = "" if msb is None else "[{}:{}] ".format(msb, lsb)
        lines.append("wire {}{};".format(span, _escape(name)))

    lines.append("")
    consumed = _consumed_nets(netlist)
    for target, source in netlist.assignments:
        if any(isinstance(bit, Const) and bit.value is None for bit in source):
            # Quartus declares an `unknown` net and drives it with 1'bx.  It is
            # dropped only while nothing reads it; a used x/z would change the
            # meaning of the netlist and is refused instead.
            unread = [bit.key for bit in target if bit.key in consumed]
            if unread:
                raise ImportRefused(
                    "net(s) {} are driven by an x/z literal and read by the "
                    "design".format(", ".join(sorted(unread)))
                )
            lines.append(
                "// dropped: assign {} = 1'bx;  (declared by Quartus, read by "
                "nothing)".format(_expression_text(target))
            )
            continue
        lines.append(
            "assign {} = {};".format(_expression_text(target), _expression_text(source))
        )

    lines.append("")
    for instance in netlist.instances:
        connections, parameters = _rewrite_instance(instance)
        parameter_text = ""
        if parameters:
            parameter_text = " #({})".format(
                ", ".join(
                    _parameter_text(name, parameters[name]) for name in sorted(parameters)
                )
            )
        pins = ", ".join(
            ".{} ({})".format(pin, _expression_text(connections[pin]))
            for pin in sorted(connections)
        )
        lines.append(
            "{}{} {} ({});".format(
                instance.type, parameter_text, _escape(instance.name), pins
            )
        )

    lines.append("")
    lines.append("endmodule")
    lines.append("")
    return "\n".join(lines)


def convert_file(path, output_path=None, source_note=None):
    """Convert a ``.vo`` file; write it when *output_path* is given."""
    netlist = parse_file(path)
    text = convert(netlist, source_note=source_note)
    if output_path is not None:
        with open(str(output_path), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    return text
