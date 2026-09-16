#!/usr/bin/env python3
"""Headless smoke check for walkthrough 14 (keccak_toy).

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

    python3 examples/agilex3_walkthroughs/14_keccak_toy/check.py [--with-hal]

Exit code 0 = every claim still holds, 1 = at least one does not.  Nothing here
writes into the working tree: the Quartus flow is not re-run, the committed
``.vo`` files are the input, and the three negative controls are built in
memory.
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

EXPORT = os.path.join(HERE, "keccak_toy.vo")
RETIMED_EXPORT = os.path.join(HERE, "keccak_retimed.vo")
ARTIFACTS = os.path.join(HERE, "artifacts")

FAILURES = []

# --- the numbers guide.html states ------------------------------------------
FLIP_FLOPS = 207
LCELLS = 659
INSTANCES = FLIP_FLOPS + LCELLS
#: HAL materialises a GND and a VCC gate on load; the export has neither.
HAL_GATES = INSTANCES + 2
#: The counterfactual export of section 10 -- same permutation, retimed.
RETIMED_FLIP_FLOPS = 207
RETIMED_LCELLS = 860
STATE_BITS = 200
LANE_WIDTH = 8
LANES = 25
NROUNDS = 18
CYCLES_TO_DONE = NROUNDS + 1  # one load cycle, then eighteen rounds
BEHAVIOUR_CYCLES = 600
#: The combinational depth the "The netlist as a graph" section states.
DAG_LEVELS = 5
#: The clock-step window: one complete permutation out of the clear.
TRACE_CYCLES = 32
#: The 5-bit substitution chi extracts to, as the guide prints it.
CHI_TABLE_HEX = (
    "00 09 12 0B 05 0C 16 0F 0A 03 18 01 0D 04 1E 07 "
    "14 15 06 17 11 10 02 13 1A 1B 08 19 1D 1C 0E 1F"
)
#: analysis.PUBLISHED_RHO reduced mod 8 -- what the wiring has to come back as.
RHO_MOD_8 = [[value % 8 for value in row] for row in analysis.PUBLISHED_RHO]
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


def _check_coverage(name, path, flip_flops, lcells, label):
    document = findings(name)
    entry = finding_by_id(document, "hal_agilex/inventory/fully-covered")
    check("{} is fully covered".format(label), entry is not None)
    if entry is None:
        return
    check(
        "{} coverage finding is proven_under_assumptions".format(label),
        entry["status"] == "proven_under_assumptions",
        entry["status"],
    )
    histogram = entry["data"]["histogram"]
    check(
        "{}: {} flip-flops and {} ALMs, nothing else".format(label, flip_flops, lcells),
        histogram == {"tennm_ff": flip_flops, "tennm_lcell_comb": lcells},
        str(histogram),
    )
    # Findings record the input's sha256, so this is the reproducibility check
    # the hal_agilex skill's CRLF pitfall is about.  On a CRLF checkout the raw
    # bytes differ; matching after normalising the line endings still shows the
    # committed document belongs to the committed export, and says which of the
    # two happened.
    recorded = document["artifacts"][0].get("sha256")
    raw, normalised = _hashes(path)
    check(
        "the committed {} findings hash the committed export".format(label),
        recorded in (raw, normalised),
        "byte-for-byte"
        if recorded == raw
        else (
            "matched only after normalising CRLF -- this checkout is not LF"
            if recorded == normalised
            else "{} vs {}".format(recorded, raw)
        ),
    )


def check_coverage(guide):
    """Section 2: both exports are inside the validated primitive coverage."""
    _check_coverage(
        "inventory.findings.json", EXPORT, FLIP_FLOPS, LCELLS, "keccak_toy"
    )
    _check_coverage(
        "inventory_retimed.findings.json",
        RETIMED_EXPORT,
        RETIMED_FLIP_FLOPS,
        RETIMED_LCELLS,
        "keccak_retimed",
    )


def check_structure(netlist, model, guide):
    """Sections 4-7: the census, the grid, theta, chi, rho/pi, iota, the schedule."""
    stats = analysis.step_stats(netlist, model)
    check(
        "{} instances: {} tennm_ff + {} tennm_lcell_comb".format(
            INSTANCES, FLIP_FLOPS, LCELLS
        ),
        stats["gate_types"] == {"tennm_ff": FLIP_FLOPS, "tennm_lcell_comb": LCELLS},
        str(stats["gate_types"]),
    )
    check(
        "the boundary is clk/rst_n/start/din -> dout/done/busy",
        sorted(entry["name"] for entry in stats["inputs"])
        == ["clk", "din", "rst_n", "start"]
        and sorted(entry["name"] for entry in stats["outputs"])
        == ["busy", "done", "dout"],
        str(stats["inputs"] + stats["outputs"]),
    )
    check(
        "din and dout are {} bits wide".format(STATE_BITS),
        all(
            entry["width"] == STATE_BITS
            for entry in stats["inputs"] + stats["outputs"]
            if entry["name"] in ("din", "dout")
        ),
        str(stats["inputs"] + stats["outputs"]),
    )

    registers = analysis.step_registers(netlist, model)
    check(
        "a {}-bit state vector and a 5-bit round counter".format(STATE_BITS),
        registers["q_vectors"].get("s") == STATE_BITS
        and registers["q_vectors"].get("rnd") == 5
        and registers["flip_flops"] == FLIP_FLOPS,
        str(registers["q_vectors"]),
    )
    check(
        "one clock, one async clear, and the enable split is 200 / 7",
        [entry["size"] for entry in registers["enable_groups"]] == [200, 7]
        and {entry["clk"] for entry in registers["enable_groups"]} == {"clk"}
        and {entry["clrn"] for entry in registers["enable_groups"]} == {"rst_n"},
        str([(entry["size"], entry["ena"]) for entry in registers["enable_groups"]]),
    )

    parity = analysis.step_parity(netlist, model)
    check(
        "40 cells are a pure XOR of exactly five flip-flops",
        parity["parity_cells"] == 40
        and parity["sources_per_cell"] == 5
        and parity["every_cell_is_a_pure_xor"],
        str(parity["parity_cells"]),
    )
    check(
        "and they partition all {} state bits into 40 classes of 5".format(STATE_BITS),
        parity["state_bits_partitioned"] == STATE_BITS
        and len(parity["column_classes"]) == 40
        and all(len(entry) == 5 for entry in parity["column_classes"]),
    )
    check(
        "the grid falls out as {} x 5 lanes of {} bits".format(5, LANE_WIDTH),
        parity["lane_width"] == LANE_WIDTH
        and parity["columns_in_the_array"] == 5
        and parity["rows_per_column"] == 5,
        parity["grid"],
    )

    theta = analysis.step_theta(netlist, model)
    check(
        "{} theta cells, each a 3-input XOR over an 11-wide cone".format(STATE_BITS),
        theta["theta_cells"] == STATE_BITS
        and theta["cone_width_histogram"] == {"11": STATE_BITS},
        str(theta["cone_width_histogram"]),
    )
    check(
        "the unrotated parity neighbour has order 5 and the composite order 8",
        theta["unrotated_neighbour_is_a_five_cycle"]
        and theta["rotated_neighbour_composed_with_it_is_an_eight_cycle"],
    )

    chi = analysis.step_chi(netlist, model)
    check(
        "{} chi cells of three theta nets each, in 40 five-cycles".format(STATE_BITS),
        chi["chi_cells"] == STATE_BITS
        and chi["inputs_per_cell"] == 3
        and chi["row_cycles"] == 40
        and chi["row_cycle_lengths"] == [5] * 40,
        "{} cycles".format(chi["row_cycles"]),
    )
    check(
        "all 40 are the same 5-bit map, and it equals keccak_chi_5 exactly",
        chi["library_match"] is not None
        and chi["library_match"]["name"] == "keccak_chi_5"
        and chi["library_match"]["tier"] == "exact",
        str(chi["library_match"]),
    )
    check(
        "the map is a bijection of degree 2, differential uniformity 8",
        chi["is_bijective"]
        and not chi["is_affine"]
        and chi["algebraic_degree"] == 2
        and chi["differential_uniformity"] == 8,
        "degree {} du {}".format(chi["algebraic_degree"], chi["differential_uniformity"]),
    )
    check(
        "the guide prints the extracted table",
        CHI_TABLE_HEX.split() == chi["table_hex"].split()
        and all(piece in guide for piece in CHI_TABLE_HEX.split()[:4]),
        chi["table_hex"],
    )
    check(
        "the S-box pass finds nothing here, because the cones are 33 wide",
        chi["sbox_pass_found"] == 0
        and chi["chi_cone_width_at_the_registers"][-1] == 33
        and any("read more than" in reason for reason in chi["sbox_pass_rejections"]),
        str(chi["chi_cone_width_at_the_registers"]),
    )

    rhopi = analysis.step_rhopi(netlist, model)
    check(
        "rho and pi cost zero cells and zero nets",
        rhopi["cells_spent_on_rho_and_pi"] == 0
        and rhopi["nets_named_after_a_rotation"] == 0,
    )
    check(
        "the 25 recovered rotation offsets are the published table mod 8",
        rhopi["recovered_rho_mod_8_spec"] == RHO_MOD_8
        and rhopi["rho_matches_published"],
        str(rhopi["recovered_rho_mod_8_spec"]),
    )
    check(
        "three lanes rotate by zero at this width",
        rhopi["lanes_rotated_by_zero"] == [[0, 0], [3, 4], [4, 3]],
        str(rhopi["lanes_rotated_by_zero"]),
    )
    check(
        "the recovered pi is lane (x,y) -> (y, 2x+3y)",
        rhopi["pi_is_lane_x_y_to_y_2x_plus_3y"],
    )

    iota = analysis.step_iota(netlist, model)
    check(
        "eight round-constant cells, four of them constant zero",
        iota["round_constant_cells"] == 8 and iota["of_those_constant_zero"] == 4,
        str(iota["round_constant_cells"]),
    )
    check(
        "the {} recovered round constants are the published schedule".format(NROUNDS),
        iota["matches_published"] and len(iota["recovered_rc"]) == NROUNDS,
        iota["recovered_rc_hex"],
    )
    check(
        "iota only ever touches bits 0, 1, 3 and 7 of one lane",
        iota["nonzero_bit_positions_spec"] == [0, 1, 3, 7]
        and iota["state_bits_touched"] == 8
        and iota["iota_lane_spec"] == [0, 0],
        str(iota["nonzero_bit_positions_spec"]),
    )
    check(
        "round 3 has no iota at all",
        iota["rounds_with_no_iota_at_all"] == [3],
        str(iota["rounds_with_no_iota_at_all"]),
    )
    check(
        "the guide quotes the recovered schedule",
        iota["recovered_rc_hex"].lower() in guide.lower(),
        iota["recovered_rc_hex"],
    )

    rounds = analysis.step_rounds(netlist, model)
    check(
        "one terminal-count cell says {} rounds, so {} cycles".format(
            NROUNDS, CYCLES_TO_DONE
        ),
        rounds["rounds"] == NROUNDS
        and rounds["terminal_count_value"] == NROUNDS - 1
        and rounds["cycles_from_accepted_start_to_done"] == CYCLES_TO_DONE,
        "{} rounds".format(rounds["rounds"]),
    )
    check(
        "200 registers share one enable; the counter and flags do not",
        rounds["gated_registers"] == STATE_BITS and rounds["ungated_registers"] == 7,
        "{} gated".format(rounds["gated_registers"]),
    )


def check_behaviour(netlist, model, guide):
    """Section 8: drive the netlist, and the bounded model comparisons."""
    run = analysis.step_vectors(netlist, model)
    check(
        "the netlist reproduces both published Keccak-f[200] vectors",
        run["all_match"],
        str([(entry["vector"], entry["output"]) for entry in run["vectors"]]),
    )
    check(
        "each takes {} cycles from an accepted start and {} rounds".format(
            CYCLES_TO_DONE, NROUNDS
        ),
        run["rounds_run"] == [NROUNDS]
        and all(
            entry["cycles_from_accepted_start_to_done"] == CYCLES_TO_DONE
            for entry in run["vectors"]
        ),
        str(run["rounds_run"]),
    )
    check(
        "the guide quotes the headline output",
        HEADLINE[2].lower() in guide.lower(),
        HEADLINE[2],
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

    for name, what, cycle in (
        ("behavior_negative_control_rho.findings.json", "moved rho offset", 9),
        ("behavior_negative_control_linear.findings.json", "chi linearised", 9),
        ("behavior_negative_control_rc.findings.json", "last round constant", 26),
    ):
        document = findings(name)
        entry = finding_by_id(document, "hal_agilex/behavior/reference-mismatch")
        check(
            "the committed '{}' negative control is caught at cycle {}".format(
                what, cycle
            ),
            entry is not None
            and entry["status"] == "bounded_counterexample"
            and entry["counterexample"]["cycle_bound"] == cycle,
            name,
        )

    # ... and re-run them, so that "the control is caught" is a live fact and
    # not a document somebody could have edited.  200 cycles, not 40: the first
    # two controls are caught at cycle 9 either way, but the third -- a wrong
    # constant in the *last* round -- is missed entirely by a 40-cycle run, and
    # caught at 46 by an 80-cycle one, because `behavior` asserts its
    # asynchronous clear at cycles//3 and moving the bound moves the clear.  A
    # shorter run is not a prefix of a longer one, which is the coverage-hole
    # lesson of walkthrough 08 with a second twist on it.
    for what, pattern, replacement, expected in NEGATIVE_CONTROLS:
        result = _run_control(pattern, replacement)
        cycle = result.get("mismatch", {}).get("cycle")
        check(
            "a reference with {} is still caught".format(what),
            "mismatch" in result,
            "diverged at cycle {}".format(cycle),
        )
        check(
            "... at the cycle the guide says it does",
            cycle == expected and "<strong>{}</strong>".format(cycle) in guide,
            "cycle {} (expected {})".format(cycle, expected),
        )

    missed = _run_control(NEGATIVE_CONTROLS[2][1], NEGATIVE_CONTROLS[2][2], cycles=40)
    check(
        "and a 40-cycle bound would have missed the last-round control entirely",
        "mismatch" not in missed,
        str(missed.get("mismatch")),
    )


NEGATIVE_CONTROLS = [
    ("one rho offset moved by one bit", r"^    \(6, 6, 3, 7, 5\),$",
     "    (6, 6, 3, 6, 5),", 9),
    ("the AND in chi replaced by an XOR", r"^NONLINEAR = 1$",
     "NONLINEAR = 0", 9),
    ("the last round's constant wrong", r"0x02, 0x80,$",
     "0x02, 0x81,", 26),
]


def _run_control(pattern, replacement, cycles=200):
    """Behaviour run against recovered_reference.py with one line rewritten."""
    import types

    with open(os.path.join(HERE, "recovered_reference.py")) as handle:
        source = handle.read()
    broken = re.sub(pattern, replacement, source, count=1, flags=re.M)
    if broken == source:
        raise SystemExit(
            "could not apply {!r} to recovered_reference.py".format(pattern)
        )
    module = types.ModuleType("keccak_negative_control")
    exec(compile(broken, "<negative-control>", "exec"), module.__dict__)
    netlist = vo_netlist.parse_file(EXPORT)
    return agilex_behavior.run_reference_check(netlist, module, cycles=cycles)


def check_identification(guide):
    """Sections 9-10: what hal_crypto makes of each export, re-derived here."""
    canonical = findings("identify.findings.json")
    family = finding_by_id(canonical, "hal_crypto/identify/family")
    style = finding_by_id(canonical, "hal_crypto/identify/classical-vs-pqc")
    check(
        "the committed verdict on keccak_toy is none-detected / undetermined",
        family is not None
        and family["data"]["family"] == "none-detected"
        and style is not None
        and style["data"]["style"] == "undetermined",
        "{} / {}".format(
            family and family["data"]["family"], style and style["data"]["style"]
        ),
    )
    check(
        "... and the confidence is medium, because cones were refused",
        family is not None and family["data"]["confidence_tier"] == "medium",
        family and family["data"]["confidence_tier"],
    )

    retimed = findings("identify_retimed.findings.json")
    family = finding_by_id(retimed, "hal_crypto/identify/family")
    style = finding_by_id(retimed, "hal_crypto/identify/classical-vs-pqc")
    check(
        "the committed verdict on keccak_retimed is sponge / undetermined",
        family is not None
        and family["data"]["family"] == "sponge"
        and style is not None
        and style["data"]["style"] == "undetermined",
        "{} / {}".format(
            family and family["data"]["family"], style and style["data"]["style"]
        ),
    )
    check(
        "the sponge verdict names the ambiguity rather than picking a side",
        style is not None
        and "SHA-3" in style["summary"]
        and "ML-KEM" in style["summary"]
        and style["status"] == "unknown",
        style and style["status"],
    )

    result = analysis.step_identify(None, None)
    check(
        "re-running the classifier still says none-detected on keccak_toy",
        result["canonical"]["family"] == "none-detected"
        and result["canonical"]["style"] == "undetermined",
        "{} / {}".format(result["canonical"]["family"], result["canonical"]["style"]),
    )
    check(
        "... and sponge with 40 keccak_chi_5 matches on keccak_retimed",
        result["retimed"]["family"] == "sponge"
        and result["retimed"]["sboxes_matched"] == 40
        and result["retimed"]["matched_names"] == ["keccak_chi_5"]
        and result["retimed"]["confidence"] == "high",
        "{} boxes".format(result["retimed"]["sboxes_matched"]),
    )
    check(
        "the retimed export is the same permutation at {} instances".format(
            RETIMED_FLIP_FLOPS + RETIMED_LCELLS
        ),
        result["retimed"]["instances"] == RETIMED_FLIP_FLOPS + RETIMED_LCELLS,
        str(result["retimed"]["instances"]),
    )
    check(
        "the guide states the sponge ambiguity in its own words",
        "ML-KEM" in guide and "SPHINCS+" in guide and "undetermined" in guide,
    )


def check_models(guide):
    """Both Python models reproduce the published vectors on their own."""
    for name in ("reference.py", "recovered_reference.py"):
        module = _load_module(os.path.join(HERE, name))
        for label, input_hex, expected in analysis.TEST_VECTORS:
            got = analysis.state_hex(
                module.keccak_f200(analysis.state_word(input_hex))
            )
            check(
                "{} reproduces {}".format(name, label),
                got == expected,
                got,
            )
        got, cycles = _run_model(module, analysis.TEST_VECTORS[0][1])
        check(
            "{} reaches done in {} cycles".format(name, CYCLES_TO_DONE),
            cycles == CYCLES_TO_DONE and got == analysis.TEST_VECTORS[0][2],
            "{} after {} cycles".format(got, cycles),
        )


def _run_model(module, input_hex):
    state = module.initial_state()
    values = {"rst_n": 1, "start": 1, "din": analysis.state_word(input_hex)}
    state = module.next_state(state, values)
    idle = {"rst_n": 1, "start": 0, "din": 0}
    cycles = 1
    while not module.outputs(state, idle)["done"]:
        state = module.next_state(state, idle)
        cycles += 1
        if cycles > 200:
            raise SystemExit("model never finished")
    return analysis.state_hex(module.outputs(state, idle)["dout"]), cycles


def _load_module(path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("keccak_model", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        holds={"start": 1, "din": 0},
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
        if frame["outputs"]["done"] == 1
    ]
    check(
        "the window contains the whole permutation and the cycle done rises",
        len(committed["frames"]) == TRACE_CYCLES and rises,
        "{} frames, done high from {}".format(
            len(committed["frames"]), rises[:1]
        ),
    )
    check(
        "and on that cycle dout carries the published all-zero-state result",
        bool(rises)
        and [
            "{:050X}".format(frame["outputs"]["dout"])
            for frame in committed["frames"]
            if frame["cycle"] == rises[0]
        ]
        == [_reversed_hex(HEADLINE[2])],
        str(
            [
                "{:050X}".format(frame["outputs"]["dout"])
                for frame in committed["frames"]
                if frame["cycle"] == rises[0]
            ][:1]
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


def _reversed_hex(state_hex):
    """A 25-byte state string as the big integer the port carries."""
    return "{:050X}".format(analysis.state_word(state_hex))


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
        "the SCC decomposition sees one machine of {} registers".format(STATE_BITS),
        biggest.get("registers", {}).get("s") == STATE_BITS,
        str([entry["registers"] for entry in components]),
    )
    check(
        "the guide says diffusion is why that component cannot be cut",
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
