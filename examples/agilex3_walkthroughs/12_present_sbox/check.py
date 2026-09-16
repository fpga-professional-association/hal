#!/usr/bin/env python3
"""Headless assertions for the 12_present_sbox walkthrough.

Every number guide.html quotes is re-derived here from the committed netlists
and asserted, so the walkthrough fails loudly if a tool, the gate library or a
netlist ever drifts away from the prose.  Nothing is written and no image is
produced: this is the smoke test, not the walk.

It needs **no HAL build, no Quartus and no Graphviz** -- only `tools/hal_agilex`
and `tools/hal_crypto`, both plain standard library.  From the repository root:

    python examples/agilex3_walkthroughs/12_present_sbox/check.py

Exit code 0 = every check passed, 1 = at least one did not.
"""

import importlib.util
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, HERE)

import analysis  # noqa: E402

from hal_agilex import behavior, inventory, vo_netlist  # noqa: E402
from hal_crypto import classify, netlist_model  # noqa: E402

VO = os.path.join(HERE, "present_sbox.vo")
NOKEEP = os.path.join(HERE, "variants", "present_nokeep.vo")
TEXTBOOK = os.path.join(HERE, "variants", "present_textbook.vo")
REFERENCE = os.path.join(HERE, "recovered_reference.py")

# --- what the guide claims -------------------------------------------------
EXPECTED_LCELL = 226
EXPECTED_FF = 151
EXPECTED_GATES = 377
EXPECTED_BANKS = [148, 3]
EXPECTED_GROUPS = 17
EXPECTED_EXACT = 16
EXPECTED_BIT_PERMUTATION = 1
EXPECTED_TABLE = "C56B90AD3EF84712"
EXPECTED_DEGREE = 3
EXPECTED_DIFFERENTIAL_UNIFORMITY = 4
EXPECTED_ROUNDS = 31
EXPECTED_KEY_WIDTH = 80
EXPECTED_ROTATION = 61
EXPECTED_ROTATION_LINKS = 71
# The cone-support tier counts the five counter-injected bits as rotation links
# too: `kreg[i] <= kreg[i+19] ^ round[j]` still reads exactly one key bit.
EXPECTED_CONE_ROTATION_LINKS = 76
EXPECTED_SUBSTITUTED = [76, 77, 78, 79]
EXPECTED_INJECTED = [15, 16, 17, 18, 19]
EXPECTED_FAMILY = "spn"
EXPECTED_STYLE = "classical-style"
EXPECTED_DAG_LEVELS = 3          # "The netlist as a graph"
TRACE_CYCLES = 34                # the clock-step window: clear, load, 31 rounds, stop
TRACE_CIPHERTEXT = 0x5579C1387B228445

# The two counterfactual exports in variants/.
EXPECTED_NOKEEP_GROUPS = 13      # extracted, none of them recognisable
EXPECTED_NOKEEP_GATES = 373
EXPECTED_TEXTBOOK_FAMILY = "none-detected"
EXPECTED_TEXTBOOK_GATES = 441

# The behaviour bound and the negative control that makes it mean something.
BEHAVIOUR_CYCLES = 600
BEHAVIOUR_ENCRYPTIONS = 16       # complete 31-round encryptions in that window
CONTROL_CYCLES = 200

_failures = []
_checks = 0


def check(label, condition, detail=""):
    global _checks
    _checks += 1
    if condition:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s%s" % (label, (" -- " + detail) if detail else ""))
        _failures.append(label)
    return bool(condition)


def guide_text():
    with open(os.path.join(HERE, "guide.html")) as handle:
        return handle.read()


def check_dag():
    """The committed levelled DAG still says what guide.html says it says.

    ``hal_viz dag`` records its counts in the .dot comment header, so this is
    pure file inspection: no HAL, no Graphviz.
    """
    print("levelled DAG")
    dot_path = os.path.join(HERE, "images", "dag.dot")
    if not check("images/dag.dot exists", os.path.isfile(dot_path), dot_path):
        return
    with open(dot_path) as handle:
        header = handle.read(4096)
    levels = None
    for line in header.splitlines():
        if line.startswith("// ") and "level(s)" in line:
            levels = int(line.split()[1])
            break
    check("the levelled DAG is %d levels deep" % EXPECTED_DAG_LEVELS,
          levels == EXPECTED_DAG_LEVELS, "levels=%s" % levels)
    check("guide.html quotes the same level count",
          "<code>%d</code> topological levels" % EXPECTED_DAG_LEVELS in guide_text())


def check_dag_interactive():
    """The committed clock-step page is built from a trace that reproduces.

    The page's whole claim is that its values are *this* run of the export, so
    this re-runs the exporter with the options guide.html prints and requires
    the same document back.  Pure Python: no HAL, no Graphviz, no browser, and
    nothing is written into the tree.
    """
    print("clock-step page")
    from hal_agilex import trace as agilex_trace

    trace_path = os.path.join(HERE, "artifacts", "dag_trace.json")
    page_path = os.path.join(HERE, "images", "dag_interactive.html")
    if not check("artifacts/dag_trace.json exists", os.path.isfile(trace_path)):
        return
    if not check("images/dag_interactive.html exists", os.path.isfile(page_path)):
        return

    with open(trace_path, encoding="utf-8") as handle:
        committed = json.load(handle)
    fresh = agilex_trace.run_trace(
        vo_netlist.parse_file(VO),
        agilex_trace.load_reference(REFERENCE),
        cycles=TRACE_CYCLES,
        holds={"start": 1, "plaintext": 0, "key_in": 0},
    )
    differences = agilex_trace.differences(committed, fresh)
    check("the committed trace reproduces from the committed .vo",
          not differences, ", ".join(differences)[:200])

    last = committed["frames"][-1]
    check("the traced window ends on the first published ciphertext, done high",
          last["outputs"]["ciphertext"] == TRACE_CIPHERTEXT
          and last["outputs"]["done"] == 1,
          "%016X" % last["outputs"]["ciphertext"])
    rounds = [frame for frame in committed["frames"] if frame["outputs"]["busy"]]
    check("the window contains all %d rounds" % EXPECTED_ROUNDS,
          len(rounds) == EXPECTED_ROUNDS, str(len(rounds)))

    with open(page_path, encoding="utf-8") as handle:
        page = handle.read()
    check("the page embeds every recorded cycle",
          page.count('"cycle":') == len(committed["frames"]),
          "%d of %d" % (page.count('"cycle":'), len(committed["frames"])))
    check("the page is self-contained: no external request",
          "http://" not in page.replace("http://www.w3.org", "")
          and "https://" not in page)
    check("guide.html links the interactive page",
          "images/dag_interactive.html" in guide_text())


def check_coverage():
    print("primitive coverage")
    document = inventory.build_document_for_file(VO)
    statuses = {finding["status"] for finding in document["findings"]}
    check("inventory reports no unsupported primitive or configuration",
          "unsupported" not in statuses and "error" not in statuses,
          ", ".join(sorted(statuses)))
    check("the export is %d instances" % EXPECTED_GATES,
          document["artifacts"][0]["gate_count"] == EXPECTED_GATES,
          str(document["artifacts"][0]["gate_count"]))


def check_recovery():
    print("structural recovery (analysis.py)")
    result = analysis.analyse(VO)

    types = result["census"]["gate_types"]
    check("%d tennm_lcell_comb" % EXPECTED_LCELL,
          types.get("tennm_lcell_comb") == EXPECTED_LCELL,
          str(types.get("tennm_lcell_comb")))
    check("%d tennm_ff" % EXPECTED_FF,
          types.get("tennm_ff") == EXPECTED_FF, str(types.get("tennm_ff")))

    sizes = [bank["size"] for bank in result["register_banks"]]
    check("two flip-flop control-pin banks, %s" % EXPECTED_BANKS,
          sizes == EXPECTED_BANKS, str(sizes))

    groups = result["substitutions"]
    check("%d four-bit bijective non-affine cone groups" % EXPECTED_GROUPS,
          len(groups) == EXPECTED_GROUPS, str(len(groups)))
    tiers = {}
    for entry in groups:
        tier = entry["matches"][0]["tier"] if entry["matches"] else "none"
        tiers[tier] = tiers.get(tier, 0) + 1
    check("%d match the library exactly" % EXPECTED_EXACT,
          tiers.get("exact") == EXPECTED_EXACT, str(tiers))
    check("%d matches only up to a bit permutation" % EXPECTED_BIT_PERMUTATION,
          tiers.get("bit_permutation") == EXPECTED_BIT_PERMUTATION, str(tiers))
    check("every group matches the same published S-box",
          all(entry["matches"] and entry["matches"][0]["name"] == "present"
              for entry in groups))
    tables = {"".join("%X" % value for value in entry["table"]) for entry in groups}
    check("the exactly-matched table is %s" % EXPECTED_TABLE,
          EXPECTED_TABLE in tables, str(sorted(tables)))
    check("algebraic degree %d, differential uniformity %d"
          % (EXPECTED_DEGREE, EXPECTED_DIFFERENTIAL_UNIFORMITY),
          all(entry["algebraic_degree"] == EXPECTED_DEGREE
              and entry["differential_uniformity"] == EXPECTED_DIFFERENTIAL_UNIFORMITY
              for entry in groups))

    perm = result["permutation"]
    check("all 64 permutation entries recovered", perm["complete"],
          str(perm["permutation"].count(None)) + " missing")
    check("the recovered permutation is the published pLayer",
          perm["matches_published_player"])

    schedule = result["key_schedule"]
    check("the key register is %d bits" % EXPECTED_KEY_WIDTH,
          schedule.get("width") == EXPECTED_KEY_WIDTH, str(schedule.get("width")))
    check("%d of them are a plain rotation, all by %d"
          % (EXPECTED_ROTATION_LINKS, EXPECTED_ROTATION),
          schedule.get("rotation_links") == EXPECTED_ROTATION_LINKS
          and schedule.get("rotate_left_by") == EXPECTED_ROTATION,
          "%s links by %s" % (schedule.get("rotation_links"),
                              schedule.get("rotation_amounts")))
    check("the substituted nibble is bits %s" % EXPECTED_SUBSTITUTED,
          schedule.get("substituted_bits") == EXPECTED_SUBSTITUTED,
          str(schedule.get("substituted_bits")))
    check("the counter is injected into bits %s" % EXPECTED_INJECTED,
          schedule.get("counter_injected_bits") == EXPECTED_INJECTED,
          str(schedule.get("counter_injected_bits")))
    check("every key bit is accounted for",
          not schedule.get("unclassified_bits"),
          str(schedule.get("unclassified_bits")))

    rounds = result["rounds"]
    check("the design is busy for %d cycles" % EXPECTED_ROUNDS,
          rounds["cycles_busy"] == EXPECTED_ROUNDS, str(rounds["cycles_busy"]))
    check("the counter runs 1..%d, monotonically" % EXPECTED_ROUNDS,
          rounds["counter_first"] == 1
          and rounds["counter_last"] == EXPECTED_ROUNDS
          and rounds["counter_monotone"])

    for vector in result["test_vectors"]:
        check("published vector %s / %s -> %s"
              % (vector["plaintext"], vector["key"], vector["expected"]),
              vector["agrees"], vector["netlist"])
    return result


def _identify(path):
    """``hal_crypto identify`` without the findings-document wrapper."""
    netlist = vo_netlist.parse_file(path)
    evidence = classify.run_passes(netlist)
    return evidence, classify.verdict(evidence)


def check_identify():
    print("hal_crypto identify")
    evidence, verdict = _identify(VO)
    check("family is %s" % EXPECTED_FAMILY, verdict["family"] == EXPECTED_FAMILY,
          verdict["family"])
    check("style is %s" % EXPECTED_STYLE, verdict["style"] == EXPECTED_STYLE,
          verdict["style"])
    check("the S-box pass reports %d substitutions" % EXPECTED_GROUPS,
          len(evidence["sbox"]["sboxes"]) == EXPECTED_GROUPS,
          str(len(evidence["sbox"]["sboxes"])))
    check("the permutation pass finds no *pure-wire* layer (there is none)",
          not [entry for entry in evidence["permutations"]
               if entry["kind"] != "identity"],
          str([entry["kind"] for entry in evidence["permutations"]]))
    check("the shift-register pass finds no chain", not evidence["shift"],
          str(len(evidence["shift"])))

    # ... and what the cone-support tier gets instead, which is section 5 and
    # section 6 of the guide, automated.
    maps = evidence["cone_permutations"]
    layer = [entry for entry in maps if entry["source"] == "subs"]
    check("the cone-support tier recovers the pLayer over all 64 bits",
          len(layer) == 1 and layer[0]["bits_observed"] == 64
          and layer[0]["destination"] == "register bank state",
          str([(e["source"], e["bits_observed"]) for e in maps]))
    if layer:
        check("the recovered pLayer equals the published PRESENT pLayer",
              [match["name"] for match in layer[0]["matches"]] == ["present_player"],
              str(layer[0]["matches"]))
        published = [63 if i == 63 else (16 * i) % 63 for i in range(64)]
        inverse = [0] * 64
        for index, target in enumerate(published):
            inverse[target] = index
        check("... bit by bit, in the convention 'subs[i] drives state[P(i)]'",
              layer[0]["permutation"] == inverse)
    rotation = [entry for entry in maps if entry["kind"] == "rotation"]
    check("the cone-support tier recovers the key rotation, left by %d"
          % EXPECTED_ROTATION,
          len(rotation) == 1
          and rotation[0]["destination"] == "register bank kreg"
          and rotation[0]["rotate_left_by"] == EXPECTED_ROTATION
          and rotation[0]["bits_observed"] == EXPECTED_CONE_ROTATION_LINKS,
          str([(e["source"], e.get("rotate_left_by"), e["bits_observed"])
               for e in rotation]))
    check("both maps are reported as the weaker, cone-support tier",
          all(entry["evidence_tier"] == "cone-support"
              and entry["read_from"] == "next-state cone support"
              for entry in maps))


def _gate_count(path):
    return len(vo_netlist.parse_file(path).instances)


def check_variants():
    print("the two counterfactual exports in variants/")
    check("without `keep`: %d instances" % EXPECTED_NOKEEP_GATES,
          _gate_count(NOKEEP) == EXPECTED_NOKEEP_GATES, str(_gate_count(NOKEEP)))
    check("textbook register placement: %d instances" % EXPECTED_TEXTBOOK_GATES,
          _gate_count(TEXTBOOK) == EXPECTED_TEXTBOOK_GATES,
          str(_gate_count(TEXTBOOK)))

    evidence, verdict = _identify(NOKEEP)
    boxes = evidence["sbox"]["sboxes"]
    check("without `keep`: %d substitutions are still extracted"
          % EXPECTED_NOKEEP_GROUPS,
          len(boxes) == EXPECTED_NOKEEP_GROUPS, str(len(boxes)))
    check("without `keep`: not one of them matches the library",
          all(not entry["matches"] for entry in boxes))
    check("without `keep`: the family is still %s" % EXPECTED_FAMILY,
          verdict["family"] == EXPECTED_FAMILY, verdict["family"])

    check("without `keep`: the pLayer is gone with the substitution layer",
          [entry["source"] for entry in evidence["cone_permutations"]] == ["kreg"],
          str([entry["source"] for entry in evidence["cone_permutations"]]))

    evidence, verdict = _identify(TEXTBOOK)
    check("textbook register placement: the family is %s"
          % EXPECTED_TEXTBOOK_FAMILY,
          verdict["family"] == EXPECTED_TEXTBOOK_FAMILY, verdict["family"])
    check("textbook register placement: no datapath substitution survives",
          all(entry["sources"][0].startswith("kreg")
              for entry in evidence["sbox"]["sboxes"]),
          str([entry["sources"] for entry in evidence["sbox"]["sboxes"]]))
    check("textbook register placement: no pLayer either, and the key rotation "
          "survives",
          [(entry["source"], entry.get("rotate_left_by"))
           for entry in evidence["cone_permutations"]]
          == [("kreg", EXPECTED_ROTATION)],
          str([entry["source"] for entry in evidence["cone_permutations"]]))


def _load_reference():
    spec = importlib.util.spec_from_file_location("recovered_reference_check", REFERENCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stimulus_coverage(reference, cycles, seed=20260908):
    """What ``behavior``'s pseudo-random stimulus actually exercises.

    A cycle count is not a justification on its own -- the question is how many
    complete 31-round encryptions fit inside it.  This replays the same
    generator, the same input order and the same two asynchronous clears
    against the reference model and counts the rising edges of ``done``.
    """
    import random

    inputs = [(name, width) for name, width in reference.INPUTS
              if name not in reference.IGNORED_INPUTS]
    generator = random.Random(seed)
    clears = {cycles // 3, (2 * cycles) // 3}
    state = reference.initial_state()
    completed = 0
    busy = 0
    previous = 0
    for cycle in range(cycles):
        values = {name: generator.randrange(1 << width) for name, width in inputs}
        values["rst_n"] = 0 if cycle in clears else 1
        if not values["rst_n"]:
            state = reference.initial_state()
        emitted = reference.outputs(state, values)
        if emitted["done"] and not previous:
            completed += 1
        previous = emitted["done"]
        busy += emitted["busy"]
        state = reference.next_state(state, values)
    return {"completed": completed, "busy_cycles": busy, "clears": sorted(clears)}


def check_behaviour():
    print("netlist versus the reconstruction, and the negative controls")
    netlist = vo_netlist.parse_file(VO)

    reference = _load_reference()
    coverage = _stimulus_coverage(reference, BEHAVIOUR_CYCLES)
    check("%d cycles of stimulus contain %d complete encryptions"
          % (BEHAVIOUR_CYCLES, BEHAVIOUR_ENCRYPTIONS),
          coverage["completed"] == BEHAVIOUR_ENCRYPTIONS, str(coverage))

    result = behavior.run_reference_check(netlist, reference, cycles=BEHAVIOUR_CYCLES)
    check("%d pseudo-random cycles agree with recovered_reference.py"
          % BEHAVIOUR_CYCLES,
          "mismatch" not in result, json.dumps(result.get("mismatch"))[:200])

    control = _load_reference()
    control.SBOX[0xA] = 0xE            # one substitution entry, F -> E
    broken = behavior.run_reference_check(netlist, control, cycles=CONTROL_CYCLES)
    check("negative control (one S-box entry) is caught, and early",
          "mismatch" in broken and broken["mismatch"]["cycle"] < CONTROL_CYCLES,
          str(broken.get("checked")))

    control = _load_reference()
    control.ROUNDS = 30                # one round short
    broken = behavior.run_reference_check(netlist, control, cycles=CONTROL_CYCLES)
    check("negative control (30 rounds instead of 31) is caught",
          "mismatch" in broken, str(broken.get("checked")))


def check_committed_findings():
    print("the committed findings documents say the same thing")
    expected = {
        "identify.findings.json": ("hal_crypto/identify/family", "spn"),
        "behavior.findings.json": ("hal_agilex/behavior/reference-equivalence", None),
    }
    for name, (finding_id, family) in expected.items():
        path = os.path.join(HERE, "artifacts", name)
        if not check("artifacts/%s exists" % name, os.path.isfile(path)):
            continue
        with open(path) as handle:
            document = json.load(handle)
        ids = [finding["id"] for finding in document["findings"]]
        check("%s carries %s" % (name, finding_id), finding_id in ids, str(ids)[:200])
        if family is not None:
            finding = [item for item in document["findings"] if item["id"] == finding_id][0]
            check("%s says family %s" % (name, family),
                  finding["data"]["family"] == family, finding["data"]["family"])

    for name in ("behavior_negative_control_sbox.findings.json",
                 "behavior_negative_control_rounds.findings.json"):
        path = os.path.join(HERE, "artifacts", name)
        if not check("artifacts/%s exists" % name, os.path.isfile(path)):
            continue
        with open(path) as handle:
            document = json.load(handle)
        statuses = {finding["status"] for finding in document["findings"]}
        check("%s is a counterexample" % name,
              "bounded_counterexample" in statuses, str(statuses))


def check_guide():
    print("guide.html quotes the numbers this script re-derived")
    guide = guide_text()
    for needle in (EXPECTED_TABLE,
                   "<code>%d</code> instances" % EXPECTED_GATES,
                   "%d rounds" % EXPECTED_ROUNDS,
                   "%d complete 31-round encryptions" % BEHAVIOUR_ENCRYPTIONS,
                   "rotation by %d" % EXPECTED_ROTATION,
                   EXPECTED_TEXTBOOK_FAMILY,
                   "cycle 6",
                   "cycle 35"):
        check("guide.html contains %r" % needle, needle in guide)


def main():
    check_dag()
    check_dag_interactive()
    check_coverage()
    check_recovery()
    check_identify()
    check_variants()
    check_behaviour()
    check_committed_findings()
    check_guide()
    print()
    if _failures:
        print("%d of %d checks FAILED" % (len(_failures), _checks))
        return 1
    print("all %d checks passed" % _checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
