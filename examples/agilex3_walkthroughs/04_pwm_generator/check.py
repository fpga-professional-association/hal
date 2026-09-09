#!/usr/bin/env python3
"""Headless smoke test for walkthrough 04 (08-bit PWM generator).

Everything asserted here is a claim the guide makes, restated as a test, so a
CI run notices when a regenerated netlist or a changed tool stops supporting
the walkthrough.

It needs **no HAL build and no Quartus** -- only `tools/hal_agilex`, which is
pure standard library.  From the repository root:

    python examples/agilex3_walkthroughs/04_pwm_generator/check.py

Exit code 0 = every check passed.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools"))

from hal_agilex import behavior, inventory, primitives, simulate, vo_import, vo_netlist  # noqa: E402

VO = os.path.join(HERE, "netlist", "pwm_generator.vo")
HAL_V = os.path.join(HERE, "netlist", "netlist.hal.v")
REFERENCE = os.path.join(HERE, "artifacts", "recovered_reference.py")

WIDTH = 8
MASK = (1 << WIDTH) - 1

_failures = []
_checks = 0


def check(label, condition, detail=""):
    global _checks
    _checks += 1
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        _failures.append(label)


def carry_chains(netlist):
    """Follow cout -> cin over the ALM instances; returns a list of chains."""
    cells = netlist.instances_of_type(primitives.LCELL)
    producer = {}
    for cell in cells:
        bit = cell.single("cout")
        if bit is not None and not isinstance(bit, vo_netlist.Const):
            producer[bit.key] = cell
    successor = {}
    predecessors = set()
    for cell in cells:
        bit = cell.single("cin")
        if bit is None or isinstance(bit, vo_netlist.Const):
            continue
        source = producer.get(bit.key)
        if source is not None:
            successor[source.name] = cell
            predecessors.add(cell.name)
    chains = []
    for cell in cells:
        if cell.name in predecessors:
            continue
        chain, current = [], cell
        while current is not None:
            chain.append(current)
            current = successor.get(current.name)
        if len(chain) > 1:
            chains.append(chain)
    return chains


def main():
    print("walkthrough 04 -- pwm_generator")

    netlist = vo_netlist.parse_file(VO)

    # ---- 1. the export is inside the modelled primitive coverage ------------
    print("\n[1] primitive coverage")
    histogram = netlist.type_histogram()
    check("18 tennm_lcell_comb", histogram.get("tennm_lcell_comb") == 18, histogram)
    check("16 tennm_ff", histogram.get("tennm_ff") == 16, histogram)
    result = inventory.inventory(netlist)
    check("no uncovered primitive", not result["uncovered"], result["uncovered"])
    check("no out-of-coverage configuration", not result["out_of_coverage"],
          result["out_of_coverage"])

    # ---- 2. the committed netlist.hal.v is the import of the committed .vo --
    print("\n[2] import rewrite is reproducible")
    rewritten = vo_import.convert(netlist, source_note=os.path.basename(VO))
    committed = open(HAL_V).read()
    check("netlist.hal.v matches a fresh rewrite",
          rewritten.strip() == committed.strip())

    # ---- 3. the structure the guide walks -----------------------------------
    print("\n[3] structure")
    chains = carry_chains(netlist)
    check("exactly two carry chains", len(chains) == 2, [len(c) for c in chains])
    lengths = sorted(len(c) for c in chains)
    check("chain lengths are 6 and 7", lengths == [6, 7], lengths)

    signatures = {}
    for ff in netlist.instances_of_type(primitives.FF):
        key = tuple(
            (ff.single(pin).key if not isinstance(ff.single(pin), vo_netlist.Const) else "const")
            for pin in ("clk", "ena", "clrn")
        )
        signatures.setdefault(key, []).append(ff.name)
    check("two flip-flop enable groups", len(signatures) == 2, sorted(signatures))
    check("both groups are 8 bits wide",
          sorted(len(v) for v in signatures.values()) == [8, 8],
          {k: len(v) for k, v in signatures.items()})
    check("one group is enabled by a LUT, not a port",
          any("combout" in key[1] for key in signatures),
          sorted(key[1] for key in signatures))

    # ---- 4. the recovered model matches the netlist -------------------------
    print("\n[4] behaviour vs the recovered model (200 pseudo-random cycles)")
    reference = behavior.load_reference(REFERENCE)
    outcome = behavior.run_reference_check(netlist, reference)
    check("no counterexample", "mismatch" not in outcome, outcome.get("mismatch"))
    check("cycles simulated", outcome.get("checked", 0) >= 200, outcome)

    # ---- 5. the comparator, exhaustively ------------------------------------
    print("\n[5] pwm_out == (cnt < duty) over all 65536 (cnt, duty) pairs")
    simulator = simulate.Simulator(netlist)
    simulator.reset()
    mismatches = 0
    tick_mismatches = 0
    for duty in range(256):
        # one cycle with run=0 loads the duty register through the decode
        simulator.set_input("rst_n", 1)
        simulator.set_input("run", 0)
        simulator.set_input("cfg_we", 1)
        simulator.set_input("cfg_addr", 0b01)
        simulator.set_input("cfg_wdata", duty)
        simulator.clock()
        simulator.set_input("cfg_we", 0)
        simulator.set_input("run", 1)
        for _ in range(256):
            simulator.settle()
            cnt = simulator.get_output("cnt")
            got_duty = simulator.get_output("duty")
            if got_duty != duty:
                mismatches += 1
                break
            if simulator.get_output("pwm_out") != (1 if cnt < duty else 0):
                mismatches += 1
            if simulator.get_output("period_tick") != (1 if cnt == MASK else 0):
                tick_mismatches += 1
            simulator.clock()
    check("pwm_out agrees for all 65536 pairs", mismatches == 0, "%d mismatches" % mismatches)
    check("period_tick agrees for all 65536 pairs", tick_mismatches == 0,
          "%d mismatches" % tick_mismatches)

    # ---- 6. the write path really is address decoded ------------------------
    print("\n[6] duty is only writable at cfg_addr == 2'b01")
    for addr in (0b00, 0b10, 0b11):
        simulator.reset()
        simulator.set_input("rst_n", 1)
        simulator.set_input("run", 0)
        simulator.set_input("cfg_we", 1)
        simulator.set_input("cfg_addr", addr)
        simulator.set_input("cfg_wdata", 0xA5)
        simulator.clock()
        simulator.settle()
        check("address %s is ignored" % format(addr, "02b"),
              simulator.get_output("duty") == 0, simulator.get_output("duty"))

    # ---- 7. the comparator's Boolean structure, cell by cell ----------------
    print("\n[7] every comparator slice is (equality -> propagate, less-than -> generate)")
    groups = {}
    for key, names in signatures.items():
        q_nets = set()
        for name in names:
            instance = next(i for i in netlist.instances_of_type(primitives.FF) if i.name == name)
            q_nets.add(instance.single("q").key)
        groups["lut" if "combout" in key[1] else "port"] = q_nets
    counter_nets = groups.get("port", set())
    threshold_nets = groups.get("lut", set())

    comparator = None
    for chain in chains:
        nets = set()
        for cell in chain:
            for pin in primitives.LCELL_DATA_PINS:
                bit = cell.single(pin)
                if bit is not None and not isinstance(bit, vo_netlist.Const):
                    nets.add(bit.key)
        if nets & counter_nets and nets & threshold_nets:
            comparator = chain
    check("one chain reads both registers", comparator is not None)

    resolve = inventory._constant_resolver(netlist)

    def driven_pin(cell, pin):
        """The bit on *pin* if it is really driven, else None (gnd/vcc/literal)."""
        bit = cell.single(pin)
        if bit is None or isinstance(bit, vo_netlist.Const):
            return None
        return None if resolve(bit) is not None else bit

    slices_checked = 0
    for cell in comparator or []:
        pins = [p for p in ("dataa", "datab", "datac", "datad")
                if driven_pin(cell, p) is not None]
        if not pins:
            continue  # the carry tap on top of the chain drives no data pin
        mask = cell.parameters.get("lut_mask", 0)
        # pair each counter bit with the threshold bit it is compared against
        counter_pins = [p for p in pins if driven_pin(cell, p).key in counter_nets]
        threshold_pins = [p for p in pins if driven_pin(cell, p).key in threshold_nets]
        pairs = list(zip(counter_pins, threshold_pins))
        inverted = {p: 1 if driven_pin(cell, p).inverted else 0 for p in pins}
        weights = list(range(len(pairs)))  # try one significance order, then the other
        ok = len(pairs) * 2 == len(pins) and pairs != []
        if ok:
            ok = False
            for order in ([weights, list(reversed(weights))] if len(pairs) > 1 else [weights]):
                good = True
                for assignment in range(1 << len(pins)):
                    # the value the LUT sees on each pin ...
                    values = {pin: (assignment >> i) & 1 for i, pin in enumerate(pins)}
                    index = sum(values.get(p, 0) << i
                                for i, p in enumerate(("dataa", "datab", "datac", "datad")))
                    f0 = (mask >> index) & 1
                    f1 = (mask >> (16 + index)) & 1
                    # ... versus the value on the net, which the export may invert
                    a = b = 0
                    equal = True
                    for position, (pin_a, pin_b) in enumerate(pairs):
                        bit_a = values[pin_a] ^ inverted[pin_a]
                        bit_b = values[pin_b] ^ inverted[pin_b]
                        a |= bit_a << order[position]
                        b |= bit_b << order[position]
                        equal = equal and bit_a == bit_b
                    if f0 != (1 if equal else 0) or f1 != (1 if a < b else 0):
                        good = False
                        break
                if good:
                    ok = True
                    break
        slices_checked += 1
        check("slice %s: propagate = equal, generate = (counter < threshold)" % cell.name, ok)
    check("all comparator slices checked", slices_checked >= 5, slices_checked)

    print("\n%d checks, %d failed" % (_checks, len(_failures)))
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
