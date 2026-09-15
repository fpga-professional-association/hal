#!/usr/bin/env python3
"""Headless smoke check for walkthrough 01 (blinky_counter).

Re-runs the load-bearing assertions of ``guide.html`` so that CI can tell when
a HAL change, a plugin change or a regenerated netlist silently invalidates the
walkthrough.  Every assertion below corresponds to a claim the guide makes.

    HAL_BASE_PATH=<build> PYTHONPATH=<build>/lib \\
        python3 examples/agilex3_walkthroughs/01_blinky_counter/check.py

Exit code 0 = every claim still holds, 1 = at least one does not.
The Quartus part of the flow is *not* re-run: the committed ``.vo`` is the
input, exactly as it is for a reader following the guide.
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import analysis  # noqa: E402  (needs HERE on the path)

FAILURES = []

# The combinational depth the guide's "The netlist as a graph" section states.
DAG_LEVELS = 24


def check(label, condition, detail=""):
    status = "ok  " if condition else "FAIL"
    print("[{}] {}{}".format(status, label, (" -- " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


def check_dag():
    """The committed DAG image and the guide agree on the level count.

    Pure file inspection -- no HAL needed.  ``hal_viz dag`` writes the counts
    into the .dot comment header, so the picture cannot silently drift away
    from the number the prose quotes.
    """
    dot_path = os.path.join(HERE, "images", "dag.dot")
    check("images/dag.dot exists", os.path.isfile(dot_path))
    if not os.path.isfile(dot_path):
        return
    with open(dot_path) as handle:
        header = handle.read(4096)
    match = re.search(r"^// (\d+) level\(s\)", header, re.M)
    levels = int(match.group(1)) if match else None
    check(
        "the levelled DAG is {} levels deep".format(DAG_LEVELS),
        levels == DAG_LEVELS,
        "levels={}".format(levels),
    )
    with open(os.path.join(HERE, "guide.html")) as handle:
        guide = handle.read()
    check(
        "guide.html quotes the same level count",
        "<code>{}</code> topological levels".format(DAG_LEVELS) in guide,
    )


def check_dag_interactive():
    """The committed clock-step page is built from a trace that reproduces.

    The page's whole claim is that its values are *this* run of the export, so
    the check re-runs the exporter with the options guide.html prints and
    requires the same document back. Pure Python: no HAL, no Graphviz, no
    browser, and nothing is written to the tree.
    """
    import json

    sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "tools"))
    from hal_agilex import trace as agilex_trace, vo_netlist

    trace_path = os.path.join(HERE, "artifacts", "dag_trace.json")
    page_path = os.path.join(HERE, "images", "dag_interactive.html")
    check("artifacts/dag_trace.json exists", os.path.isfile(trace_path))
    check("images/dag_interactive.html exists", os.path.isfile(page_path))
    if not (os.path.isfile(trace_path) and os.path.isfile(page_path)):
        return

    with open(trace_path, encoding="utf-8") as handle:
        committed = json.load(handle)
    fresh = agilex_trace.run_trace(
        vo_netlist.parse_file(os.path.join(HERE, "blinky_counter.vo")),
        agilex_trace.load_reference(os.path.join(HERE, "recovered_reference.py")),
        cycles=32,
    )
    differences = agilex_trace.differences(committed, fresh)
    check(
        "the committed trace reproduces from the committed .vo",
        not differences,
        ", ".join(differences),
    )
    with open(page_path, encoding="utf-8") as handle:
        page = handle.read()
    check(
        "the page embeds every recorded cycle",
        page.count('"cycle":') == len(committed["frames"]),
        "{} of {}".format(page.count('"cycle":'), len(committed["frames"])),
    )
    check(
        "the page is self-contained: no external request",
        "http://" not in page.replace("http://www.w3.org", "")
        and "https://" not in page,
    )
    with open(os.path.join(HERE, "guide.html")) as handle:
        check(
            "guide.html links the interactive page",
            "images/dag_interactive.html" in handle.read(),
        )


def main():
    hal_py, netlist = analysis.load()
    report = analysis.elaborate(hal_py, netlist)

    # --- the import and the gate library still agree ------------------------
    check(
        "netlist loads with AGILEX_TENNM",
        netlist.get_gate_library().get_name() == "AGILEX_TENNM",
        netlist.get_gate_library().get_name(),
    )
    check(
        "every ALM got semantics, none refused",
        report["elaborated"] == 24 and not report["refused"],
        "elaborated={} refused={}".format(report["elaborated"], len(report["refused"])),
    )

    # --- the committed pictures still say what the guide says they say ------
    check_dag()
    check_dag_interactive()

    # --- step 1: first contact ---------------------------------------------
    stats = analysis.step_stats(hal_py, netlist)
    check(
        "50 gates: 24 ff + 24 alm + gnd + vcc",
        stats["gate_types"]
        == {"HAL_GND": 1, "HAL_VCC": 1, "tennm_ff": 24, "tennm_lcell_comb": 24},
        str(stats["gate_types"]),
    )
    check(
        "boundary is clk, rst_n -> led",
        stats["global_inputs"] == ["clk", "rst_n"]
        and stats["global_outputs"] == ["led"],
        "{} -> {}".format(stats["global_inputs"], stats["global_outputs"]),
    )
    check(
        "the netlist is flat: one module",
        stats["modules"] == 1,
        "modules={}".format(stats["modules"]),
    )

    # --- step 2: one register bank -----------------------------------------
    registers = analysis.step_registers(hal_py, netlist)
    check(
        "all 24 flip-flops share clk/clrn/ena -> one candidate bank",
        len(registers["groups"]) == 1 and registers["groups"][0]["size"] == 24,
        "groups={}".format([g["size"] for g in registers["groups"]]),
    )
    group = registers["groups"][0]
    check(
        "the bank's clock is clk and its async clear is rst_n",
        group["clk"] == "clk" and group["clrn"] == "rst_n",
        "clk={} clrn={}".format(group["clk"], group["clrn"]),
    )
    check(
        "the bank has no enable: ena is tied to the constant 1",
        group["ena"] in ("vcc", "'1'", "1"),
        "ena={}".format(group["ena"]),
    )

    # --- step 3: 24 bit-slice loops, and nothing wider ----------------------
    # A ripple-carry counter has no word-level feedback: each bit feeds only
    # its own future (ff -> alm -> ff), and the coupling between bits is the
    # strictly forward carry chain of step 4. So the SCCs are 24 two-gate
    # loops -- one {tennm_ff, tennm_lcell_comb} pair per bit -- and any
    # component larger than that would mean LFSR-style cross-bit feedback.
    scc = analysis.step_scc(hal_py, netlist)
    check(
        "24 strongly connected components, one per bit slice",
        len(scc["components"]) == 24,
        "components={}".format([c["size"] for c in scc["components"]]),
    )
    check(
        "every loop is one flip-flop with its own ALM, nothing wider",
        all(
            c["size"] == 2 and c["gate_types"] == {"tennm_ff": 1, "tennm_lcell_comb": 1}
            for c in scc["components"]
        ),
        "sizes={} biggest={}".format(
            [c["size"] for c in scc["components"]],
            scc["components"][0]["gate_types"] if scc["components"] else None,
        ),
    )
    check(
        "the 24 loops cover all 24 registers",
        sum(c["gate_types"].get("tennm_ff", 0) for c in scc["components"]) == 24,
        "ffs={}".format(sum(c["gate_types"].get("tennm_ff", 0) for c in scc["components"])),
    )

    # --- step 4: one carry chain, 23 cells long ----------------------------
    chain = analysis.step_chain(hal_py, netlist)
    check(
        "exactly one carry chain",
        len(chain["chains"]) == 1,
        "chains={}".format([c["length"] for c in chain["chains"]]),
    )
    check(
        "the chain is 23 cells long (bits 1..23; bit 0 is a plain inverter)",
        chain["chains"][0]["length"] == 23,
        "length={}".format(chain["chains"][0]["length"]),
    )
    first = chain["chains"][0]["slices"][0]
    check(
        "the first slice is the only one with two register operands",
        len(first["data_inputs"]) == 2,
        "{} -> {}".format(first["cell"], sorted(first["data_inputs"])),
    )
    rest = chain["chains"][0]["slices"][1:]
    check(
        "every other slice takes exactly one register operand: +0 with carry",
        all(len(s["data_inputs"]) == 1 for s in rest),
        "widths={}".format(sorted({len(s["data_inputs"]) for s in rest})),
    )
    check(
        "every chain slice's sumout feeds exactly one register d pin",
        all(
            len(s["sumout_sink"]) == 1 and s["sumout_sink"][0].endswith(".d")
            for s in chain["chains"][0]["slices"]
        ),
        "",
    )

    # --- step 5: the increment is visible in the Boolean functions ----------
    increment = analysis.step_increment(hal_py, netlist)
    by_name = {cell["cell"]: cell for cell in increment["cells"]}
    normal = [c for c in increment["cells"] if c["mode"] == "normal"]
    check(
        "exactly one cell is in normal (non-arithmetic) mode: the LSB toggle",
        len(normal) == 1,
        "cells={}".format([c["cell"] for c in normal]),
    )
    if normal:
        check(
            "that cell inverts its single register input",
            len(normal[0]["signal_inputs"]) == 1
            and "!" in normal[0]["functions"].get("combout", ""),
            "{} = {}".format(normal[0]["cell"], normal[0]["functions"]),
        )
    arithmetic = [c for c in increment["cells"] if c["mode"] == "arithmetic"]
    check(
        "23 arithmetic cells",
        len(arithmetic) == 23,
        "n={}".format(len(arithmetic)),
    )
    bottom = by_name.get(chain["chains"][0]["slices"][0]["cell"], {})
    check(
        "the bottom of the chain starts from carry-in 0",
        bottom.get("constant_inputs", {}).get("cin") == 0,
        "cin={}".format(bottom.get("constant_inputs", {}).get("cin")),
    )

    # --- step 6: the single output is one register bit ----------------------
    outputs = analysis.step_output(hal_py, netlist)
    check(
        "led is driven by exactly one tennm_ff q pin",
        len(outputs["outputs"]) == 1
        and len(outputs["outputs"][0]["driven_by"]) == 1
        and outputs["outputs"][0]["driven_by"][0].endswith(".q"),
        str(outputs["outputs"]),
    )
    led_driver = outputs["outputs"][0]["driven_by"][0].rsplit(".", 1)[0]
    order = analysis.step_order(hal_py, netlist)
    positions = {entry["register"]: entry["chain_position"] for entry in order["order"]}
    check(
        "led's register sits at the top of the carry chain",
        positions.get(led_driver) == order["chain_length"] - 1,
        "{} at position {} of {}".format(
            led_driver, positions.get(led_driver), order["chain_length"]
        ),
    )

    print()
    if FAILURES:
        print("{} check(s) FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
