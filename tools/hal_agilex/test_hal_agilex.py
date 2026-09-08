"""Unit tests for the Agilex support package. No HAL, no Quartus needed.

Run from the repository root::

    python -m unittest discover -s tools/hal_agilex -t tools -p "test_hal_agilex.py"

The tests are deliberately about the claims, not the plumbing: that the
primitive semantics reproduce the vendor export's own behaviour, that the
import rewrite is exact, that uncovered primitives are refused rather than
dropped, and that every findings document validates against the shared schema.
"""

import json
import os
import random
import unittest

from hal_agilex import (
    behavior,
    hal_adapter,
    inventory,
    library,
    primitives,
    recognize,
    simulate,
    vo_import,
    vo_netlist,
)

from hal_findings import model, serialize, validate

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))

COUNTER = os.path.join(FIXTURES, "agilex3_counter_adder")
LUT_LOGIC = os.path.join(FIXTURES, "agilex3_lut_logic")
MIXED = os.path.join(FIXTURES, "agilex3_mixed_blocks")


def load(directory, name):
    return vo_netlist.parse_file(os.path.join(directory, name))


# ---------------------------------------------------------------------------
# a tiny Boolean expression evaluator, used to check the expressions the HAL
# adapter hands to BooleanFunction.from_string without needing HAL
# ---------------------------------------------------------------------------


def evaluate_expression(expression, values):
    tokens = (
        expression.replace("(", " ( ")
        .replace(")", " ) ")
        .replace("!", " ! ")
        .replace("&", " & ")
        .replace("|", " | ")
        .replace("^", " ^ ")
        .split()
    )
    position = [0]

    def parse_primary():
        token = tokens[position[0]]
        position[0] += 1
        if token == "(":
            value = parse_or()
            assert tokens[position[0]] == ")", expression
            position[0] += 1
            return value
        if token == "!":
            return 1 - parse_primary()
        if token in ("0b0", "0"):
            return 0
        if token in ("0b1", "1"):
            return 1
        return values[token]

    def parse_binary(next_level, operators):
        value = next_level()
        while position[0] < len(tokens) and tokens[position[0]] in operators:
            operator = tokens[position[0]]
            position[0] += 1
            right = next_level()
            if operator == "&":
                value &= right
            elif operator == "|":
                value |= right
            else:
                value ^= right
        return value

    def parse_and():
        return parse_binary(parse_primary, ("&",))

    def parse_xor():
        return parse_binary(parse_and, ("^",))

    def parse_or():
        return parse_binary(parse_xor, ("|",))

    value = parse_or()
    assert position[0] == len(tokens), expression
    return value


class PrimitiveSemanticsTest(unittest.TestCase):
    def test_combout_matches_mask_bit(self):
        mask = 0x123456789ABCDEF0
        for vector in range(64):
            values = {
                pin: (vector >> index) & 1
                for index, pin in enumerate(primitives.LCELL_DATA_PINS)
            }
            self.assertEqual(primitives.combout(mask, values), (mask >> vector) & 1)

    def test_arithmetic_outputs_are_a_full_adder(self):
        # The mask of the shipped adder slices, with datac/datad as operands.
        mask = 0x00000000000F0FF0
        for c in (0, 1):
            for d in (0, 1):
                for cin in (0, 1):
                    values = {"dataa": 0, "datab": 0, "datac": c, "datad": d}
                    sumout, cout = primitives.arithmetic_outputs(mask, values, cin)
                    # datac/datad carry the inverted operands in this export.
                    x, y = 1 - c, 1 - d
                    self.assertEqual(sumout, x ^ y ^ cin)
                    self.assertEqual(cout, 1 if (x + y + cin) >= 2 else 0)

    def test_inversion_absorption_preserves_the_function(self):
        generator = random.Random(4)
        for _ in range(50):
            mask = generator.getrandbits(64)
            inverted = generator.sample(
                list(primitives.LCELL_DATA_PINS), generator.randrange(1, 4)
            )
            absorbed = primitives.absorb_input_inversions(mask, inverted)
            for vector in range(64):
                original = {
                    pin: (vector >> index) & 1
                    for index, pin in enumerate(primitives.LCELL_DATA_PINS)
                }
                rewritten = dict(original)
                for pin in inverted:
                    rewritten[pin] = 1 - rewritten[pin]
                self.assertEqual(
                    primitives.combout(mask, original),
                    primitives.combout(absorbed, rewritten),
                )

    def test_absorption_refuses_pins_that_do_not_address_the_mask(self):
        with self.assertRaises(primitives.UnsupportedConfiguration):
            primitives.absorb_input_inversions(0, ["cin"])

    def test_classification_of_the_shipped_masks(self):
        slice_mask = 0x00000000000F0FF0
        classification = primitives.classify_arithmetic_cell(slice_mask)
        self.assertEqual(classification["kind"], "full_adder_slice")
        self.assertEqual(classification["operands"], ("datac", "datad"))
        self.assertEqual(classification["operand_polarity"], "inverted")
        self.assertEqual(
            primitives.classify_arithmetic_cell(0)["kind"], "carry_tap"
        )
        # A plain 4-input LUT is not an adder slice.
        self.assertIsNone(primitives.classify_arithmetic_cell(0x000000000000FFFF))

    def test_unvalidated_modes_are_refused(self):
        with self.assertRaises(primitives.UnsupportedConfiguration):
            primitives.check_lcell_configuration({"extended_lut": "on"}, set(), False)
        with self.assertRaises(primitives.UnsupportedConfiguration):
            primitives.check_lcell_configuration({"shared_arith": "on"}, set(), False)
        with self.assertRaises(primitives.UnsupportedConfiguration):
            primitives.check_lcell_configuration(
                {"lut_mask": 1 << 40}, {"datac"}, True, {"datae": 0, "dataf": 0}
            )
        with self.assertRaises(primitives.UnsupportedConfiguration):
            primitives.check_ff_configuration({"clk", "sclr"}, {})
        with self.assertRaises(primitives.UnsupportedConfiguration):
            primitives.check_ff_configuration({"clk"}, {"devpor": 0})


class ReaderTest(unittest.TestCase):
    def test_counter_export_structure(self):
        netlist = load(COUNTER, "counter_adder.vo")
        self.assertEqual(netlist.name, "counter_adder")
        self.assertEqual(
            netlist.type_histogram(), {"tennm_lcell_comb": 9, "tennm_ff": 8}
        )
        self.assertEqual(netlist.ports("input"), ["clk", "rst_n", "en", "addend"])
        self.assertEqual(netlist.width("addend"), 8)
        cell = next(
            instance for instance in netlist.instances if instance.name == "add_0~6"
        )
        self.assertEqual(cell.parameters["lut_mask"], 0x000F0FF0)
        self.assertEqual(cell.parameters["shared_arith"], "off")
        self.assertTrue(cell.single("datac").inverted)

    def test_unknown_verilog_is_refused_not_skipped(self):
        with self.assertRaises(vo_netlist.VerilogSubsetError):
            vo_netlist.parse_text("module m (); always @(posedge clk) q <= d; endmodule")

    def test_mixed_export_is_read_completely(self):
        netlist = load(MIXED, "mixed_blocks.vo")
        self.assertEqual(
            netlist.type_histogram(),
            {"tennm_ram_block": 8, "tennm_mac": 1, "tennm_ff": 17, "tennm_lcell_comb": 1},
        )


class SimulationTest(unittest.TestCase):
    def test_counter_matches_its_rtl_reference(self):
        netlist = load(COUNTER, "counter_adder.vo")
        reference = behavior.load_reference(os.path.join(COUNTER, "reference.py"))
        result = behavior.run_reference_check(netlist, reference, cycles=200)
        self.assertNotIn("mismatch", result)
        self.assertEqual(result["checked"], 200)

    def test_lut_logic_matches_its_rtl_reference_exhaustively(self):
        netlist = load(LUT_LOGIC, "lut_logic.vo")
        reference = behavior.load_reference(os.path.join(LUT_LOGIC, "reference.py"))
        result = behavior.run_reference_check(netlist, reference)
        self.assertNotIn("mismatch", result)
        self.assertTrue(result["exhaustive"])
        self.assertEqual(result["checked"], 64)

    def test_uncovered_primitives_stop_the_simulator(self):
        netlist = load(MIXED, "mixed_blocks.vo")
        with self.assertRaises(simulate.SimulationError) as context:
            simulate.build(netlist)
        self.assertIn("tennm_ram_block", str(context.exception))


class ImportTest(unittest.TestCase):
    def _equivalent(self, directory, export, driver):
        original = load(directory, export)
        converted = vo_netlist.parse_text(vo_import.convert(original))
        driver(simulate.build(original), simulate.build(converted))

    def test_counter_import_is_semantics_preserving(self):
        def drive(first, second):
            generator = random.Random(11)
            for cycle in range(120):
                values = {
                    "addend": generator.randrange(256),
                    "en": generator.randrange(2),
                    "rst_n": 0 if cycle == 30 else 1,
                }
                for simulator in (first, second):
                    for name, value in values.items():
                        simulator.set_input(name, value)
                    if values["rst_n"] == 0:
                        simulator.apply_async_clear()
                self.assertEqual(first.get_output("count"), second.get_output("count"))
                self.assertEqual(
                    first.get_output("carry_out"), second.get_output("carry_out")
                )
                first.clock()
                second.clock()

        self._equivalent(COUNTER, "counter_adder.vo", drive)

    def test_lut_logic_import_is_semantics_preserving(self):
        def drive(first, second):
            for vector in range(64):
                for index, name in enumerate("abcdef"):
                    first.set_input(name, (vector >> index) & 1)
                    second.set_input(name, (vector >> index) & 1)
                for output in ("y0", "y1", "y2"):
                    self.assertEqual(
                        first.get_output(output), second.get_output(output), output
                    )

        self._equivalent(LUT_LOGIC, "lut_logic.vo", drive)

    def test_committed_imported_netlists_are_up_to_date(self):
        for directory, export, imported in (
            (COUNTER, "counter_adder.vo", "counter_adder.hal.v"),
            (LUT_LOGIC, "lut_logic.vo", "lut_logic.hal.v"),
        ):
            expected = vo_import.convert_file(
                os.path.join(directory, export), source_note=export
            )
            with open(os.path.join(directory, imported), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), expected, imported)

    def test_import_refuses_uncovered_primitives(self):
        with self.assertRaises(vo_import.ImportRefused) as context:
            vo_import.convert_file(os.path.join(MIXED, "mixed_blocks.vo"))
        message = str(context.exception)
        self.assertIn("tennm_ram_block", message)
        self.assertIn("tennm_mac", message)

    def test_import_refuses_an_inversion_it_cannot_absorb(self):
        netlist = load(COUNTER, "counter_adder.vo")
        cell = next(
            instance
            for instance in netlist.instances
            if instance.name == "add_0~11"
        )
        cin = cell.single("cin")
        cell.connections["cin"] = [vo_netlist.Bit(cin.name, cin.index, True)]
        with self.assertRaises(vo_import.ImportRefused) as context:
            vo_import.convert(netlist)
        self.assertIn("cin", str(context.exception))


class InventoryTest(unittest.TestCase):
    def test_covered_export_reports_full_coverage(self):
        path = os.path.join(COUNTER, "counter_adder.vo")
        document = inventory.build_document_for_file(path)
        validate.validate_document(document)
        statuses = [finding["status"] for finding in document["findings"]]
        self.assertEqual(statuses, ["proven_under_assumptions"])
        self.assertEqual(
            document["artifacts"][0]["device_name"],
            "Altera A3CW135BM16AE6S Package MBGA896",
        )
        self.assertIn(
            "26.1.0", document["artifacts"][0]["description"]
        )

    def test_uncovered_primitives_are_named(self):
        path = os.path.join(MIXED, "mixed_blocks.vo")
        document = inventory.build_document_for_file(path)
        validate.validate_document(document)
        unsupported = [
            finding
            for finding in document["findings"]
            if finding["status"] == model.STATUS_UNSUPPORTED
        ]
        self.assertEqual(len(unsupported), 1)
        entry = unsupported[0]["unsupported"]
        self.assertEqual(entry["kind"], "primitive")
        named = {item["gate_type"]: item["count"] for item in entry["primitives"]}
        self.assertEqual(named, {"tennm_mac": 1, "tennm_ram_block": 8})
        for item in entry["primitives"]:
            self.assertTrue(item["reason"])

    def test_out_of_coverage_configuration_is_reported(self):
        netlist = load(COUNTER, "counter_adder.vo")
        register = netlist.instances_of_type(primitives.FF)[0]
        register.connections["sclr"] = [vo_netlist.Bit("en")]
        report = inventory.inventory(netlist)
        self.assertEqual(len(report["out_of_coverage"]), 1)
        self.assertIn("sclr", report["out_of_coverage"][0]["reason"])
        document = inventory.build_document(
            netlist, os.path.join(COUNTER, "counter_adder.vo")
        )
        validate.validate_document(document)
        statuses = {finding["status"] for finding in document["findings"]}
        self.assertEqual(statuses, {model.STATUS_UNSUPPORTED})


class RecognitionTest(unittest.TestCase):
    def test_architecture_dispatch(self):
        self.assertEqual(
            recognize.select_architecture(load(COUNTER, "counter_adder.vo")),
            "altera_agilex_tennm",
        )
        foreign = vo_netlist.parse_text(
            "module m (a, y); input a; output y; wire y;\n"
            "LUT1 #(.INIT (2'h2)) lut (.I0(a), .O(y));\nendmodule"
        )
        self.assertIsNone(recognize.select_architecture(foreign))

    def test_carry_chain_and_accumulator(self):
        netlist = load(COUNTER, "counter_adder.vo")
        chains = recognize.carry_chains(netlist)
        self.assertEqual(len(chains), 1)
        self.assertEqual([cell.name for cell in chains[0]][0], "add_0~6")
        self.assertEqual(len(chains[0]), 9)

        candidates = [
            candidate
            for candidate in recognize.recognize_adders(netlist)
            if candidate["recognized"]
        ]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["width"], 8)
        self.assertTrue(candidates[0]["is_accumulator"])
        self.assertEqual(candidates[0]["carry_tap"].name, "add_0~1")

    def test_function_check_is_exhaustive_and_passes(self):
        netlist = load(COUNTER, "counter_adder.vo")
        candidate = recognize.recognize_adders(netlist)[0]
        result = recognize.check_adder_function(netlist, candidate)
        self.assertTrue(result["exhaustive"])
        self.assertEqual(result["checked"], 65536)
        self.assertNotIn("counterexample", result)

    def test_findings_document_of_the_counter(self):
        path = os.path.join(COUNTER, "counter_adder.vo")
        netlist = load(COUNTER, "counter_adder.vo")
        artifact = inventory._artifact(netlist, path, "counter_adder")
        document = recognize.build_document(netlist, artifact)
        validate.validate_document(document)
        statuses = [finding["status"] for finding in document["findings"]]
        self.assertEqual(
            statuses, [model.STATUS_HEURISTIC, model.STATUS_PROVEN_UNDER_ASSUMPTIONS]
        )
        pattern = document["findings"][0]
        self.assertFalse(model.is_unbounded_proof(pattern))
        self.assertEqual(pattern["data"]["width"], 8)
        proof = document["findings"][1]
        self.assertTrue(model.is_unbounded_proof(proof))
        self.assertEqual(proof["metrics"]["assignments_checked"], 65536)

    def test_no_adder_is_unknown_not_a_denial(self):
        path = os.path.join(LUT_LOGIC, "lut_logic.vo")
        netlist = load(LUT_LOGIC, "lut_logic.vo")
        artifact = inventory._artifact(netlist, path, "lut_logic")
        document = recognize.build_document(netlist, artifact)
        validate.validate_document(document)
        self.assertEqual(
            [finding["status"] for finding in document["findings"]],
            [model.STATUS_UNKNOWN],
        )


class BehaviorDocumentTest(unittest.TestCase):
    def _document(self, directory, export, cycles=200):
        path = os.path.join(directory, export)
        netlist = vo_netlist.parse_file(path)
        reference = behavior.load_reference(os.path.join(directory, "reference.py"))
        result = behavior.run_reference_check(netlist, reference, cycles=cycles)
        artifact = inventory._artifact(netlist, path, netlist.name)
        document = behavior.build_document(netlist, artifact, result)
        validate.validate_document(document)
        return document

    def test_sequential_claim_is_bounded(self):
        document = self._document(COUNTER, "counter_adder.vo", cycles=64)
        finding = document["findings"][0]
        self.assertEqual(finding["status"], model.STATUS_PROVEN_BOUNDED)
        self.assertEqual(finding["bounds"]["cycle_bound"], 64)
        self.assertFalse(model.is_unbounded_proof(finding))
        self.assertTrue(model.is_bounded_claim(finding))

    def test_exhaustive_combinational_claim_is_unbounded(self):
        document = self._document(LUT_LOGIC, "lut_logic.vo")
        finding = document["findings"][0]
        self.assertEqual(finding["status"], model.STATUS_PROVEN_UNDER_ASSUMPTIONS)
        self.assertTrue(model.is_unbounded_proof(finding))

    def test_a_wrong_reference_produces_a_counterexample(self):
        path = os.path.join(LUT_LOGIC, "lut_logic.vo")
        netlist = vo_netlist.parse_file(path)
        reference = behavior.load_reference(os.path.join(LUT_LOGIC, "reference.py"))

        class Broken(object):
            KIND = reference.KIND
            INPUTS = reference.INPUTS
            OUTPUTS = reference.OUTPUTS

            @staticmethod
            def evaluate(values):
                result = reference.evaluate(values)
                result["y0"] = 1 - result["y0"]
                return result

        result = behavior.run_reference_check(netlist, Broken())
        self.assertIn("mismatch", result)
        artifact = inventory._artifact(netlist, path, netlist.name)
        document = behavior.build_document(netlist, artifact, result)
        validate.validate_document(document)
        self.assertEqual(document["findings"][0]["status"], model.STATUS_COUNTEREXAMPLE)


class HalAdapterExpressionTest(unittest.TestCase):
    """The HAL adapter builds expression strings; check them without HAL."""

    def test_combout_expression_matches_the_mask(self):
        for mask in (0x8F88FFFFFFFF8F88, 0x9600969696009696, 0, (1 << 64) - 1):
            expression = hal_adapter.combout_expression(mask)
            for vector in range(64):
                values = {
                    pin: (vector >> index) & 1
                    for index, pin in enumerate(primitives.LCELL_DATA_PINS)
                }
                self.assertEqual(
                    evaluate_expression(expression, values),
                    primitives.combout(mask, values),
                    "mask 0x{:016X} vector {}".format(mask, vector),
                )

    def test_arithmetic_expressions_match_the_model(self):
        for mask in (0x000F0FF0, 0x0000FF00, 0):
            sum_expression, carry_expression = hal_adapter.arithmetic_expressions(mask)
            for vector in range(16):
                for cin in (0, 1):
                    values = {
                        pin: (vector >> index) & 1
                        for index, pin in enumerate(primitives.LCELL_DATA_PINS[:4])
                    }
                    environment = dict(values)
                    environment["cin"] = cin
                    expected_sum, expected_carry = primitives.arithmetic_outputs(
                        mask, values, cin
                    )
                    self.assertEqual(
                        evaluate_expression(sum_expression, environment), expected_sum
                    )
                    self.assertEqual(
                        evaluate_expression(carry_expression, environment), expected_carry
                    )


class GateLibraryTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(REPO_ROOT, library.LIBRARY_PATH)

    def test_committed_library_matches_the_generator(self):
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), library.dumps())

    def test_library_covers_the_export(self):
        with open(self.path, encoding="utf-8") as handle:
            definition = json.load(handle)
        cells = {cell["name"]: cell for cell in definition["cells"]}
        self.assertEqual(
            set(cells), {"HAL_GND", "HAL_VCC", primitives.LCELL, primitives.FF}
        )

        netlist = load(COUNTER, "counter_adder.vo")
        for instance in netlist.instances:
            cell = cells[instance.type]
            declared = {
                pin["name"]
                for group in cell["pin_groups"]
                for pin in group["pins"]
            }
            self.assertLessEqual(set(instance.connections), declared, instance.type)

    def test_lcell_carries_no_lut_config(self):
        # HAL would derive an empty function for a ten-input LUT; the semantics
        # come from hal_adapter instead, and the library must not pretend
        # otherwise.
        with open(self.path, encoding="utf-8") as handle:
            definition = json.load(handle)
        lcell = next(
            cell for cell in definition["cells"] if cell["name"] == primitives.LCELL
        )
        self.assertNotIn("lut_config", lcell)
        self.assertNotIn(
            "function",
            json.dumps(lcell),
            "the ALM function depends on lut_mask and cannot be a static one",
        )

    def test_ff_config_is_the_validated_one(self):
        with open(self.path, encoding="utf-8") as handle:
            definition = json.load(handle)
        ff = next(
            cell for cell in definition["cells"] if cell["name"] == primitives.FF
        )
        self.assertEqual(ff["ff_config"]["clocked_on"], "clk")
        self.assertEqual(ff["ff_config"]["clear_on"], "(! clrn)")
        self.assertIn("ena", ff["ff_config"]["next_state"])
        for pin in primitives.FF_MUST_BE_ZERO_PINS:
            self.assertNotIn(pin, ff["ff_config"]["next_state"])


class FixtureManifestTest(unittest.TestCase):
    def test_every_fixture_records_device_version_and_commands(self):
        for directory in (COUNTER, LUT_LOGIC, MIXED):
            with open(os.path.join(directory, "MANIFEST.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertEqual(manifest["device"], "A3CW135BM16AE6S")
            self.assertEqual(manifest["family"], "Agilex 3")
            self.assertIn("26.1.0", manifest["quartus_version"])
            self.assertTrue(any("quartus_eda" in step for step in manifest["export_flow"]))
            self.assertTrue(manifest["redistribution"])
            self.assertTrue(os.path.isfile(os.path.join(directory, "GROUND_TRUTH.md")))
            for name, entry in manifest["files"].items():
                path = os.path.join(directory, name)
                self.assertTrue(os.path.isfile(path), path)
                self.assertEqual(serialize.sha256_file(path), entry["sha256"], name)


if __name__ == "__main__":
    unittest.main()
