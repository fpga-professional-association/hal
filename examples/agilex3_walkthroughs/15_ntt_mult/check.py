#!/usr/bin/env python3
"""Headless smoke check for walkthrough 15 (ntt_mult).

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

    python3 examples/agilex3_walkthroughs/15_ntt_mult/check.py [--with-hal]

Exit code 0 = every claim still holds, 1 = at least one does not.  Nothing here
writes into the working tree: the Quartus flow is not re-run, the committed
``.vo`` files are the input, and the three negative controls are built in
memory.
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

EXPORT = os.path.join(HERE, "ntt_mult.vo")
DSP_EXPORT = os.path.join(HERE, "ntt_dsp.vo")
ARTIFACTS = os.path.join(HERE, "artifacts")

FAILURES = []

# --- the numbers guide.html states ------------------------------------------
FLIP_FLOPS = 299
LCELLS = 910
INSTANCES = FLIP_FLOPS + LCELLS
#: HAL materialises a GND and a VCC gate on load; the export has neither.
HAL_GATES = INSTANCES + 2
#: The counterfactual export of section 10: the same RTL with the DSP allowed.
DSP_FLIP_FLOPS = 299
DSP_LCELLS = 835
DSP_MACS = 1

MODULUS = 257
DEGREE = 16
WIDTH = 9
PSI = 15
OMEGA = 225
PSI_ORDER = 32
INVERSE_DEGREE = 241
COEFFICIENT_REGISTERS = 288
CONTROL_REGISTERS = 11
COUNTER_WIDTH = 6
PHASE_WIDTH = 3
PHASE_LENGTHS = [64, 16, 16, 32, 16]
CYCLES_PER_PRODUCT = 144
#: One load cycle, then the 144.
CYCLES_TO_DONE = CYCLES_PER_PRODUCT + 1
TWIDDLE_CONSTANTS = 81
BEHAVIOUR_CYCLES = 600
#: The combinational depth the "The netlist as a graph" section states.
DAG_LEVELS = 43
#: ... and the levels of it the drawn datapath cone covers.
DAG_CONE_LEVELS = 34
#: The clock-step window: one clear, one load, then thirty butterfly steps.
TRACE_CYCLES = 32
TRACE_BUSY_CYCLES = 30
#: The sums the modulus derivation is checked on: every one a 9-bit adder makes.
MODULUS_VECTORS = 1023

#: (label, sed pattern, replacement, cycle the mismatch is reported at)
NEGATIVE_CONTROLS = [
    (
        "one forward twiddle moved by one",
        "    15, 240, 197, 68, 34, 30, 121, 137,",
        "    15, 240, 197, 68, 34, 30, 121, 136,",
        67,
    ),
    (
        "the modulus moved from 257 to 256",
        "MODULUS = 257",
        "MODULUS = 256",
        36,
    ),
    (
        "one post-scale constant moved by one",
        "    241, 256, 4, 193, 129, 249, 32, 2,",
        "    240, 256, 4, 193, 129, 249, 32, 2,",
        132,
    ),
]


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
    with open(os.path.join(HERE, "guide.html"), encoding="utf-8") as handle:
        return handle.read()


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------


def check_coverage(guide):
    """Section 2: one export is covered, and the other deliberately is not."""
    document = findings("inventory.findings.json")
    entry = finding_by_id(document, "hal_agilex/inventory/fully-covered")
    check("ntt_mult is fully covered", entry is not None)
    if entry is not None:
        check(
            "the coverage finding is proven_under_assumptions",
            entry["status"] == "proven_under_assumptions",
            entry["status"],
        )
        histogram = entry["data"]["histogram"]
        check(
            "ntt_mult: {} flip-flops and {} ALMs, nothing else".format(
                FLIP_FLOPS, LCELLS
            ),
            histogram == {"tennm_ff": FLIP_FLOPS, "tennm_lcell_comb": LCELLS},
            str(histogram),
        )
    # Findings record the input's sha256, so this is the reproducibility check
    # the hal_agilex skill's CRLF pitfall is about.
    recorded = document["artifacts"][0].get("sha256")
    raw, normalised = _hashes(EXPORT)
    check(
        "the committed ntt_mult findings hash the committed export",
        recorded in (raw, normalised),
        "byte-for-byte"
        if recorded == raw
        else (
            "matched only after normalising CRLF -- this checkout is not LF"
            if recorded == normalised
            else "{} vs {}".format(recorded, raw)
        ),
    )

    dsp = findings("inventory_dsp.findings.json")
    entry = finding_by_id(dsp, "hal_agilex/inventory/uncovered-primitives")
    check(
        "the DSP counterfactual is reported unsupported, with a tennm_mac",
        entry is not None
        and entry["status"] == "unsupported"
        and "tennm_mac" in entry["data"]["examples"],
        entry and str(entry["data"].get("examples")),
    )
    recorded = dsp["artifacts"][0].get("sha256")
    raw, normalised = _hashes(DSP_EXPORT)
    check(
        "the committed ntt_dsp findings hash the committed export",
        recorded in (raw, normalised),
        "byte-for-byte" if recorded == raw else "CRLF-normalised",
    )
    check(
        "the guide says a 9x9 multiply is what puts a DSP in the netlist",
        "multstyle" in guide and "tennm_mac" in guide,
    )


def check_census(netlist, model, guide):
    """Section 3: the census, the boundary and the depth."""
    stats = analysis.step_stats(netlist, model)
    check(
        "{} instances: {} tennm_ff + {} tennm_lcell_comb".format(
            INSTANCES, FLIP_FLOPS, LCELLS
        ),
        stats["gate_types"] == {"tennm_ff": FLIP_FLOPS, "tennm_lcell_comb": LCELLS},
        str(stats["gate_types"]),
    )
    check(
        "the boundary is clk/rst_n/start/a_in/b_in -> c_out/done/busy",
        sorted(stats["input_ports"]) == ["a_in", "b_in", "clk", "rst_n", "start"]
        and sorted(stats["output_ports"]) == ["busy", "c_out", "done"],
        str(sorted(stats["input_ports"]) + sorted(stats["output_ports"])),
    )
    check(
        "the data ports are 128 / 128 in and 144 out",
        stats["input_ports"]["a_in"] == 128
        and stats["input_ports"]["b_in"] == 128
        and stats["output_ports"]["c_out"] == 144,
        str(stats["input_ports"]),
    )
    check(
        "eight bits of input and nine bits of output per coefficient",
        stats["input_ports"]["a_in"] // DEGREE == 8
        and stats["output_ports"]["c_out"] // DEGREE == WIDTH,
    )
    check(
        "the combinational core is {} levels deep".format(DAG_LEVELS - 1),
        stats["combinational_levels"] == DAG_LEVELS - 1,
        str(stats["combinational_levels"]),
    )
    check(
        "the guide contrasts that depth with walkthrough 11's ARX round",
        "18 levels" in guide or "18</code> levels" in guide,
    )


def check_registers(netlist, model, guide):
    """Section 4: two banks of sixteen nine-bit coefficients."""
    layout = analysis.step_registers(netlist, model)
    check(
        "{} coefficient flip-flops and {} control ones".format(
            COEFFICIENT_REGISTERS, CONTROL_REGISTERS
        ),
        layout["coefficient_registers"] == COEFFICIENT_REGISTERS
        and len(layout["control_registers"]) == CONTROL_REGISTERS,
        "{} / {}".format(
            layout["coefficient_registers"], len(layout["control_registers"])
        ),
    )
    check(
        "two banks of sixteen coefficients, nine bits each",
        layout["bank_count"] == 2
        and layout["coefficients_per_bank"] == [DEGREE]
        and layout["bits_per_coefficient"] == [WIDTH],
        "{} banks, {} coefficients, {} bits".format(
            layout["bank_count"],
            layout["coefficients_per_bank"],
            layout["bits_per_coefficient"],
        ),
    )
    check(
        "the load path and the adder path agree on every shared bit",
        layout["port_bits_per_coefficient"] == 8,
    )


def check_schedule(netlist, model, guide):
    """Section 5: the control orbit."""
    schedule = analysis.step_schedule(netlist, model)
    check(
        "{} cycles from an accepted start to done".format(CYCLES_PER_PRODUCT),
        schedule["cycles_from_load_to_done"] == CYCLES_PER_PRODUCT,
        str(schedule["cycles_from_load_to_done"]),
    )
    check(
        "a {}-bit counter and a {}-bit phase, found without names".format(
            COUNTER_WIDTH, PHASE_WIDTH
        ),
        schedule["counter_width"] == COUNTER_WIDTH
        and schedule["phase_width"] == PHASE_WIDTH,
        "{} / {}".format(schedule["counter_width"], schedule["phase_width"]),
    )
    check(
        "five phases of {}".format(", ".join(str(n) for n in PHASE_LENGTHS)),
        schedule["phase_lengths"] == PHASE_LENGTHS,
        str(schedule["phase_lengths"]),
    )
    check(
        "the counter bits toggle 143, 71, 35, 17, 6 and 2 times",
        list(schedule["counter_toggles"].values()) == [143, 71, 35, 17, 6, 2],
        str(list(schedule["counter_toggles"].values())),
    )


def check_butterfly(netlist, model, guide):
    """Section 6: one butterfly, and the modulus behind its correction."""
    butterfly = analysis.step_butterfly(netlist, model)
    check(
        "exactly one butterfly, {} bits wide".format(WIDTH),
        butterfly["butterfly_count"] == 1 and butterfly["butterfly_width"] == WIDTH,
        "{} pair(s) of width {}".format(
            butterfly["butterfly_count"], butterfly["butterfly_width"]
        ),
    )
    check(
        "its two chains read the same {} operand nets".format(2 * WIDTH),
        len(butterfly["operand_nets"]) == 2 * WIDTH,
        str(len(butterfly["operand_nets"])),
    )
    operations = sorted(entry["operation"] for entry in butterfly["verified_adders"])
    check(
        "the subtracter is recognised at all -- a vendor one, not a fixture one",
        "subtract" in operations,
        str(operations),
    )
    check(
        "the guide says why: a folded inversion and a leading carry seed",
        "carry seed" in guide or "carry-seed" in guide,
    )

    modulus = analysis.step_modulus(netlist, model)
    check(
        "there is no constant-operand carry chain anywhere",
        modulus["constant_operand_chains"] == 0,
        str(modulus["constant_operand_chains"]),
    )
    check(
        "the modulus is {} and it is prime".format(MODULUS),
        modulus["modulus"] == MODULUS and modulus["modulus_is_prime"],
        str(modulus["modulus"]),
    )
    check(
        "... derived from the correction and checked on all {} sums".format(
            MODULUS_VECTORS
        ),
        modulus["checked_every_sum"]
        and modulus["vectors_checked"] == MODULUS_VECTORS,
        str(modulus["vectors_checked"]),
    )
    check(
        "... and it matches nothing in the published-parameter library",
        modulus["published_parameter_match"] == [],
        str(modulus["published_parameter_match"]),
    )


def check_twiddles(netlist, model, guide):
    """Section 7: eighty-one constants off the multiplier's own operand."""
    twiddles = analysis.step_twiddles(netlist, model)
    check(
        "{} twiddle constants recovered".format(TWIDDLE_CONSTANTS),
        twiddles["constants_recovered"] == TWIDDLE_CONSTANTS,
        str(twiddles["constants_recovered"]),
    )
    periods = sorted(
        entry["period"] for entry in twiddles["phases"].values() if entry["period"]
    )
    check(
        "two 32-entry tables, one of 16 and one constant",
        periods == [1, 16, 32, 32],
        str(periods),
    )
    driven = [
        phase for phase, entry in twiddles["phases"].items() if entry["data_driven"]
    ]
    check(
        "exactly one phase's 'twiddle' is data and not a table",
        len(driven) == 1,
        str(driven),
    )

    ring = analysis.step_ring(netlist, model)
    check(
        "the ring is Z_{}[x] / (x^{} + 1)".format(MODULUS, DEGREE),
        ring["ring"] == "Z_{}[x] / (x^{} + 1)".format(MODULUS, DEGREE),
        ring["ring"],
    )
    check(
        "psi = {} has order {}, and omega = psi^2 = {}".format(PSI, PSI_ORDER, OMEGA),
        ring["psi"] == PSI
        and ring["psi_order"] == PSI_ORDER
        and ring["omega"] == OMEGA,
        "{} / {} / {}".format(ring["psi"], ring["psi_order"], ring["omega"]),
    )
    check(
        "the inverse table is omega^-brv3(g), and the post scale is n^-1 = {}".format(
            INVERSE_DEGREE
        ),
        ring["inverse_table_matches_omega_inverse"]
        and ring["post_scale"] == INVERSE_DEGREE
        and ring["post_scale_is_inverse_degree"],
        str(ring["post_scale"]),
    )


def check_addresses(netlist, model, guide):
    """Section 8: the butterfly schedule and the bit-reversal passes."""
    addresses = analysis.step_addresses(netlist, model)
    check(
        "96 butterfly steps: 64 forward and 32 inverse",
        addresses["butterfly_steps"] == 96,
        str(addresses["butterfly_steps"]),
    )
    check(
        "the pair distance is 8, 4, 2, 1 in every block of eight",
        addresses["pair_distances_per_block_of_eight"]
        == [[8], [4], [2], [1]] * 3,
        str(addresses["pair_distances_per_block_of_eight"]),
    )
    check(
        "every butterfly writes back the pair it read",
        addresses["reads_and_writes_are_the_same_pair"],
    )
    reversals = addresses["cross_bank_permutations"]
    check(
        "the two cross-bank passes are the bit-reversal permutation",
        len(reversals) == 2
        and all(entry["is_bit_reversal"] for entry in reversals.values()),
        str({key: entry["is_bit_reversal"] for key, entry in reversals.items()}),
    )


def check_behaviour(netlist, model, guide):
    """Section 9: drive the netlist, and the bounded model comparisons."""
    run = analysis.step_vectors(netlist, model)
    check(
        "the netlist agrees with the schoolbook negacyclic product",
        run["all_agree"],
        str([(entry["case"], entry["agrees"]) for entry in run["cases"]]),
    )
    check(
        "x^15 * x comes back as -1, so the quotient is x^16 + 1",
        run["wrap_is_negative"],
    )
    check(
        "each product takes {} cycles from an accepted start".format(CYCLES_TO_DONE),
        all(
            entry["cycles_from_accepted_start_to_done"] == CYCLES_TO_DONE
            for entry in run["cases"]
        ),
        str([entry["cycles_from_accepted_start_to_done"] for entry in run["cases"]]),
    )

    for name, label in (
        ("behavior_design.findings.json", "reference.py"),
        ("behavior_recovered.findings.json", "recovered_reference.py"),
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

    for (what, _pattern, _replacement, cycle), name in zip(
        NEGATIVE_CONTROLS,
        (
            "behavior_negative_control_twiddle.findings.json",
            "behavior_negative_control_modulus.findings.json",
            "behavior_negative_control_post.findings.json",
        ),
    ):
        document = findings(name)
        entry = finding_by_id(document, "hal_agilex/behavior/reference-mismatch")
        check(
            "the committed '{}' control is caught at cycle {}".format(what, cycle),
            entry is not None
            and entry["status"] == "bounded_counterexample"
            and entry["counterexample"]["cycle_bound"] == cycle,
            name,
        )

    # ... and re-run them, so that "the control is caught" is a live fact and
    # not a document somebody could have edited.  600 cycles, not 200: the third
    # control -- one wrong constant in the *last* of five phases -- passes a
    # 200-cycle run clean, because `behavior` asserts its asynchronous clear at
    # cycles//3 and a third of 200 is less than the 144 cycles a product takes.
    # A shorter run is not a prefix of a longer one; walkthrough 14 hit the same
    # wall from the other side.
    for what, pattern, replacement, expected in NEGATIVE_CONTROLS:
        result = _run_control(pattern, replacement, BEHAVIOUR_CYCLES)
        cycle = result.get("mismatch", {}).get("cycle")
        check(
            "a reference with {} is still caught".format(what),
            "mismatch" in result,
            "diverged at cycle {}".format(cycle),
        )
        check(
            "... at the cycle the guide says it does",
            cycle == expected,
            "{} vs {}".format(cycle, expected),
        )
    short = _run_control(
        NEGATIVE_CONTROLS[2][1], NEGATIVE_CONTROLS[2][2], 200
    )
    check(
        "... and the last one is missed entirely by a 200-cycle bound",
        "mismatch" not in short,
        str(short.get("mismatch", {}).get("cycle")),
    )


def _run_control(pattern, replacement, cycles):
    """Patch recovered_reference.py in memory and re-run the bounded check."""
    path = os.path.join(HERE, "recovered_reference.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    if pattern not in source:
        raise SystemExit("negative control pattern not found: {!r}".format(pattern))
    patched = source.replace(pattern, replacement, 1)
    module = types.ModuleType("ntt_negative_control")
    exec(compile(patched, "<negative-control>", "exec"), module.__dict__)  # noqa: S102
    return agilex_behavior.run_reference_check(
        vo_netlist.parse_file(EXPORT), module, cycles=cycles
    )


def check_models(guide):
    """Both Python models compute the schoolbook product on their own."""
    cases = [
        ([1] + [0] * 15, [1] + [0] * 15),
        ([0] * 15 + [1], [0, 1] + [0] * 14),
        (
            [(37 * index + 11) % 256 for index in range(16)],
            [(91 * index + 5) % 256 for index in range(16)],
        ),
    ]
    spec_side = _load_module(os.path.join(HERE, "reference.py"), "ntt_spec")
    netlist_side = _load_module(
        os.path.join(HERE, "recovered_reference.py"), "ntt_recovered"
    )
    for first, second in cases:
        want = spec_side.schoolbook(first, second)
        check(
            "reference.py reproduces the schoolbook product",
            spec_side.negacyclic_multiply(first, second) == want,
        )
        check(
            "recovered_reference.py reproduces the same product",
            netlist_side.negacyclic_multiply(first, second) == want,
        )
    check(
        "the two models are not the same arithmetic",
        "% MODULUS" not in open(
            os.path.join(HERE, "recovered_reference.py"), encoding="utf-8"
        ).read(),
    )


def check_identification(guide):
    """Section 10: what hal_crypto makes of each export, re-derived here."""
    document = findings("identify.findings.json")
    family = finding_by_id(document, "hal_crypto/identify/family")
    style = finding_by_id(document, "hal_crypto/identify/classical-vs-pqc")
    check(
        "the committed verdict on ntt_mult is lattice-ntt / pqc-style",
        family is not None
        and family["data"]["family"] == "lattice-ntt"
        and style is not None
        and style["data"]["style"] == "pqc-style",
        "{} / {}".format(
            family and family["data"]["family"], style and style["data"]["style"]
        ),
    )
    check(
        "... at medium confidence, because the modulus is in no library",
        family is not None and family["data"]["confidence_tier"] == "medium",
        family and family["data"]["confidence_tier"],
    )
    check(
        "the style finding refuses to read the toy parameters as a scheme",
        style is not None
        and "does NOT identify a scheme" in style["summary"]
        and "no published-parameter library" in style["summary"],
        style and style["summary"][:80],
    )
    entry = finding_by_id(document, "hal_crypto/ntt/butterfly")
    check(
        "the butterfly finding names the modulus it recovered",
        entry is not None
        and "modulus {}".format(MODULUS) in entry["title"]
        and entry["data"]["recovered_moduli"] == [MODULUS],
        entry and entry["title"],
    )

    dsp = findings("identify_dsp.findings.json")
    family = finding_by_id(dsp, "hal_crypto/identify/family")
    check(
        "the DSP export still comes back lattice-ntt -- the butterfly is outside",
        family is not None and family["data"]["family"] == "lattice-ntt",
        family and family["data"]["family"],
    )
    check(
        "... but with a spurious arx reading the covered export does not have",
        family is not None and "arx" in family["data"]["families_present"],
        family and str(family["data"]["families_present"]),
    )

    result = analysis.step_identify(None, None)
    check(
        "re-running the classifier still says lattice-ntt / pqc-style",
        result["exports"]["ntt_mult"]["family"] == "lattice-ntt"
        and result["exports"]["ntt_mult"]["style"] == "pqc-style"
        and result["exports"]["ntt_mult"]["recovered_moduli"] == [MODULUS],
        str(result["exports"]["ntt_mult"]["family"]),
    )
    check(
        "the guide states the honesty framing in its own words",
        "not a post-quantum implementation" in guide,
    )


def check_dag(guide):
    """The committed DAG image and the guide agree on the level count."""
    dot_path = os.path.join(HERE, "images", "dag.dot")
    check("images/dag.dot exists", os.path.isfile(dot_path))
    if not os.path.isfile(dot_path):
        return
    with open(dot_path, encoding="utf-8") as handle:
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

    # The whole-netlist graph is computed but not drawn: `dot` does not finish
    # on it.  What the guide embeds is the datapath cone, and that one is a
    # picture, so the page has to point at it and it has to exist.
    cone_path = os.path.join(HERE, "images", "dag_datapath.dot")
    check("images/dag_datapath.dot exists", os.path.isfile(cone_path))
    check(
        "images/dag_datapath.svg exists and is what the guide embeds",
        os.path.isfile(os.path.join(HERE, "images", "dag_datapath.svg"))
        and 'src="images/dag_datapath.svg"' in guide,
    )
    if os.path.isfile(cone_path):
        with open(cone_path, encoding="utf-8") as handle:
            header = handle.read(4096)
        match = re.search(r"^// (\d+) level\(s\)", header, re.M)
        cone_levels = int(match.group(1)) if match else None
        check(
            "the datapath cone is {} of those levels".format(DAG_CONE_LEVELS),
            cone_levels == DAG_CONE_LEVELS,
            "levels={}".format(cone_levels),
        )
    check(
        "the guide says why the whole graph is not drawn",
        "without finishing" in guide,
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
        holds={"start": 1},
    )
    differences = agilex_trace.differences(committed, fresh)
    check(
        "the committed trace reproduces from the committed .vo",
        not differences,
        ", ".join(differences),
    )
    busy = [
        frame["cycle"] for frame in committed["frames"] if frame["outputs"]["busy"]
    ]
    check(
        "the window is {} cycles: a clear, a load, then {} busy ones".format(
            TRACE_CYCLES, TRACE_BUSY_CYCLES
        ),
        len(committed["frames"]) == TRACE_CYCLES
        and len(busy) == TRACE_BUSY_CYCLES
        and busy == list(range(TRACE_CYCLES - TRACE_BUSY_CYCLES, TRACE_CYCLES)),
        "{} frames, busy on {}".format(len(committed["frames"]), len(busy)),
    )

    with open(page_path, encoding="utf-8") as handle:
        page = handle.read()
    check(
        "the page embeds every recorded cycle",
        page.count('"cycle":') == len(committed["frames"]),
        str(page.count('"cycle":')),
    )


def check_hal(guide):
    """The hal_py tier: a second reader and a plugin's own graph algorithm."""
    result = analysis.step_hal(None, None)
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
    biggest = result.get("largest_component") or {}
    registers = biggest.get("registers", {})
    check(
        "the SCC decomposition puts both coefficient banks in one component",
        registers.get("pa") == 144 and registers.get("pb") == 144,
        str(registers),
    )
    check(
        "the guide says why that component cannot be cut",
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
    check_census(netlist, model, guide)
    check_registers(netlist, model, guide)
    check_schedule(netlist, model, guide)
    check_butterfly(netlist, model, guide)
    check_twiddles(netlist, model, guide)
    check_addresses(netlist, model, guide)
    check_behaviour(netlist, model, guide)
    check_models(guide)
    check_identification(guide)
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
