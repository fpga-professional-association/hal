#!/usr/bin/env python3
"""Headless smoke check for walkthrough 13 (trivium_stream).

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

    python3 examples/agilex3_walkthroughs/13_trivium_stream/check.py [--with-hal]

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

EXPORT = os.path.join(HERE, "trivium_stream.vo")
ARTIFACTS = os.path.join(HERE, "artifacts")

FAILURES = []

# --- the numbers guide.html states ------------------------------------------
FLIP_FLOPS = 301
LCELLS = 310
INSTANCES = FLIP_FLOPS + LCELLS
#: HAL materialises a GND and a VCC gate on load; the export has neither.
HAL_GATES = INSTANCES + 2
STATE_BITS = 288
#: The three segment lengths, in the cipher's own order A, B, C.
SEGMENT_LENGTHS = [93, 84, 111]
WARMUP_STEPS = 4 * STATE_BITS  # 1152
MODULI = [64, 18]
CYCLES_TO_VALID = WARMUP_STEPS + 1  # one load cycle, then 1152 steps
BEHAVIOUR_CYCLES = 3600
#: The combinational depth the "The netlist as a graph" section states.
DAG_LEVELS = 3
#: The clock-step window: the end of one warm-up and the cycle `ks_valid` rises.
TRACE_CYCLES = 32
TRACE_SKIP = 1140
#: The select that has to be held for any shift link to exist at all.
MODE = {"net": "start", "value": 0}
HEADLINE = analysis.TEST_VECTORS[analysis.HEADLINE_VECTOR]


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
    """Sections 4-7: the census, the chains, the feedback, the load."""
    stats = analysis.step_stats(netlist, model)
    check(
        "{} instances: {} tennm_ff + {} tennm_lcell_comb".format(
            INSTANCES, FLIP_FLOPS, LCELLS
        ),
        stats["gate_types"] == {"tennm_ff": FLIP_FLOPS, "tennm_lcell_comb": LCELLS},
        str(stats["gate_types"]),
    )
    check(
        "the boundary is clk/rst_n/start/key/iv -> ks/ks_valid/busy",
        sorted(entry["name"] for entry in stats["inputs"])
        == ["clk", "iv", "key", "rst_n", "start"]
        and sorted(entry["name"] for entry in stats["outputs"])
        == ["busy", "ks", "ks_valid"],
        str(stats["inputs"] + stats["outputs"]),
    )

    registers = analysis.step_registers(netlist, model)
    check(
        "a 288-bit state vector, a 6-bit and a 5-bit counter",
        registers["q_vectors"].get("s") == STATE_BITS
        and registers["q_vectors"].get("lo") == 6
        and registers["q_vectors"].get("hi") == 5,
        str(registers["q_vectors"]),
    )
    check(
        "one clock, one async clear, and the enable split is 296 / 5",
        [entry["size"] for entry in registers["enable_groups"]] == [296, 5]
        and {entry["clk"] for entry in registers["enable_groups"]} == {"clk"}
        and {entry["clrn"] for entry in registers["enable_groups"]} == {"rst_n"},
        str([(entry["size"], entry["ena"]) for entry in registers["enable_groups"]]),
    )

    chains = analysis.step_chains(netlist, model)
    check(
        "not one of the {} registers is a plain shift link".format(FLIP_FLOPS),
        chains["registers"] == FLIP_FLOPS
        and chains["plain_shift_links_without_holding_anything"] == 0,
        "{} links".format(chains["plain_shift_links_without_holding_anything"]),
    )
    check(
        "the only mode candidate is the load select",
        chains["mode_candidates"] == [MODE["net"]],
        str(chains["mode_candidates"]),
    )
    check(
        "three segments of {} stages, {} bits in all".format(
            "/".join(str(length) for length in SEGMENT_LENGTHS), STATE_BITS
        ),
        chains["chains"] == 3
        and sorted(entry["length"] for entry in chains["segments"])
        == sorted(SEGMENT_LENGTHS)
        and chains["total_stages"] == STATE_BITS,
        str([entry["length"] for entry in chains["segments"]]),
    )
    check(
        "every segment is a coupled, autonomous NLFSR seen with {} = {}".format(
            MODE["net"], MODE["value"]
        ),
        all(
            entry["kind"] == "nlfsr"
            and entry["coupled"]
            and entry["autonomous"]
            and entry["mode"] == MODE
            and len(entry["coupled_chains"]) == 1
            for entry in chains["segments"]
        ),
        str([(entry["kind"], entry["coupled"], entry["mode"]) for entry in chains["segments"]]),
    )
    check(
        "the three segments form a ring, each reading exactly one other",
        sorted(
            (entry["chain"], entry["coupled_chains"][0])
            for entry in chains["segments"]
        )
        == [(0, 1), (1, 2), (2, 0)],
        str([(e["chain"], e["coupled_chains"]) for e in chains["segments"]]),
    )

    feedback = analysis.step_feedback(netlist, model)
    equations = sorted(entry["equation_spec"] for entry in feedback["segments"])
    check(
        "the three recovered feedbacks are the published Trivium ones",
        equations
        == sorted(
            [
                "s171 + s66 + s93 + s91 * s92",
                "s162 + s177 + s264 + s175 * s176",
                "s243 + s288 + s69 + s286 * s287",
            ]
        ),
        str(equations),
    )
    check(
        "each has degree 2: exactly one AND term, three of them in all",
        feedback["degrees"] == [2] and feedback["and_terms"] == 3,
        "degrees={} and_terms={}".format(feedback["degrees"], feedback["and_terms"]),
    )
    check(
        "every AND is over two adjacent stages",
        all(
            abs(
                int(term[0][2:-1]) - int(term[1][2:-1])
            )
            == 1
            for entry in feedback["segments"]
            for term in entry["product_terms"]
        ),
        str([entry["product_terms"] for entry in feedback["segments"]]),
    )

    output = analysis.step_output(netlist, model)
    check(
        "the output cell is a pure XOR of six state bits",
        output["is_pure_xor"] and output["arity"] == 6 and output["constant"] == 0,
        str(output["sources_spec"]),
    )
    check(
        "all six are s66/s93/s162/s177/s243/s288",
        output["sources_spec"] == [66, 93, 162, 177, 243, 288],
        str(output["sources_spec"]),
    )
    check(
        "every one of them is a linear feedback tap and none is an AND input",
        len(output["also_a_feedback_tap"]) == 6
        and not output["not_a_feedback_tap"]
        and not output["reads_any_and_term_input"],
        str(output["not_a_feedback_tap"] + output["reads_any_and_term_input"]),
    )

    load = analysis.step_load(netlist, model)
    fields = {entry["port"]: entry for entry in load["fields"]}
    check(
        "the load puts the key at s1..s80 and the iv at s94..s173",
        set(fields) == {"key", "iv"}
        and fields["key"]["state_range"] == [0, 79]
        and fields["key"]["identity_offset"] == 0
        and fields["iv"]["state_range"] == [93, 172]
        and fields["iv"]["identity_offset"] == 93,
        str(load["fields"]),
    )
    check(
        "and three ones at s286/s287/s288, with 125 zeros in two runs",
        load["ones_at_spec"] == [286, 287, 288]
        and load["zero_bits"] == 125
        and [run["length"] for run in load["zero_runs"]] == [13, 112],
        str(load["ones_at_spec"]) + " " + str(load["zero_runs"]),
    )

    warmup = analysis.step_warmup(netlist, model)
    check(
        "two terminal-count cells with moduli {} and {}".format(*MODULI),
        warmup["moduli"] == MODULI,
        str(warmup["terminal_count_cells"]),
    )
    check(
        "their product is {} = 4 x {} state bits".format(WARMUP_STEPS, STATE_BITS),
        warmup["warmup_steps"] == WARMUP_STEPS
        and warmup["state_bits"] == STATE_BITS
        and warmup["steps_per_state_bit"] == 4,
        "{} steps".format(warmup["warmup_steps"]),
    )
    check(
        "the guide states the same warm-up",
        "1152" in guide and ("4 &times; 288" in guide or "4 × 288" in guide),
    )


def check_behaviour(netlist, model, guide):
    """Section 8: drive the netlist, and the bounded model comparisons."""
    run = analysis.step_keystream(netlist, model)
    check(
        "the netlist reproduces all {} published eSTREAM vectors".format(
            len(analysis.TEST_VECTORS)
        ),
        run["all_match"],
        str([(entry["vector"], entry["keystream"]) for entry in run["vectors"]]),
    )
    check(
        "each takes {} cycles from an accepted start and {} warm-up steps".format(
            CYCLES_TO_VALID, WARMUP_STEPS
        ),
        run["warmup_steps"] == [WARMUP_STEPS]
        and all(
            entry["cycles_from_accepted_start_to_valid"] == CYCLES_TO_VALID
            for entry in run["vectors"]
        ),
        str(run["warmup_steps"]),
    )
    check(
        "the guide quotes the headline keystream",
        HEADLINE[3].lower() in guide.lower(),
        HEADLINE[3],
    )

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

    for name, what in (
        ("behavior_negative_control_tap.findings.json", "moved tap"),
        ("behavior_negative_control_linear.findings.json", "AND replaced by XOR"),
    ):
        document = findings(name)
        entry = finding_by_id(document, "hal_agilex/behavior/reference-mismatch")
        check(
            "the committed '{}' negative control is a counterexample".format(what),
            entry is not None and entry["status"] == "bounded_counterexample",
            name,
        )

    # ... and re-run them, so that "the control is caught" is a live fact and
    # not a document somebody could have edited.  400 cycles, not 200: both
    # controls diverge at cycle 71, and `behavior` asserts its asynchronous
    # clear at cycles//3, so a 200-cycle run clears the state at cycle 66 and
    # the divergence never happens.  That is the coverage-hole lesson of
    # walkthrough 08 in miniature, and it is why the bound is chosen rather
    # than picked.
    for what, pattern, replacement in NEGATIVE_CONTROLS:
        result = _run_control(pattern, replacement)
        cycle = result.get("mismatch", {}).get("cycle")
        check(
            "a reference with {} is still caught".format(what),
            "mismatch" in result,
            "diverged at cycle {}".format(cycle),
        )
        check(
            "... and not immediately: the wrong bit has to shift to the output",
            cycle is not None and 8 < cycle < 133,
            "cycle {}".format(cycle),
        )
        check(
            "the guide quotes the divergence cycle it actually gets",
            cycle is not None and "<strong>{}</strong>".format(cycle) in guide,
            "cycle {}".format(cycle),
        )


NEGATIVE_CONTROLS = [
    ("one feedback tap moved by one stage", r"^T3_TAPS = \(242, 287, 68\)$",
     "T3_TAPS = (242, 287, 69)"),
    ("the AND terms replaced by XORs", r"^NONLINEAR = 1$", "NONLINEAR = 0"),
]


def _run_control(pattern, replacement, cycles=400):
    """Behaviour run against recovered_reference.py with one line rewritten."""
    import types

    with open(os.path.join(HERE, "recovered_reference.py")) as handle:
        source = handle.read()
    broken = re.sub(pattern, replacement, source, count=1, flags=re.M)
    if broken == source:
        raise SystemExit(
            "could not apply {!r} to recovered_reference.py".format(pattern)
        )
    module = types.ModuleType("trivium_negative_control")
    exec(compile(broken, "<negative-control>", "exec"), module.__dict__)
    netlist = vo_netlist.parse_file(EXPORT)
    return agilex_behavior.run_reference_check(netlist, module, cycles=cycles)


def check_identification(guide):
    """Section 9: what hal_crypto makes of the export, re-derived here."""
    document = findings("identify.findings.json")
    family = finding_by_id(document, "hal_crypto/identify/family")
    style = finding_by_id(document, "hal_crypto/identify/classical-vs-pqc")
    check(
        "the committed verdict is family lfsr-stream, classical-style",
        family is not None
        and family["data"]["family"] == "lfsr-stream"
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
        "re-running the classifier still says lfsr-stream / classical-style",
        decision["family"] == "lfsr-stream"
        and decision["style"] == "classical-style"
        and decision["confidence"] == "high",
        "{} / {} / {}".format(
            decision["family"], decision["style"], decision["confidence"]
        ),
    )
    check(
        "three NLFSRs, no LFSR: every feedback has a degree-2 term",
        [entry["kind"] for entry in evidence["shift"]] == ["nlfsr"] * 3
        and all(entry["feedback_degree"] == 2 for entry in evidence["shift"]),
        str([(entry["kind"], entry.get("feedback_degree")) for entry in evidence["shift"]]),
    )
    check(
        "none of them carries a feedback polynomial",
        all("polynomial" not in entry for entry in evidence["shift"]),
    )
    check(
        "the guide quotes the verdict and the walkthrough 05 contrast",
        "lfsr-stream" in guide and "05_lfsr_prng" in guide,
    )


def check_dag(guide):
    """The committed DAG image and the guide agree on the level count."""
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
    """The committed clock-step page is built from a trace that reproduces."""
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
        skip=TRACE_SKIP,
        holds={"start": 1, "key": 0, "iv": 0},
    )
    differences = agilex_trace.differences(committed, fresh)
    check(
        "the committed trace reproduces from the committed .vo",
        not differences,
        ", ".join(differences),
    )
    rises = [
        frame["cycle"]
        for frame in committed["frames"]
        if frame["outputs"]["ks_valid"] == 1
    ]
    check(
        "the window contains exactly the cycle the warm-up ends on",
        len(committed["frames"]) == TRACE_CYCLES and len(rises) == 1,
        "{} frames, ks_valid high at {}".format(len(committed["frames"]), rises),
    )
    check(
        "and on that cycle ks carries z1 of the all-zero-key vector",
        bool(rises)
        and [
            frame["outputs"]["ks"]
            for frame in committed["frames"]
            if frame["cycle"] == rises[0]
        ]
        == [int(HEADLINE[3][:2], 16) & 1],
        "ks={}".format(
            [
                frame["outputs"]["ks"]
                for frame in committed["frames"]
                if frame["cycle"] == rises[0]
            ]
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
    """Both Python models reproduce the published vectors on their own."""
    for name in ("reference.py", "recovered_reference.py"):
        module = _load_module(os.path.join(HERE, name))
        for label, key_hex, iv_hex, expected in analysis.TEST_VECTORS:
            got, cycles = _run_model(module, key_hex, iv_hex)
            check(
                "{} reproduces {} in {} cycles".format(name, label, CYCLES_TO_VALID),
                got == expected and cycles == CYCLES_TO_VALID,
                "{} after {} cycles".format(got, cycles),
            )


def _run_model(module, key_hex, iv_hex, bits=64):
    state = module.initial_state()
    state = module.next_state(
        state,
        {
            "rst_n": 1,
            "start": 1,
            "key": analysis.port_word(key_hex),
            "iv": analysis.port_word(iv_hex),
        },
    )
    idle = {"rst_n": 1, "start": 0, "key": 0, "iv": 0}
    cycles = 1
    while not module.outputs(state, idle)["ks_valid"]:
        state = module.next_state(state, idle)
        cycles += 1
        if cycles > 4000:
            raise SystemExit("model never validated")
    stream = []
    for _ in range(bits):
        stream.append(module.outputs(state, idle)["ks"])
        state = module.next_state(state, idle)
    return analysis.keystream_hex(stream), cycles


def _load_module(path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("trivium_model", path)
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
    biggest = components[0] if components else {}
    check(
        "the SCC decomposition sees one machine, not three",
        biggest.get("registers", {}).get("s") == STATE_BITS,
        str([entry["registers"] for entry in components]),
    )
    check(
        "the guide says the chain walk is what cuts that component into three",
        "strongly connected" in guide,
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
