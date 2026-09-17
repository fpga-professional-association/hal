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
permutation, cone-support    ``mux_bank16`` -- a 2-to-1 datapath multiplexer
                             bank, in both the shape where two sources
                             explain it and the shape where no bit reads
                             only one bit of one source
NTT butterfly / modulus      ``butterfly4`` has the butterfly and no
                             modulus; ``counter8`` has neither;
                             ``ntt_fermat17`` has both in the shapes a
                             *vendor* emits them in
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
SPECK_EXPORT = os.path.join(WALKTHROUGHS, "11_speck_toy", "speck_toy.vo")
TRIVIUM_EXPORT = os.path.join(
    WALKTHROUGHS, "13_trivium_stream", "trivium_stream.vo"
)
PRESENT_EXPORT = os.path.join(WALKTHROUGHS, "12_present_sbox", "present_sbox.vo")
PRESENT_NOKEEP_EXPORT = os.path.join(
    WALKTHROUGHS, "12_present_sbox", "variants", "present_nokeep.vo"
)
PRESENT_TEXTBOOK_EXPORT = os.path.join(
    WALKTHROUGHS, "12_present_sbox", "variants", "present_textbook.vo"
)
KECCAK_EXPORT = os.path.join(WALKTHROUGHS, "14_keccak_toy", "keccak_toy.vo")
KECCAK_RETIMED_EXPORT = os.path.join(
    WALKTHROUGHS, "14_keccak_toy", "keccak_retimed.vo"
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

    def test_cofactor_recovers_an_xor_packed_under_a_multiplexer(self):
        # f(s, p, a, b) = s ? p : (a ^ b) -- one ALM, and not an XOR of
        # anything until the select is held.
        values = []
        for index in range(16):
            s, p, a, b = ((index >> shift) & 1 for shift in range(4))
            values.append(p if s else (a ^ b))
        table = boolfunc.TruthTable(("s", "p", "a", "b"), values)
        self.assertFalse(table.is_affine())
        held = table.cofactor(0, 0).restricted()
        self.assertEqual(("a", "b"), held.inputs)
        self.assertEqual((0, ["a", "b"]), held.linear_terms())
        self.assertEqual(("p",), table.cofactor(0, 1).restricted().inputs)
        with self.assertRaises(IndexError):
            table.cofactor(4, 0)

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


class ChiRowTest(unittest.TestCase):
    """A substitution whose output bits read a *subset* of the inputs.

    ``y_i = x_i ^ (~x_{i+1} & x_{i+2})`` reads three of five, so the shape the
    rest of the S-box pass is built on -- "some cone's support names the whole
    box" -- does not hold, and neither does it for Ascon or any other chi-like
    row map.  See ``fixtures/GROUND_TRUTH.md``.
    """

    def test_no_cone_reads_all_five_sources(self):
        """The premise: the single-net search could not have found this."""
        model = fixture("keccak_chi_layer")
        nets, _ = sbox._combinational_nets(model)
        supports = {frozenset(table.inputs) for table in nets.values()}
        rows = [
            frozenset("state[{}]".format(index) for index in group)
            for group in (range(5), range(5, 10))
        ]
        for row in rows:
            self.assertNotIn(row, supports)
        self.assertEqual({3, 5}, {len(item) for item in supports})

    def test_both_rows_are_extracted_and_match_keccak_chi(self):
        result = sbox.identify(fixture("keccak_chi_layer"))
        self.assertEqual(2, len(result["sboxes"]))
        for entry in result["sboxes"]:
            self.assertEqual(5, entry["bits"])
            self.assertEqual(
                list(known.SBOXES["keccak_chi_5"]["sbox"].table), entry["table"]
            )
            self.assertEqual(2, entry["algebraic_degree"])
            tiers = {match["name"]: match["tier"] for match in entry["matches"]}
            self.assertEqual("exact", tiers["keccak_chi_5"])
        self.assertEqual(
            [
                ["state[0]", "state[1]", "state[2]", "state[3]", "state[4]"],
                ["state[5]", "state[6]", "state[7]", "state[8]", "state[9]"],
            ],
            [entry["sources"] for entry in result["sboxes"]],
        )

    def test_the_load_multiplexer_is_not_swallowed_into_the_cluster(self):
        """Growing by *any* neighbour merges the rows and the load path."""
        model = fixture("keccak_chi_layer")
        nets, _ = sbox._combinational_nets(model)
        by_source = {}
        for key, table in nets.items():
            for name in table.inputs:
                by_source.setdefault(name, set()).add(key)
        supports = sbox.cluster_supports(nets, by_source)
        self.assertIn(
            frozenset("state[{}]".format(index) for index in range(5)), supports
        )
        self.assertIn(
            frozenset("state[{}]".format(index) for index in range(5, 10)), supports
        )
        for support in supports:
            self.assertNotIn("load", support)
            self.assertFalse(
                any(name.startswith("seed") for name in support), sorted(support)
            )

    def test_the_present_layer_is_still_found_by_the_single_net_search(self):
        """The new candidates only ever *append*: nothing was rerouted."""
        model = fixture("present_sbox_layer")
        nets, _ = sbox._combinational_nets(model)
        supports = {frozenset(table.inputs) for table in nets.values()}
        for group in (range(4), range(4, 8)):
            self.assertIn(
                frozenset("state[{}]".format(index) for index in group), supports
            )


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


class LoadableChainTest(unittest.TestCase):
    """A parallel load turns every shift link into a multiplexer."""

    def test_a_loadable_lfsr_is_invisible_until_the_select_is_held(self):
        model = fixture("lfsr16_loadable")
        updates = shiftreg.register_updates(model)
        self.assertEqual(
            [], [name for name, u in updates.items() if u.single_register_link()]
        )

    def test_the_mode_candidate_is_the_load_select_not_a_seed_bit(self):
        model = fixture("lfsr16_loadable")
        candidates = shiftreg.mode_candidates(shiftreg.register_updates(model))
        self.assertEqual(["load"], candidates)

    def test_holding_the_select_recovers_the_identical_lfsr(self):
        loadable = shiftreg.find_shift_structures(fixture("lfsr16_loadable"))
        plain = shiftreg.find_shift_structures(fixture("lfsr16_fibonacci"))
        self.assertEqual(1, len(loadable))
        entry = loadable[0]
        self.assertEqual("lfsr", entry["kind"])
        self.assertEqual({"net": "load", "value": 0}, entry["mode"])
        for key in ("taps", "polynomial", "period", "maximal_length", "length"):
            self.assertEqual(plain[0][key], entry[key], key)

    def test_an_unheld_structure_carries_no_mode(self):
        self.assertIsNone(
            shiftreg.find_shift_structures(fixture("lfsr16_fibonacci"))[0]["mode"]
        )

    def test_holding_a_select_never_manufactures_an_open_chain(self):
        """12_present_sbox is loadable register banks and no feedback anywhere.

        Its four banks *do* become shift chains with ``start`` held at 0, and
        reporting them would make every parallel-load register file in every
        design a finding.  Only a feedback register is worth the extra
        assumption, so nothing comes back.
        """
        export = os.path.join(WALKTHROUGHS, "12_present_sbox", "present_sbox.vo")
        self.assertEqual([], shiftreg.find_shift_structures(load(export)))

    def test_the_mode_is_reported_in_the_finding_text(self):
        document = identify(os.path.join(FIXTURES, "lfsr16_loadable.vo"))
        entry = finding_by_id(document, "hal_crypto/lfsr/polynomial")
        self.assertEqual({"net": "load", "value": 0}, entry["data"]["mode"])
        self.assertIn("load held at 0", entry["summary"])


def _coupled_linear_verilog(lengths=(8, 10)):
    """Two chains closed through each other by a *linear* feedback.

    The same shape as the ``coupled_nlfsr`` fixture with the AND term dropped,
    assembled with the fixture generator and handed straight to the shared
    reader, so it goes through exactly the path a committed fixture would.
    """
    first, second = lengths
    builder = synth.Builder("coupled_linear")
    builder.port("input", "clk")
    builder.port("input", "rst_n")
    builder.port("output", "dout")
    builder.wire("a", first)
    builder.wire("b", second)
    builder.wire("head_a")
    builder.wire("head_b")

    def parity(values):
        result = 0
        for value in values:
            result ^= value
        return result

    builder.function("fa", "head_a", ["a[5]", "b[9]", "b[7]"], parity)
    builder.function("fb", "head_b", ["b[6]", "a[7]", "a[5]"], parity)
    for index in range(first):
        data = "head_a" if index == 0 else "a[{}]".format(index - 1)
        builder.register("a_{}".format(index), data, "a[{}]".format(index), clear="rst_n")
    for index in range(second):
        data = "head_b" if index == 0 else "b[{}]".format(index - 1)
        builder.register("b_{}".format(index), data, "b[{}]".format(index), clear="rst_n")
    builder.assign("dout", "a[{}]".format(first - 1))
    return builder.dumps()


class CoupledRegisterTest(unittest.TestCase):
    """Trivium/Grain-style registers close through a sibling, not onto themselves."""

    def test_two_chains_closed_through_each_other_are_both_nlfsrs(self):
        structures = shiftreg.find_shift_structures(fixture("coupled_nlfsr"))
        self.assertEqual(2, len(structures))
        self.assertEqual(["nlfsr", "nlfsr"], [e["kind"] for e in structures])
        self.assertEqual([8, 10], [e["length"] for e in structures])
        self.assertTrue(all(e["coupled"] for e in structures))
        self.assertEqual([1], structures[0]["coupled_chains"])
        self.assertEqual([0], structures[1]["coupled_chains"])

    def test_a_coupled_feedback_names_the_chain_of_every_variable(self):
        first = shiftreg.find_shift_structures(fixture("coupled_nlfsr"))[0]
        self.assertEqual("s0[5] ^ s1[9] ^ (s1[7] & s1[8])", first["feedback_anf"])
        self.assertEqual([5], first["taps"])
        self.assertEqual(
            [("b[7]", 1, 7), ("b[8]", 1, 8), ("b[9]", 1, 9)],
            [
                (tap["register"], tap["chain"], tap["stage"])
                for tap in first["coupled_taps"]
            ],
        )

    def test_a_self_contained_chain_keeps_the_unqualified_spelling(self):
        entry = shiftreg.find_shift_structures(fixture("nlfsr16"))[0]
        self.assertFalse(entry["coupled"])
        self.assertNotIn("s0[", entry["feedback_anf"])
        self.assertIn("s[", entry["feedback_anf"])

    def test_a_coupled_nlfsr_reports_no_polynomial_and_no_period(self):
        for entry in shiftreg.find_shift_structures(fixture("coupled_nlfsr")):
            self.assertNotIn("polynomial", entry)
            self.assertNotIn("period", entry)
            self.assertTrue(entry["coupled_taps"])

    def test_a_coupled_but_linear_pair_is_an_lfsr_with_polynomial_None(self):
        """The one branch no committed fixture reaches: coupled *and* linear.

        ``coupled_nlfsr`` is nonlinear by construction, so "an LFSR that has no
        polynomial because it reads a sibling" would otherwise be unexercised.
        Built here instead of committed as a fourteenth fixture because it
        exists to cover a branch, not to be a shape anyone analyses.
        """
        model = NetlistModel(vo_netlist.parse_text(_coupled_linear_verilog()))
        structures = shiftreg.find_shift_structures(model)
        self.assertEqual(["lfsr", "lfsr"], [e["kind"] for e in structures])
        for entry in structures:
            self.assertTrue(entry["coupled"])
            self.assertIsNone(entry["polynomial"])
            self.assertIsNone(entry["polynomial_reciprocal"])
            self.assertNotIn("period", entry)
            self.assertIn("has none", entry["polynomial_convention"])
        self.assertEqual(
            ["s0[5] ^ s1[7] ^ s1[9]", "s0[5] ^ s0[7] ^ s1[6]"],
            [entry["feedback_anf"] for entry in structures],
        )

    def test_the_findings_document_survives_a_missing_polynomial(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "coupled_linear.vo")
            with open(path, "w", newline="\n") as handle:
                handle.write(_coupled_linear_verilog())
            netlist = vo_netlist.parse_file(path)
            artifact = findings.artifact_for(netlist, path)
        document = classify.build_document(netlist, artifact)
        validate.validate_document(document)
        entry = finding_by_id(document, "hal_crypto/lfsr/polynomial/0")
        self.assertIn("over coupled chain(s) 1", entry["title"])
        self.assertIsNone(entry["data"]["polynomial"])

    def test_the_pair_is_a_keystream_generator_family(self):
        document = identify(os.path.join(FIXTURES, "coupled_nlfsr.vo"))
        family = finding_by_id(document, "hal_crypto/identify/family")
        self.assertEqual("lfsr-stream", family["data"]["family"])
        entry = finding_by_id(document, "hal_crypto/nlfsr/feedback/0")
        self.assertIn("coupled to chain(s) 1", entry["title"])
        self.assertIn("closed through a sibling", entry["summary"])


class TriviumExportTest(unittest.TestCase):
    """13_trivium_stream: both shapes above, in one real Quartus export.

    Every number here is stated in that walkthrough's ``spec.md`` in the
    cipher's own 1-based numbering; the export's ``s[i]`` is spec bit
    ``s(i+1)``, and stage *k* of a segment is the head plus *k*.
    """

    def structures(self):
        return shiftreg.find_shift_structures(load(TRIVIUM_EXPORT))

    def test_the_three_segments_are_93_84_and_111_stages(self):
        by_head = {e["head"]: e for e in self.structures()}
        self.assertEqual(
            {"s[0]": 93, "s[93]": 84, "s[177]": 111},
            {head: entry["length"] for head, entry in by_head.items()},
        )
        self.assertEqual(288, sum(entry["length"] for entry in by_head.values()))

    def test_every_segment_is_a_coupled_nlfsr_of_degree_two(self):
        for entry in self.structures():
            self.assertEqual("nlfsr", entry["kind"])
            self.assertTrue(entry["coupled"])
            self.assertEqual(2, entry["feedback_degree"])
            self.assertEqual(1, len(entry["nonlinear_terms"]))
            self.assertEqual({"net": "start", "value": 0}, entry["mode"])

    def test_the_recovered_feedback_is_the_published_trivium_one(self):
        by_head = {e["head"]: e for e in self.structures()}
        chain_of = {e["head"]: e["chain"] for e in self.structures()}
        a, b, c = chain_of["s[0]"], chain_of["s[93]"], chain_of["s[177]"]
        # t3 = s243 ^ s288 ^ (s286 & s287) ^ s69, driving the head of segment A;
        # s243/s286/s287/s288 are stages 65/108/109/110 of segment C and s69 is
        # stage 68 of A itself.
        self.assertEqual(
            "s{a}[68] ^ s{c}[110] ^ s{c}[65] ^ (s{c}[108] & s{c}[109])".format(
                a=a, c=c
            ),
            by_head["s[0]"]["feedback_anf"],
        )
        # t1 = s66 ^ s93 ^ (s91 & s92) ^ s171 -> head of B
        self.assertEqual(
            "s{a}[65] ^ s{a}[92] ^ s{b}[77] ^ (s{a}[90] & s{a}[91])".format(a=a, b=b),
            by_head["s[93]"]["feedback_anf"],
        )
        # t2 = s162 ^ s177 ^ (s175 & s176) ^ s264 -> head of C
        self.assertEqual(
            "s{c}[86] ^ s{b}[68] ^ s{b}[83] ^ (s{b}[81] & s{b}[82])".format(b=b, c=c),
            by_head["s[177]"]["feedback_anf"],
        )

    def test_each_segment_reads_one_stage_of_itself_and_four_of_a_sibling(self):
        for entry in self.structures():
            self.assertEqual(1, len(entry["taps"]))
            self.assertEqual(4, len(entry["coupled_taps"]))
            self.assertEqual(1, len(entry["coupled_chains"]))

    def test_the_export_is_a_classical_style_keystream_generator(self):
        netlist = vo_netlist.parse_file(TRIVIUM_EXPORT)
        decision = classify.verdict(classify.run_passes(netlist))
        self.assertEqual("lfsr-stream", decision["family"])
        self.assertEqual("classical-style", decision["style"])
        self.assertEqual("high", decision["confidence"])

    def test_without_the_and_terms_it_would_have_been_an_lfsr(self):
        """The one structural fact that separates Trivium from walkthrough 05."""
        lfsr = shiftreg.find_shift_structures(load(LFSR_EXPORT))[0]
        self.assertEqual("lfsr", lfsr["kind"])
        self.assertIsNotNone(lfsr["polynomial"])
        for entry in self.structures():
            self.assertEqual("nlfsr", entry["kind"])
            self.assertNotIn("polynomial", entry)


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

    def test_a_vendor_subtracter_is_recognized(self):
        """Folded inversion plus a leading carry seed -- what Quartus emits.

        ``butterfly4`` spells ``a - b`` with an inverter cell and a carry-in
        tied to vcc, which no synthesiser can do: an ALM's ``cin`` comes only
        from the previous cell's ``cout``.  This is the same subtraction the way
        a real export has it, and before the fix it was not an adder at all.
        """
        operations = sorted(
            entry["operation"] for entry in arith.adders(fixture("ntt_fermat17"))
        )
        self.assertEqual(["add", "subtract"], operations)

    def test_a_carry_seed_is_read_as_the_constant_it_emits(self):
        model = fixture("ntt_fermat17")
        seeds = [
            arith.carry_seed(model, chain[0])
            for chain in arith.chains(model)
        ]
        self.assertIn(1, seeds)
        self.assertIn(None, seeds)

    def test_a_subtract_chain_carries_its_carry_in_of_one(self):
        entries = [
            entry
            for entry in arith.adders(fixture("ntt_fermat17"))
            if entry["operation"] == "subtract"
        ]
        self.assertEqual(1, len(entries))
        self.assertEqual(1, entries[0]["carry_in"])
        self.assertEqual(5, entries[0]["width"])

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

    def test_one_rotation_seen_twice_is_reported_once(self):
        # arx_round8 names the rotated word *and* the register bank behind it
        # reads the same rotation; the count is a count of rotations.
        result = arx.identify(fixture("arx_round8"))
        signatures = {
            (entry["source"], entry["width"], entry["rotation"])
            for entry in result["rotations"]
        }
        self.assertEqual(len(signatures), len(result["rotations"]))

    def test_the_speck_export_is_arx_although_it_names_nothing(self):
        """A real Quartus export hides both the R and the X of ARX.

        Nothing in ``11_speck_toy`` carries a rotated word (the wiring is free,
        so the synthesiser kept no vector for it) and not one cell is a
        standalone XOR (each is packed with the load multiplexer next to it).
        The round is still there and the pass has to find it.
        """
        model = load(SPECK_EXPORT)
        result = arx.identify(model)
        self.assertEqual("arx-candidate", result["verdict"])
        self.assertEqual(2, len(result["adders"]))

        self.assertEqual(0, result["standalone_xor_cells"])
        self.assertGreaterEqual(result["conditional_xor_cells"], arx.MIN_XOR_LAYER)
        self.assertTrue(result["xor_layer_reads_adder_output"])
        self.assertTrue(result["rotation_on_adder_operand"])

        # no rotation came from a named vector: they are all read off a layer
        self.assertEqual(
            set(),
            {
                entry["read_from"]
                for entry in result["rotations"]
                if entry["read_from"] == "named vector"
            },
        )
        by_source = {entry["source"]: entry for entry in result["rotations"]}
        self.assertEqual(
            {"x": 7, "l0": 7, "y": 2, "k": 2},
            {name: entry["rotate_left_by"] if name in ("y", "k") else entry["rotation"]
             for name, entry in by_source.items()},
        )
        self.assertEqual({16}, {entry["width"] for entry in result["rotations"]})
        self.assertEqual(
            ["speck_32"], [entry["name"] for entry in result["rotation_families"]]
        )

    def test_a_packed_xor_never_makes_a_plain_counter_arx(self):
        """The cofactor test must not manufacture an XOR layer out of nothing."""
        for path in (COUNTER_EXPORT, CRC_EXPORT):
            result = arx.identify(load(path))
            self.assertEqual("not-arx", result["verdict"], path)


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


class LayerRotationTest(unittest.TestCase):
    """Rotations read off an ordered cell layer instead of a named vector."""

    def test_a_full_rotation_is_classified(self):
        keys = ["v[{}]".format((index + 3) % 8) for index in range(8)]
        entry = permutation.vector_rotation(keys)
        self.assertEqual("rotation", entry["kind"])
        self.assertEqual("v", entry["source"])
        self.assertEqual(8, entry["width"])
        self.assertEqual(3, entry["rotation"])
        self.assertEqual(5, entry["rotate_left_by"])
        self.assertEqual(8, entry["bits_observed"])

    def test_a_layer_one_bit_short_still_classifies_against_the_full_width(self):
        # a carry chain's top slice is classified separately, so the operand
        # list is one bit short of the bank it reads
        keys = ["v[{}]".format((index + 7) % 16) for index in range(15)]
        entry = permutation.vector_rotation(keys, width=16)
        self.assertEqual(7, entry["rotation"])
        self.assertEqual(16, entry["width"])
        self.assertEqual(15, entry["bits_observed"])
        # with no declared width the highest index still fixes it at 16 here,
        # but a rotation that never reaches the top bit is not classifiable
        # without one
        self.assertEqual(16, permutation.vector_rotation(keys)["width"])
        low = ["v[{}]".format((index + 2) % 16) for index in range(12)]
        self.assertEqual(16, permutation.vector_rotation(low, width=16)["width"])
        self.assertEqual(14, permutation.vector_rotation(low)["width"])

    def test_straight_wiring_and_mixed_sources_are_not_rotations(self):
        self.assertIsNone(
            permutation.vector_rotation(["v[{}]".format(i) for i in range(8)])
        )
        self.assertIsNone(
            permutation.vector_rotation(["a[0]", "b[1]", "a[2]", "a[3]"])
        )
        self.assertIsNone(permutation.vector_rotation(["v[1]", "v[2]", "v[0]"]))

    def test_speck_export_rotations_come_from_the_next_state_fan_in(self):
        entries = permutation.register_bank_rotations(load(SPECK_EXPORT))
        self.assertEqual(
            {"k": 2, "y": 2},
            {entry["source"]: entry["rotate_left_by"] for entry in entries},
        )
        for entry in entries:
            self.assertEqual(16, entry["width"])
            self.assertIn("next-state", entry["destination"])

    def test_a_partial_bank_is_refused_rather_than_offset_corrected(self):
        """Bits 1..n of a vector would read as a rotation of 1; refuse instead."""
        model = load(SPECK_EXPORT)
        full = {entry["source"] for entry in permutation.register_bank_rotations(model)}
        self.assertEqual({"k", "y"}, full)

        class _Partial(object):
            """The same model with bit 0 of the y bank hidden."""

            def __init__(self, inner):
                self._inner = inner
                self.ff_instances = [
                    ff
                    for ff in inner.ff_instances
                    if getattr((ff.connections.get("q") or [None])[0], "key", "")
                    != "y[0]"
                ]

            def __getattr__(self, name):
                return getattr(self._inner, name)

        partial = {
            entry["source"]
            for entry in permutation.register_bank_rotations(_Partial(model))
        }
        self.assertEqual({"k"}, partial)

    def test_a_holding_register_bank_is_not_a_rotation(self):
        # every bit of an accumulator's next state reads its own bit on the
        # hold path, which is the identity and must not be reported
        entries = permutation.register_bank_rotations(fixture("counter8"))
        self.assertEqual([], entries)


def _xor_linked_permutation_verilog(width=16):
    """A permutation whose every link is an XOR with one round-key bit.

    ``nxt[P(i)] = sub[i] ^ rkey[P(i)]`` with ``P(i) = 4i mod 15``.  ``sub`` is
    built out of two-input cells so that nothing peels through it: the point of
    the fixture is that the *only* way to the map is the cone support, and the
    only thing between the layers is one XOR operand per link.
    """
    builder = synth.Builder("xor_linked_permutation")
    builder.port("input", "clk")
    builder.port("input", "rst_n")
    builder.port("input", "rkey", width)
    builder.port("output", "dout", width)
    builder.wire("state", width)
    builder.wire("sub", width)
    builder.wire("nxt", width)
    for index in range(width):
        builder.lut(
            "sb_{}".format(index),
            "sub[{}]".format(index),
            ["state[{}]".format(index), "state[{}]".format((index + 1) % width)],
            [0, 0, 0, 1],
        )
    for index in range(width):
        target = width - 1 if index == width - 1 else (4 * index) % (width - 1)
        builder.lut(
            "rk_{}".format(target),
            "nxt[{}]".format(target),
            ["sub[{}]".format(index), "rkey[{}]".format(target)],
            [0, 1, 1, 0],
        )
    for index in range(width):
        builder.register(
            "state_{}".format(index), "nxt[{}]".format(index),
            "state[{}]".format(index), clear="rst_n",
        )
        builder.assign("dout[{}]".format(index), "state[{}]".format(index))
    return builder.dumps()


class ConeSupportPermutationTest(unittest.TestCase):
    """Maps that survive one cell per link, and the ones that must not.

    The wiring tier answers "is this map the wiring"; this tier answers "does
    each destination bit read exactly one bit of that vector". The second
    question is the one a real export leaves open, and the tests below pin both
    what it recovers and what it refuses.
    """

    def test_a_keyed_spn_round_is_recovered_when_the_wiring_tier_is_blind(self):
        model = fixture("spn_round16")
        self.assertEqual(
            [],
            [
                entry
                for entry in permutation.find_permutations(model)
                if entry["kind"] != "identity"
            ],
        )
        entries = permutation.cone_support_maps(model)
        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual("general", entry["kind"])
        self.assertEqual("sub", entry["source"])
        self.assertEqual("register bank state", entry["destination"])
        self.assertEqual(16, entry["width"])
        self.assertEqual(16, entry["bits_observed"])
        self.assertTrue(entry["complete"])
        self.assertEqual("next-state cone support", entry["read_from"])
        self.assertEqual("cone-support", entry["evidence_tier"])
        # P(i) = 4i mod 15, read in the other direction
        self.assertEqual(
            [0, 4, 8, 12, 1, 5, 9, 13, 2, 6, 10, 14, 3, 7, 11, 15],
            entry["permutation"],
        )
        self.assertEqual("gated", entry["link_form"])
        self.assertEqual(["load"], entry["shared_side_inputs"])

    def test_a_pure_xor_link_is_reported_as_one(self):
        model = NetlistModel(vo_netlist.parse_text(_xor_linked_permutation_verilog()))
        entries = permutation.cone_support_maps(model)
        self.assertEqual(1, len(entries))
        self.assertEqual("xor", entries[0]["link_form"])
        self.assertEqual(1, entries[0]["max_side_inputs"])
        self.assertEqual(
            [0, 4, 8, 12, 1, 5, 9, 13, 2, 6, 10, 14, 3, 7, 11, 15],
            entries[0]["permutation"],
        )

    def test_a_two_to_one_datapath_mux_bank_is_not_a_permutation(self):
        """The negative control: neither multiplexer shape may be reported."""
        rejections = []
        model = fixture("mux_bank16")
        self.assertEqual([], permutation.cone_support_maps(model, rejections=rejections))
        self.assertEqual(1, len(rejections))
        self.assertEqual("register bank y", rejections[0]["destination"])
        self.assertEqual(["a", "b"], rejections[0]["sources"])
        self.assertIn("multiplexer", rejections[0]["reason"])
        # the `z` bank -- sel ? a[i] : a[i-1] -- produces no candidate at all,
        # because no bit's next state reads only one bit of `a`
        self.assertNotIn(
            "register bank z", [entry["destination"] for entry in rejections]
        )

    def test_a_map_the_wiring_tier_already_has_is_not_repeated(self):
        rejections = []
        entries = permutation.cone_support_maps(fixture("rotate16"), rejections=rejections)
        self.assertEqual([], entries)
        self.assertEqual(["front"], rejections[0]["sources"])
        self.assertIn("stronger claim", rejections[0]["reason"])

    def test_a_rotation_the_cell_pins_already_give_is_not_repeated(self):
        """``register_bank_rotations`` reads arx_round8's y rotation; leave it there."""
        self.assertEqual(
            {"y"},
            {
                entry["source"]
                for entry in permutation.register_bank_rotations(fixture("arx_round8"))
            },
        )
        self.assertEqual([], permutation.cone_support_maps(fixture("arx_round8")))

    def test_shift_chains_are_not_reported_as_rotations(self):
        """A bank missing exactly its head is an open chain, not a rotation."""
        for name in ("lfsr16_fibonacci", "lfsr16_galois", "nlfsr16", "shift16_plain"):
            self.assertEqual([], permutation.cone_support_maps(fixture(name)), name)
        self.assertEqual([], permutation.cone_support_maps(load(TRIVIUM_EXPORT)))

    def test_a_counter_and_a_plain_register_file_produce_nothing(self):
        for name in ("counter8", "butterfly4", "present_sbox_layer"):
            self.assertEqual([], permutation.cone_support_maps(fixture(name)), name)


class PresentExportRecoveryTest(unittest.TestCase):
    """The acceptance case: walkthrough 12's committed Quartus exports.

    Both layers of PRESENT-80 are in ``present_sbox.vo`` and neither is a wire:
    the round key sits between the substitution layer and the datapath
    register, and a parallel key load sits on every link of the key register.
    Section 5 of that walkthrough's ``guide.html`` recovers both by hand; these
    tests are the automated form of the same statements.
    """

    def test_the_player_is_recovered_and_matches_the_published_table(self):
        entries = permutation.cone_support_maps(load(PRESENT_EXPORT))
        layer = [entry for entry in entries if entry["source"] == "subs"]
        self.assertEqual(1, len(layer))
        entry = layer[0]
        self.assertEqual("register bank state", entry["destination"])
        self.assertEqual(64, entry["width"])
        self.assertEqual(64, entry["bits_observed"])
        self.assertEqual(
            ["present_player"], [match["name"] for match in entry["matches"]]
        )
        self.assertEqual(
            "src[i] drives dest[P(i)]", entry["matches"][0]["convention"]
        )
        published = [63 if i == 63 else (16 * i) % 63 for i in range(64)]
        inverse = [0] * 64
        for index, target in enumerate(published):
            inverse[target] = index
        self.assertEqual(inverse, entry["permutation"])

    def test_the_key_register_rotation_is_recovered(self):
        entries = permutation.cone_support_maps(load(PRESENT_EXPORT))
        rotations = [entry for entry in entries if entry["kind"] == "rotation"]
        self.assertEqual(1, len(rotations))
        entry = rotations[0]
        self.assertEqual("register bank kreg", entry["destination"])
        self.assertEqual("kreg", entry["source"])
        self.assertEqual(80, entry["width"])
        self.assertEqual(61, entry["rotate_left_by"])
        # 76 of 80: the four substituted bits read the S-box output instead
        self.assertEqual(76, entry["bits_observed"])
        self.assertFalse(entry["complete"])

    def test_the_wiring_tier_still_sees_nothing_and_says_so(self):
        """The new tier does not silently upgrade the old one's silence."""
        self.assertEqual(
            [],
            [
                entry
                for entry in permutation.find_permutations(load(PRESENT_EXPORT))
                if entry["kind"] != "identity"
            ],
        )

    def test_the_finding_is_proven_only_under_the_side_input_assumption(self):
        document = identify(PRESENT_EXPORT)
        entry = finding_by_id(document, "hal_crypto/permutation/cone-support/1")
        self.assertEqual("proven_under_assumptions", entry["status"])
        self.assertIn(
            "one-side-input-per-link",
            [assumption["id"] for assumption in entry["assumptions"]],
        )
        self.assertIn("present_player", entry["title"])
        self.assertIn("weaker claim of the two", entry["summary"])

    def test_the_textbook_variant_keeps_the_player_hidden(self):
        """The counterfactual: without a named substitution layer, no pLayer.

        ``present_textbook.vo`` holds the state *before* the key addition, so
        every S-box cone reaches back through the key XOR to eight sources and
        the substitution layer is not a signal at all. The key schedule's
        rotation survives -- it does not depend on the substitution -- and the
        datapath permutation does not. Recovering it would need the
        substitution layer first, which is exactly what that variant destroys.
        """
        entries = permutation.cone_support_maps(load(PRESENT_TEXTBOOK_EXPORT))
        self.assertEqual(
            [("register bank kreg", "kreg", 61)],
            [
                (entry["destination"], entry["source"], entry["rotate_left_by"])
                for entry in entries
            ],
        )

    def test_the_nokeep_variant_keeps_the_player_hidden_too(self):
        entries = permutation.cone_support_maps(load(PRESENT_NOKEEP_EXPORT))
        self.assertEqual(["kreg"], [entry["source"] for entry in entries])

    def test_the_family_verdicts_of_all_three_exports_are_unchanged(self):
        for path, family in (
            (PRESENT_EXPORT, "spn"),
            (PRESENT_NOKEEP_EXPORT, "spn"),
            (PRESENT_TEXTBOOK_EXPORT, "none-detected"),
        ):
            netlist = vo_netlist.parse_file(path)
            self.assertEqual(
                family, classify.verdict(classify.run_passes(netlist))["family"], path
            )


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

    def test_a_modulus_with_no_constant_chain_is_read_off_the_correction(self):
        """``q = 2**4 + 1`` is too cheap to need a chain, and is found anyway."""
        model = fixture("ntt_fermat17")
        constants = [
            entry
            for entry in arith.adders(model)
            if entry["operation"] == "add_constant"
        ]
        self.assertEqual([], constants)
        result = ntt.identify(model)
        self.assertEqual(1, result["butterfly_count"])
        self.assertEqual("modular-arithmetic-candidate", result["verdict"])
        self.assertEqual([17], result["recovered_moduli"])
        self.assertEqual([], result["named_moduli"])

    def test_the_reduction_tier_names_the_nets_it_located(self):
        entries = ntt.reduction_moduli(
            fixture("ntt_fermat17"), ntt.butterflies(arith.adders(fixture("ntt_fermat17")))
        )
        self.assertEqual(1, len(entries))
        entry = entries[0]
        self.assertEqual(17, entry["modulus"])
        self.assertTrue(entry["checked_every_sum"])
        self.assertEqual(63, entry["vectors_checked"])
        self.assertEqual(
            ["sum_mod[{}]".format(index) for index in range(5)], entry["sum_nets"]
        )
        self.assertEqual(
            ["dif_mod[{}]".format(index) for index in range(5)],
            entry["difference_nets"],
        )

    def test_the_two_modulus_tiers_agree_where_both_fire(self):
        """The constant-chain fixture's 3329 is reached the other way too."""
        result = ntt.identify(fixture("ntt_stage13"))
        self.assertEqual([3329], result["named_moduli"])
        self.assertEqual([3329], result["recovered_moduli"])

    def test_a_butterfly_with_no_correction_recovers_no_modulus(self):
        """`reduction_moduli` is not a second way to guess."""
        model = fixture("butterfly4")
        entries = ntt.reduction_moduli(model, ntt.butterflies(arith.adders(model)))
        self.assertEqual([], entries)
        self.assertNotIn("recovered_moduli", ntt.identify(model))


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

    def test_walkthrough_speck_is_arx_and_classical(self):
        document = identify(SPECK_EXPORT)
        validate.validate_document(document)
        family = finding_by_id(document, "hal_crypto/identify/family")
        self.assertEqual("arx", family["data"]["family"])
        self.assertEqual(["arx"], family["data"]["families_present"])
        self.assertEqual("high", family["data"]["confidence_tier"])
        style = finding_by_id(document, "hal_crypto/identify/classical-vs-pqc")
        self.assertEqual("classical-style", style["data"]["style"])
        round_finding = finding_by_id(document, "hal_crypto/arx/round")
        self.assertEqual("heuristic", round_finding["status"])
        # the published rotation set is quoted, the cipher is never claimed
        text = json.dumps(document)
        self.assertIn("speck_32", text)
        for word in ("is SPECK", "is Speck", "implements SPECK", "implements Speck"):
            self.assertNotIn(word, text)

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

    def test_sponge_is_not_placed_on_the_classical_pqc_axis(self):
        """A Keccak permutation is SHA-3 *and* the XOF inside ML-KEM/ML-DSA."""
        document = identify(os.path.join(FIXTURES, "keccak_chi_layer.vo"))
        validate.validate_document(document)
        family = finding_by_id(document, "hal_crypto/identify/family")
        self.assertEqual("sponge", family["data"]["family"])
        self.assertIn("sponge", family["data"]["families_present"])
        style = finding_by_id(document, "hal_crypto/identify/classical-vs-pqc")
        self.assertEqual("undetermined", style["data"]["style"])
        self.assertEqual("unknown", style["status"])
        # the ambiguity has to be stated, not left for the reader to infer
        self.assertIn("SHA-3", style["summary"])
        self.assertIn("ML-KEM", style["summary"])
        # ... and stating it is not the same as claiming the design is one
        text = json.dumps(document)
        for word in ("is SHA-3", "is ML-KEM", "implements SHA-3", "implements SHAKE"):
            self.assertNotIn(word, text)

    def test_the_two_keccak_exports_differ_only_in_what_can_be_seen(self):
        """14_keccak_toy: one round per cycle, with the register moved half a round.

        Same permutation, same interface, same nineteen cycles.  In the
        canonical form chi reads the register bank *through* theta and its
        cones are thirty-three flip-flops wide, so the pass refuses to
        enumerate them and the honest answer is ``none-detected``.  Retimed,
        chi sits on the register outputs and the same command finds forty of
        them.
        """
        canonical = identify(KECCAK_EXPORT)
        validate.validate_document(canonical)
        family = finding_by_id(canonical, "hal_crypto/identify/family")
        self.assertEqual("none-detected", family["data"]["family"])
        # a coverage limit, not a clean negative
        self.assertEqual("medium", family["data"]["confidence_tier"])
        boxes = finding_by_id(canonical, "hal_crypto/sbox/none")
        self.assertTrue(
            any(
                "read more than" in entry["reason"]
                for entry in boxes["data"]["rejected"]
            ),
            boxes["data"]["rejected"],
        )

        retimed = identify(KECCAK_RETIMED_EXPORT)
        validate.validate_document(retimed)
        family = finding_by_id(retimed, "hal_crypto/identify/family")
        self.assertEqual("sponge", family["data"]["family"])
        self.assertEqual("high", family["data"]["confidence_tier"])
        style = finding_by_id(retimed, "hal_crypto/identify/classical-vs-pqc")
        self.assertEqual("undetermined", style["data"]["style"])
        matches = [
            entry
            for entry in retimed["findings"]
            if entry["id"].startswith("hal_crypto/sbox/library-match")
        ]
        self.assertEqual(40, len(matches))  # five rows x eight bit-slices
        for entry in matches:
            self.assertEqual(5, entry["data"]["bits"])
            self.assertEqual(
                ["keccak_chi_5"], [m["name"] for m in entry["data"]["matches"]]
            )

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
