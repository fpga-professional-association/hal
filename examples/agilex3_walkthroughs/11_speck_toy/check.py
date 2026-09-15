#!/usr/bin/env python3
"""Headless smoke check for walkthrough 11 (speck_toy).

Re-runs the load-bearing assertions of ``guide.html`` so that CI can tell when
a tool change, a plugin change or a regenerated netlist silently invalidates
the walkthrough.  Every assertion below corresponds to a claim the guide makes.

Two tiers:

* by default everything runs on a plain Python 3 interpreter -- the structural
  analysis is ``tools/hal_agilex`` and ``tools/hal_crypto``, both HAL-free, and
  the committed findings documents are re-read rather than regenerated;
* ``--with-hal`` adds the tier that loads ``netlist.hal.v`` through ``hal_py``
  and the ``AGILEX_TENNM`` gate library, which needs ``HAL_BASE_PATH`` /
  ``HAL_PY_PATH`` / ``PYTHONPATH`` pointing at a build.

    python3 examples/agilex3_walkthroughs/11_speck_toy/check.py [--with-hal]

Exit code 0 = every claim still holds, 1 = at least one does not.  Nothing here
writes into the working tree: the Quartus flow is not re-run, the committed
``.vo`` is the input, and the two negative controls are built in memory.
"""

import argparse
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
TOOLS = os.path.join(REPO, "tools")
for path in (HERE, TOOLS):
    if path not in sys.path:
        sys.path.insert(0, path)

import analysis  # noqa: E402  (needs HERE on the path)

from hal_agilex import behavior as agilex_behavior  # noqa: E402
from hal_agilex import vo_netlist  # noqa: E402
from hal_crypto import classify  # noqa: E402

EXPORT = os.path.join(HERE, "speck_toy.vo")
ARTIFACTS = os.path.join(HERE, "artifacts")

FAILURES = []

# --- the numbers guide.html states ------------------------------------------
FLIP_FLOPS = 104
LCELLS = 137
INSTANCES = FLIP_FLOPS + LCELLS
#: HAL materialises a GND and a VCC gate on load; the export has neither.
HAL_GATES = INSTANCES + 2
ALPHA = 7
BETA = 2
WORD = 16
ROUNDS = 22
CYCLES_TO_DONE = 23
XOR_CELLS = 54
BEHAVIOUR_CYCLES = 2000
#: The combinational depth the "The netlist as a graph" section states.
DAG_LEVELS = 18
CIPHERTEXT = "0xa86842f2"
#: The clock-step window: the clear, the load, the 22 rounds and the done cycle.
TRACE_CYCLES = 25
TEST_PLAINTEXT = analysis.TEST_PLAINTEXT
TEST_KEY = analysis.TEST_KEY


def check(label, condition, detail=""):
    status = "ok  " if condition else "FAIL"
    print("[{}] {}{}".format(status, label, (" -- " + detail) if detail else ""))
    if not condition:
        FAILURES.append(label)


def findings(name):
    with open(os.path.join(ARTIFACTS, name)) as handle:
        return json.load(handle)


def finding_by_id(document, finding_id):
    for entry in document["findings"]:
        if entry["id"] == finding_id:
            return entry
    return None


def _hashes(path):
    """``(raw, crlf-normalised)`` sha256 of *path*."""
    with open(path, "rb") as handle:
        data = handle.read()
    return (
        hashlib.sha256(data).hexdigest(),
        hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest(),
    )


def guide_text():
    with open(os.path.join(HERE, "guide.html")) as handle:
        return handle.read()


# ---------------------------------------------------------------------------


def check_coverage(guide):
    """Section 2: the export is inside the validated primitive coverage."""
    document = findings("inventory.findings.json")
    entry = finding_by_id(document, "hal_agilex/inventory/fully-covered")
    check("the export is fully covered", entry is not None)
    if entry is None:
        return
    check(
        "coverage finding is proven_under_assumptions",
        entry["status"] == "proven_under_assumptions",
        entry["status"],
    )
    histogram = entry["data"]["histogram"]
    check(
        "{} flip-flops and {} ALMs, nothing else".format(FLIP_FLOPS, LCELLS),
        histogram == {"tennm_ff": FLIP_FLOPS, "tennm_lcell_comb": LCELLS},
        str(histogram),
    )
    # Findings record the input's sha256, so this is the reproducibility check
    # the hal_agilex skill's CRLF pitfall is about.  On a CRLF checkout the raw
    # bytes differ; matching after normalising the line endings still shows the
    # committed document belongs to the committed export, and says which of the
    # two happened.
    recorded = document["artifacts"][0].get("sha256")
    raw, normalised = _hashes(EXPORT)
    check(
        "the committed findings hash the committed export",
        recorded in (raw, normalised),
        "byte-for-byte"
        if recorded == raw
        else (
            "matched only after normalising CRLF -- this checkout is not LF"
            if recorded == normalised
            else "{} vs {}".format(recorded, raw)
        ),
    )


def check_structure(netlist, model, guide):
    """Sections 4-7: the census, the chains, the rotations, the XOR layer."""
    stats = analysis.step_stats(netlist, model)
    check(
        "{} instances: {} tennm_ff + {} tennm_lcell_comb".format(
            INSTANCES, FLIP_FLOPS, LCELLS
        ),
        stats["gate_types"] == {"tennm_ff": FLIP_FLOPS, "tennm_lcell_comb": LCELLS},
        str(stats["gate_types"]),
    )
    check(
        "the boundary is clk/rst_n/start/pt/key -> ct/busy/done",
        sorted(entry["name"] for entry in stats["inputs"])
        == ["clk", "key", "pt", "rst_n", "start"]
        and sorted(entry["name"] for entry in stats["outputs"])
        == ["busy", "ct", "done"],
        str(stats["inputs"] + stats["outputs"]),
    )

    registers = analysis.step_registers(netlist, model)
    groups = registers["enable_groups"]
    check(
        "one clock, one async clear, three enable groups (96 / 5 / 3)",
        len(groups) == 3
        and [entry["size"] for entry in groups] == [96, 5, 3]
        and {entry["clk"] for entry in groups} == {"clk"}
        and {entry["clrn"] for entry in groups} == {"rst_n"},
        str([(entry["size"], entry["ena"]) for entry in groups]),
    )
    check(
        "six 16-bit word banks and a 5-bit counter",
        registers["q_vectors"]
        == {"k": 16, "l0": 16, "l1": 16, "l2": 16, "rnd": 5, "x": 16, "y": 16},
        str(registers["q_vectors"]),
    )

    chains = analysis.step_chains(netlist, model)
    check(
        "exactly two carry chains, both verified as addition",
        chains["chains"] == 2
        and len(chains["adders"]) == 2
        and all(entry["operation"] == "add" for entry in chains["adders"]),
        str([(entry["cells"][0], entry["operation"]) for entry in chains["adders"]]),
    )
    rotated = {}
    for entry in chains["adders"]:
        for operand in entry["operands"].values():
            if operand["rotation"]:
                rotated[operand["vector"]] = operand["rotation"]["rotation"]
    check(
        "one operand of each chain is a rotation by {} of a 16-bit bank".format(ALPHA),
        rotated == {"x": ALPHA, "l0": ALPHA},
        str(rotated),
    )
    check(
        "the other operand of each chain is straight wiring",
        sum(
            1
            for entry in chains["adders"]
            for operand in entry["operands"].values()
            if operand["rotation"] is None
        )
        == 2,
    )

    banks = analysis.step_banks(netlist, model)
    left = {entry["source"]: entry["rotate_left_by"] for entry in banks["rotations"]}
    check(
        "the y and k banks each read themselves rotated left by {}".format(BETA),
        left == {"y": BETA, "k": BETA},
        str(left),
    )
    check(
        "both are 16 bits wide, read off all 16 next-state cells",
        all(
            entry["width"] == WORD and entry["bits_observed"] == WORD
            for entry in banks["rotations"]
        ),
    )

    xors = analysis.step_xor(netlist, model)
    check(
        "{} XOR cells, and not one of them is standalone".format(XOR_CELLS),
        xors["xor_cells"] == XOR_CELLS and xors["standalone"] == 0,
        "xor={} standalone={}".format(xors["xor_cells"], xors["standalone"]),
    )
    check(
        "the load select is what has to be held to see them",
        any(key.endswith("=1") and "run" in key for key in xors["cofactors"]),
        str(xors["cofactors"]),
    )

    recovered = analysis.step_round(netlist, model)
    check(
        "the recovered round is ROR {} / ROL {} on {}-bit words".format(
            ALPHA, BETA, WORD
        ),
        recovered["alpha_right_rotation"] == ALPHA
        and recovered["beta_left_rotation"] == BETA
        and recovered["word_bits"] == WORD,
        str(recovered["round_function"]),
    )
    check(
        "the guide states the same round function",
        "ROR(x, 7) + y" in guide and "ROL(y, 2)" in guide,
    )


def check_behaviour(netlist, model, guide):
    """Section 8: drive the netlist, and the bounded model comparisons."""
    run = analysis.step_encrypt(netlist, model)
    check(
        "the netlist encrypts the published Speck32/64 vector",
        run["ciphertext"] == CIPHERTEXT and run["matches_published_vector"],
        run["ciphertext"],
    )
    check(
        "it takes {} cycles and {} rounds".format(CYCLES_TO_DONE, ROUNDS),
        run["cycles_from_accepted_start_to_done"] == CYCLES_TO_DONE
        and run["rounds"] == ROUNDS
        and run["round_counter_range"] == [0, ROUNDS - 1],
        "{} cycles, counter {}".format(
            run["cycles_from_accepted_start_to_done"], run["round_counter_range"]
        ),
    )
    check("the guide quotes the ciphertext", CIPHERTEXT[2:] in guide.lower())

    for name, label in (
        ("behavior_design.findings.json", "design.v"),
        ("behavior_recovered.findings.json", "recovered.v"),
    ):
        document = findings(name)
        entry = finding_by_id(document, "hal_agilex/behavior/reference-equivalence")
        check(
            "the netlist matches the {} model for {} cycles".format(
                label, BEHAVIOUR_CYCLES
            ),
            entry is not None
            and entry["status"] == "proven_bounded"
            and entry["metrics"]["cycles_checked"] == BEHAVIOUR_CYCLES,
            name,
        )

    for name, constant in (
        ("behavior_negative_control_alpha8.findings.json", "ALPHA"),
        ("behavior_negative_control_beta3.findings.json", "BETA"),
    ):
        document = findings(name)
        entry = finding_by_id(document, "hal_agilex/behavior/reference-mismatch")
        check(
            "the committed {} negative control is a counterexample".format(constant),
            entry is not None and entry["status"] == "bounded_counterexample",
            name,
        )

    # ... and re-run them, so that "the control is caught" is a live fact and
    # not a document somebody could have edited.  Both diverge within a few
    # cycles, so a short bound is enough here.
    for constant, wrong in (("ALPHA", 8), ("BETA", 3)):
        result = _run_control(constant, wrong)
        check(
            "a reference with {} = {} is still caught".format(constant, wrong),
            "mismatch" in result,
            "diverged at cycle {}".format(result.get("mismatch", {}).get("cycle")),
        )


def _run_control(constant, wrong, cycles=60):
    """Behaviour run against recovered_reference.py with one constant moved."""
    import importlib.util
    import types

    source = open(os.path.join(HERE, "recovered_reference.py")).read()
    broken = re.sub(
        r"^{} = \d+$".format(constant),
        "{} = {}".format(constant, wrong),
        source,
        count=1,
        flags=re.M,
    )
    if broken == source:
        raise SystemExit("could not move {} in recovered_reference.py".format(constant))
    module = types.ModuleType("speck_negative_control")
    exec(compile(broken, "<negative-control>", "exec"), module.__dict__)
    netlist = vo_netlist.parse_file(EXPORT)
    return agilex_behavior.run_reference_check(netlist, module, cycles=cycles)


def check_identification(guide):
    """Section 9: what hal_crypto makes of the export, re-derived here."""
    document = findings("identify.findings.json")
    family = finding_by_id(document, "hal_crypto/identify/family")
    style = finding_by_id(document, "hal_crypto/identify/classical-vs-pqc")
    check(
        "the committed verdict is family arx, classical-style",
        family is not None
        and family["data"]["family"] == "arx"
        and style is not None
        and style["data"]["style"] == "classical-style",
        "{} / {}".format(
            family and family["data"]["family"], style and style["data"]["style"]
        ),
    )

    netlist = vo_netlist.parse_file(EXPORT)
    evidence = classify.run_passes(netlist)
    decision = classify.verdict(evidence)
    check(
        "re-running the classifier still says arx / classical-style",
        decision["family"] == "arx" and decision["style"] == "classical-style",
        "{} / {}".format(decision["family"], decision["style"]),
    )
    check(
        "the confidence tier is high, on four rotations and two adders",
        decision["confidence"] == "high"
        and len(evidence["arx"]["rotations"]) == 4
        and len(evidence["arx"]["adders"]) == 2,
        "{} / {} rotations / {} adders".format(
            decision["confidence"],
            len(evidence["arx"]["rotations"]),
            len(evidence["arx"]["adders"]),
        ),
    )
    names = [entry["name"] for entry in evidence["arx"]["rotation_families"]]
    check(
        "the rotation amounts match the published speck_32 set and nothing else",
        names == ["speck_32"],
        str(names),
    )
    check(
        "the guide quotes the verdict and the caveat",
        "speck_32" in guide and "classical-style" in guide,
    )


def check_dag(guide):
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
    check(
        "guide.html quotes the same level count",
        "<code>{}</code> topological levels".format(DAG_LEVELS) in guide,
    )


def check_dag_interactive(guide):
    """The committed clock-step page is built from a trace that reproduces.

    The page's whole claim is that its values are *this* run of the export, so
    this re-runs the exporter with the options guide.html prints and requires
    the same document back.  Pure Python: no HAL, no Graphviz, no browser, and
    nothing is written into the tree.
    """
    from hal_agilex import trace as agilex_trace

    trace_path = os.path.join(ARTIFACTS, "dag_trace.json")
    page_path = os.path.join(HERE, "images", "dag_interactive.html")
    check("artifacts/dag_trace.json exists", os.path.isfile(trace_path))
    check("images/dag_interactive.html exists", os.path.isfile(page_path))
    if not (os.path.isfile(trace_path) and os.path.isfile(page_path)):
        return

    with open(trace_path, encoding="utf-8") as handle:
        committed = json.load(handle)
    fresh = agilex_trace.run_trace(
        vo_netlist.parse_file(EXPORT),
        agilex_trace.load_reference(os.path.join(HERE, "recovered_reference.py")),
        cycles=TRACE_CYCLES,
        holds={"start": 1, "pt": TEST_PLAINTEXT, "key": TEST_KEY},
    )
    differences = agilex_trace.differences(committed, fresh)
    check(
        "the committed trace reproduces from the committed .vo",
        not differences,
        ", ".join(differences),
    )
    check(
        "the traced window is one whole encryption ending in the published vector",
        len(committed["frames"]) == TRACE_CYCLES
        and committed["frames"][-1]["outputs"]["done"] == 1
        and committed["frames"][-1]["outputs"]["ct"] == int(CIPHERTEXT, 16)
        and sum(frame["outputs"]["busy"] for frame in committed["frames"]) == ROUNDS,
        "{} frames, {} busy, last ct=0x{:08x}".format(
            len(committed["frames"]),
            sum(frame["outputs"]["busy"] for frame in committed["frames"]),
            committed["frames"][-1]["outputs"]["ct"],
        ),
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
    check(
        "guide.html links the interactive page",
        "images/dag_interactive.html" in guide,
    )


def check_models(guide):
    """Both Python models reproduce the published vector on their own."""
    for name in ("reference.py", "recovered_reference.py"):
        module = _load_module(os.path.join(HERE, name))
        state = module.initial_state()
        state = module.next_state(
            state,
            {
                "rst_n": 1,
                "start": 1,
                "pt": analysis.TEST_PLAINTEXT,
                "key": analysis.TEST_KEY,
            },
        )
        idle = {"rst_n": 1, "start": 0, "pt": 0, "key": 0}
        cycles = 1
        while not module.outputs(state, idle)["done"] and cycles < 100:
            state = module.next_state(state, idle)
            cycles += 1
        out = module.outputs(state, idle)
        check(
            "{} reproduces the published vector in {} cycles".format(
                name, CYCLES_TO_DONE
            ),
            out["ct"] == int(CIPHERTEXT, 16) and cycles == CYCLES_TO_DONE,
            "ct=0x{:08x} after {} cycles".format(out["ct"], cycles),
        )


def _load_module(path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("speck_model", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_hal(guide):
    """The tier that needs a built HAL: a second reader on the same netlist."""
    netlist, model = analysis.load()
    result = analysis.step_hal(netlist, model)
    check(
        "netlist.hal.v loads with AGILEX_TENNM",
        result["gate_library"] == "AGILEX_TENNM",
        result["gate_library"],
    )
    check(
        "HAL sees the same {} instances plus its two constant gates".format(INSTANCES),
        result["gates"] == HAL_GATES
        and result["gate_types"].get("tennm_ff") == FLIP_FLOPS
        and result["gate_types"].get("tennm_lcell_comb") == LCELLS,
        "{} gates {}".format(result["gates"], result["gate_types"]),
    )
    check(
        "every ALM got semantics, none refused",
        result["elaborated"] == LCELLS and not result["refused"],
        "elaborated={} refused={}".format(result["elaborated"], len(result["refused"])),
    )
    check(
        "the netlist is flat: one module",
        result["modules"] == 1,
        "modules={}".format(result["modules"]),
    )
    components = result["components"]
    check(
        "four strongly connected components, sized 144 / 80 / 14 / 2",
        [entry["size"] for entry in components] == [144, 80, 14, 2],
        str([entry["size"] for entry in components]),
    )
    check(
        "and they are the key schedule, the data path, the control and a flag",
        [sorted(entry["registers"]) for entry in components]
        == [
            ["k", "l0", "l1", "l2"],
            ["x", "y"],
            ["pen", "rnd", "run"],
            ["fin"],
        ],
        str([sorted(entry["registers"]) for entry in components]),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--with-hal",
        action="store_true",
        help="also run the tier that loads the netlist through hal_py",
    )
    args = parser.parse_args(argv)

    guide = guide_text()
    netlist, model = analysis.load()

    check_coverage(guide)
    check_structure(netlist, model, guide)
    check_behaviour(netlist, model, guide)
    check_identification(guide)
    check_models(guide)
    check_dag(guide)
    check_dag_interactive(guide)

    if args.with_hal:
        check_hal(guide)
    else:
        print("[skip] the hal_py tier (pass --with-hal with a built HAL)")

    print()
    if FAILURES:
        print("{} check(s) FAILED: {}".format(len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
