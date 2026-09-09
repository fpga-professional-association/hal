#!/usr/bin/env python3
"""Headless smoke test for the 03_uart_tx walkthrough.

Re-derives every claim the guide makes that can be checked without a human
looking at a picture, and fails loudly if the netlist, the anonymisation, the
probe or the reconstruction stops agreeing with `guide.html`.

Two tiers:

* **always** -- pure Python, standard library plus `tools/hal_agilex`.  Checks
  the export's primitive coverage, the anonymisation, the black-box probe and
  the agreement between the exported netlist and `reference.py`.
* **with a built HAL** (`HAL_PY_PATH` / `PYTHONPATH` set) -- additionally runs
  `analyze.py` and asserts the structural conclusions: one clock domain, the
  nine-deep shift chain, and the two four-bit register groups.

    python3 check.py                 # tier 1 only, exits 0/1
    HAL_PY_PATH=/work/build/lib python3 check.py --with-hal
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, HERE)

FAILURES = []


def check(name, condition, detail=""):
    status = "ok  " if condition else "FAIL"
    print("[{}] {}{}".format(status, name, ("  -- " + detail) if detail else ""))
    if not condition:
        FAILURES.append(name)
    return condition


# ---------------------------------------------------------------------------
# tier 1
# ---------------------------------------------------------------------------


def check_coverage():
    from hal_agilex import inventory, vo_netlist

    nl = vo_netlist.parse_file(os.path.join(HERE, "uart_tx.vo"))
    hist = nl.type_histogram()
    check("export contains only modelled primitives",
          set(hist) <= {"tennm_ff", "tennm_lcell_comb"}, str(hist))
    check("export has 18 flip-flops", hist.get("tennm_ff") == 18, str(hist.get("tennm_ff")))
    check("export has 22 ALM cells", hist.get("tennm_lcell_comb") == 22,
          str(hist.get("tennm_lcell_comb")))

    doc = inventory.build_document(nl, path=os.path.join(HERE, "uart_tx.vo"))
    statuses = {f["status"] for f in doc["findings"]}
    check("hal_agilex inventory reports no gap",
          statuses == {"proven_under_assumptions"}, str(sorted(statuses)))


def _sim(path):
    from hal_agilex import simulate, vo_netlist
    return simulate.build(vo_netlist.parse_file(path))


def check_anonymisation(cycles=400):
    """The anonymised netlist must be the same circuit, not merely similar."""
    import random

    a = _sim(os.path.join(HERE, "netlist.hal.v"))
    b = _sim(os.path.join(HERE, "netlist_anon.hal.v"))
    pm = json.load(open(os.path.join(HERE, "netlist_anon.hal.v.map.json")))["ports"]

    def drive(rst_n, start, data):
        a.set_input("clk", 0)
        a.set_input("rst_n", rst_n); b.set_input(pm["rst_n"], rst_n)
        a.set_input("tx_start", start); b.set_input(pm["tx_start"], start)
        a.set_input("tx_data", data)
        for i in range(8):
            b.set_input(pm["tx_data[%d]" % i], (data >> i) & 1)

    a.reset(); b.reset()
    drive(0, 0, 0)
    a.apply_async_clear(); b.apply_async_clear()

    random.seed(20260908)
    bad = 0
    for cycle in range(cycles):
        rst_n = 0 if cycle in (0, 133) else 1
        drive(rst_n, random.randint(0, 1) if cycle % 5 == 0 else 0, random.randint(0, 255))
        if not rst_n:
            a.apply_async_clear(); b.apply_async_clear()
        a.settle(); b.settle()
        for name in ("tx", "tx_busy"):
            if a.get_output(name) != b.get_output(pm[name]):
                bad += 1
        a.clock(); b.clock()
    check("anonymised netlist is behaviourally identical to the import",
          bad == 0, "{} mismatching output samples over {} cycles".format(bad, cycles))


def check_netlist_vs_reference(cycles=1200):
    """The export must agree with spec.md's executable model."""
    import random
    import reference

    sim = _sim(os.path.join(HERE, "netlist.hal.v"))
    model = reference.UartTx()

    sim.reset()
    for name in ("clk", "rst_n", "tx_start"):
        sim.set_input(name, 0)
    sim.set_input("tx_data", 0)
    sim.apply_async_clear()
    model.clear()

    random.seed(4242)
    bad = 0
    for cycle in range(cycles):
        rst_n = 0 if cycle in (0, 500) else 1
        start = random.randint(0, 1) if cycle % 7 == 0 else 0
        data = random.randint(0, 255)
        sim.set_input("rst_n", rst_n)
        sim.set_input("tx_start", start)
        sim.set_input("tx_data", data)
        if not rst_n:
            # rst_n is asynchronous: both sides must show the cleared outputs in
            # the same cycle the level is applied, not one edge later.
            sim.apply_async_clear()
            model.clear()
        sim.settle()
        if (sim.get_output("tx"), sim.get_output("tx_busy")) != (model.tx, model.tx_busy):
            bad += 1
        sim.clock()
        model.clock(tx_start=start, tx_data=data, rst_n=rst_n)
    check("exported netlist matches reference.py (bounded, {} cycles)".format(cycles),
          bad == 0, "{} mismatching cycles".format(bad))


def check_probe(outdir):
    """The black-box probe must recover the frame the guide describes."""
    import probe

    result = probe.probe(REPO, os.path.join(HERE, "netlist_anon.hal.v"), outdir)
    pm = json.load(open(os.path.join(HERE, "netlist_anon.hal.v.map.json")))["ports"]
    inverse = {v: k for k, v in pm.items()}

    check("probe finds a single clock port", result["clock"] == pm["clk"], result["clock"])
    check("probe finds the asynchronous reset port", result["reset"] == pm["rst_n"], result["reset"])
    check("probe finds the request port", result["request"] == pm["tx_start"], result["request"])
    check("probe finds the serial output", result["serial_output"] == pm["tx"],
          result["serial_output"])
    check("probe finds the status output", result["status_output"] == pm["tx_busy"],
          result["status_output"])
    check("probe recovers a 16-cycle bit period", result["bit_period_cycles"] == 16,
          str(result["bit_period_cycles"]))
    check("probe recovers a 10-slot frame", result["frame_slots"] == 10,
          str(result["frame_slots"]))
    check("probe recovers start bit 0 and stop bit 1",
          result["framing_slots"] == {"0": 0, "9": 1}, str(result["framing_slots"]))
    recovered = [inverse[p] for p in result["payload_inputs_in_wire_order"]]
    check("probe recovers LSB-first payload order",
          recovered == ["tx_data[%d]" % i for i in range(8)], " ".join(recovered))
    check("probe measures the one-cycle busy tail",
          result["busy_cycles"] == 161, str(result["busy_cycles"]))
    return result


# ---------------------------------------------------------------------------
# tier 2 -- needs a built HAL
# ---------------------------------------------------------------------------


def check_structure(outdir):
    path = os.path.join(outdir, "analysis.json")
    if not os.path.exists(path):
        cmd = [sys.executable, os.path.join(HERE, "analyze.py"), "--repo", REPO, "-o", outdir]
        print("running:", " ".join(cmd))
        rc = subprocess.call(cmd)
        if not check("analyze.py runs", rc == 0, "exit {}".format(rc)):
            return
    data = json.load(open(path))

    pm = json.load(open(os.path.join(HERE, "netlist_anon.hal.v.map.json")))
    inv_gate = {v: k for k, v in pm["gates"].items()}
    inv_port = {v: k for k, v in pm["ports"].items()}

    check("exactly one clock net drives every flip-flop",
          len(data["clock_reset"]["clock_nets"]) == 1, str(data["clock_reset"]["clock_nets"]))
    check("exactly one asynchronous clear net",
          len(data["clock_reset"]["reset_nets"]) == 1, str(data["clock_reset"]["reset_nets"]))

    reg = data["register_scc"]
    check("the register graph splits 9 control / 9 datapath",
          len(reg["control"]) == 9 and len(reg["datapath"]) == 9,
          "{} control, {} datapath".format(len(reg["control"]), len(reg["datapath"])))

    chains = reg["chains"]
    longest = max(chains, key=len) if chains else []
    check("the datapath is one 9-deep chain", len(longest) == 9,
          "longest chain = {}".format(len(longest)))
    recovered_chain = [inv_gate[g] for g in longest]
    check("the chain is shreg[8] .. shreg[0], in order",
          recovered_chain == ["shreg[%d]" % i for i in range(8, -1, -1)],
          " ".join(recovered_chain))

    cnt = data["counters"]
    check("exactly one flop carries all the feedback",
          len(cnt["feedback_flops"]) == 1, str(cnt["feedback_flops"]))
    check("the feedback flop is the busy flag",
          cnt["flag"] and inv_gate[cnt["flag"]] == "busy",
          inv_gate.get(cnt["flag"], "?"))
    fast = [inv_gate[g] for g in cnt["fast_counter"]]
    slow = [inv_gate[g] for g in cnt["slow_counter"]]
    check("the gating (fast) counter is baud_cnt[0..3], LSB first",
          fast == ["baud_cnt[%d]" % i for i in range(4)], " ".join(fast))
    check("the gated (slow) counter is bit_cnt[0..3], LSB first",
          slow == ["bit_cnt[%d]" % i for i in range(4)], " ".join(slow))

    # what the gate-level SCC alone could and could not do
    outside = data["gate_scc"]["ffs_outside_gate_sccs"]
    check("the gate-level SCC leaves 7 of the 9 chain flops outside feedback",
          len(outside) == 7, "{} flop(s): {}".format(
              len(outside), " ".join(inv_gate[g] for g in outside)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-hal", action="store_true",
                    help="also run analyze.py and check the structural claims")
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "artifacts"))
    args = ap.parse_args(argv)

    print("== tier 1: no HAL build required")
    check_coverage()
    check_anonymisation()
    check_netlist_vs_reference()
    check_probe(args.output)

    if args.with_hal:
        print()
        print("== tier 2: structural analysis (needs a built HAL)")
        check_structure(args.output)

    print()
    if FAILURES:
        print("{} check(s) FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
