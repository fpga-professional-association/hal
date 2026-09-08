"""Offline tests: no HAL build, no hal_py, standard library only.

These run the *whole* recovery -- the same code the ``hal_py`` path runs -- on
the shipped ``apb_regs`` fixture and check it against the hand-written ground
truth, so a regression in the analysis fails here rather than in a container.
``test_apb_recover_hal.py`` covers what these cannot: that ``hal_py`` builds
the same circuit model from the same netlist.

    python -m unittest discover -s tools/hal_apb_recover -t tools -p "test_*.py"
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from hal_apb_recover import circuit as circuit_model  # noqa: E402
from hal_apb_recover import findings as findings_module  # noqa: E402
from hal_apb_recover import regmap, replay  # noqa: E402
from hal_apb_recover.hgl_library import (  # noqa: E402
    GateLibrary,
    GateLibraryError,
    evaluate_expression,
    parse_expression,
)
from hal_apb_recover.mapping import ApbMapping, MappingError, load_mapping  # noqa: E402
from hal_apb_recover.recover import RecoveryOptions, recover  # noqa: E402
from hal_apb_recover.verilog_source import VerilogError, read_netlist  # noqa: E402
from hal_findings import validate as findings_validate  # noqa: E402

REPO_ROOT = os.path.dirname(_TOOLS)
FIXTURE_DIR = os.path.join(_TOOLS, "hal_apb_recover", "fixtures", "apb_regs")
NETLIST = os.path.join(FIXTURE_DIR, "apb_regs.v")
MAPPING = os.path.join(FIXTURE_DIR, "mapping.json")
GROUND_TRUTH = os.path.join(FIXTURE_DIR, "ground_truth.json")
GATE_LIBRARY = os.path.join(
    REPO_ROOT, "plugins", "gate_libraries", "definitions", "NangateOpenCellLibrary.hgl"
)

X = circuit_model.X


def load_fixture(options=None):
    library = GateLibrary.from_file(GATE_LIBRARY)
    netlist = read_netlist(NETLIST, library)
    mapping = load_mapping(MAPPING, netlist)
    return netlist, mapping, recover(netlist, mapping, options or RecoveryOptions())


class ExpressionTest(unittest.TestCase):
    """The three-valued evaluator is the foundation of every tier claim."""

    def evaluate(self, text, **values):
        return evaluate_expression(parse_expression(text), values)

    def test_controlling_values_beat_unknowns(self):
        # sound abstraction: 0 & X is 0, not X -- this is what lets a probe
        # conclude something while other inputs are unconstrained
        self.assertEqual(self.evaluate("(A & B)", A=0), 0)
        self.assertEqual(self.evaluate("(A | B)", A=1), 1)
        self.assertIs(self.evaluate("(A & B)", A=1), X)
        self.assertIs(self.evaluate("(A | B)", A=0), X)

    def test_xor_never_resolves_with_an_unknown(self):
        self.assertIs(self.evaluate("(A ^ B)", A=1), X)
        self.assertEqual(self.evaluate("(A ^ B)", A=1, B=1), 0)

    def test_mux_selects(self):
        text = "((S & B) | (A & (! S)))"
        self.assertEqual(self.evaluate(text, S=1, B=1), 1)
        self.assertEqual(self.evaluate(text, S=1, B=0), 0)
        self.assertEqual(self.evaluate(text, S=0, A=1), 1)
        # with the select known, the unselected input cannot matter
        self.assertEqual(self.evaluate(text, S=0, A=0), 0)

    def test_constants_and_precedence(self):
        self.assertEqual(self.evaluate("0b1"), 1)
        self.assertEqual(self.evaluate("0b0"), 0)
        self.assertEqual(self.evaluate("A & B | C", A=0, B=0, C=1), 1)
        self.assertEqual(self.evaluate("! A & B", A=1, B=1), 0)

    def test_malformed_expression_is_an_error(self):
        with self.assertRaises(GateLibraryError):
            parse_expression("(A & ")
        with self.assertRaises(GateLibraryError):
            parse_expression("A $ B")


class VerilogReaderTest(unittest.TestCase):
    def read(self, text, name="tiny.v"):
        directory = tempfile.mkdtemp(prefix="apb_reader_")
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return read_netlist(path, GateLibrary.from_file(GATE_LIBRARY))

    def test_bit_selects_use_hal_net_names(self):
        netlist = self.read(
            """
            module tiny (a, z);
              input [1:0] a;
              output z;
              AND2_X1 g0 (.A1(a[0]), .A2(a[1]), .ZN(z));
            endmodule
            """
        )
        self.assertIn("a(0)", netlist.net_names.values())
        self.assertIn("a(1)", netlist.net_names.values())
        values = netlist.evaluate({netlist.resolve_net("a(0)"): 1,
                                   netlist.resolve_net("a(1)"): 1})
        self.assertEqual(values[netlist.resolve_net("z")], 1)

    def test_assign_collapses_into_one_net(self):
        netlist = self.read(
            """
            module tiny (a, z);
              input a;
              output z;
              wire w;
              INV_X1 g0 (.A(a), .ZN(w));
              assign z = w;
            endmodule
            """
        )
        values = netlist.evaluate({netlist.resolve_net("a"): 0})
        self.assertEqual(values[netlist.resolve_net("z")], 1)

    def test_unknown_gate_type_is_refused(self):
        with self.assertRaises(VerilogError) as raised:
            self.read(
                "module tiny (a, z); input a; output z; NOT_A_CELL g0 (.A(a), .ZN(z)); endmodule"
            )
        self.assertIn("NOT_A_CELL", str(raised.exception))

    def test_latch_is_unsupported_not_approximated(self):
        netlist = self.read(
            """
            module tiny (d, g, q);
              input d;
              input g;
              output q;
              DLH_X1 l0 (.D(d), .G(g), .Q(q));
            endmodule
            """
        )
        self.assertEqual([gate.type_name for gate in netlist.unsupported_gates], ["DLH_X1"])
        self.assertEqual(netlist.state_elements, [])
        values = netlist.evaluate({netlist.resolve_net("d"): 1, netlist.resolve_net("g"): 1})
        self.assertIs(values.get(netlist.resolve_net("q")), X)

    def test_combinational_loop_is_reported_not_evaluated(self):
        netlist = self.read(
            """
            module tiny (a, z);
              input a;
              output z;
              wire w;
              AND2_X1 g0 (.A1(a), .A2(w), .ZN(z));
              INV_X1 g1 (.A(z), .ZN(w));
            endmodule
            """
        )
        self.assertEqual(sorted(netlist.combinational_loop), ["g0", "g1"])


class MappingValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.circuit = read_netlist(NETLIST, GateLibrary.from_file(GATE_LIBRARY))
        with open(MAPPING, "r", encoding="utf-8") as handle:
            cls.document = json.load(handle)

    def mapping(self, mutate):
        document = copy.deepcopy(self.document)
        mutate(document)
        return ApbMapping(document, self.circuit)

    def test_the_fixture_mapping_is_accepted(self):
        mapping = ApbMapping(copy.deepcopy(self.document), self.circuit)
        self.assertEqual(mapping.data_width, 16)
        self.assertEqual(mapping.address_width, 5)
        self.assertEqual(mapping.strobe_lanes, 2)
        self.assertEqual(mapping.addresses(), [0, 4, 8, 12, 16, 20, 24, 28])

    def test_a_missing_net_is_rejected_with_the_name(self):
        with self.assertRaises(MappingError) as raised:
            self.mapping(lambda document: document["apb"].update(psel="io_99"))
        self.assertIn("io_99", str(raised.exception))

    def test_a_net_used_twice_is_rejected(self):
        with self.assertRaises(MappingError) as raised:
            self.mapping(lambda document: document["apb"].update(penable=document["apb"]["psel"]))
        self.assertIn("mapped to both", str(raised.exception))

    def test_prdata_must_be_an_output(self):
        def mutate(document):
            document["apb"]["prdata"][0] = document["apb"]["pwdata"][0]

        with self.assertRaises(MappingError) as raised:
            self.mapping(mutate)
        self.assertIn("primary output", str(raised.exception))

    def test_strobe_width_must_cover_the_data_bus(self):
        with self.assertRaises(MappingError) as raised:
            self.mapping(lambda document: document["assumptions"].update(byte_lane_bits=4))
        self.assertIn("cover", str(raised.exception))


class GroundTruthTest(unittest.TestCase):
    """Every acceptance criterion of the issue, checked against the fixture."""

    @classmethod
    def setUpClass(cls):
        cls.circuit, cls.mapping, cls.document = load_fixture()
        with open(GROUND_TRUTH, "r", encoding="utf-8") as handle:
            cls.truth = json.load(handle)
        cls.by_address = {
            register["address"]: register for register in cls.document["registers"]
        }
        cls.truth_by_address = {
            register["address"]: register for register in cls.truth["registers"]
        }

    def fields(self, address):
        return {field["bit"]: field for field in self.by_address[address]["fields"]}

    def test_the_document_is_well_formed(self):
        regmap.validate_document(self.document)

    def test_every_ground_truth_address_was_recovered(self):
        self.assertEqual(sorted(self.by_address), sorted(self.truth_by_address))
        self.assertEqual(
            sorted(entry["address"] for entry in self.document["unmapped_addresses"]),
            sorted(self.truth["unmapped_addresses"]),
        )

    def test_every_flip_flop_was_attributed(self):
        coverage = self.document["coverage"]
        self.assertEqual(
            coverage["flip_flops_total"], self.truth["expected_totals"]["flip_flops"]
        )
        self.assertEqual(coverage["flip_flops_unassociated"], [])

    def test_access_semantics_match_the_ground_truth(self):
        for address, truth in self.truth_by_address.items():
            recovered = self.by_address[address]
            self.assertEqual(
                recovered["access"], truth["access"], "access of 0x{:02x}".format(address)
            )
            self.assertEqual(
                recovered["reset_value"]["value"],
                truth["reset_value"],
                "reset value of 0x{:02x}".format(address),
            )
            self.assertTrue(recovered["reset_value"]["complete"])

    def test_field_level_semantics_and_confidence_match(self):
        for address, truth in self.truth_by_address.items():
            recovered = self.fields(address)
            for expected in truth["fields"]:
                field = recovered[expected["bit"]]
                where = "0x{:02x}[{}]".format(address, expected["bit"])
                self.assertEqual(field["access"], expected["access"], where)
                self.assertEqual(
                    field["confidence"], expected["expected_confidence"], where
                )
                if "template" in expected:
                    self.assertEqual(field["template"], expected["template"], where)
                if "reset_value" in expected:
                    self.assertEqual(field["reset_value"], expected["reset_value"], where)
                if "constant" in expected:
                    self.assertEqual(field["constant_value"], expected["constant"], where)
                if "strobe_lanes" in expected:
                    self.assertEqual(field["strobe_lanes"], expected["strobe_lanes"], where)

    def test_the_three_confidence_tiers_are_all_used(self):
        tiers = {
            field["confidence"]
            for register in self.document["registers"]
            for field in register["fields"]
        }
        self.assertEqual(
            tiers, {"proven_under_assumptions", "proven_bounded", "heuristic"}
        )

    def test_the_intentional_address_alias_is_detected(self):
        classes = self.document["alias_classes"]
        self.assertEqual(len(classes), self.truth["expected_totals"]["alias_classes"])
        self.assertEqual(classes[0]["addresses"], [0x00, 0x08])
        self.assertEqual(self.by_address[0x08]["canonical_address"], 0x00)
        self.assertTrue(self.by_address[0x08]["is_alias"])
        self.assertFalse(self.by_address[0x00]["is_alias"])

    def test_write_one_to_clear_is_recovered_with_its_truth_table(self):
        for bit, field in self.fields(0x04).items():
            if field["kind"] != "storage":
                continue
            self.assertEqual(field["template"], "write_one_to_clear")
            # writing a 1 clears, writing a 0 holds -- the whole point of W1C
            self.assertEqual(
                field["next_state_table"],
                {"d0s0": 0, "d0s1": 1, "d1s0": 0, "d1s1": 0},
                "bit {}".format(bit),
            )

    def test_byte_strobes_gate_the_write_lane_by_lane(self):
        for address in (0x00, 0x10):
            for bit, field in self.fields(address).items():
                if field["kind"] != "storage":
                    continue
                self.assertEqual(
                    field["strobe_lanes"], [0 if bit < 8 else 1], "0x{:02x}[{}]".format(address, bit)
                )

    def test_a_write_requires_the_full_access_phase(self):
        field = self.fields(0x00)[0]
        self.assertEqual(sorted(field["write_requires"]), ["penable", "psel", "pwrite"])

    def test_the_guarded_register_is_reported_as_guarded_not_as_read_only(self):
        for bit, field in self.fields(0x10).items():
            self.assertEqual(field["template"], "write")
            self.assertEqual(field["confidence"], "heuristic")
            guard = field["guarded_by"]
            self.assertTrue(guard["candidates"], "bit {} has no guard candidate".format(bit))
            self.assertEqual(guard["alternative_template"], "hold")
            # the guard must be CTRL bit 0
            ctrl_bit0 = self.fields(0x00)[0]["storage"]
            self.assertIn(ctrl_bit0, [entry["storage"] for entry in guard["candidates"]])

    def test_the_read_only_identification_register_has_no_storage(self):
        for field in self.by_address[0x0C]["fields"]:
            self.assertEqual(field["kind"], "constant")
            self.assertIsNone(field["storage"])
        value = 0
        for field in self.by_address[0x0C]["fields"]:
            if field["constant_value"]:
                value |= 1 << field["bit"]
        self.assertEqual(value, 0xA53C)

    def test_status_bits_without_storage_are_reported_as_constant_reads(self):
        fields = self.fields(0x04)
        for bit in range(8, 16):
            self.assertEqual(fields[bit]["kind"], "constant")
            self.assertEqual(fields[bit]["constant_value"], 0)

    def test_the_unmodelled_primitive_is_reported_not_assumed(self):
        primitives = self.document["coverage"]["unsupported_gate_types"]
        self.assertEqual([entry["gate_type"] for entry in primitives], ["DLH_X1"])
        self.assertEqual(primitives[0]["count"], 1)

    def test_incomplete_coverage_is_stated_explicitly(self):
        coverage = self.document["coverage"]
        self.assertTrue(coverage["limits"])
        self.assertEqual(coverage["address_bits"]["never_varied"], [0, 1])
        # PADDR[3] has no effect at 0x00 because 0x00 and 0x08 alias
        self.assertEqual(
            coverage["address_bits"]["no_effect_at_probe_address"], [0, 1, 3]
        )

    def test_no_field_was_left_unresolved_in_this_fixture(self):
        self.assertEqual(self.document["unresolved_fields"], [])

    def test_the_recovery_is_deterministic(self):
        _, _, again = load_fixture()
        first = regmap.behavioural_summary(self.document)
        second = regmap.behavioural_summary(again)
        self.assertEqual(first, second)
        self.assertEqual(
            json.dumps(_strip_volatile(self.document), sort_keys=True),
            json.dumps(_strip_volatile(again), sort_keys=True),
        )


def _strip_volatile(document):
    copied = copy.deepcopy(document)
    copied.pop("metrics", None)
    copied["netlist"].pop("source", None)
    return copied


class UnresolvedFieldTest(unittest.TestCase):
    """A field the analysis cannot characterise must be listed, not invented."""

    def test_a_data_dependent_write_is_reported_unresolved(self):
        directory = tempfile.mkdtemp(prefix="apb_unresolved_")
        netlist_path = os.path.join(directory, "odd.v")
        # d0 stores pwdata[0] XOR pwdata[1]: no single-data-bit template fits,
        # so the recovery must refuse to name an access type for it.
        with open(netlist_path, "w", encoding="utf-8") as handle:
            handle.write(
                """
                module odd (clk, rstn, psel, penable, pwrite, a0, w0, w1, r0);
                  input clk; input rstn; input psel; input penable; input pwrite;
                  input a0; input w0; input w1; output r0;
                  wire acc; wire wr; wire mixed; wire d; wire q; wire nrd; wire rd;
                  AND2_X1 g0 (.A1(psel), .A2(penable), .ZN(acc));
                  AND2_X1 g1 (.A1(acc), .A2(pwrite), .ZN(wr));
                  XOR2_X1 g2 (.A(w0), .B(w1), .Z(mixed));
                  MUX2_X1 g3 (.A(q), .B(mixed), .S(wr), .Z(d));
                  DFFR_X1 g4 (.D(d), .RN(rstn), .CK(clk), .Q(q));
                  INV_X1 g5 (.A(pwrite), .ZN(nrd));
                  AND2_X1 g6 (.A1(acc), .A2(nrd), .ZN(rd));
                  AND2_X1 g7 (.A1(rd), .A2(q), .ZN(r0));
                endmodule
                """
            )
        mapping_path = os.path.join(directory, "mapping.json")
        with open(mapping_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "schema_version": "1.0.0",
                    "design": "odd",
                    "clock": {"net": "clk", "edge": "rising"},
                    "reset": {"net": "rstn", "active": "low"},
                    "apb": {
                        "psel": "psel",
                        "penable": "penable",
                        "pwrite": "pwrite",
                        "paddr": ["a0"],
                        "pwdata": ["w0", "w1"],
                        "prdata": ["r0"],
                    },
                    "address": {"base": 0, "stride": 1, "count": 2},
                    "assumptions": {"non_apb_inputs": "zero", "byte_lane_bits": 8},
                },
                handle,
            )

        circuit = read_netlist(netlist_path, GateLibrary.from_file(GATE_LIBRARY))
        mapping = load_mapping(mapping_path, circuit)
        document = recover(circuit, mapping)
        regmap.validate_document(document)
        self.assertTrue(
            document["unresolved_fields"],
            "a write that mixes two data bits must not be given an access type",
        )
        for entry in document["unresolved_fields"]:
            self.assertIn("PWDATA", entry["reason"])
        # nothing was invented in its place: the bit stays readable, but no
        # field claims to know how a write changes it
        for register in document["registers"]:
            for field in register["fields"]:
                self.assertIn(field.get("template"), ("hold", "constant"))
                self.assertEqual(field.get("access"), "read-only")


class FindingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.circuit, cls.mapping, cls.document = load_fixture()
        cls.findings = findings_module.build_document(
            cls.document, netlist_path=NETLIST
        )

    def test_the_findings_document_validates_against_the_shared_schema(self):
        findings_validate.validate_document(self.findings)

    def test_statuses_reflect_the_recovered_confidence(self):
        statuses = {finding["status"] for finding in self.findings["findings"]}
        self.assertIn("proven_under_assumptions", statuses)
        self.assertIn("proven_bounded", statuses)
        self.assertIn("heuristic", statuses)
        self.assertIn("unsupported", statuses)

    def test_a_proof_is_unbounded_and_a_bounded_check_is_not(self):
        from hal_findings import model

        for finding in self.findings["findings"]:
            if finding["status"] == "proven_under_assumptions":
                self.assertTrue(model.is_unbounded_proof(finding), finding["id"])
                self.assertTrue(finding["assumptions"])
            if finding["status"] == "proven_bounded":
                self.assertFalse(model.is_unbounded_proof(finding), finding["id"])
                self.assertEqual(finding["bounds"]["cycle_bound"], 1)

    def test_the_guarded_register_never_claims_a_proof(self):
        for finding in self.findings["findings"]:
            if finding["id"].startswith("apb/register/00000010"):
                self.assertEqual(finding["status"], "heuristic")

    def test_the_unmodelled_primitive_is_an_unsupported_finding(self):
        matches = [
            finding
            for finding in self.findings["findings"]
            if finding["id"] == "apb/coverage/unsupported-primitives"
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["status"], "unsupported")
        self.assertEqual(
            [entry["gate_type"] for entry in matches[0]["unsupported"]["primitives"]],
            ["DLH_X1"],
        )

    def test_the_address_window_is_reported_as_a_coverage_gap(self):
        matches = [
            finding
            for finding in self.findings["findings"]
            if finding["id"] == "apb/coverage/address-window"
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["unsupported"]["kind"], "configuration")

    def test_gate_references_are_scoped_to_the_artifact(self):
        artifact_ids = {artifact["artifact_id"] for artifact in self.findings["artifacts"]}
        for finding in self.findings["findings"]:
            for gate in finding["scope"].get("gates", []):
                self.assertIn(gate["artifact_id"], artifact_ids)


class ReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.circuit, cls.mapping, cls.document = load_fixture()
        cls.replay = replay.build_replay(cls.document)

    def test_the_generated_replay_passes_against_the_netlist(self):
        results = replay.run_replay(self.circuit, self.mapping, self.replay)
        ok, lines = replay.format_results(results)
        self.assertTrue(ok, "\n".join(lines))
        self.assertGreater(len(results), 20)

    def test_the_replay_exercises_reads_writes_and_forces(self):
        operations = {transaction["op"] for transaction in self.replay["transactions"]}
        self.assertEqual(operations, {"force_state", "write", "read"})

    def test_a_wrong_register_map_makes_the_replay_fail(self):
        # If the replay passed for any map, it would be worthless. Claim the
        # write-one-to-clear register is a plain read-write one and check that
        # the netlist contradicts it.
        broken = copy.deepcopy(self.document)
        for register in broken["registers"]:
            if register["address"] != 0x04:
                continue
            for field in register["fields"]:
                if field["kind"] == "storage":
                    field["template"] = "write"
                    field["access"] = "read-write"
        results = replay.run_replay(
            self.circuit, self.mapping, replay.build_replay(broken)
        )
        ok, lines = replay.format_results(results)
        self.assertFalse(ok, "a wrong register map must not replay cleanly")
        self.assertTrue(any("expected" in line for line in lines))

    def test_a_replay_from_another_netlist_is_refused(self):
        foreign = copy.deepcopy(self.replay)
        foreign["transactions"][0]["bits"] = {"not_a_gate": 1}
        with self.assertRaises(replay.ReplayError):
            replay.run_replay(self.circuit, self.mapping, foreign)


class MarkdownTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, _, cls.document = load_fixture()
        cls.markdown = regmap.to_markdown(cls.document)

    def test_every_register_and_alias_appears(self):
        for register in self.document["registers"]:
            self.assertIn(register["address_hex"], self.markdown)
        self.assertIn("Address aliases", self.markdown)
        self.assertIn("Addresses with no register", self.markdown)

    def test_the_confidence_vocabulary_is_explained(self):
        self.assertIn("proven under assumptions", self.markdown)
        self.assertIn("bounded check (1 transfer)", self.markdown)
        self.assertIn("inferred", self.markdown)

    def test_the_assumptions_are_printed(self):
        self.assertIn("apb/mapping", self.markdown)
        self.assertIn("apb/address-window", self.markdown)


class RegisterMapValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, _, cls.document = load_fixture()

    def mutate(self, mutation):
        document = copy.deepcopy(self.document)
        mutation(document)
        return document

    def test_an_unknown_schema_version_is_refused(self):
        with self.assertRaises(regmap.RegisterMapError):
            regmap.validate_document(
                self.mutate(lambda document: document.update(schema_version="9.9.9"))
            )

    def test_a_duplicate_address_is_refused(self):
        def mutation(document):
            document["registers"].append(copy.deepcopy(document["registers"][0]))

        with self.assertRaises(regmap.RegisterMapError):
            regmap.validate_document(self.mutate(mutation))

    def test_an_invented_confidence_is_refused(self):
        def mutation(document):
            document["registers"][0]["fields"][0]["confidence"] = "definitely"

        with self.assertRaises(regmap.RegisterMapError):
            regmap.validate_document(self.mutate(mutation))

    def test_a_storage_field_without_storage_is_refused(self):
        def mutation(document):
            for register in document["registers"]:
                for field in register["fields"]:
                    if field["kind"] == "storage":
                        field["storage"] = None
                        return

        with self.assertRaises(regmap.RegisterMapError):
            regmap.validate_document(self.mutate(mutation))


class CommandLineTest(unittest.TestCase):
    def test_recover_and_replay_round_trip_through_the_cli(self):
        directory = tempfile.mkdtemp(prefix="apb_cli_")
        base = os.path.join(directory, "map")
        command = [
            sys.executable,
            os.path.join(_TOOLS, "hal_apb_recover"),
            "recover",
            NETLIST,
            MAPPING,
            "--gate-library",
            GATE_LIBRARY,
            "-o",
            base,
            "-q",
        ]
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", "replace"),
        )
        for suffix in (".json", ".md", ".findings.json", ".replay.json"):
            self.assertTrue(os.path.isfile(base + suffix), base + suffix)

        replay_command = [
            sys.executable,
            os.path.join(_TOOLS, "hal_apb_recover"),
            "replay",
            NETLIST,
            MAPPING,
            base + ".replay.json",
            "--gate-library",
            GATE_LIBRARY,
        ]
        completed = subprocess.run(
            replay_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout.decode("utf-8", "replace")
            + completed.stderr.decode("utf-8", "replace"),
        )

    def test_a_broken_mapping_exits_one_with_a_message(self):
        directory = tempfile.mkdtemp(prefix="apb_cli_bad_")
        mapping_path = os.path.join(directory, "mapping.json")
        with open(MAPPING, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        document["apb"]["psel"] = "does_not_exist"
        with open(mapping_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)

        completed = subprocess.run(
            [
                sys.executable,
                os.path.join(_TOOLS, "hal_apb_recover"),
                "recover",
                NETLIST,
                mapping_path,
                "--gate-library",
                GATE_LIBRARY,
                "-o",
                os.path.join(directory, "map"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("does_not_exist", completed.stderr.decode("utf-8", "replace"))


class FixtureTest(unittest.TestCase):
    def test_the_committed_fixture_matches_its_generator(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "apb_regs_generate", os.path.join(FIXTURE_DIR, "generate.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with open(NETLIST, "r", encoding="utf-8") as handle:
            committed = handle.read()
        self.assertEqual(
            module.emit_verilog(module.build()),
            committed,
            "apb_regs.v is out of date; rerun fixtures/apb_regs/generate.py",
        )
        with open(MAPPING, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), module.emit_mapping())
        with open(GROUND_TRUTH, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), module.emit_ground_truth())

    def test_the_fixture_hides_every_meaningful_name(self):
        with open(NETLIST, "r", encoding="utf-8") as handle:
            text = handle.read().lower()
        for leak in ("paddr", "pwdata", "prdata", "ctrl", "status", "scratch", "pstrb"):
            self.assertNotIn(
                leak,
                text.split("module", 1)[1],
                "the fixture must not leak the name {!r}".format(leak),
            )


if __name__ == "__main__":
    unittest.main()
