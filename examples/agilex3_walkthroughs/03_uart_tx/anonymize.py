#!/usr/bin/env python3
"""Strip every design-derived name from an imported Agilex netlist.

Why this exists
---------------
`quartus_eda` writes a *post-synthesis* netlist, and post-synthesis netlists
keep the RTL's identifiers: `netlist.hal.v` still says `shreg[3]`, `baud_cnt`,
`busy` and `tx_data`.  That is convenient for debugging a flow and useless for
teaching reverse engineering -- a walkthrough that reads the answer off the net
names has not reverse engineered anything.

A netlist recovered from a *bitstream* has none of that.  It has cells, nets,
and the pins of the package.  So this script produces the same circuit with
that information removed:

* the module becomes `top`;
* every port becomes a scalar `PORT_nn` -- buses are exploded, because a
  bitstream gives you pins, not vectors;
* every internal net becomes a scalar `n_nnn` -- again exploded, because the
  bus grouping of `shreg`/`baud_cnt`/`bit_cnt` is exactly what the exercise is
  supposed to *recover*;
* every gate instance becomes `g_nnn`;
* the order of the new names is scrambled by a salted hash of the old name, so
  neighbouring indices carry no information either.

Structure, gate types, parameters (`lut_mask`, `extended_lut`, ...) and the
constant nets `gnd`/`vcc`/`devclrn`/`devpor`/`devoe` are untouched: those are
device facts, not design names.

The mapping is written next to the output as `<output>.map.json` so the guide
can check its recovered names against ground truth *after* the analysis --
never during it.

Usage:
    python anonymize.py netlist.hal.v -o netlist_anon.hal.v
"""

import argparse
import hashlib
import json
import re
import sys

# Nets that describe the device, not the design.  Keeping them makes the output
# readable without leaking anything: every Agilex export has exactly these.
KEEP = {"gnd", "vcc", "devclrn", "devpor", "devoe", "unknown"}

SALT = "hal_agilex_walkthrough_03"

IDENT = r"(?:\\\S+\s|[A-Za-z_][A-Za-z0-9_$]*)"
BITREF = re.compile(r"(?P<name>" + IDENT + r")(?:\[(?P<idx>\d+)\])?")
GATE = re.compile(
    r"^(?P<type>tennm_\w+)\s+#\((?P<params>.*?)\)\s+"
    r"(?P<name>\\\S+\s|[A-Za-z_][A-Za-z0-9_$]*)\s*\((?P<conns>.*)\);\s*$"
)
DECL = re.compile(r"^(?P<kind>input|output|inout|wire)\s+(?:\[(?P<hi>\d+):(?P<lo>\d+)\]\s+)?(?P<name>\S+?)\s*;\s*$")
ASSIGN = re.compile(r"^assign\s+(?P<lhs>.+?)\s*=\s*(?P<rhs>.+?)\s*;\s*$")


def unescape(tok):
    """`\\foo ` -> `foo`; plain identifiers pass through."""
    tok = tok.strip()
    return tok[1:] if tok.startswith("\\") else tok


def escape(name):
    """Emit a Verilog identifier, escaping when it is not a plain one."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
        return name
    return "\\" + name + " "


def rank(name):
    return hashlib.sha256((SALT + "|" + name).encode()).hexdigest()


def parse(text):
    """Return (ports, decls, assigns, gates) from a hal_agilex-imported netlist."""
    ports, decls, assigns, gates = [], [], [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("module "):
            inner = line[line.index("(") + 1 : line.rindex(")")]
            ports = [unescape(p) for p in inner.split(",")]
            continue
        if line == "endmodule":
            continue
        m = GATE.match(line)
        if m:
            gates.append(m)
            continue
        m = DECL.match(line)
        if m:
            decls.append(m)
            continue
        m = ASSIGN.match(line)
        if m:
            assigns.append(m)
            continue
        raise SystemExit("anonymize.py: unrecognised line: " + line)
    return ports, decls, assigns, gates


def bits_of(decl):
    name = unescape(decl.group("name"))
    if decl.group("hi") is None:
        return [(name, None)]
    hi, lo = int(decl.group("hi")), int(decl.group("lo"))
    return [(name, i) for i in range(min(hi, lo), max(hi, lo) + 1)]


def key(name, idx):
    return name if idx is None else "{}[{}]".format(name, idx)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("netlist", help="a netlist produced by `hal_agilex import`")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--module", default="top", help="name for the anonymised module")
    args = ap.parse_args(argv)

    text = open(args.netlist, encoding="utf-8").read()
    ports, decls, assigns, gates = parse(text)

    port_names = set(ports)
    port_bits, net_bits = [], []
    directions = {}
    for d in decls:
        kind = d.group("kind")
        name = unescape(d.group("name"))
        if kind in ("input", "output", "inout"):
            directions[name] = kind
            port_bits.extend(bits_of(d))
        elif name not in port_names and name not in KEEP:
            net_bits.extend(bits_of(d))

    # scrambled, deterministic naming
    port_map, net_map = {}, {}
    for i, (n, idx) in enumerate(sorted(port_bits, key=lambda b: rank(key(*b)))):
        port_map[key(n, idx)] = "PORT_{:02d}".format(i)
    for i, (n, idx) in enumerate(sorted(net_bits, key=lambda b: rank(key(*b)))):
        net_map[key(n, idx)] = "n_{:03d}".format(i)

    gate_map = {}
    ordered_gates = sorted(gates, key=lambda g: rank(unescape(g.group("name"))))
    for i, g in enumerate(ordered_gates):
        gate_map[unescape(g.group("name"))] = "g_{:03d}".format(i)

    def sub_net(expr):
        expr = expr.strip()
        if expr in ("", "1'b0", "1'b1", "1'bx"):
            return expr
        if expr.startswith("\\"):
            # An escaped identifier runs to the next whitespace; any brackets
            # inside it are part of the name, not a bit select.
            name, idx = expr[1:], None
        else:
            m = BITREF.fullmatch(expr)
            if not m:
                raise SystemExit("anonymize.py: unrecognised net expression: " + expr)
            name = unescape(m.group("name"))
            idx = int(m.group("idx")) if m.group("idx") else None
        k = key(name, idx)
        if name in KEEP:
            return name
        if k in port_map:
            return port_map[k]
        if k in net_map:
            return net_map[k]
        raise SystemExit("anonymize.py: undeclared net: " + k)

    out = []
    out.append("// Anonymised netlist: same circuit as the import, no design names.")
    out.append("// Produced by anonymize.py from " + args.netlist + ".")
    out.append("// Ports, nets and instances are renamed; buses are exploded to scalars;")
    out.append("// gate types and parameters are untouched.  Name map: <output>.map.json")
    out.append("")

    new_ports = [port_map[key(n, i)] for n, i in port_bits]
    new_ports.sort(key=lambda p: int(p.split("_")[1]))
    out.append("module {} ({});".format(args.module, ", ".join(new_ports)))
    for n, idx in sorted(port_bits, key=lambda b: port_map[key(*b)]):
        out.append("{} {};".format(directions[n], port_map[key(n, idx)]))
    for n, idx in sorted(net_bits, key=lambda b: net_map[key(*b)]):
        out.append("wire {};".format(net_map[key(n, idx)]))
    for n in sorted(KEEP):
        if any(unescape(d.group("name")) == n for d in decls):
            out.append("wire {};".format(n))
    out.append("")

    for a in assigns:
        out.append("assign {} = {};".format(sub_net(a.group("lhs")), sub_net(a.group("rhs"))))
    out.append("")

    for g in ordered_gates:
        conns = []
        for part in g.group("conns").split(","):
            part = part.strip()
            pm = re.fullmatch(r"\.(?P<pin>\w+)\s*\((?P<net>.*)\)", part)
            if not pm:
                raise SystemExit("anonymize.py: unrecognised connection: " + part)
            conns.append(".{} ({})".format(pm.group("pin"), sub_net(pm.group("net"))))
        out.append(
            "{} #({}) {} ({});".format(
                g.group("type"),
                g.group("params"),
                gate_map[unescape(g.group("name"))],
                ", ".join(conns),
            )
        )
    out.append("")
    out.append("endmodule")

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")

    mapping = {
        "source": args.netlist,
        "module": {unescape(ports[0]) if False else "uart_tx": args.module},
        "ports": port_map,
        "nets": net_map,
        "gates": gate_map,
    }
    with open(args.output + ".map.json", "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, indent=2, sort_keys=True)

    print(args.output)
    print(args.output + ".map.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
