#!/usr/bin/env python3
"""
validate_warmup.py -- prove gds2netlist.py correct on the warm-up design.

The warm-up ships both the "final" GDS (names stripped) and the DEF it was
written from, so the DEF is ground truth:

  * ``COMPONENTS`` gives the real instance name, cell and placement of every
    cell, which lets us put a name on every instance we extracted,
  * ``NETS`` gives the real connectivity: for every net, the list of
    ``(instance, pin)`` terminals plus any top-level ``PIN``.

We then check that the *partition of instance pins* produced by the geometric
extractor is identical to the DEF's partition -- i.e. that the two netlists are
isomorphic, not merely similar.

Usage::

    python validate_warmup.py [--gds warmup/04_final.gds]
                              [--def warmup/03_post_place_and_route.def]
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gds2netlist import Extractor, POWER_PINS  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def unescape(name: str) -> str:
    return name.replace("\\", "")


def parse_def(path):
    """Return (components, nets, pins).

    components: name -> dict(cell, x, y, orient)
    nets:       netname -> set((instance, pin)) and set of top PIN names
    pins:       pinname -> netname
    """
    components = {}
    nets = {}
    pins = {}

    with open(path) as fh:
        text = fh.read()

    def section(tag):
        m = re.search(r"^%s\s+\d+\s*;\s*$(.*?)^END %s\s*$" % (tag, tag),
                      text, re.S | re.M)
        return m.group(1) if m else ""

    # ---- COMPONENTS ----
    comp_re = re.compile(
        r"-\s+(\S+)\s+(\S+)\s+(.*?);", re.S)
    for m in comp_re.finditer(section("COMPONENTS")):
        name, cell, rest = unescape(m.group(1)), m.group(2), m.group(3)
        pm = re.search(r"(?:FIXED|PLACED|COVER)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+(\w+)", rest)
        if not pm:
            continue
        components[name] = {"cell": cell, "x": int(pm.group(1)),
                            "y": int(pm.group(2)), "orient": pm.group(3)}

    # ---- PINS ----
    for m in re.finditer(r"-\s+(\S+)\s+\+\s+NET\s+(\S+)", section("PINS")):
        pins[unescape(m.group(1))] = unescape(m.group(2))

    # ---- NETS ----
    body = section("NETS")
    # split on statement boundaries: each net starts with "- " at the margin
    for chunk in re.split(r"\n(?=\s*-\s)", body):
        chunk = chunk.strip()
        if not chunk.startswith("-"):
            continue
        head = chunk.split("+", 1)[0]          # terminals come before the first '+'
        nm = re.match(r"-\s+(\S+)", head)
        if not nm:
            continue
        netname = unescape(nm.group(1))
        terms = set()
        ports = set()
        for t in re.finditer(r"\(\s*(\S+)\s+(\S+)\s*\)", head):
            a, b = unescape(t.group(1)), unescape(t.group(2))
            if a == "PIN":
                ports.add(b)
            else:
                terms.add((a, b))
        nets[netname] = {"terminals": terms, "ports": ports}
    return components, nets, pins


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--gds", default=os.path.join(ROOT, "warmup", "04_final.gds"))
    ap.add_argument("--def", dest="deffile",
                    default=os.path.join(ROOT, "warmup", "03_post_place_and_route.def"))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    print("== extracting %s" % args.gds)
    ex = Extractor(args.gds, verbose=False).run()
    comps, def_nets, def_pins = parse_def(args.deffile)

    print("DEF   : %d components, %d nets, %d pins" % (len(comps), len(def_nets), len(def_pins)))
    print("GDS   : %d instances, %d nets, %d ports"
          % (len(ex.instances), len(ex.nets), len(ex.ports)))

    # ---------------------------------------------------------------
    # 1. match instances by (cell, placement)
    # ---------------------------------------------------------------
    by_key = collections.defaultdict(list)
    for name, c in comps.items():
        by_key[(c["cell"], c["x"], c["y"])].append(name)

    name_of_inst = {}
    unmatched_gds = []
    used = set()
    for inst in ex.instances:
        key = (inst["cell"], inst["x"], inst["y"])
        cands = [n for n in by_key.get(key, []) if n not in used]
        if not cands:
            unmatched_gds.append(inst)
            continue
        used.add(cands[0])
        name_of_inst[inst["name"]] = cands[0]
    unmatched_def = [n for n in comps if n not in used]

    print("\n-- instance matching --")
    print("matched            : %d / %d" % (len(name_of_inst), len(comps)))
    print("GDS without DEF    : %d" % len(unmatched_gds))
    print("DEF without GDS    : %d" % len(unmatched_def))
    for inst in unmatched_gds[:10]:
        print("   GDS-only:", inst["name"], inst["cell"], inst["x"], inst["y"], inst["orient"])
    for n in unmatched_def[:10]:
        print("   DEF-only:", n, comps[n])
    if unmatched_gds or unmatched_def:
        print("!! instance matching incomplete -- aborting connectivity check")
        return 1

    # orientation cross-check
    bad_orient = [(i["name"], i["orient"], comps[name_of_inst[i["name"]]]["orient"])
                  for i in ex.instances
                  if i["orient"] != comps[name_of_inst[i["name"]]]["orient"]]
    print("orientation mismatches: %d" % len(bad_orient))
    for b in bad_orient[:5]:
        print("   ", b)

    # ---------------------------------------------------------------
    # 2. build both partitions over (def_instance, pin)
    # ---------------------------------------------------------------
    # every signal pin that physically exists on every placed cell
    all_pins = set()
    for inst in ex.instances:
        dn = name_of_inst[inst["name"]]
        for pin in ex.cell_models[inst["cell"]].pins:
            if pin in POWER_PINS:
                continue
            all_pins.add((dn, pin))

    gds_net_of_pin = {}
    for inst in ex.instances:
        dn = name_of_inst[inst["name"]]
        for pin, net in inst["conns"].items():
            gds_net_of_pin[(dn, pin)] = net

    def_net_of_pin = {}
    def_unknown_terms = []
    for netname, info in def_nets.items():
        for t in info["terminals"]:
            if t not in all_pins:
                def_unknown_terms.append((netname, t))
            def_net_of_pin[t] = netname

    print("\n-- pin inventory --")
    print("signal pins on placed cells (from GDS cell labels): %d" % len(all_pins))
    print("pins named in DEF NETS                            : %d" % len(def_net_of_pin))
    print("DEF terminals with no matching GDS pin            : %d" % len(def_unknown_terms))
    for x in def_unknown_terms[:10]:
        print("   ", x)

    unconnected_in_def = sorted(all_pins - set(def_net_of_pin))
    print("pins present in GDS but absent from DEF NETS      : %d" % len(unconnected_in_def))
    for x in unconnected_in_def[:10]:
        print("   ", x, "-> gds net", gds_net_of_pin.get(x))

    # ---------------------------------------------------------------
    # 3. partition isomorphism
    # ---------------------------------------------------------------
    shared = sorted(set(def_net_of_pin) & all_pins)
    def_groups = collections.defaultdict(set)
    gds_groups = collections.defaultdict(set)
    for p in shared:
        def_groups[def_net_of_pin[p]].add(p)
        gds_groups[gds_net_of_pin[p]].add(p)

    d2g = {}
    g2d = {}
    ok_pins = 0
    bad = []
    for dn, members in def_groups.items():
        gnames = {gds_net_of_pin[p] for p in members}
        if len(gnames) != 1:
            bad.append(("split", dn, sorted(gnames)))
            continue
        gn = gnames.pop()
        if gn in g2d and g2d[gn] != dn:
            bad.append(("merged", dn, gn, g2d[gn]))
            continue
        if gds_groups[gn] != members:
            bad.append(("extra-members", dn, gn,
                        sorted(gds_groups[gn] - members)))
            continue
        d2g[dn] = gn
        g2d[gn] = dn
        ok_pins += len(members)

    print("\n-- net partition isomorphism (over %d shared pins) --" % len(shared))
    print("DEF nets with cell terminals : %d" % len(def_groups))
    print("bijectively matched nets     : %d" % len(d2g))
    print("pins in correctly matched nets: %d / %d  (%.2f%%)"
          % (ok_pins, len(shared), 100.0 * ok_pins / max(1, len(shared))))
    print("defects                      : %d" % len(bad))
    for b in bad[:20]:
        print("   ", b)

    # ---------------------------------------------------------------
    # 4. port names
    # ---------------------------------------------------------------
    print("\n-- top-level ports --")
    port_problems = []
    for pin, netname in sorted(def_pins.items()):
        if pin in POWER_PINS:
            continue
        info = def_nets.get(netname)
        if not info or not info["terminals"]:
            continue
        term = sorted(info["terminals"])[0]
        gn = gds_net_of_pin.get(term)
        status = "OK" if gn == pin else "MISMATCH"
        if gn != pin:
            port_problems.append((pin, netname, gn))
        print("   %-8s DEF net %-10s -> GDS net %-12s %s" % (pin, netname, gn, status))

    # ---------------------------------------------------------------
    # 5. cell-level pin name check against 01_netlist.v (optional)
    # ---------------------------------------------------------------
    vpath = os.path.join(ROOT, "warmup", "01_netlist.v")
    if os.path.exists(vpath):
        vnets = parse_verilog_conns(vpath)
        vshared = sorted(set(vnets) & all_pins)
        vgroups = collections.defaultdict(set)
        for p in vshared:
            vgroups[vnets[p]].add(p)
        vbad = 0
        for vn, members in vgroups.items():
            gnames = {gds_net_of_pin[p] for p in members}
            if len(gnames) != 1 or gds_groups.get(gnames.copy().pop(), set()) != members:
                vbad += 1
        print("\n-- cross-check against 01_netlist.v --")
        print("verilog terminals shared with GDS: %d" % len(vshared))
        print("verilog nets: %d, mismatching: %d" % (len(vgroups), vbad))

    ok = (not bad and not unmatched_gds and not unmatched_def
          and not def_unknown_terms and not port_problems and not bad_orient)
    print("\nRESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def parse_verilog_conns(path):
    """(instance, pin) -> net from a flat structural Verilog netlist."""
    with open(path) as fh:
        text = fh.read()
    text = re.sub(r"//.*", "", text)
    out = {}
    inst_re = re.compile(
        r"(sky130_\w+)\s+(\\?\S+?)\s*\((.*?)\)\s*;", re.S)
    for m in inst_re.finditer(text):
        inst = unescape(m.group(2)).strip()
        for c in re.finditer(r"\.(\w+)\s*\(\s*([^)]*?)\s*\)", m.group(3)):
            net = unescape(c.group(2)).strip()
            if net:
                out[(inst, c.group(1))] = net
    return out


if __name__ == "__main__":
    raise SystemExit(main())
