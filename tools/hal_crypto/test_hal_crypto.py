"""Unit tests for the structural crypto identification passes.

No HAL, no Quartus, no network.  Run from the repository root::

    python -m unittest discover -s tools/hal_crypto -t tools -p "test_*.py"

The tests are organised around the claims, not the plumbing.  Every recognizer
gets a fixture it *must* fire on and at least one it *must not*:

===========================  ==============================================
recognizer                   negative control
===========================  ==============================================
S-box extraction             ``counter8``, ``lfsr16_fibonacci`` (no
                             bijective non-affine cone group at all)
S-box *matching*             ``unknown_sbox_layer`` -- a real 4-bit
                             bijection that is in no library, checked
                             against every entry at every tier
LFSR feedback polynomial     ``nlfsr16`` (has taps, has no polynomial) and
                             ``shift16_plain`` (has a chain, has no
                             feedback)
ARX                          ``counter8`` and ``01_blinky_counter`` -- an
                             adder with no rotation and no XOR layer
permutation layer            ``counter8`` (every wire map is the identity)
NTT butterfly / modulus      ``butterfly4`` has the butterfly and no
                             modulus; ``counter8`` has neither
classifier                   ``01_blinky_counter`` must be ``none-detected``
===========================  ==============================================

Two of the cases are the real walkthrough exports rather than synthesized
shapes, because the acceptance criterion for this tool is stated in terms of
them: ``05_lfsr_prng`` must come back ``lfsr-stream`` with the polynomial its
own ``spec.md`` states, and ``01_blinky_counter`` must come back
``none-detected``.
"""

import json
import os
import random
import unittest

from hal_agilex import vo_netlist

from hal_crypto import (
    arith,
    arx,
    boolfunc,
    classify,
    findings,
    known,
    ntt,
    permutation,
    sbox,
    shiftreg,
)
from hal_crypto.cli import main as cli_main
from hal_crypto.fixtures import synth
from hal_crypto.netlist_model import NetlistModel

from hal_findings import validate

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
WALKTHROUGHS = os.path.join(REPO_ROOT, "examples", "agilex3_walkthroughs")

LFSR_EXPORT = os.path.join(WALKTHROUGHS, "05_lfsr_prng", "lfsr_prng.vo")
COUNTER_EXPORT = os.path.join(WALKTHROUGHS, "01_blinky_counter", "blinky_counter.vo")
CRC_EXPORT = os.path.join(
    WALKTHROUGHS, "10_crc8_checker", "netlist", "crc8_checker.vo"
)

_MODEL_CACHE = {}


def fixture(name):
    """The parsed model of a committed fixture, built once per process."""
    return load(os.path.join(FIXTURES, "{}.vo".format(name)))


def load(path):
    if path not in _MODEL_CACHE:
        _MODEL_CACHE[path] = NetlistModel(vo_netlist.parse_file(path))
    return _MODEL_CACHE[path]


def identify(path):
    netlist = vo_netlist.parse_file(path)
    artifact = findings.artifact_for(netlist, path)
    return classify.build_document(netlist, artifact)


def finding_by_id(document, finding_id):
    for entry in document["findings"]:
        if entry["id"] == finding_id:
            return entry
    raise AssertionError(
        "no finding {!r} in {}".format(
            finding_id, [entry["id"] for entry in document["findings"]]
        )
    )


# ---------------------------------------------------------------------------
# the Boolean layer
# ---------------------------------------------------------------------------


class TruthTableTest(unittest.TestCase):
    def test_anf_of_a_known_function(self):
        # f(a, b, c) = a ^ (b & c) ^ 1
        values = [
            (a ^ (b & c) ^ 1)
            for index in range(8)
            for a, b, c in [((index >> 0) & 1, (index >> 1) & 1, (index >> 2) & 1)]
        ]
        table = boolfunc.TruthTable(("a", "b", "c"), values)
        self.assertEqual([(), ("a",), ("b", "c")], table.anf())
        self.assertEqual(2, table.algebraic_degree())
        self.assertFalse(table.is_affine())
        self.assertEqual("1 ^ a ^ (b & c)", boolfunc.anf_string(table))

    def test_affine_function_reports_its_terms(self):
        values = [((index >> 0) & 1) ^ ((index >> 2) & 1) for index in range(8)]
        table = boolfunc.TruthTable(("a", "b", "c"), values)
        self.assertTrue(table.is_affine())
        self.assertEqual((0, ["a", "c"]), table.linear_terms())
        self.assertEqual(("a", "c"), table.support())
        self.assertEqual(("a", "c"), table.restricted().inputs)

    def test_restricted_drops_only_unused_inputs(self):
        values = [(index >> 1) & 1 for index in range(8)]
        table = boolfunc.TruthTable(("a", "b", "c"), values)
        restricted = table.restricted()
        self.assertEqual(("b",), restricted.inputs)
        self.assertEqual((0, 1), restricted.values)

    def test_polynomial_conventions_are_reciprocal(self):
        exponents = boolfunc.polynomial_exponents([3, 12, 14, 15])
        self.assertEqual([16, 15, 13, 4, 0], exponents)
        self.assertEqual(
            "x^16 + x^15 + x^13 + x^4 + 1", boolfunc.polynomial_from_exponents(exponents)
        )
        self.assertEqual(
            "x^16 + x^12 + x^3 + x + 1",
            boolfunc.polynomial_from_exponents(boolfunc.reciprocal_exponents(exponents)),
        )


class SboxAlgebraTest(unittest.TestCase):
    def test_present_sbox_properties(self):
        box = known.SBOXES["present"]["sbox"]
        self.assertTrue(box.is_bijective())
        self.assertEqual(3, box.algebraic_degree())
        self.assertEqual(4, box.differential_uniformity())

    def test_aes_sbox_is_derived_correctly(self):
        box = known.SBOXES["aes"]["sbox"]
        # three published values, from FIPS 197 figure 7
        self.assertEqual(0x63, box.table[0x00])
        self.assertEqual(0x7C, box.table[0x01])
        self.assertEqual(0xED, box.table[0x53])
        self.assertEqual(0x16, box.table[0xFF])
        self.assertEqual(4, box.differential_uniformity())
        self.assertEqual(7, box.algebraic_degree())

    def test_every_library_sbox_is_a_non_affine_bijection(self):
        for name, entry in known.sbox_entries():
            box = entry["sbox"]
            self.assertTrue(box.is_bijective(), "{} is not a bijection".format(name))
            self.assertFalse(box.is_affine(), "{} is affine".format(name))
            self.assertTrue(entry["reference"], "{} has no reference".format(name))

    def test_library_permutations_are_permutations(self):
        for name, entry in known.permutation_entries():
            values = list(entry["permutation"])
            self.assertEqual(
                sorted(values), list(range(entry["width"])), "{} is not a permutation".format(name)
            )

    def test_exact_match_beats_the_other_tiers(self):
        box = known.SBOXES["present"]["sbox"]
        result = boolfunc.match_sbox(box, box, name="present")
        self.assertEqual("exact", result.tier)

    def test_xor_constant_tier_is_reported_as_such(self):
        box = known.SBOXES["present"]["sbox"]
        shifted = box.xor_offsets(0b0101, 0b1100)
        result = boolfunc.match_sbox(shifted, box, name="present")
        self.assertEqual("xor_constant", result.tier)
        self.assertEqual(0b0101, result.detail["input_constant"])
        self.assertEqual(0b1100, result.detail["output_constant"])

    def test_bit_permutation_tier_is_never_reported_as_exact(self):
        box = known.SBOXES["gift"]["sbox"]
        permuted = box.permute_inputs((1, 2, 3, 0)).permute_outputs((2, 3, 0, 1))
        self.assertNotEqual(box.table, permuted.table)
        result = boolfunc.match_sbox(permuted, box, name="gift")
        self.assertIsNotNone(result)
        self.assertEqual("bit_permutation", result.tier)
        # and the reported permutations really do reconstruct the candidate
        rebuilt = box.permute_inputs(
            tuple(result.detail["input_permutation"])
        ).permute_outputs(tuple(result.detail["output_permutation"]))
        self.assertEqual(permuted.table, rebuilt.table)

    def test_the_control_bijection_matches_nothing_at_any_tier(self):
        control = boolfunc.Sbox(4, synth._control_bijection())
        self.assertTrue(control.is_bijective())
        self.assertFalse(control.is_affine())
        self.assertEqual([], sbox.match_library(control))

    def test_a_random_non_library_bijection_rarely_matches(self):
        """A sanity bound on the library's false-positive rate at 4 bits."""
        generator = random.Random(20260914)
        library_tables = {
            entry["sbox"].table for _, entry in known.sbox_entries(bits=4)
        }
        hits = 0
        for _ in range(64):
            table = list(range(16))
            generator.shuffle(table)
            candidate = boolfunc.Sbox(4, table)
            if candidate.table in library_tables or candidate.is_affine():
                continue
            if sbox.match_library(candidate):
                hits += 1
        self.assertLessEqual(hits, 4, "the 4-bit library matches too freely")

    def test_permutation_search_note_states_when_it_did_not_search(self):
        self.assertIn("searched exhaustively", boolfunc.permutation_search_note(4))
        self.assertIn("NOT searched", boolfunc.permutation_search_note(8))


# ---------------------------------------------------------------------------
# the netlist model
# ---------------------------------------------------------------------------


class NetlistModelTest(unittest.TestCase):
    def test_cone_evaluation_matches_the_vendor_equation(self):
        """The LFSR feedback cell's own `// Equation(s):` comment says XOR of four."""
        model = load(LFSR_EXPORT)
        table = model.cone("feedback~combout").restricted()
        self.assertEqual(
            ("state[12]", "state[14]", "state[15]", "state[3]"), table.inputs
        )
        self.assertEqual((0, ["state[12]", "state[14]", "state[15]", "state[3]"]),
                         table.linear_terms())

    def test_sampled_evaluation_agrees_with_the_truth_table(self):
        model = load(LFSR_EXPORT)
        table = model.cone("feedback~combout").restricted()
        generator = random.Random(7)
        samples = [
            {name: generator.randint(0, 1) for name in table.inputs} for _ in range(50)
        ]
        values = model.evaluate_samples(["feedback~combout"], samples)
        for index, sample in enumerate(samples):
            self.assertEqual(
                table.evaluate(sample), values["feedback~combout"][index]
            )

    def test_peel_sees_through_inverters_and_aliases(self):
        model = fixture("arx_round8")
        # rot[0] is an `assign` alias of y[5]: a rotation by 3 over 8 bits
        self.assertEqual(("y[5]", False), model.peel("rot[0]"))
        # an XOR cell is not a buffer, so the peel stops at it
        self.assertEqual(("xored[0]", False), model.peel("xored[0]"))

    def test_peel_records_the_inversion_it_walked_through(self):
        model = fixture("butterfly4")
        self.assertEqual(("b[0]", True), model.peel("nb[0]"))

    def test_registers_and_inputs_are_the_cone_sources(self):
        model = load(LFSR_EXPORT)
        self.assertTrue(model.is_source("state[0]"))
        self.assertTrue(model.is_source("clk"))
        self.assertFalse(model.is_source("feedback~combout"))


# ---------------------------------------------------------------------------
# S-box pass
# ---------------------------------------------------------------------------


class SboxPassTest(unittest.TestCase):
    def test_present_layer_is_extracted_and_matched(self):
        result = sbox.identify(fixture("present_sbox_layer"))
        self.assertEqual(2, len(result["sboxes"]))
        for entry in result["sboxes"]:
            self.assertEqual(4, entry["bits"])
            self.assertEqual(list(known.SBOXES["present"]["sbox"].table), entry["table"])
            names = [match["name"] for match in entry["matches"]]
            self.assertIn("present", names)
            tiers = {match["name"]: match["tier"] for match in entry["matches"]}
            self.assertEqual("exact", tiers["present"])

    def test_unknown_sbox_is_extracted_but_matches_nothing(self):
        result = sbox.identify(fixture("unknown_sbox_layer"))
        self.assertEqual(2, len(result["sboxes"]))
        for entry in result["sboxes"]:
            self.assertEqual([], entry["matches"])
            self.assertGreaterEqual(entry["algebraic_degree"], 2)

    def test_no_sbox_in_an_adder_or_an_lfsr(self):
        for name in ("counter8", "lfsr16_fibonacci", "butterfly4", "rotate16"):
            result = sbox.identify(fixture(name))
            self.assertEqual([], result["sboxes"], "{} produced an S-box".format(name))

    def test_no_sbox_in_the_walkthrough_counter(self):
        result = sbox.identify(load(COUNTER_EXPORT))
        self.assertEqual([], result["sboxes"])


# ---------------------------------------------------------------------------
# shift register / LFSR pass
# ---------------------------------------------------------------------------


class ShiftRegisterPassTest(unittest.TestCase):
    def test_walkthrough_lfsr_polynomial_matches_its_specification(self):
        """05_lfsr_prng/spec.md: x^16 + x^15 + x^13 + x^4 + 1, taps 15, 14, 12, 3."""
        structures = shiftreg.find_shift_structures(load(LFSR_EXPORT))
        self.assertEqual(1, len(structures))
        entry = structures[0]
        self.assertEqual("lfsr", entry["kind"])
        self.assertEqual("fibonacci", entry["form"])
        self.assertEqual(16, entry["length"])
        self.assertEqual([3, 12, 14, 15], entry["taps"])
        self.assertEqual("x^16 + x^15 + x^13 + x^4 + 1", entry["polynomial"])
        self.assertEqual(0, entry["feedback_constant"])
        self.assertTrue(entry["autonomous"])
        self.assertEqual(65535, entry["period"])
        self.assertTrue(entry["maximal_length"])

    def test_the_inverted_storage_gauge_is_reported(self):
        """Quartus stores half the stages inverted to load the seed 16'hACE1."""
        entry = shiftreg.find_shift_structures(load(LFSR_EXPORT))[0]
        self.assertEqual("complemented", entry["state_encoding"])
        self.assertNotEqual([0] * 16, entry["gauge"])

    def test_synthesized_fibonacci_lfsr(self):
        entry = shiftreg.find_shift_structures(fixture("lfsr16_fibonacci"))[0]
        self.assertEqual("fibonacci", entry["form"])
        self.assertEqual([3, 12, 14, 15], entry["taps"])
        self.assertEqual("x^16 + x^15 + x^13 + x^4 + 1", entry["polynomial"])

    def test_galois_form_is_distinguished_from_fibonacci(self):
        entry = shiftreg.find_shift_structures(fixture("lfsr16_galois"))[0]
        self.assertEqual("galois", entry["form"])
        self.assertEqual([1, 3, 12], entry["injection_stages"])
        # the same characteristic polynomial as the Fibonacci fixture
        self.assertEqual("x^16 + x^15 + x^13 + x^4 + 1", entry["polynomial"])

    def test_nonlinear_feedback_is_an_nlfsr_with_no_polynomial(self):
        entry = shiftreg.find_shift_structures(fixture("nlfsr16"))[0]
        self.assertEqual("nlfsr", entry["kind"])
        self.assertNotIn("polynomial", entry)
        self.assertEqual(2, entry["feedback_degree"])
        self.assertIn("&", entry["feedback_anf"])

    def test_a_plain_shift_register_is_not_a_feedback_register(self):
        entry = shiftreg.find_shift_structures(fixture("shift16_plain"))[0]
        self.assertEqual("shift_register", entry["kind"])
        self.assertEqual(16, entry["length"])
        self.assertIn("does not feed back", entry["reason"])

    def test_a_counter_is_not_a_shift_register(self):
        self.assertEqual([], shiftreg.find_shift_structures(fixture("counter8")))
        self.assertEqual([], shiftreg.find_shift_structures(load(COUNTER_EXPORT)))

    def test_crc_is_a_data_absorbing_register_not_an_autonomous_one(self):
        """10_crc8_checker: polynomial 0x07 = x^8 + x^2 + x + 1, read from the other end."""
        entry = shiftreg.find_shift_structures(load(CRC_EXPORT))[0]
        self.assertEqual("lfsr", entry["kind"])
        self.assertEqual("galois", entry["form"])
        self.assertFalse(entry["autonomous"])
        self.assertEqual(["din"], entry["data_inputs"])
        self.assertEqual("x^8 + x^2 + x + 1", entry["polynomial_reciprocal"])

    def test_berlekamp_massey_recovers_a_known_recurrence(self):
        # s_n = s_{n-1} ^ s_{n-4}: connection polynomial 1 + x + x^4
        sequence = [1, 0, 0, 0]
        for index in range(4, 40):
            sequence.append(sequence[index - 1] ^ sequence[index - 4])
        self.assertEqual([1, 1, 0, 0, 1], shiftreg.berlekamp_massey(sequence))

    def test_period_of_a_primitive_polynomial_is_maximal(self):
        self.assertEqual(65535, shiftreg.lfsr_period(16, [3, 12, 14, 15]))
        self.assertEqual(15, shiftreg.lfsr_period(4, [2, 3]))


# ---------------------------------------------------------------------------
# arithmetic, ARX, permutation, NTT
# ---------------------------------------------------------------------------


class ArithmeticTest(unittest.TestCase):
    def test_adder_is_verified_not_just_recognized(self):
        entries = arith.adders(fixture("counter8"))
        self.assertEqual(1, len(entries))
        self.assertEqual("add", entries[0]["operation"])
        self.assertEqual(8, entries[0]["width"])

    def test_subtraction_is_told_apart_from_addition(self):
        operations = sorted(entry["operation"] for entry in arith.adders(fixture("butterfly4")))
        self.assertEqual(["add", "subtract"], operations)

    def test_constant_operand_chain_reports_the_constant(self):
        entries = [
            entry
            for entry in arith.adders(fixture("ntt_stage13"))
            if entry["operation"] == "add_constant"
        ]
        self.assertEqual(1, len(entries))
        self.assertEqual(3329, entries[0]["subtrahend"])

    def test_the_walkthrough_counter_chain_is_an_adder(self):
        entries = arith.adders(load(COUNTER_EXPORT))
        self.assertEqual(1, len(entries))
        self.assertEqual("add", entries[0]["operation"])
        self.assertEqual(23, entries[0]["width"])


class ArxPassTest(unittest.TestCase):
    def test_arx_round_is_recognized(self):
        result = arx.identify(fixture("arx_round8"))
        self.assertEqual("arx-candidate", result["verdict"])
        self.assertEqual(1, len(result["adders"]))
        self.assertEqual([3], [entry["rotate_left_by"] for entry in result["rotations"]])
        self.assertGreaterEqual(result["xor_cells"], arx.MIN_XOR_LAYER)

    def test_an_adder_alone_is_not_arx(self):
        result = arx.identify(fixture("counter8"))
        self.assertEqual("not-arx", result["verdict"])
        self.assertEqual(1, len(result["adders"]))
        self.assertTrue(any("rotation" in reason for reason in result["missing"]))
        self.assertTrue(any("XOR" in reason for reason in result["missing"]))

    def test_the_walkthrough_counter_is_not_arx(self):
        result = arx.identify(load(COUNTER_EXPORT))
        self.assertEqual("not-arx", result["verdict"])

    def test_a_rotation_alone_is_not_arx(self):
        result = arx.identify(fixture("rotate16"))
        self.assertEqual("not-arx", result["verdict"])
        self.assertTrue(any("adder" in reason for reason in result["missing"]))


class PermutationPassTest(unittest.TestCase):
    def test_rotation_between_banks_is_found_with_both_spellings(self):
        entries = [
            entry
            for entry in permutation.find_permutations(fixture("rotate16"))
            if entry["kind"] == "rotation"
        ]
        self.assertEqual(1, len(entries))
        self.assertEqual(16, entries[0]["width"])
        self.assertEqual(5, entries[0]["rotate_left_by"])
        self.assertEqual(11, entries[0]["rotation"])

    def test_present_player_is_matched_in_the_published_convention(self):
        published = list(known.PERMUTATIONS["present_player"]["permutation"])
        matches = permutation.match_permutation(published)
        self.assertEqual(["present_player"], [entry["name"] for entry in matches])

    def test_an_identity_map_is_classified_as_identity(self):
        self.assertEqual(
            {"kind": "identity"}, permutation.classify_permutation(list(range(8)))
        )

    def test_a_counter_has_no_non_identity_permutation(self):
        entries = [
            entry
            for entry in permutation.find_permutations(fixture("counter8"))
            if entry["kind"] != "identity"
        ]
        self.assertEqual([], entries)


class NttPassTest(unittest.TestCase):
    def test_butterfly_without_a_modulus_is_reported_as_incomplete(self):
        result = ntt.identify(fixture("butterfly4"))
        self.assertEqual(1, result["butterfly_count"])
        self.assertEqual("no-modular-transform", result["verdict"])
        self.assertTrue(any("modulus" in reason for reason in result["missing"]))

    def test_butterfly_with_a_library_modulus(self):
        result = ntt.identify(fixture("ntt_stage13"))
        self.assertEqual("lattice-style-modular-transform", result["verdict"])
        self.assertEqual([3329], result["named_moduli"])
        self.assertEqual(1, result["butterfly_count"])

    def test_a_counter_has_no_butterfly(self):
        result = ntt.identify(fixture("counter8"))
        self.assertEqual(0, result["butterfly_count"])
        self.assertEqual("no-modular-transform", result["verdict"])


# ---------------------------------------------------------------------------
# the classifier
# ---------------------------------------------------------------------------


class ClassifierTest(unittest.TestCase):
    def test_walkthrough_lfsr_is_an_lfsr_stream(self):
        document = identify(LFSR_EXPORT)
        validate.validate_document(document)
        family = finding_by_id(document, "hal_crypto/identify/family")
        self.assertEqual("lfsr-stream", family["data"]["family"])
        style = finding_by_id(document, "hal_crypto/identify/classical-vs-pqc")
        self.assertEqual("classical-style", style["data"]["style"])
        polynomial = finding_by_id(document, "hal_crypto/lfsr/polynomial")
        self.assertEqual("proven_under_assumptions", polynomial["status"])
        self.assertIn("x^16 + x^15 + x^13 + x^4 + 1", polynomial["title"])

    def test_walkthrough_counter_is_none_detected(self):
        document = identify(COUNTER_EXPORT)
        validate.validate_document(document)
        family = finding_by_id(document, "hal_crypto/identify/family")
        self.assertEqual("none-detected", family["data"]["family"])
        self.assertEqual([], family["data"]["families_present"])
        self.assertEqual("unknown", family["status"])
        style = finding_by_id(document, "hal_crypto/identify/classical-vs-pqc")
        self.assertEqual("undetermined", style["data"]["style"])
        # the negative has to be explained, not shrugged at
        self.assertTrue(family["data"]["evidence"])
        self.assertTrue(any("ARX" in line for line in family["data"]["evidence"]))

    def test_every_fixture_reaches_its_declared_verdict(self):
        for name, entry in sorted(synth.FIXTURES.items()):
            path = os.path.join(FIXTURES, "{}.vo".format(name))
            document = identify(path)
            validate.validate_document(document)
            family = finding_by_id(document, "hal_crypto/identify/family")
            style = finding_by_id(document, "hal_crypto/identify/classical-vs-pqc")
            self.assertEqual(entry["family"], family["data"]["family"], name)
            self.assertEqual(entry["style"], style["data"]["style"], name)

    def test_pqc_wording_never_names_a_scheme(self):
        document = identify(os.path.join(FIXTURES, "ntt_stage13.vo"))
        style = finding_by_id(document, "hal_crypto/identify/classical-vs-pqc")
        self.assertEqual("pqc-style", style["data"]["style"])
        self.assertIn("does NOT identify a scheme", style["summary"])
        for entry in document["findings"]:
            text = json.dumps(entry)
            for word in ("is Kyber", "is ML-KEM", "implements Kyber"):
                self.assertNotIn(word, text)

    def test_classical_wording_does_not_rule_out_pqc(self):
        document = identify(os.path.join(FIXTURES, "arx_round8.vo"))
        style = finding_by_id(document, "hal_crypto/identify/classical-vs-pqc")
        self.assertIn("does NOT rule out post-quantum", style["summary"])

    def test_every_walkthrough_export_produces_a_valid_document(self):
        exports = []
        for root, _, files in os.walk(WALKTHROUGHS):
            for name in sorted(files):
                if name.endswith(".vo"):
                    exports.append(os.path.join(root, name))
        self.assertGreaterEqual(len(exports), 8)
        for path in exports:
            document = identify(path)
            validate.validate_document(document)
            self.assertTrue(document["artifacts"][0]["sha256"])


# ---------------------------------------------------------------------------
# fixtures and CLI
# ---------------------------------------------------------------------------


class FixtureTest(unittest.TestCase):
    def test_committed_fixtures_match_the_generator(self):
        stale = synth.check_all(FIXTURES)
        self.assertEqual(
            [],
            stale,
            "run 'python tools/hal_crypto fixtures --write' to regenerate",
        )

    def test_the_manifest_lists_every_fixture(self):
        with open(os.path.join(FIXTURES, synth.MANIFEST_NAME), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(
            sorted("{}.vo".format(name) for name in synth.FIXTURES),
            sorted(manifest["files"]),
        )
        self.assertIn("not vendor exports", manifest["kind"])

    def test_generated_fixtures_go_through_the_shared_reader(self):
        for name, text in synth.build_all().items():
            parsed = vo_netlist.parse_text(text)
            self.assertTrue(parsed.instances, name)
            for instance in parsed.instances:
                self.assertIn(instance.type, ("tennm_lcell_comb", "tennm_ff"), name)


class HalAdapterTest(unittest.TestCase):
    """The hal_py-facing module, on the paths that do not need hal_py."""

    class _StubNetlist(object):
        def get_design_name(self):
            return "stub"

        def get_gates(self):
            return [1, 2, 3]

    def test_importing_the_adapter_does_not_need_hal_py(self):
        from hal_crypto import hal_adapter

        self.assertEqual("hal_crypto.hal_adapter", hal_adapter.PRODUCER["name"])

    def test_the_sibling_export_is_found_next_to_an_imported_netlist(self):
        from hal_crypto import hal_adapter

        imported = os.path.join(WALKTHROUGHS, "05_lfsr_prng", "netlist.hal.v")
        self.assertEqual(LFSR_EXPORT, hal_adapter.source_export(imported))

    def test_a_missing_export_is_an_unsupported_finding_not_a_guess(self):
        import tempfile

        from hal_crypto import hal_adapter

        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "netlist.hal.v")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("// no .vo next to this one\n")
        document = hal_adapter.build_document(None, self._StubNetlist(), path)
        validate.validate_document(document)
        self.assertEqual(1, len(document["findings"]))
        self.assertEqual("unsupported", document["findings"][0]["status"])
        self.assertEqual("format", document["findings"][0]["unsupported"]["kind"])


class CliTest(unittest.TestCase):
    def _run(self, argv):
        import io
        import contextlib

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
            status = cli_main(argv)
        return status, stream.getvalue()

    def test_identify_writes_a_valid_document(self):
        status, text = self._run(["identify", LFSR_EXPORT])
        self.assertEqual(0, status)
        document = json.loads(text)
        validate.validate_document(document)
        self.assertEqual("hal_crypto.classify", document["producer"]["name"])

    def test_per_pass_subcommands_run(self):
        for command in ("sbox", "lfsr", "arx", "permutation", "ntt"):
            status, text = self._run([command, LFSR_EXPORT])
            self.assertEqual(0, status, command)
            validate.validate_document(json.loads(text))

    def test_fixtures_check_passes_on_the_committed_tree(self):
        status, _ = self._run(["fixtures", "--check"])
        self.assertEqual(0, status)

    def test_the_elaborate_argv_the_plugin_load_audit_uses_still_parses(self):
        """tests/headless_smoke/tool_cli_plugin_load_smoke.py runs exactly this shape.

        That script needs a real build, so it cannot run here -- but the argument
        names it depends on can, and renaming one of them without noticing is
        how a repo-wide audit turns into a mystery failure an hour into CI.
        """
        from hal_crypto.cli import build_parser

        args = build_parser().parse_args(
            [
                "elaborate",
                "netlist.hal.v",
                "--gate-library",
                "AGILEX_TENNM.hgl",
                "-o",
                "crypto_findings.json",
            ]
        )
        self.assertEqual("netlist.hal.v", args.netlist)
        self.assertEqual("AGILEX_TENNM.hgl", args.gate_library)
        self.assertEqual("crypto_findings.json", args.output)
        self.assertEqual([], args.hal_lib)
        self.assertFalse(args.strict)
        self.assertEqual("command_elaborate", args.func.__name__)

    def test_unreadable_input_fails_cleanly(self):
        status, _ = self._run(["identify", os.path.join(HERE, "no_such_file.vo")])
        self.assertEqual(1, status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
