#!/usr/bin/env python3
"""Strip every identifier from netlist.hal.v that leaks the original design.

Quartus is generous to a reverse engineer: it keeps RTL signal names, so the
export literally contains `crc_r[7:0]`, an instance called `feedback` and a
`reduce_nor_0`.  A walkthrough that leans on those names teaches nothing, and a
real target (an obfuscated netlist, a netlist recovered from a bitstream, a
design built with name mangling on) will not have them.

So the walkthrough runs on *two* copies of the same circuit:

  netlist.hal.v        as exported -- names intact, used only to check answers
  netlist.anon.hal.v   identical structure, every design name replaced

Only the things a reverse engineer genuinely has are preserved:

  * the gate types and their pin names (`tennm_ff.clk`, `.ena`, `.clrn`, ...),
    because those come from the device, not from the design;
  * the `lut_mask` / `extended_lut` / `shared_arith` parameters, likewise;
  * the connectivity, exactly;
  * top-level port direction, width and bus grouping -- package pins really are
    grouped and ordered, so pretending otherwise would be unfair the other way;
  * `gnd`/`vcc` and the `devclrn`/`devpor`/`devoe` housekeeping nets.

Everything else becomes `top`, `port_i<n>`, `port_o<n>`, `n<n>`, `u<n>`, and the
numbering is deliberately scrambled so that neither the instance order nor an
internal vector declaration hands over which flip-flops form a word.

    python anonymize.py netlist/netlist.hal.v netlist/netlist.anon.hal.v
"""

import re
import sys

# Nets HAL/Quartus need structurally; they carry no design information.
KEEP_NETS = {"gnd", "vcc", "devclrn", "devpor", "devoe", "unknown"}
GATE_TYPES = {"tennm_ff", "tennm_lcell_comb", "HAL_GND", "HAL_VCC"}

# `\escaped name ` or a plain identifier
IDENT = re.compile(r"\\[^\s]+\s|[A-Za-z_][A-Za-z0-9_$]*")

INSTANCE = re.compile(
    r"^(?:tennm_ff|tennm_lcell_comb)\s*#\((?:[^()]|\([^()]*\))*\)\s*"
    r"(\\[^\s]+\s|[A-Za-z_][\w$]*)\s*\(",
    re.M,
)

PORT_DECL = re.compile(
    r"^(input|output)\s*(\[[^\]]*\]\s*)?(\\[^\s]+\s|[A-Za-z_][\w$]*)\s*;", re.M
)
WIRE_DECL = re.compile(
    r"^wire\s*(\[[^\]]*\]\s*)?(\\[^\s]+\s|[A-Za-z_][\w$]*)\s*;", re.M
)


def _norm(token):
    """Escaped Verilog identifiers carry a leading backslash and a trailing space."""
    return token[1:].rstrip() if token.startswith("\\") else token


def _scramble(items):
    """A fixed, version-independent permutation: stride by the first coprime > 1."""
    count = len(items)
    stride = next((s for s in range(2, count) if _gcd(s, count) == 1), 1)
    return [items[(stride * k + 3) % count] for k in range(count)]


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def rename_instances(text):
    """Replace instance names by `u<n>` positionally (they may clash with nets)."""
    spans = [match.span(1) for match in INSTANCE.finditer(text)]
    labels = _scramble(["u%d" % index for index in range(len(spans))])
    for (start, end), label in sorted(zip(spans, labels), reverse=True):
        trailer = " " if text[end - 1].isspace() else ""
        text = text[:start] + label + trailer + text[end:]
    return text


def split_internal_buses(text):
    """Turn every internal `wire [h:l] name;` into unrelated scalar wires.

    Quartus keeps the RTL's vector declaration, so even after renaming, the bits
    of a register bank would arrive pre-grouped and pre-ordered.  A reverse
    engineer has to *recover* which flip-flops form a word and in which order,
    so the exercise removes that hint.  Only internal wires are split; top-level
    bus ports stay buses.
    """
    ports = {_norm(name) for _, _, name in PORT_DECL.findall(text)}

    for decl, high, low, name in re.findall(
        r"^(wire\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*([A-Za-z_][\w$]*)\s*;)$", text, re.M
    ):
        if name in ports:
            continue
        bits = list(range(int(low), int(high) + 1))
        for bit in bits:
            text = text.replace("%s[%d]" % (name, bit), "__bit_%s_%d__" % (name, bit))
        text = text.replace(
            decl,
            "\n".join("wire __bit_%s_%d__;" % (name, bit) for bit in _scramble(bits)),
        )
    return text


def build_map(text):
    """Collect module name, ports and wires -> new names."""
    rename = {}

    module = re.search(r"^module\s+([A-Za-z_][\w$]*)\s*\(", text, re.M)
    if not module:
        raise SystemExit("no module header found")
    rename[module.group(1)] = "top"

    inputs, outputs = [], []
    for direction, _, name in PORT_DECL.findall(text):
        (inputs if direction == "input" else outputs).append(_norm(name))
    for index, name in enumerate(inputs):
        rename[name] = "port_i%d" % index
    for index, name in enumerate(outputs):
        rename[name] = "port_o%d" % index

    wires = []
    for _, name in WIRE_DECL.findall(text):
        name = _norm(name)
        if name in KEEP_NETS or name in rename or name in wires:
            continue
        wires.append(name)
    for index, name in enumerate(_scramble(wires)):
        rename[name] = "n%d" % index
    return rename


def apply_map(text, rename):
    out, pos = [], 0
    for match in IDENT.finditer(text):
        token = match.group(0)
        name = _norm(token)
        if name in GATE_TYPES or name in KEEP_NETS or name not in rename:
            continue
        if match.start() and text[match.start() - 1] == ".":
            # A pin or parameter name: a device property, not a design name.
            continue
        out.append(text[pos:match.start()])
        out.append(rename[name])
        if token.startswith("\\") and token[-1].isspace():
            out.append(token[-1])
        pos = match.end()
    out.append(text[pos:])
    return "".join(out)


HEADER = """\
// Structurally identical to netlist.hal.v with every design-specific identifier
// removed, internal vectors split into unrelated scalars and both instance and
// net numbering scrambled (see anonymize.py).  This is the file the walkthrough
// analyses: what a netlist looks like when the vendor tool did not hand you the
// RTL names.
// Load with the AGILEX_TENNM gate library.

"""


def main(argv):
    if len(argv) != 3:
        raise SystemExit(__doc__)
    text = open(argv[1]).read()
    text = re.sub(r"\A(//[^\n]*\n)+", "", text)   # the header names the source
    text = rename_instances(text)
    text = split_internal_buses(text)
    text = apply_map(text, build_map(text))
    open(argv[2], "w").write(HEADER + text.lstrip("\n"))
    print(argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
