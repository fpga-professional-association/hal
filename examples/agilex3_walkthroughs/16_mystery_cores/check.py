#!/usr/bin/env python3
"""Headless smoke check for walkthrough 16 (mystery cores).

Re-runs the load-bearing assertions of ``guide.html`` so that CI can tell when a
tool change, a plugin change or a regenerated netlist silently invalidates the
walkthrough.  For this one that matters more than usual: the whole point is a
*measurement*, and a measurement nobody re-takes is an anecdote.

What is re-derived rather than read:

* every blind step for every core, from ``cores/*.anon.hal.v``, compared field
  for field against the committed ``artifacts/step_<core>.json``;
* the whole score table, including the named-versus-blinded control;
* six negative controls, re-run live -- five must be caught and the sixth must
  **not** be, because it moves a constant on an unreachable path.

Two tiers:

* by default everything runs on a plain Python 3 interpreter -- the analysis is
  ``tools/hal_agilex`` and ``tools/hal_crypto``, both HAL-free;
* ``--with-hal`` adds a load of one blinded export through ``hal_py`` and the
  ``AGILEX_TENNM`` gate library, which needs ``HAL_BASE_PATH`` / ``HAL_PY_PATH``
  / ``PYTHONPATH`` pointing at a build.

    python3 examples/agilex3_walkthroughs/16_mystery_cores/check.py [--with-hal]

Exit code 0 = every claim still holds, 1 = at least one does not.  Nothing here
writes into the working tree: the Quartus flow is not re-run, the committed
exports are the input, and the negative controls are built in memory.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
TOOLS = os.path.join(REPO, "tools")
for path in (HERE, TOOLS):
    if path not in sys.path:
        sys.path.insert(0, path)

import analysis  # noqa: E402  (needs HERE on the path)

from hal_agilex import behavior as agilex_behavior  # noqa: E402
from hal_agilex import vo_netlist  # noqa: E402

ARTIFACTS = os.path.join(HERE, "artifacts")
IMAGES = os.path.join(HERE, "images")
GROUND_TRUTH = os.path.join(HERE, "ground_truth")
GATE_LIBRARY = os.path.join(
    REPO, "plugins", "gate_libraries", "definitions", "AGILEX_TENNM.hgl"
)

FAILURES = []

# --- the numbers guide.html states ------------------------------------------

CORES = analysis.CORES

#: instances, flip-flops, ALM cells, carry chains, DAG levels, per core.
CENSUS = {
    "core_a": (866, 207, 659, 0, 5),
    "core_b": (120, 49, 71, 1, 18),
    "core_c": (611, 301, 310, 0, 3),
    "core_d": (241, 104, 137, 2, 18),
    "core_e": (1209, 299, 910, 10, 43),
}

#: The register components the blind pass finds, largest first.
COMPONENTS = {
    "core_a": [200, 6],
    "core_b": [31],
    "core_c": [288, 12],
    "core_d": [64, 32, 7],
    "core_e": [288, 10],
}

#: ``(truth family, blind family, deciding rule, tool family)``.
VERDICTS = {
    "core_a": ("sponge", "sponge", "R2-sponge", "none-detected"),
    "core_b": ("none-detected", "none-detected", "R6-none", "none-detected"),
    "core_c": ("lfsr-stream", "lfsr-stream", "R5-stream", "lfsr-stream"),
    "core_d": ("arx", "arx", "R4-arx", "arx"),
    "core_e": ("lattice-ntt", "lattice-ntt", "R1-lattice", "lattice-ntt"),
}

#: The headline: the method gets all five, the tool gets four of five blinded
#: and four of five named.
#:
#: The tool's blinded score and the blinding delta are the two numbers this
#: walkthrough *moved*.  On the day it was written the tool scored three of five
#: blinded and lost core_d's family to the anonymiser; the two gaps behind that
#: were filed as issues #101 and #102 rather than patched here, and fixing them
#: afterwards is what closed the delta.  The numbers below are therefore the
#: re-measurement, not the original run, and guide.html section 7 carries both
#: columns -- a measurement nobody re-takes is an anecdote, which is why this
#: file re-derives the score table live instead of reading it.
BLIND_CORRECT = 5
TOOL_CORRECT_BLINDED = 4
TOOL_CORRECT_NAMED = 4
BLINDING_LOSSES = []
FACTS_RECOVERED = 16
FACTS_TOTAL = 24
RULES_NEVER_EXERCISED = ["R3-spn"]

#: core_a's theta layer: forty cells, each a parity of exactly five registers.
PARITY_CELLS = 40
PARITY_WIDTH = 5
#: ... and the chi layer showing up as a refusal rather than as a function.
CORE_A_UNREADABLE_CONES = 200
#: core_c's three coupled segments.
TRIVIUM_SEGMENTS = [84, 93, 111]
#: core_d's two adders, and core_b's one counter, all sixteen cells long.
ADDER_WIDTH = 15
CHAIN_CELLS = 16
COUNTER_ADDEND = 1
#: core_b's receive register.
SHIFT_STAGES = 8
#: core_e's modulus, recovered from what the correction computes.
MODULUS = 257

#: ``(core, label, pattern, replacement, cycles, caught)``
NEGATIVE_CONTROLS = [
    ("core_a", "one rho offset moved by one",
     "    (1, 44, 10, 45, 2),", "    (1, 44, 10, 45, 3),", 600, True),
    ("core_b", "the frame delimiter moved from 0x7E to 0x7F",
     "DELIMITER = 0x7E", "DELIMITER = 0x7F", 6000, True),
    ("core_b", "the watchdog limit moved by one -- unreachable, so not caught",
     "AGE_LIMIT = 0xFFFF", "AGE_LIMIT = 0xFFFE", 6000, False),
    ("core_c", "one feedback tap moved from s171 to s170",
     "^ _s(s, 171)", "^ _s(s, 170)", 3600, True),
    ("core_d", "the first rotation moved from 7 to 8",
     "ALPHA = 7", "ALPHA = 8", 2000, True),
    ("core_e", "the modulus moved from 257 to 251",
     "Q = 257", "Q = 251", 600, True),
]

#: The positive behaviour bounds the reveal committed.
BEHAVIOUR_CYCLES = {
    "core_a": 600,
    "core_b": 6000,
    "core_c": 3600,
    "core_d": 2000,
    "core_e": 600,
}


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


def step_data(core):
    with open(os.path.join(ARTIFACTS, "step_{}.json".format(core))) as handle:
        return json.load(handle)


def _hashes(path):
    """``(raw, crlf-normalised)`` sha256 of *path*."""
    with open(path, "rb") as handle:
        data = handle.read()
    return (
        hashlib.sha256(data).hexdigest(),
        hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest(),
    )


def guide_text():
    with open(os.path.join(HERE, "guide.html"), encoding="utf-8") as handle:
        return handle.read()


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------


def check_blinding_discipline():
    """The blind half must not be able to read the answers.

    Crude on purpose: the check is a regular expression over this walkthrough's
    own source, because an assertion that needs interpretation is one that stops
    being run.  ``GROUND_TRUTH`` may be *named* in the module -- ``reveal`` uses
    it -- but no function reachable from ``blind`` may mention it.
    """
    with open(os.path.join(HERE, "analysis.py"), encoding="utf-8") as handle:
        source = handle.read()

    blind_region = source[: source.index("# the reveal")]
    offenders = [
        line.strip()
        for line in blind_region.splitlines()
        if "GROUND_TRUTH" in line and not line.lstrip().startswith("#")
        and "GROUND_TRUTH = " not in line
    ]
    check(
        "no blind step reads ground_truth/",
        not offenders,
        "; ".join(offenders[:3]),
    )
    check(
        "the blinded exports are the only input the blind pass names",
        'os.path.join(CORES_DIR, core + ".anon.hal.v")' in blind_region,
    )
    for core in CORES:
        path = analysis.core_export(core)
        check(
            "{} is committed under cores/".format(core),
            os.path.isfile(path),
            os.path.relpath(path, REPO),
        )


def check_manifest_hashes():
    """Every committed file still hashes to what the answer key recorded."""
    truth = analysis.manifest()
    mismatched = []
    counted = 0
    for core in CORES:
        for label, entry in sorted(truth["cores"][core]["files"].items()):
            path = os.path.join(HERE, entry["path"])
            if not os.path.isfile(path):
                mismatched.append("{}:{} missing".format(core, label))
                continue
            counted += 1
            raw, normalised = _hashes(path)
            if entry["sha256"] not in (raw, normalised):
                mismatched.append("{}:{}".format(core, label))
    check(
        "all {} files in MANIFEST.json hash as recorded".format(counted),
        not mismatched,
        "; ".join(mismatched[:4]),
    )
    check(
        "the manifest covers all five cores",
        sorted(truth["cores"]) == sorted(CORES),
    )


def check_coverage():
    """Section 2: all five blinded exports are inside the validated coverage."""
    for core in CORES:
        document = findings("inventory_{}.findings.json".format(core))
        entry = finding_by_id(document, "hal_agilex/inventory/fully-covered")
        check(
            "{}: every primitive is inside the validated coverage".format(core),
            entry is not None and entry["status"] == "proven_under_assumptions",
            "" if entry else "no fully-covered finding",
        )
        recorded = document["artifacts"][0].get("sha256")
        raw, normalised = _hashes(analysis.core_export(core))
        check(
            "{}: the committed inventory hashes the committed export".format(core),
            recorded in (raw, normalised),
            "byte-for-byte" if recorded == raw else "CRLF-normalised",
        )


def check_census(blind_steps, guide):
    """Section 3: the census, re-derived, and the trap inside it."""
    for core in CORES:
        instances, flops, cells, chains, levels = CENSUS[core]
        census = blind_steps[core]["census"]
        shape = blind_steps[core]["shape"]
        check(
            "{}: {} instances = {} tennm_ff + {} tennm_lcell_comb".format(
                core, instances, flops, cells
            ),
            census["instances"] == instances
            and census["gate_types"].get("tennm_ff") == flops
            and census["gate_types"].get("tennm_lcell_comb") == cells,
            str(census["gate_types"]),
        )
        check(
            "{}: {} carry chain(s), {} DAG levels".format(core, chains, levels),
            census["carry_chains"] == chains and shape["dag_levels"] == levels,
            "{} chains, {} levels".format(census["carry_chains"], shape["dag_levels"]),
        )

    b_census = blind_steps["core_b"]["census"]
    d_census = blind_steps["core_d"]["census"]
    check(
        "the decoy's chain is as wide as the cipher's, {} cells each".format(
            CHAIN_CELLS
        ),
        b_census["carry_chain_lengths"] == [CHAIN_CELLS]
        and d_census["carry_chain_lengths"] == [CHAIN_CELLS, CHAIN_CELLS],
        "{} vs {}".format(
            b_census["carry_chain_lengths"], d_census["carry_chain_lengths"]
        ),
    )
    check(
        "... and as deep: both {} levels".format(CENSUS["core_b"][4]),
        blind_steps["core_b"]["shape"]["dag_levels"]
        == blind_steps["core_d"]["shape"]["dag_levels"],
    )
    check(
        "what separates them is one operand: a + {} versus two vectors".format(
            COUNTER_ADDEND
        ),
        b_census["constant_adders"] == [COUNTER_ADDEND]
        and b_census["two_operand_adders"] == []
        and d_census["two_operand_adders"] == [ADDER_WIDTH, ADDER_WIDTH],
        "{} / {}".format(b_census["constant_adders"], d_census["two_operand_adders"]),
    )
    check(
        "the guide makes that comparison in prose",
        "adds the constant" in guide and "two operand vectors" in guide,
    )


def check_state(blind_steps, guide):
    """Section 4: the state partition, and the one-way coupling in core_d."""
    for core in CORES:
        check(
            "{}: register components {}".format(core, COMPONENTS[core]),
            blind_steps[core]["state"]["component_sizes"] == COMPONENTS[core],
            str(blind_steps[core]["state"]["component_sizes"]),
        )
    check(
        "core_d: a 64-bit state drives a 32-bit one and nothing comes back",
        any(
            entry["from"] == 64 and entry["to"] == 32
            for entry in blind_steps["core_d"]["state"]["one_way_couplings"]
        )
        and not any(
            entry["from"] == 32 and entry["to"] == 64
            for entry in blind_steps["core_d"]["state"]["one_way_couplings"]
        ),
        str(blind_steps["core_d"]["state"]["one_way_couplings"]),
    )
    check(
        "core_c: three coupled feedback registers of {} stages".format(
            " + ".join(str(n) for n in TRIVIUM_SEGMENTS)
        ),
        sorted(blind_steps["core_c"]["state"]["autonomous_feedback_stages"])
        == TRIVIUM_SEGMENTS,
        str(blind_steps["core_c"]["state"]["autonomous_feedback_stages"]),
    )
    check(
        "core_c: none of them is visible until an external net is held",
        all(
            entry["mode"] is not None
            for entry in blind_steps["core_c"]["state"]["shift_structures"]
        ),
    )
    check(
        "core_b: an {}-stage shift chain whose head reads only inputs".format(
            SHIFT_STAGES
        ),
        blind_steps["core_b"]["state"]["longest_single_predecessor_chain"]
        == SHIFT_STAGES
        and blind_steps["core_b"]["state"]["chain_head_reads_only_inputs"],
        str(blind_steps["core_b"]["state"]["longest_single_predecessor_chain"]),
    )
    # Issue #102, filed by this walkthrough and fixed afterwards: the chain walk
    # followed successors, every stage of the receive register has a capture
    # register hanging off it, and the walk stopped at the first fork -- so the
    # answer was not "a short chain" but no structure at all.  What the fix is
    # allowed to produce is exactly this: an *open* chain, honestly labelled.
    # Turning the decoy's receive register into a feedback register would be a
    # false positive on the one core that has no cryptography in it.
    structures = blind_steps["core_b"]["state"]["shift_structures"]
    check(
        "core_b: hal_crypto's chain walk now finds the tapped chain too",
        len(structures) == 1
        and structures[0]["length"] == SHIFT_STAGES
        and structures[0]["kind"] == "shift_register"
        and structures[0]["mode"] is None,
        str([(entry["length"], entry["kind"]) for entry in structures]),
    )
    check(
        "core_b: ... and does not turn the decoy into a feedback register",
        blind_steps["core_b"]["state"]["autonomous_feedback_stages"] == []
        and blind_steps["core_b"]["call"]["identify_family"] == "none-detected",
    )
    check(
        "the guide says the tapped chain was a gap, and what closed it",
        "tapped" in guide and "issue #102" in guide,
    )


def check_nonlinearity(blind_steps, guide):
    """Section 5: the parity layer, and a nonlinear layer seen as a refusal."""
    nonlinear = blind_steps["core_a"]["nonlinearity"]
    check(
        "core_a: {} disjoint cells, each a parity of exactly {} registers".format(
            PARITY_CELLS, PARITY_WIDTH
        ),
        nonlinear["disjoint_register_xor_cells"].get(str(PARITY_WIDTH))
        == PARITY_CELLS,
        str(nonlinear["disjoint_register_xor_cells"]),
    )
    check(
        "core_a: {} next-state cones are too wide to enumerate".format(
            CORE_A_UNREADABLE_CONES
        ),
        nonlinear["unreadable_cones"] == CORE_A_UNREADABLE_CONES,
        str(nonlinear["unreadable_cones"]),
    )
    check(
        "the guide reads that refusal as evidence, not as a missing measurement",
        "refusal" in guide,
    )
    check(
        "no other core has a five-wide parity layer",
        all(
            blind_steps[core]["nonlinearity"]["parity_cells"] == 0
            for core in CORES
            if core != "core_a"
        ),
    )


def check_calls(blind_steps, guide):
    """Section 6: the five blind calls and the rules that produced them."""
    for core in CORES:
        _truth, family, rule, tool = VERDICTS[core]
        call = blind_steps[core]["call"]
        check(
            "{}: blind call {} by {}".format(core, family, rule),
            call["family"] == family and call["deciding_rule"] == rule,
            "{} by {}".format(call["family"], call["deciding_rule"]),
        )
        check(
            "{}: hal_crypto identify said {}".format(core, tool),
            call["identify_family"] == tool,
            call["identify_family"],
        )
        document = findings("blind_{}.findings.json".format(core))
        entry = finding_by_id(document, "walkthrough16/{}/call".format(core))
        check(
            "{}: the committed blind findings document re-derives".format(core),
            entry is not None and entry["data"]["call"] == call,
        )
    check(
        "core_e: R1 fires on the tool *and* on the chain census",
        len(blind_steps["core_e"]["call"]["all_reasons"]) >= 1
        and "R4-arx" in blind_steps["core_e"]["call"]["rules_fired"],
        "which is why rule order, not evidence, keeps it out of R4",
    )
    check(
        "the guide says rule order is what separates core_e from core_d",
        "rule order" in guide or "order of the table" in guide,
    )
    check(
        "core_e's confidence is clamped to the tool's own",
        blind_steps["core_e"]["call"]["confidence"]
        == blind_steps["core_e"]["call"]["identify_confidence"] == "medium",
    )


def check_score(blind_steps, guide):
    """Section 7: the score table, re-derived and compared to the committed one."""
    truth = analysis.manifest()
    table = analysis.score(blind_steps, truth)
    with open(os.path.join(ARTIFACTS, "score.json")) as handle:
        committed = json.load(handle)
    check(
        "the committed score table re-derives exactly",
        table == committed,
    )
    check(
        "the method gets {} of {} right".format(BLIND_CORRECT, len(CORES)),
        table["blind_correct"] == BLIND_CORRECT
        and table["blind_outcomes"] == {"correct": BLIND_CORRECT},
        str(table["blind_outcomes"]),
    )
    check(
        "hal_crypto alone gets {} of {} on the blinded exports".format(
            TOOL_CORRECT_BLINDED, len(CORES)
        ),
        table["tool_correct"] == TOOL_CORRECT_BLINDED,
        str(table["tool_outcomes"]),
    )
    check(
        "... and {} of {} on the named ones".format(TOOL_CORRECT_NAMED, len(CORES)),
        table["tool_correct_named"] == TOOL_CORRECT_NAMED,
        str(table["tool_correct_named"]),
    )
    check(
        "blinding costs no family at all, on any of the five"
        if not BLINDING_LOSSES
        else "blinding costs exactly one family, on {}".format(
            ", ".join(BLINDING_LOSSES)
        ),
        table["blinding_losses"] == BLINDING_LOSSES,
        str(table["blinding_losses"]),
    )
    # Issue #101, the other gap this walkthrough filed: the blinded Speck export
    # lost all four rotations to the anonymiser, and with them the family, while
    # both carry chains and all 54 XOR cells survived.  The fix has to bring the
    # family back on the *blinded* copy without moving the named one, so both
    # halves are checked here rather than only the score.
    check(
        "core_d: the blinded copy reaches the same family as the named one",
        all(
            entry["named_family"] == entry["blinded_family"] == "arx"
            for entry in table["blinding_delta"]
            if entry["core"] == "core_d"
        ),
    )
    check(
        "{} of {} key facts recovered".format(FACTS_RECOVERED, FACTS_TOTAL),
        table["facts_recovered"] == FACTS_RECOVERED
        and table["facts_total"] == FACTS_TOTAL,
        "{}/{}".format(table["facts_recovered"], table["facts_total"]),
    )
    check(
        "the rules never exercised are reported: {}".format(
            ", ".join(RULES_NEVER_EXERCISED)
        ),
        table["rules_never_exercised"] == RULES_NEVER_EXERCISED,
        str(table["rules_never_exercised"]),
    )
    check(
        "every truth row matches ground_truth/MANIFEST.json",
        all(
            row["truth_family"] == VERDICTS[row["core"]][0] for row in table["cores"]
        ),
    )
    check(
        "core_e's modulus {} came back from the blinded export".format(MODULUS),
        MODULUS in blind_steps["core_e"]["identify"]["recovered_moduli"],
    )
    check(
        "the guide states the {}/{} headline".format(
            BLIND_CORRECT, TOOL_CORRECT_BLINDED
        ),
        "five of five" in guide and "four of five" in guide,
    )
    check(
        "... and keeps the number it started from, so the move is on the page",
        "three of five" in guide and "3 of 5" in guide,
    )


def check_behaviour(guide):
    """Section 8: the ground truth is bound to behaviour, not asserted."""
    for core in CORES:
        document = findings("behavior_{}.findings.json".format(core))
        entry = finding_by_id(
            document, "hal_agilex/behavior/reference-equivalence"
        )
        check(
            "{}: the named export matches its reference for {} cycles".format(
                core, BEHAVIOUR_CYCLES[core]
            ),
            entry is not None
            and entry["status"] == "proven_bounded"
            and entry["bounds"]["cycle_bound"] == BEHAVIOUR_CYCLES[core],
            "" if entry else "no equivalence finding",
        )
        export = os.path.join(GROUND_TRUTH, "exports", core + ".vo")
        recorded = document["artifacts"][0].get("sha256")
        raw, normalised = _hashes(export)
        check(
            "{}: that run used the committed named export".format(core),
            recorded in (raw, normalised),
        )


def check_negative_controls():
    """Section 8b: five controls are caught live, and the sixth is not.

    Re-run rather than read, because a committed counterexample proves that a
    wrong model was once caught and not that this simulator would catch it now.
    """
    for index, entry in enumerate(NEGATIVE_CONTROLS):
        core, label, pattern, replacement, cycles, caught = entry
        source = os.path.join(GROUND_TRUTH, "references", core + "_reference.py")
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        if pattern not in text:
            check("{}: control pattern present -- {}".format(core, label), False)
            continue
        broken = text.replace(pattern, replacement, 1)
        module = types.ModuleType("mystery_control_{}".format(index))
        exec(compile(broken, "<negative-control>", "exec"), module.__dict__)  # noqa: S102
        result = agilex_behavior.run_reference_check(
            vo_netlist.parse_file(
                os.path.join(GROUND_TRUTH, "exports", core + ".vo")
            ),
            module,
            cycles=cycles,
        )
        mismatch = result.get("mismatch")
        if caught:
            check(
                "{}: negative control caught -- {}".format(core, label),
                mismatch is not None,
                "at cycle {}".format(mismatch["cycle"]) if mismatch else "not caught",
            )
        else:
            check(
                "{}: negative control NOT caught, as documented -- {}".format(
                    core, label
                ),
                mismatch is None,
                "an unreachable path cannot be observed by any bounded run",
            )


def check_images(blind_steps, guide):
    """Section 9: one levelled DAG per core, and the level counts agree."""
    for core in CORES:
        dot_path = os.path.join(IMAGES, "dag_{}.dot".format(core))
        check("images/dag_{}.dot exists".format(core), os.path.isfile(dot_path))
        if not os.path.isfile(dot_path):
            continue
        with open(dot_path, encoding="utf-8") as handle:
            header = handle.read(4096)
        match = re.search(r"^// (\d+) level\(s\)", header, re.M)
        levels = int(match.group(1)) if match else None
        check(
            "{}: the drawing has the {} levels the census measured".format(
                core, blind_steps[core]["shape"]["dag_levels"]
            ),
            levels == blind_steps[core]["shape"]["dag_levels"],
            "{} in the .dot".format(levels),
        )
        svg = os.path.join(IMAGES, "dag_{}.svg".format(core))
        if core == "core_e":
            check(
                "core_e is committed as counts only, not as a drawing",
                not os.path.isfile(svg),
                "1209 nodes over 43 levels does not lay out",
            )
        else:
            check("images/dag_{}.svg exists".format(core), os.path.isfile(svg))
    for name in (
        "cell_counter_core_b.svg",
        "cell_adder_core_d.svg",
        "cell_parity_core_a.svg",
        "module_tree_core_b.svg",
    ):
        check("images/{} exists".format(name), os.path.isfile(os.path.join(IMAGES, name)))
        check(
            "the guide embeds images/{}".format(name),
            name in guide,
        )


def check_guide(guide):
    """The guide tells the blind story before the reveal, and files its gaps."""
    blind_at = guide.find("id=\"blind\"")
    reveal_at = guide.find("id=\"reveal\"")
    check(
        "the guide's blind section comes before its reveal",
        blind_at != -1 and reveal_at != -1 and blind_at < reveal_at,
    )
    check(
        "the guide names both tool gaps it found",
        "rotation" in guide and "vector" in guide,
    )
    check(
        "the guide says no hal_crypto pass was changed for this walkthrough",
        "was changed" in guide or "not patched" in guide or "were changed" in guide,
    )
    check(
        "... and says the two it filed were fixed afterwards and re-measured",
        "issue #101" in guide and "issue #102" in guide and "re-measured" in guide,
    )
    check(
        "the guide states the decoy is the only out-of-sample core",
        "out-of-sample" in guide,
    )
    for core in CORES:
        check(
            "the guide has a section for {}".format(core),
            'id="{}"'.format(core) in guide,
        )


def check_hal_tier():
    """``--with-hal``: a second reader agrees on the census of one core."""
    for entry in os.environ.get("HAL_PY_PATH", "").split(os.pathsep):
        if entry and entry not in sys.path:
            sys.path.insert(0, entry)
    import hal_py

    hal_py.plugin_manager.load_all_plugins()
    path = analysis.core_export("core_b")
    loaded = hal_py.NetlistFactory.load_netlist(path, GATE_LIBRARY)
    check("core_b loads through hal_py", loaded is not None)
    if loaded is None:
        return

    from hal_agilex import hal_adapter

    report = hal_adapter.elaborate(hal_py, loaded)
    instances, flops, cells, _chains, _levels = CENSUS["core_b"]
    histogram = {}
    for gate in loaded.get_gates():
        name = gate.get_type().get_name()
        histogram[name] = histogram.get(name, 0) + 1
    check(
        "HAL sees the same {} instances (plus its GND and VCC)".format(instances),
        histogram.get("tennm_ff") == flops
        and histogram.get("tennm_lcell_comb") == cells
        and len(loaded.get_gates()) == instances + 2,
        str(histogram),
    )
    refused = report["refused"]
    refused_count = len(refused) if isinstance(refused, (list, tuple)) else refused
    check(
        "every covered cell gets a Boolean function attached",
        refused_count == 0 and report["elaborated"] == cells,
        "{} elaborated, {} refused".format(report["elaborated"], refused_count),
    )


# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--with-hal",
        action="store_true",
        help="also run the tier that loads a core through hal_py",
    )
    args = parser.parse_args(argv)

    guide = guide_text()

    print("== re-deriving every blind step from cores/ ==")
    blind_steps = {}
    for core in CORES:
        blind_steps[core] = analysis.blind(core)
        committed = step_data(core)
        check(
            "{}: the committed step data re-derives exactly".format(core),
            blind_steps[core] == committed,
        )
    print()

    check_blinding_discipline()
    check_manifest_hashes()
    check_coverage()
    check_census(blind_steps, guide)
    check_state(blind_steps, guide)
    check_nonlinearity(blind_steps, guide)
    check_calls(blind_steps, guide)
    check_score(blind_steps, guide)
    check_behaviour(guide)
    check_negative_controls()
    check_images(blind_steps, guide)
    check_guide(guide)

    if args.with_hal:
        print()
        check_hal_tier()

    print()
    if FAILURES:
        print("{} check(s) failed:".format(len(FAILURES)))
        for label in FAILURES:
            print("  - {}".format(label))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
