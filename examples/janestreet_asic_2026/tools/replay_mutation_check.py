#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""replay_mutation_check.py -- how much does the VCD replay actually prove?

``replay_vcd.py`` shows the extracted netlist reproduces ``example_inputs.vcd``
exactly.  That is necessary but not obviously *sufficient*: the recorded VCD
contains two **failing** attempts, so ``success`` is 0 throughout and ``O``
only ever emits the fixed "TRY AGAIN" message.  A netlist that got the
key-comparison logic completely wrong would still print "TRY AGAIN".

This script measures that blind spot empirically.  It injects single-cell pin
swaps into the extracted netlist, keeps only the ones that genuinely change the
cell's truth table (``xor2`` ``A<->B`` is not a mutation), replays the VCD
through each mutant, and reports the detection rate.

A low detection rate is **not** a bug in the extraction or in the replay -- it
is a statement about the stimulus: the comparator cone is simply unobservable
when every attempt fails.  Use the number to calibrate how much confidence the
green replay buys, and where an independent check (structural review of the
comparator, or a solved key) is still required.

Usage::

    python tools/replay_mutation_check.py --trials 60 --seed 7
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import sys
from typing import List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import sc_sim            # noqa: E402
import replay_vcd as R   # noqa: E402

POWER_PINS = {"VPWR", "VGND", "VPB", "VNB"}
OUTPUT_PINS = {"X", "Y", "Q", "Q_N"}


def swap_is_functional(cell_name: str, a: str, b: str) -> bool:
    """True if swapping input pins ``a``/``b`` changes what the cell computes.

    Commutative swaps (``nand2`` ``A<->B``) are filtered out so the detection
    rate is not diluted by no-op "mutants".  Flip-flop pin swaps are always
    treated as functional -- the cell library models them as a record, not a
    truth table.
    """
    cd = sc_sim.lookup_cell(cell_name)
    if cd is None or cd.is_seq:
        return True
    ins = list(cd.inputs)
    if a not in ins or b not in ins:
        return True
    for bits in itertools.product((0, 1), repeat=len(ins)):
        v = dict(zip(ins, bits))
        w = dict(v)
        w[a], w[b] = v[b], v[a]
        for _pin, fn in cd.outputs.items():
            if fn(v) != fn(w):
                return True
    return False


def mismatching_edges(design: dict, edges: Sequence["R.Edge"]) -> int:
    sim = sc_sim.Simulator(sc_sim.load_json(design))
    bad = 0
    for e in edges:
        sim.step({k: v for k, v in e.stim.items() if v is not None})
        if sim.get_bus("O", 8) != e.exp_o or sim.get("success") != e.exp_success:
            bad += 1
    return bad


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--vcd", default=R.DEFAULT_VCD)
    ap.add_argument("--netlist", default=R.DEFAULT_NETLIST)
    ap.add_argument("--trials", type=int, default=60)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args(argv)

    id2name, blocks = R.parse_vcd(args.vcd)
    edges, _notes = R.extract_posedges(id2name, blocks)
    with open(args.netlist, "r", encoding="utf-8") as fh:
        base = json.load(fh)

    baseline = mismatching_edges(base, edges)
    lines: List[str] = []

    def out(s: str = "") -> None:
        lines.append(s)
        print(s)

    out("mutation sensitivity of the VCD replay")
    out("=" * 70)
    out("netlist  : %s" % args.netlist)
    out("baseline : %d mismatching edges (must be 0)" % baseline)
    if baseline:
        out("ABORT: the unmutated netlist does not reproduce the VCD.")
        return 1
    out()

    logic = [i for i, inst in enumerate(base["instances"])
             if not inst.get("physical_only") and len(inst.get("connections", {})) >= 3]
    rng = random.Random(args.seed)
    made = detected = errors = 0
    while made < args.trials:
        k = rng.choice(logic)
        inst = base["instances"][k]
        pins = [p for p in inst["connections"] if p not in POWER_PINS | OUTPUT_PINS]
        if len(pins) < 2:
            continue
        a, b = rng.sample(pins, 2)
        if inst["connections"][a] == inst["connections"][b]:
            continue
        if not swap_is_functional(inst["cell"], a, b):
            continue
        mutant = json.loads(json.dumps(base))
        conns = mutant["instances"][k]["connections"]
        conns[a], conns[b] = inst["connections"][b], inst["connections"][a]
        made += 1
        try:
            bad = mismatching_edges(mutant, edges)
        except Exception as exc:                      # loops, unknown cells
            errors += 1
            made -= 1
            out("  %-28s %-30s %s<->%s  UNSIMULATABLE (%s)"
                % (inst["name"], inst["cell"], a, b, type(exc).__name__))
            continue
        if bad:
            detected += 1
        out("  %-28s %-30s %-7s<->%-7s  mismatching edges=%d"
            % (inst["name"], inst["cell"], a, b, bad))

    out()
    out("functionally-real mutants : %d" % made)
    out("detected by the replay    : %d (%.0f%%)" % (detected, 100.0 * detected / max(made, 1)))
    out("unsimulatable mutants     : %d" % errors)
    out()
    out("Reading: the undetected mutants sit in the key-comparison cone, whose")
    out("only observable is `success` -- and `success` is 0 for every attempt the")
    out("VCD records.  The replay therefore validates the control path (reset,")
    out("the 121-cycle shift window, the output FSM and the message ROM) far more")
    out("strongly than it validates the comparator.")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        print("\nwrote %s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
