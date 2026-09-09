#!/usr/bin/env python3
"""Headless smoke test for the 08_shift_debouncer walkthrough.

Re-derives every claim `guide.html` makes that can be checked without a human
looking at a picture, and fails loudly if the netlist, the probe, the
reconstruction or the structural analysis stops agreeing with the guide.

Two tiers:

* **always** -- pure Python, standard library plus `tools/hal_agilex`.  Checks
  the export's primitive coverage, that `netlist.hal.v` is the same circuit as
  the vendor `.vo`, the agreement between the export and both `reference.py`
  and `recovered_reference.py`, the behavioural probe, and two negative
  controls.
* **with a built HAL** (`HAL_PY_PATH` / `PYTHONPATH` set) -- additionally runs
  `analyze.py` and asserts the structural conclusions: one clock domain with no
  enables, the 4-flop feedback group, the saturating transition relation, the
  hysteresis table and the edge-detector cell.

    python3 check.py                 # tier 1 only, exits 0/1
    HAL_PY_PATH=/work/build/lib python3 check.py --with-hal
"""

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, HERE)

VO = os.path.join(HERE, "shift_debouncer.vo")
HALV = os.path.join(HERE, "netlist.hal.v")

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
    from hal_agilex import inventory, recognize, vo_netlist

    nl = vo_netlist.parse_file(VO)
    hist = nl.type_histogram()
    check("export contains only modelled primitives",
          set(hist) <= {"tennm_ff", "tennm_lcell_comb"}, str(hist))
    check("export has 8 flip-flops", hist.get("tennm_ff") == 8, str(hist.get("tennm_ff")))
    check("export has 6 ALM cells", hist.get("tennm_lcell_comb") == 6,
          str(hist.get("tennm_lcell_comb")))
    check("design.v's '8 flip-flops expected' prediction holds",
          hist.get("tennm_ff") == 8, "2 sync + 4 counter + state + state_d")

    doc = inventory.build_document(nl, path=VO)
    statuses = {f["status"] for f in doc["findings"]}
    check("hal_agilex inventory reports no coverage gap",
          statuses == {"proven_under_assumptions"}, str(sorted(statuses)))

    art = inventory._artifact(nl, VO, nl.name)
    rec = recognize.build_document(nl, art)
    ids = {f["id"]: f for f in rec["findings"]}
    check("hal_agilex recognize finds no carry chain (there is no adder here)",
          "hal_agilex/recognize/no-adder" in ids
          and ids["hal_agilex/recognize/no-adder"]["status"] == "unknown",
          str(sorted(ids)))


def check_shipped_findings():
    """The committed findings documents still say what the guide quotes."""
    base = os.path.join(HERE, "artifacts")
    inv = json.load(open(os.path.join(base, "inventory.findings.json")))
    check("shipped inventory histogram is 8 ff + 6 ALM",
          inv["findings"][0]["data"]["histogram"] == {"tennm_ff": 8, "tennm_lcell_comb": 6},
          str(inv["findings"][0]["data"]["histogram"]))
    check("shipped inventory records 14 instances",
          inv["artifacts"][0]["gate_count"] == 14, str(inv["artifacts"][0]["gate_count"]))
    beh = json.load(open(os.path.join(base, "behavior.findings.json")))
    f = beh["findings"][0]
    check("shipped behaviour finding is proven_bounded at 200 cycles",
          f["status"] == "proven_bounded" and f["bounds"]["cycle_bound"] == 200,
          "{} / {}".format(f["status"], f["bounds"]["cycle_bound"]))


def _sim(path):
    from hal_agilex import simulate, vo_netlist
    return simulate.build(vo_netlist.parse_file(path))


def check_import_is_the_same_circuit(cycles=600):
    """netlist.hal.v is analysed structurally; the .vo is probed.  Same circuit?"""
    import random

    a, b = _sim(VO), _sim(HALV)
    a.reset(); b.reset()
    for sim in (a, b):
        for name in ("clk", "rst_n", "btn_raw"):
            sim.set_input(name, 0)
        sim.apply_async_clear()

    random.seed(20260908)
    bad = 0
    for cycle in range(cycles):
        rst_n = 0 if cycle in (0, 211) else 1
        btn = random.randint(0, 1) if cycle % 3 == 0 else (cycle // 7) % 2
        for sim in (a, b):
            sim.set_input("rst_n", rst_n)
            sim.set_input("btn_raw", btn)
            if not rst_n:
                sim.apply_async_clear()
            sim.settle()
        for name in ("btn_state", "btn_rise"):
            if a.get_output(name) != b.get_output(name):
                bad += 1
        a.clock(); b.clock()
    check("netlist.hal.v behaves identically to the vendor export",
          bad == 0, "{} mismatching output samples over {} cycles".format(bad, cycles))


def _reference_run(module_path, cycles):
    from hal_agilex import behavior, vo_netlist
    nl = vo_netlist.parse_file(VO)
    module = behavior.load_reference(module_path)
    return behavior.run_reference_check(nl, module, cycles=cycles)


def check_netlist_vs_references(cycles=3000):
    for label, name in (("reference.py (the RTL model)", "reference.py"),
                        ("recovered_reference.py (the reconstruction)",
                         "recovered_reference.py")):
        res = _reference_run(os.path.join(HERE, name), cycles)
        check("export matches {} for {} cycles".format(label, cycles),
              "mismatch" not in res, json.dumps(res.get("mismatch", {}))[:160])


def _mutant(subs, tag):
    src = open(os.path.join(HERE, "reference.py")).read()
    for old, new in subs:
        if old not in src:
            raise RuntimeError("mutation {!r} does not apply to reference.py".format(old))
        src = src.replace(old, new)
    path = os.path.join(tempfile.mkdtemp(prefix="ctl_" + tag + "_"), "reference.py")
    with open(path, "w", newline="\n") as fh:
        fh.write(src)
    return path


def check_negative_controls():
    """"It passed" only means something if a wrong model would have failed."""
    nosat = _mutant([(" and not at_max:", ":"), (" and not at_min:", ":")], "nosat")
    res = _reference_run(nosat, 200)
    check("control A: removing the saturation clamp IS caught",
          "mismatch" in res, "caught at cycle {}".format(
              res.get("mismatch", {}).get("cycle")))
    check("control A is caught almost immediately (cycle < 10)",
          res.get("mismatch", {}).get("cycle", 999) < 10,
          str(res.get("mismatch", {}).get("cycle")))

    max14 = _mutant([("CNT_MAX = 0xF", "CNT_MAX = 0xE")], "max14")
    short = _reference_run(max14, 200)
    check("control B: an off-by-one end stop SURVIVES the shipped 200-cycle bound",
          "mismatch" not in short,
          "this is a real coverage hole, not a pass")
    long_ = _reference_run(max14, 2000)
    check("control B is caught at 2000 cycles",
          "mismatch" in long_, "caught at cycle {}".format(
              long_.get("mismatch", {}).get("cycle")))
    check("control B's first divergence is at cycle 280",
          long_.get("mismatch", {}).get("cycle") == 280,
          str(long_.get("mismatch", {}).get("cycle")))


def check_probe(outdir):
    """The black-box probe must recover the numbers the guide prints."""
    import probe

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = probe.probe(REPO, VO, outdir, images=None)

    check("probe finds a single clock port", result["clock"] == "clk", result["clock"])
    check("probe finds the asynchronous reset port", result["reset"] == "rst_n",
          result["reset"])
    check("probe is left with exactly one data input",
          result["data_input"] == "btn_raw", result["data_input"])
    check("probe separates the level output from the pulse output",
          (result["level_output"], result["pulse_output"]) == ("btn_state", "btn_rise"),
          "{} / {}".format(result["level_output"], result["pulse_output"]))
    check("press latency is 18 cycles", result["press_latency_cycles"] == 18,
          str(result["press_latency_cycles"]))
    check("release latency is 18 cycles too", result["release_latency_cycles"] == 18,
          str(result["release_latency_cycles"]))
    check("the pulse is exactly one cycle wide and fires once",
          result["pulse_width_cycles"] == 1 and result["pulses_per_press"] == 1,
          "width {} x{}".format(result["pulse_width_cycles"], result["pulses_per_press"]))
    check("the shortest accepted press is 15 cycles",
          result["min_accepted_press_cycles"] == 15,
          str(result["min_accepted_press_cycles"]))
    check("a 14-cycle press produces no output change at all",
          result["accepted_by_press_width"][14] == 0
          and result["accepted_by_press_width"][15] == 1,
          "k=14 -> {}, k=15 -> {}".format(result["accepted_by_press_width"][14],
                                          result["accepted_by_press_width"][15]))

    b = result["bounce"]
    check("the burst really does bounce (16 edges in 24 samples)",
          b["raw_edges_in_burst"] == 16, str(b["raw_edges_in_burst"]))
    check("the level output does not move at all during the burst",
          b["level_edges_in_burst"] == 0, str(b["level_edges_in_burst"]))
    check("exactly one pulse comes out of the whole burst",
          b["pulse_count"] == 1 and b["pulse_widths"] == [1], str(b))
    check("the level rises 16 cycles after the line settles, not 18",
          b["cycles_after_settling"] == 16,
          "the burst left the integrator part-way up")

    sq = dict((int(k), v) for k, v in result["square_wave_output_edges"].items())
    check("square waves with a half-period below 15 are rejected outright",
          all(sq[h] == 0 for h in (5, 10, 14)), str(sq))
    check("a half-period of 15 gets through", sq[15] > 0, str(sq))
    return result


# ---------------------------------------------------------------------------
# tier 2 -- needs a built HAL
# ---------------------------------------------------------------------------


def check_structure(outdir):
    path = os.path.join(outdir, "analysis.json")
    if not os.path.exists(path):
        cmd = [sys.executable, os.path.join(HERE, "analyze.py"), "--repo", REPO,
               "-o", outdir]
        print("running:", " ".join(cmd))
        rc = subprocess.call(cmd, stdout=subprocess.DEVNULL)
        if not check("analyze.py runs", rc == 0, "exit {}".format(rc)):
            return
    data = json.load(open(path))

    el = data["elaborate"]
    check("all 6 ALM cells elaborate, none refused",
          el["elaborated"] == 6 and el["checked_ff"] == 8,
          "elaborated={} checked_ff={}".format(el["elaborated"], el["checked_ff"]))

    stats = data["stats"]
    check("16 gates: 8 ff + 6 ALM + gnd + vcc",
          stats["gate_types"] == {"tennm_ff": 8, "tennm_lcell_comb": 6,
                                  "HAL_GND": 1, "HAL_VCC": 1},
          str(stats["gate_types"]))
    check("the boundary is btn_raw/clk/rst_n -> btn_rise/btn_state",
          stats["inputs"] == ["btn_raw", "clk", "rst_n"]
          and stats["outputs"] == ["btn_rise", "btn_state"],
          "{} -> {}".format(stats["inputs"], stats["outputs"]))
    check("the netlist is flat: one module", stats["num_modules"] == 1,
          str(stats["num_modules"]))

    cr = data["clock_reset"]
    check("exactly one clock net drives every flip-flop",
          cr["clock_nets"] == ["clk"], str(cr["clock_nets"]))
    check("exactly one asynchronous clear net",
          cr["reset_nets"] == ["rst_n"], str(cr["reset_nets"]))
    check("every enable is tied to the constant 1: no clock enables at all",
          cr["enable_nets"] == ["1'b1"], str(cr["enable_nets"]))
    check("all 8 flops share one control-pin signature",
          len(cr["signatures"]) == 1 and len(cr["signatures"][0]["gates"]) == 8,
          "{} signature(s)".format(len(cr["signatures"])))

    scc = data["gate_scc"]
    sizes = [len(c) for c in scc["gate_sccs"]]
    check("the gate graph has exactly 2 non-trivial SCCs, of 8 and 2 gates",
          sizes == [8, 2], str(sizes))
    check("3 flip-flops sit outside every gate-level loop",
          sorted(scc["ffs_outside_gate_sccs"]) == ["state_d", "sync[0]", "sync[1]"],
          " ".join(scc["ffs_outside_gate_sccs"]))

    reg = data["register_scc"]
    check("the register graph splits into one 4-flop word, one holder, 3 delays",
          [len(c) for c in reg["words"]] == [4] and len(reg["holders"]) == 1
          and len(reg["delays"]) == 3,
          "words={} holders={} delays={}".format(
              [len(c) for c in reg["words"]], reg["holders"], reg["delays"]))
    check("the 4-flop word is cnt[0..3]",
          reg["words"][0] == ["cnt[0]", "cnt[1]", "cnt[2]", "cnt[3]"],
          " ".join(reg["words"][0]))
    check("the self-holding singleton is the state bit",
          reg["holders"] == ["state"], str(reg["holders"]))
    chains = sorted([len(c) for c in reg["chains"]], reverse=True)
    check("the delay flops form a 2-chain and a 1-chain",
          chains == [2, 1], str(chains))
    two = [c for c in reg["chains"] if len(c) == 2][0]
    check("the 2-chain is sync[0] -> sync[1] and is fed by btn_raw",
          two == ["sync[0]", "sync[1]"]
          and data["registers"]["sync[0]"]["d_inputs"] == ["btn_raw"],
          " -> ".join(two))

    luts = data["luts"]
    check("6 ALM cells, and two of them share a lut_mask",
          len(luts) == 6
          and luts["cnt[3]~0"]["lut_mask"] == luts["i38~0"]["lut_mask"],
          "{} vs {}".format(luts["cnt[3]~0"]["lut_mask"], luts["i38~0"]["lut_mask"]))
    check("the two cells sharing a mask read different nets",
          luts["cnt[3]~0"]["pins"] != luts["i38~0"]["pins"], "")

    sem = data["semantics"]
    grp = sem["group"]
    check("the counter's external control net is sync(1)",
          grp["external_nets"] == ["sync(1)"], str(grp["external_nets"]))
    check("both directions walk all 16 states as a single path",
          all(len(v) == 16 for v in grp["orbits"].values()),
          str({k: len(v) for k, v in grp["orbits"].items()}))
    ups = [v for v in grp["orbits"].values() if v[0] == "0000"]
    downs = [v for v in grp["orbits"].values() if v[0] == "1111"]
    check("one path runs 0000 -> 1111 and the other 1111 -> 0000",
          len(ups) == 1 and len(downs) == 1
          and ups[0][-1] == "1111" and downs[0][-1] == "0000", "")
    check("the up path is 0,1,2,...,15 in plain binary",
          ups[0] == [format(i, "04b") for i in range(16)], " ".join(ups[0][:4]) + " ...")
    check("the traversal is 15 steps -- the number probe C measured",
          grp["path_length"] - 1 == 15, str(grp["path_length"] - 1))
    check("bit order recovered from the path is cnt[0],cnt[1],cnt[2],cnt[3]",
          grp["bit_order_lsb_first"] == ["cnt[0]", "cnt[1]", "cnt[2]", "cnt[3]"],
          " ".join(grp["bit_order_lsb_first"]))

    flag = sem["flag"]
    check("the hysteresis bit is SET only at counter value 15",
          flag["set_values"] == [15], str(flag["set_values"]))
    check("the hysteresis bit is CLEARed only at counter value 0",
          flag["clear_values"] == [0], str(flag["clear_values"]))
    check("the hysteresis bit HOLDs at all 14 values in between",
          flag["hold_values"] == list(range(1, 15)), str(flag["hold_values"]))

    oc = sem["output_cell"]
    check("the output cell is a rising-edge detector",
          oc["name"] == "btn_rise~0"
          and [oc["rows"][str(i)] for i in range(4)] == [0, 1, 0, 0],
          str(oc["rows"]))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-hal", action="store_true",
                    help="also run analyze.py and check the structural claims")
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "artifacts"))
    args = ap.parse_args(argv)

    print("== tier 1: no HAL build required")
    check_coverage()
    check_shipped_findings()
    check_import_is_the_same_circuit()
    check_netlist_vs_references()
    check_negative_controls()
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
