#!/usr/bin/env python3
"""Headless assertions for the 10_crc8_checker walkthrough.

Every claim guide.html makes about what the tools *showed* is re-derived here
and asserted, so the walkthrough breaks loudly if HAL, the gate library or the
netlist ever drift apart.  No image is produced and nothing is written: this is
the smoke test, not the walk.

    export HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib
    python examples/agilex3_walkthroughs/10_crc8_checker/check.py

Exit 0 = every assertion held.  Exit 1 = at least one did not.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import re_walk  # noqa: E402

ANON = os.path.join(HERE, "netlist", "netlist.anon.hal.v")
NAMED = os.path.join(HERE, "netlist", "netlist.hal.v")

EXPECTED_POLYNOMIAL = 0x07
EXPECTED_WIDTH = 8
EXPECTED_CRC = 0xF4          # CRC-8/SMBUS check value for "123456789"
EXPECTED_FF = 8
EXPECTED_LCELL = 5


class Checker(object):
    def __init__(self):
        self.failures = []

    def ok(self, condition, message):
        print("%-5s %s" % ("PASS" if condition else "FAIL", message))
        if not condition:
            self.failures.append(message)

    def equal(self, actual, expected, message):
        self.ok(actual == expected, "%s (got %r, want %r)" % (message, actual, expected))


def run(checker, path, label):
    print("--- %s (%s)" % (label, os.path.relpath(path, HERE)))
    design = re_walk.load(path)

    stats = re_walk.collect_stats(design)
    checker.equal(stats["gate_types"].get("tennm_ff"), EXPECTED_FF,
                  "%s: flip-flop count" % label)
    checker.equal(stats["gate_types"].get("tennm_lcell_comb"), EXPECTED_LCELL,
                  "%s: ALM count" % label)
    checker.equal(len(stats["refused"]), 0,
                  "%s: every gate is inside the modelled coverage" % label)

    ports = re_walk.collect_ports(design)
    roles = sorted(entry["role"] for entry in ports["inputs"].values())
    checker.equal(roles, ["asynchronous reset, active low", "clock", "clock enable", "data"],
                  "%s: the four primary inputs classify structurally" % label)

    registers = re_walk.collect_registers(design)
    checker.equal(len(registers["banks"]), 1,
                  "%s: all flip-flops share one clock/reset/enable signature" % label)
    sccs = registers.get("sccs")
    checker.ok(sccs is not None and len(sccs) == 1 and len(sccs[0]) >= EXPECTED_FF,
               "%s: exactly one strongly connected component covering the register bank"
               % label)

    functions = re_walk.collect_functions(design)
    checker.ok(all(row["affine"] for row in functions["rows"]),
               "%s: every next-state function is affine over GF(2)" % label)

    lfsr = re_walk.recover_lfsr(design)
    checker.ok(lfsr["ok"], "%s: an LFSR/CRC model fits (%s)"
               % (label, lfsr.get("reason", "fitted")))
    if not lfsr["ok"]:
        return
    checker.equal(lfsr["width"], EXPECTED_WIDTH, "%s: recovered state width" % label)
    checker.equal(lfsr["polynomial"], EXPECTED_POLYNOMIAL,
                  "%s: recovered polynomial" % label)
    checker.equal(lfsr["taps"], [0, 1, 2], "%s: recovered tap positions" % label)
    checker.equal(lfsr["inversions"], [], "%s: no stage is inverted" % label)

    flags = [entry for entry in re_walk.collect_match(design, lfsr)
             if entry["is_zero_flag"]]
    checker.equal(len(flags), 1, "%s: exactly one output is the 'state == 0' flag" % label)

    run_message = re_walk.simulate_codeword(design, lfsr)
    checker.equal(run_message["remainder"], EXPECTED_CRC,
                  "%s: netlist CRC of \"123456789\"" % label)
    checker.equal(re_walk.reference_crc(re_walk.MESSAGE, lfsr["polynomial"]),
                  EXPECTED_CRC,
                  "%s: textbook CRC with the recovered polynomial agrees" % label)

    good = re_walk.simulate_codeword(design, lfsr, appended=EXPECTED_CRC)
    checker.equal(good["remainder"], 0, "%s: remainder of message||crc" % label)
    if flags:
        checker.equal(good["sim"].read(flags[0]["net"]), 1,
                      "%s: the flag accepts the intact codeword" % label)

    bad = re_walk.simulate_codeword(design, lfsr, appended=EXPECTED_CRC, flip=17)
    checker.ok(bad["remainder"] != 0,
               "%s: a single flipped bit leaves a non-zero remainder" % label)
    if flags:
        checker.equal(bad["sim"].read(flags[0]["net"]), 0,
                      "%s: the flag rejects the corrupted codeword" % label)
    return lfsr


def main():
    checker = Checker()
    anon = run(checker, ANON, "anonymised netlist")
    print()
    named = run(checker, NAMED, "as-exported netlist")
    print()
    if anon and named:
        checker.equal(named["polynomial"], anon["polynomial"],
                      "both copies of the netlist yield the same polynomial")
        checker.equal(named["taps"], anon["taps"],
                      "both copies of the netlist yield the same taps")
    print()
    if checker.failures:
        print("%d assertion(s) FAILED" % len(checker.failures))
        return 1
    print("all assertions held")
    return 0


if __name__ == "__main__":
    sys.exit(main())
