#!/usr/bin/env python3
"""Strip every identifier from ``netlist.hal.v`` that a real target would not carry.

Quartus keeps RTL identifiers in its EDA export: the state flip-flops in this
walkthrough come out named ``state.S_RED``, ``state.S_GREEN`` and so on, and the
dwell counter comes out as a bus called ``tick``.  That is a gift you do not get
from a netlist recovered out of a bitstream, and reverse-engineering a design
whose registers are already labelled with their meaning teaches nothing.

This script produces ``netlist_anon.hal.v``: the *same* netlist -- same gate
types, same parameters, same connectivity -- with

  * the module and all of its ports renamed (``top``, ``i0..``, ``o0..``),
  * every instance renamed ``g<N>`` and every internal net renamed ``n<N>``,
    numbered in order of first appearance,
  * vector wires split into scalars, so the bus grouping of ``tick[3:0]``
    is not a free hint either.

Gate types, pin names, parameter names, literals and the constant nets
(``gnd``, ``vcc``, ``devclrn``, ``devpor``, ``devoe``) are left alone: those are
properties of the device, not of the design.

    python anonymize.py netlist.hal.v -o netlist_anon.hal.v -m anonymize_map.json

The map is written out so the guide can un-blind the result at the end and so
``check.py`` can verify the rename is a pure relabelling.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

KEYWORDS = {
    "module", "endmodule", "input", "output", "inout", "wire", "reg",
    "assign", "defparam", "parameter", "tri1", "supply0", "supply1",
}
GATE_TYPES = {"tennm_ff", "tennm_lcell_comb", "HAL_GND", "HAL_VCC"}
DEVICE_NETS = {"gnd", "vcc", "devclrn", "devpor", "devoe", "unknown"}
KEEP = KEYWORDS | GATE_TYPES | DEVICE_NETS

#: ``\escaped ident `` (terminated by whitespace) or a plain identifier.
TOKEN = re.compile(r"(\\\S+[ \t\n])|([A-Za-z_][A-Za-z0-9_$]*)")
#: string literals and sized literals such as ``64'hBBBB`` / ``1'b0``
PROTECTED = re.compile(r"\"[^\"\n]*\"|\d*'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_]+")
VECTOR_DECL = re.compile(r"^(input|output|wire)\s+\[(\d+):(\d+)\]\s+(\w+)\s*;", re.M)
INSTANCE = re.compile(
    r"^(?:tennm_ff|tennm_lcell_comb|HAL_GND|HAL_VCC)\s+(?:#\(.*\)\s*)?(\\\S+[ \t]|\w+)\s*\(",
    re.M,
)


def protected_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in PROTECTED.finditer(text)]


def in_span(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def expand_vectors(text: str) -> str:
    """Turn ``wire [3:0] tick;`` into scalar wires and rewrite ``tick[i]``."""
    expanded: dict[str, list[int]] = {}

    def repl(m: re.Match) -> str:
        kind, hi, lo, name = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        idx = list(range(min(lo, hi), max(lo, hi) + 1))
        expanded[name] = idx
        return "\n".join(f"{kind} {name}$b{i};" for i in idx)

    text = VECTOR_DECL.sub(repl, text)
    for name, idx in expanded.items():
        for i in idx:
            text = re.sub(rf"(?<![\w$]){re.escape(name)}\[{i}\]", f"{name}$b{i}", text)
    return text


def token_name(raw: str) -> tuple[str, str]:
    """Split a matched token into (identifier, trailing whitespace)."""
    if raw.startswith("\\"):
        name = raw[1:].rstrip()
        return name, raw[len(name) + 1:]
    return raw, ""


def instance_offsets(text: str) -> set[int]:
    """Character offsets at which an *instance name* token starts.

    Verilog keeps instance names and net names in different namespaces, and this
    netlist uses that: the flip-flop instance ``\\tick[3]`` drives the net
    ``tick[3]``.  Renaming has to keep the two apart, so instance names are
    located by position rather than by spelling.
    """
    return {m.start(1) for m in INSTANCE.finditer(text)}


def build_maps(text: str, ports: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Assign opaque names in order of first appearance (nets, then instances)."""
    nets: dict[str, str] = {"traffic_fsm": "top"}
    gates: dict[str, str] = {}
    n_in = n_out = 0
    for port in ports:
        if re.search(rf"^input\s+{re.escape(port)}\s*;", text, re.M):
            nets[port] = f"i{n_in}"
            n_in += 1
        else:
            nets[port] = f"o{n_out}"
            n_out += 1

    inst_at = instance_offsets(text)
    spans = protected_spans(text)
    n_gate = n_net = 0
    for m in TOKEN.finditer(text):
        if in_span(m.start(), spans):
            continue
        if m.start() > 0 and text[m.start() - 1] in ".'":
            continue  # a pin/parameter name, or the base of a literal
        name, _ = token_name(m.group(0))
        if name in KEEP:
            continue
        if m.start() in inst_at:
            if name not in gates:
                n_gate += 1
                gates[name] = f"g{n_gate}"
        elif name not in nets:
            n_net += 1
            nets[name] = f"n{n_net}"
    return nets, gates


def apply_maps(text: str, nets: dict[str, str], gates: dict[str, str]) -> str:
    inst_at = instance_offsets(text)
    spans = protected_spans(text)
    out: list[str] = []
    pos = 0
    for m in TOKEN.finditer(text):
        out.append(text[pos:m.start()])
        pos = m.end()
        raw = m.group(0)
        if in_span(m.start(), spans) or (m.start() > 0 and text[m.start() - 1] in ".'"):
            out.append(raw)
            continue
        name, tail = token_name(raw)
        table = gates if m.start() in inst_at else nets
        new = table.get(name)
        out.append((new + tail) if new else raw)
    out.append(text[pos:])
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("netlist", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("-m", "--map", type=Path, default=None)
    args = ap.parse_args(argv)

    text = args.netlist.read_text(encoding="utf-8")
    # Drop the provenance header: it names the source design.
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("//"))
    body = expand_vectors(body)

    header = re.search(r"module\s+(\w+)\s*\(([^)]*)\)\s*;", body, re.S)
    if header is None:
        print("no module header found", file=sys.stderr)
        return 1
    ports = [p.strip() for p in header.group(2).split(",") if p.strip()]

    nets, gates = build_maps(body, ports)
    result = apply_maps(body, nets, gates)
    result = (
        "// Anonymized netlist: identical structure to netlist.hal.v with every\n"
        "// design-derived identifier replaced.  Produced by anonymize.py.\n"
        "// Load with plugins/gate_libraries/definitions/AGILEX_TENNM.hgl.\n"
        + result.lstrip("\n")
    )
    args.output.write_text(result, encoding="utf-8")
    if args.map:
        args.map.write_text(
            json.dumps({"nets": nets, "gates": gates}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
