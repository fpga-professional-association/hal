"""Unit tests for hal_explain -- the whole workflow, without a HAL build.

Run from the repository root::

    python -m unittest discover -s tools/hal_explain -t tools -p "test_*.py"

The fixture netlist is read by ``hal_cdc``'s dependency-free reader, and the
three findings documents in ``fixtures/`` stand in for the analyses, so
everything except :mod:`hal_explain.collect` is exercised here: adapters,
merging, unknown regions, connectivity, the schema, the diagram and the report.
What these tests cannot cover is whether the real bindings still behave the way
the stubs do; that is
``tests/headless_smoke/explain_blocks_smoke.py``.
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(TOOLS)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from hal_cdc.fixture_netlist import load_fixture  # noqa: E402
from hal_findings import model as findings_model  # noqa: E402
from hal_findings import serialize as findings_serialize  # noqa: E402

from hal_explain import compose as compose_module  # noqa: E402
from hal_explain import diagram as diagram_module  # noqa: E402
from hal_explain import inventory as inventory_module  # noqa: E402
from hal_explain import model  # noqa: E402
from hal_explain import report as report_module  # noqa: E402
from hal_explain import serialize  # noqa: E402
from hal_explain import validate as validate_module  # noqa: E402
from hal_explain.adapters import common as adapter_common  # noqa: E402
from hal_explain.adapters import fsm as fsm_adapter  # noqa: E402
from hal_explain.adapters import module_identification as modid_adapter  # noqa: E402
from hal_explain.adapters import select_adapter  # noqa: E402
from hal_explain.cli import main as cli_main  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
NETLIST = os.path.join(FIXTURES, "accumulator.v")
GATE_LIBRARY = os.path.join(
    REPO_ROOT, "plugins", "gate_libraries", "definitions", "example_library.hgl"
)
GROUND_TRUTH = os.path.join(FIXTURES, "accumulator_ground_truth.json")
DOCUMENTS = [
    os.path.join(FIXTURES, "findings-dataflow.json"),
    os.path.join(FIXTURES, "findings-module-identification.json"),
    os.path.join(FIXTURES, "findings-fsm.json"),
]


def build_inventory():
    view = load_fixture(NETLIST, GATE_LIBRARY)
    return inventory_module.from_netlist_view(view, path=NETLIST)


def build_model(paths=None, **kwargs):
    inventory = build_inventory()
    sources = compose_module.load_sources(paths or DOCUMENTS)
    return compose_module.compose(
        inventory, sources, generated_at="2026-01-01T00:00:00Z", **kwargs
    )


def load_ground_truth():
    with open(GROUND_TRUTH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def gate_names(refs):
    return sorted(ref["name"] for ref in refs)


def block_by_gates(document, names):
    wanted = sorted(names)
    for block in document["blocks"]:
        if gate_names(block["gates"]) == wanted:
            return block
    return None


# ---------------------------------------------------------------------------
# the intermediate representation itself
# ---------------------------------------------------------------------------


class ModelTest(unittest.TestCase):
    def test_confidence_is_derived_from_status_only(self):
        self.assertEqual(
            model.confidence_for_status("proven_under_assumptions"), "verified"
        )
        self.assertEqual(model.confidence_for_status("proven_bounded"), "verified")
        self.assertEqual(model.confidence_for_status("heuristic"), "heuristic")
        self.assertEqual(model.confidence_for_status("counterexample"), "refuted")
        for status in ("unknown", "timeout", "error", "unsupported"):
            self.assertEqual(model.confidence_for_status(status), "unknown")

    def test_an_unknown_future_status_is_never_verified(self):
        self.assertEqual(model.confidence_for_status("proven_everything_ever"), "unknown")

    def test_a_claim_cannot_declare_a_confidence_its_status_does_not_support(self):
        evidence = [model.evidence_ref("s", "f", "heuristic")]
        with self.assertRaises(ValueError) as raised:
            model.claim("c", "text", "heuristic", [1], evidence, confidence="verified")
        self.assertIn("derived from the status", str(raised.exception))

    def test_a_claim_without_gates_or_evidence_is_refused(self):
        evidence = [model.evidence_ref("s", "f", "heuristic")]
        with self.assertRaises(ValueError):
            model.claim("c", "text", "heuristic", [], evidence)
        with self.assertRaises(ValueError):
            model.claim("c", "text", "heuristic", [1], [])

    def test_a_block_takes_the_strongest_claim_confidence(self):
        claims = [
            model.claim(
                "c1", "guess", "heuristic", [1], [model.evidence_ref("s", "f1", "heuristic")]
            ),
            model.claim(
                "c2",
                "proof",
                "proven_under_assumptions",
                [1],
                [model.evidence_ref("s", "f2", "proven_under_assumptions")],
            ),
        ]
        block = model.block("b", "register", "label", [model.gate_ref("n", 1, "g")], claims)
        self.assertEqual(block["confidence"], "verified")
        self.assertFalse(block["contested"])

    def test_a_refuted_claim_marks_the_block_contested(self):
        claims = [
            model.claim(
                "c1",
                "refuted",
                "counterexample",
                [1],
                [model.evidence_ref("s", "f1", "counterexample")],
            )
        ]
        block = model.block("b", "register", "label", [model.gate_ref("n", 1, "g")], claims)
        self.assertTrue(block["contested"])
        self.assertEqual(block["confidence"], "refuted")

    def test_coverage_that_does_not_add_up_is_refused(self):
        with self.assertRaises(ValueError) as raised:
            model.coverage(10, 5, 3)
        self.assertIn("does not add up", str(raised.exception))


# ---------------------------------------------------------------------------
# the inventory
# ---------------------------------------------------------------------------


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.inventory = build_inventory()

    def test_it_sees_every_gate_and_net_of_the_fixture(self):
        truth = load_ground_truth()
        self.assertEqual(len(self.inventory.gates), truth["gate_count"])
        self.assertEqual(len(self.inventory.nets), truth["net_count"])

    def test_gate_type_semantics_come_from_the_library(self):
        acc = self.inventory.gate_id_by_name("acc_r0")
        self.assertTrue(self.inventory.is_sequential(acc))
        self.assertFalse(self.inventory.is_sequential(self.inventory.gate_id_by_name("a_x0")))
        self.assertTrue(self.inventory.is_constant(self.inventory.gate_id_by_name("u_gnd")))

    def test_control_pins_are_recognised_by_pin_type(self):
        acc = self.inventory.gate_id_by_name("acc_r0")
        self.assertTrue(self.inventory.is_control_pin(acc, "C"))
        self.assertTrue(self.inventory.is_control_pin(acc, "CE"))
        self.assertFalse(self.inventory.is_control_pin(acc, "D"))

    def test_a_tie_cell_output_counts_as_a_constant_net(self):
        const0 = self.inventory.net(
            [nid for nid, net in self.inventory.nets.items() if net["name"] == "const0"][0]
        )
        self.assertTrue(self.inventory.is_constant_net(const0["id"]))

    def test_it_round_trips_through_json(self):
        payload = json.loads(json.dumps(self.inventory.to_json()))
        restored = inventory_module.from_json(payload)
        self.assertEqual(restored.to_json(), self.inventory.to_json())

    def test_an_unsupported_version_is_refused(self):
        payload = self.inventory.to_json()
        payload["inventory_version"] = "9.9.9"
        with self.assertRaises(inventory_module.InventoryError):
            inventory_module.from_json(payload)


# ---------------------------------------------------------------------------
# adapter selection and gate resolution
# ---------------------------------------------------------------------------


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.inventory = build_inventory()

    def test_each_document_selects_its_own_adapter(self):
        for path, expected in zip(
            DOCUMENTS, ["dataflow", "module_identification", "fsm"]
        ):
            document = findings_serialize.read_document(path)
            name, _module = select_adapter(document)
            self.assertEqual(name, expected, path)

    def test_an_unrecognisable_document_is_an_error_not_a_guess(self):
        document = {"analysis": {"plugin": {"name": "something_else"}}, "findings": []}
        with self.assertRaises(KeyError):
            select_adapter(document)

    def test_an_explicit_adapter_wins(self):
        document = findings_serialize.read_document(DOCUMENTS[0])
        name, _module = select_adapter(document, "fsm")
        self.assertEqual(name, "fsm")

    def test_gates_resolve_by_id_and_fall_back_to_name(self):
        source = adapter_common.SourceDocument("s", {"findings": []})
        by_id = adapter_common.resolve_gates(
            [{"artifact_id": "netlist", "id": self.inventory.gate_id_by_name("acc_r0"),
              "name": "acc_r0"}],
            self.inventory,
            source,
            "test",
        )
        self.assertEqual(by_id, [self.inventory.gate_id_by_name("acc_r0")])
        by_name = adapter_common.resolve_gates(
            [{"artifact_id": "netlist", "id": 9999, "name": "acc_r1"}],
            self.inventory,
            source,
            "test",
        )
        self.assertEqual(by_name, [self.inventory.gate_id_by_name("acc_r1")])
        self.assertEqual(source.unresolved, [])

    def test_an_unresolvable_gate_is_recorded_not_dropped_silently(self):
        source = adapter_common.SourceDocument("s", {"findings": []})
        resolved = adapter_common.resolve_gates(
            [{"artifact_id": "netlist", "id": 9999, "name": "no_such_gate"}],
            self.inventory,
            source,
            "finding/1",
        )
        self.assertEqual(resolved, [])
        self.assertEqual(len(source.unresolved), 1)
        self.assertIn("no_such_gate", source.unresolved[0])

    def test_module_identification_type_names_map_to_block_kinds(self):
        self.assertEqual(modid_adapter.block_kind_for_types(["addition"]), "arithmetic")
        self.assertEqual(modid_adapter.block_kind_for_types(["counter"]), "counter")
        self.assertEqual(modid_adapter.block_kind_for_types(["less_than"]), "comparator")
        self.assertEqual(modid_adapter.block_kind_for_types(["none"]), "other")
        self.assertEqual(
            modid_adapter.block_kind_for_types(["addition", "less_than"]), "datapath"
        )

    def test_fsm_machine_ids_ignore_the_non_machine_segments(self):
        self.assertEqual(fsm_adapter.machine_id_of("fsm/machine01/transitions"), "machine01")
        self.assertIsNone(fsm_adapter.machine_id_of("fsm/candidate/001"))
        self.assertIsNone(fsm_adapter.machine_id_of("fsm/coverage/uncovered"))
        self.assertIsNone(fsm_adapter.machine_id_of("dataflow/group/0001"))


# ---------------------------------------------------------------------------
# composition against the recorded ground truth
# ---------------------------------------------------------------------------


class ComposeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build_model()
        cls.truth = load_ground_truth()

    def test_the_model_validates(self):
        validate_module.validate_document(self.document)

    def test_every_expected_block_is_present_with_the_expected_confidence(self):
        for expected in self.truth["blocks"]:
            block = block_by_gates(self.document, expected["gates"])
            self.assertIsNotNone(block, "no block with the gate set of " + expected["id"])
            self.assertEqual(block["kind"], expected["kind"], expected["id"])
            self.assertEqual(block["confidence"], expected["confidence"], expected["id"])
            if expected.get("operation"):
                self.assertEqual(
                    (block.get("attributes") or {}).get("operation"), expected["operation"]
                )

    def test_dana_and_fsm_agreeing_on_a_gate_set_produce_one_block_with_both_claims(self):
        expected = [entry for entry in self.truth["blocks"] if entry["id"] == "controller"][0]
        block = block_by_gates(self.document, expected["gates"])
        self.assertEqual(block["kind"], "state_machine")
        self.assertEqual(
            sorted(claim["confidence"] for claim in block["claims"]),
            sorted(expected["claim_confidences"]),
        )
        sources = {
            ref["source_id"]
            for claim in block["claims"]
            for ref in claim["evidence"]
        }
        self.assertEqual(sources, {"findings-dataflow", "findings-fsm"})

    def test_a_heuristic_register_stays_heuristic_next_to_a_verified_adder(self):
        register = block_by_gates(self.document, ["acc_r0", "acc_r1", "acc_r2", "acc_r3"])
        self.assertEqual(register["confidence"], "heuristic")
        adder = block_by_gates(
            self.document,
            [entry for entry in self.truth["blocks"] if entry["id"] == "adder"][0]["gates"],
        )
        self.assertEqual(adder["confidence"], "verified")

    def test_an_unverified_candidate_is_inconclusive_not_absent(self):
        parity = block_by_gates(self.document, ["p_x0", "p_x1", "p_x2"])
        self.assertEqual(parity["confidence"], "unknown")
        self.assertEqual(len(parity["claims"]), 1)
        self.assertEqual(parity["claims"][0]["status"], "unknown")
        self.assertIn("not a statement that", parity["claims"][0]["text"])

    def test_unclaimed_gates_survive_as_unknown_regions(self):
        actual = sorted(
            gate_names(region["gates"]) for region in self.document["unknown_regions"]
        )
        expected = sorted(sorted(entry["gates"]) for entry in self.truth["unknown_regions"])
        self.assertEqual(actual, expected)

    def test_the_controller_next_state_logic_is_unclassified_and_visible(self):
        region = [
            entry
            for entry in self.document["unknown_regions"]
            if "f_mux" in gate_names(entry["gates"])
        ]
        self.assertEqual(len(region), 1)
        self.assertEqual(
            gate_names(region[0]["gates"]), sorted(["f_nb", "f_nf", "f_mux", "f_da", "f_db"])
        )

    def test_the_overlap_is_recorded_rather_than_resolved_silently(self):
        overlapping = sorted(entry["gate"]["name"] for entry in self.document["overlaps"])
        self.assertEqual(overlapping, sorted(self.truth["overlap_gates"]))
        for entry in self.document["overlaps"]:
            self.assertEqual(len(entry["block_ids"]), 2)
            # the verified block owns the gate for connectivity purposes
            self.assertTrue(entry["owner"].startswith("block/arithmetic/"))

    def test_coverage_matches_the_ground_truth(self):
        for key, value in self.truth["coverage"].items():
            self.assertEqual(self.document["coverage"][key], value, key)

    def test_every_gate_is_either_in_a_block_or_in_a_region(self):
        inventory = build_inventory()
        in_blocks = {ref["id"] for block in self.document["blocks"] for ref in block["gates"]}
        in_regions = {
            ref["id"] for region in self.document["unknown_regions"] for ref in region["gates"]
        }
        self.assertEqual(in_blocks & in_regions, set())
        self.assertEqual(in_blocks | in_regions, set(inventory.gate_ids()))

    def test_edges_only_connect_declared_nodes(self):
        nodes = (
            {block["block_id"] for block in self.document["blocks"]}
            | {region["region_id"] for region in self.document["unknown_regions"]}
            | {port["port_id"] for port in self.document["ports"]}
        )
        for edge in self.document["edges"]:
            self.assertIn(edge["source"], nodes)
            self.assertIn(edge["target"], nodes)

    def test_a_clock_only_edge_is_marked_control(self):
        control = [
            edge
            for edge in self.document["edges"]
            if edge["source"] == "port/inputs"
            and edge["target"].startswith("block/state_machine/")
        ]
        self.assertEqual(len(control), 1)
        self.assertEqual(control[0]["kind"], "control")

    def test_every_claim_links_a_gate_set_and_a_declared_source(self):
        source_ids = {source["source_id"] for source in self.document["sources"]}
        for block in self.document["blocks"]:
            for claim in block["claims"]:
                self.assertTrue(claim["gates"])
                self.assertTrue(claim["evidence"])
                for evidence in claim["evidence"]:
                    self.assertIn(evidence["source_id"], source_ids)
                    self.assertTrue(evidence["finding_id"])

    def test_undischarged_assumptions_travel_with_the_verified_claims(self):
        adder = block_by_gates(
            self.document,
            [entry for entry in self.truth["blocks"] if entry["id"] == "adder"][0]["gates"],
        )
        evidence = adder["claims"][0]["evidence"][0]
        self.assertIn("candidate-boundary", evidence["open_assumptions"])

    def test_composition_is_deterministic(self):
        again = build_model()
        self.assertEqual(serialize.dumps(self.document), serialize.dumps(again))
        self.assertEqual(
            serialize.document_digest(self.document), serialize.document_digest(again)
        )

    def test_composing_no_documents_makes_the_whole_netlist_unknown(self):
        inventory = build_inventory()
        document = compose_module.compose(inventory, [], generated_at="2026-01-01T00:00:00Z")
        validate_module.validate_document(document)
        self.assertEqual(document["blocks"], [])
        self.assertEqual(document["coverage"]["gates_in_blocks"], 0)
        self.assertEqual(document["coverage"]["gates_unclassified"], len(inventory.gates))
        self.assertTrue(
            any("one big unknown region" in note for note in document["notes"])
        )

    def test_a_findings_document_about_another_netlist_is_reported_not_absorbed(self):
        inventory = build_inventory()
        document = findings_serialize.read_document(DOCUMENTS[0])
        for finding in document["findings"]:
            for ref in finding.get("scope", {}).get("gates", []):
                ref["id"] = ref["id"] + 5000
                ref["name"] = "alien_" + ref["name"]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "findings-alien.json")
            findings_serialize.write_document(document, path)
            sources = compose_module.load_sources([path])
            composed = compose_module.compose(
                inventory, sources, generated_at="2026-01-01T00:00:00Z"
            )
        validate_module.validate_document(composed)
        self.assertEqual(composed["blocks"], [])
        self.assertTrue(composed["sources"][0]["unresolved_gates"])
        self.assertTrue(
            any("could not be resolved" in note for note in composed["notes"])
        )


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.document = build_model()

    def test_a_promoted_confidence_is_caught(self):
        self.document["blocks"][0]["claims"][0]["confidence"] = "verified"
        self.document["blocks"][0]["confidence"] = "verified"
        errors = validate_module.collect_errors(self.document)
        self.assertTrue(any("derived from the status" in error for error in errors))

    def test_a_gate_in_a_block_and_in_a_region_is_caught(self):
        block = self.document["blocks"][0]
        self.document["unknown_regions"][0]["gates"].append(dict(block["gates"][0]))
        errors = validate_module.collect_errors(self.document)
        self.assertTrue(
            any("classified or not" in error for error in errors), errors
        )

    def test_evidence_pointing_at_an_undeclared_source_is_caught(self):
        self.document["blocks"][0]["claims"][0]["evidence"][0]["source_id"] = "nowhere"
        errors = validate_module.collect_errors(self.document)
        self.assertTrue(any("does not declare" in error for error in errors))

    def test_an_edge_to_a_node_that_does_not_exist_is_caught(self):
        self.document["edges"].append(
            {"source": "block/ghost/0001", "target": "port/outputs", "net_count": 1}
        )
        errors = validate_module.collect_errors(self.document)
        self.assertTrue(any("not a block, unknown region" in error for error in errors))

    def test_coverage_tampering_is_caught(self):
        self.document["coverage"]["gates_in_blocks"] += 1
        self.document["coverage"]["gates_unclassified"] -= 1
        errors = validate_module.collect_errors(self.document)
        self.assertTrue(any("distinct gates appear in" in error for error in errors), errors)

    def test_an_unsupported_schema_version_is_refused(self):
        self.document["schema_version"] = "9.9.9"
        with self.assertRaises(validate_module.BlockModelValidationError):
            validate_module.collect_errors(self.document)


# ---------------------------------------------------------------------------
# diagram and report
# ---------------------------------------------------------------------------


class DiagramTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build_model()
        cls.dot = diagram_module.block_diagram(cls.document).to_dot()

    def test_every_block_and_every_region_is_drawn(self):
        for block in self.document["blocks"]:
            self.assertIn('"{}"'.format(block["block_id"]), self.dot)
        for region in self.document["unknown_regions"]:
            self.assertIn('"{}"'.format(region["region_id"]), self.dot)

    def test_confidences_are_visually_distinct(self):
        verified = diagram_module.CONFIDENCE_STYLE["verified"]
        heuristic = diagram_module.CONFIDENCE_STYLE["heuristic"]
        self.assertNotEqual(verified["style"], heuristic["style"])
        self.assertNotEqual(verified["color"], heuristic["color"])
        self.assertIn('peripheries="2"', self.dot)
        self.assertIn("filled,dashed", self.dot)

    def test_the_legend_explains_the_encoding_inside_the_drawing(self):
        self.assertIn("cluster_legend", self.dot)
        self.assertIn("a decision procedure proved it", self.dot)
        self.assertIn("gates no analysis claimed", self.dot)

    def test_overlapping_blocks_are_linked_so_neither_looks_orphaned(self):
        self.assertIn("shares 4 gate(s)", self.dot)

    def test_it_is_deterministic(self):
        self.assertEqual(self.dot, diagram_module.block_diagram(self.document).to_dot())

    def test_it_is_written_with_lf_endings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "blocks.dot")
            diagram_module.write_block_diagram(self.document, path)
            with open(path, "rb") as handle:
                self.assertNotIn(b"\r\n", handle.read())


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = build_model()
        cls.text = report_module.build_report(cls.document)

    def test_verified_and_heuristic_have_separate_sections(self):
        self.assertIn("## Verified blocks", self.text)
        self.assertIn("## Heuristic blocks", self.text)
        self.assertIn("## Inconclusive blocks", self.text)
        self.assertLess(
            self.text.index("## Verified blocks"), self.text.index("## Heuristic blocks")
        )

    def test_every_claim_prints_its_gate_set_and_its_finding(self):
        for block in self.document["blocks"]:
            for claim in block["claims"]:
                self.assertIn(claim["text"], self.text)
                for evidence in claim["evidence"]:
                    self.assertIn(
                        "{}#{}".format(evidence["source_id"], evidence["finding_id"]),
                        self.text,
                    )

    def test_undischarged_assumptions_are_printed_next_to_the_proof(self):
        self.assertIn("assumptions NOT discharged", self.text)
        self.assertIn("candidate-boundary", self.text)

    def test_unknown_regions_are_a_section_of_their_own(self):
        self.assertIn("## Unclassified regions (3)", self.text)
        self.assertIn("f_mux", self.text)
        self.assertIn("not explained by any analysis", self.text)

    def test_the_overlap_is_explained(self):
        self.assertIn("## Overlapping claims", self.text)
        self.assertIn("diagram owner", self.text)

    def test_it_says_it_used_no_language_model(self):
        self.assertIn("nothing here was written by a language model", self.text)

    def test_the_evidence_index_lists_every_finding(self):
        self.assertIn("## Evidence index", self.text)
        self.assertIn("fsm/machine01/transitions", self.text)

    def test_it_is_deterministic(self):
        self.assertEqual(self.text, report_module.build_report(self.document))


# ---------------------------------------------------------------------------
# fixtures and CLI
# ---------------------------------------------------------------------------


class FixtureTest(unittest.TestCase):
    def test_the_recorded_findings_documents_are_up_to_date(self):
        script = os.path.join(FIXTURES, "_generate.py")
        completed = subprocess.run(
            [sys.executable, script, "--check"],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            completed.returncode,
            0,
            "the recorded findings fixtures are stale; rerun "
            "tools/hal_explain/fixtures/_generate.py\n"
            + completed.stderr.decode("utf-8", "replace"),
        )

    def test_the_recorded_digests_do_not_depend_on_the_checkout(self):
        """The recorded hashes are of the LF form of the inputs, on every OS.

        A Windows checkout has CRLF line endings in the working tree, so hashing
        the files where they lie would record a different digest there than on
        Linux and the fixtures could only ever match one of the two.  The
        generator hashes the canonical LF bytes instead; this pins that, and
        fails on a Windows checkout if anyone reverts it.
        """
        recorded = {}
        for path in DOCUMENTS:
            document = findings_serialize.read_document(path)
            for artifact in document["artifacts"]:
                if artifact.get("sha256"):
                    recorded[artifact["path"]] = artifact["sha256"]
                library = artifact.get("gate_library") or {}
                if library.get("sha256"):
                    recorded[library["path"]] = library["sha256"]
        self.assertEqual(
            sorted(recorded),
            [
                "plugins/gate_libraries/definitions/example_library.hgl",
                "tools/hal_explain/fixtures/accumulator.v",
            ],
        )
        for relative, digest in sorted(recorded.items()):
            source = os.path.join(REPO_ROOT, *relative.split("/"))
            with open(source, "rb") as handle:
                data = handle.read().replace(b"\r\n", b"\n")
            self.assertEqual(
                hashlib.sha256(data).hexdigest(),
                digest,
                relative + " is recorded with a digest of something else",
            )

    def test_the_ground_truth_matches_the_fixture_netlist(self):
        truth = load_ground_truth()
        inventory = build_inventory()
        self.assertEqual(len(inventory.gates), truth["gate_count"])
        for entry in truth["blocks"] + truth["unknown_regions"]:
            for name in entry["gates"]:
                self.assertIsNotNone(
                    inventory.gate_id_by_name(name),
                    "ground truth names a gate the fixture does not have: " + name,
                )


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.inventory_path = os.path.join(self.tmp, "inventory.json")
        self.model_path = os.path.join(self.tmp, "blocks.json")
        # Several of these tests drive the CLI down its *failure* paths on
        # purpose, and the CLI is right to describe those failures on stderr.
        # Captured rather than printed, so that a passing run stays quiet and
        # the diagnostics can be asserted on instead of merely being read.
        self.captured = io.StringIO()
        patched = contextlib.redirect_stderr(self.captured)
        patched.__enter__()
        self.addCleanup(patched.__exit__, None, None, None)

    def stderr(self):
        return self.captured.getvalue()

    def _inventory(self):
        code = cli_main(
            [
                "--quiet",
                "inventory",
                NETLIST,
                "--gate-library",
                GATE_LIBRARY,
                "--fixture-reader",
                "-o",
                self.inventory_path,
            ]
        )
        self.assertEqual(code, 0)

    def _compose(self, extra=()):
        arguments = ["--quiet", "compose", "--inventory", self.inventory_path]
        for path in DOCUMENTS:
            arguments += ["--findings", path]
        arguments += ["-o", self.model_path, "--generated-at", "2026-01-01T00:00:00Z"]
        return cli_main(arguments + list(extra))

    def test_the_whole_pipeline_runs_from_the_command_line(self):
        self._inventory()
        dot = os.path.join(self.tmp, "blocks.dot")
        report = os.path.join(self.tmp, "report.md")
        self.assertEqual(self._compose(["--dot", dot, "--report", report]), 0)
        self.assertTrue(os.path.isfile(self.model_path))
        self.assertTrue(os.path.isfile(dot))
        self.assertTrue(os.path.isfile(report))
        self.assertEqual(cli_main(["--quiet", "validate", self.model_path]), 0)
        self.assertEqual(
            cli_main(["--quiet", "diagram", self.model_path, "-o", dot + "2"]), 0
        )
        self.assertEqual(
            cli_main(["--quiet", "report", self.model_path, "-o", report + "2"]), 0
        )

    def test_min_classified_below_the_threshold_exits_one(self):
        self._inventory()
        self.assertEqual(self._compose(["--min-classified", "0.99"]), 1)
        self.assertIn("--min-classified", self.stderr())
        self.assertEqual(self._compose(["--min-classified", "0.5"]), 0)

    def test_a_missing_inventory_is_exit_two_not_exit_one(self):
        code = cli_main(
            [
                "--quiet",
                "compose",
                "--inventory",
                os.path.join(self.tmp, "nope.json"),
                "--findings",
                DOCUMENTS[0],
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("could not read the inventory", self.stderr())

    def test_an_invalid_model_fails_validation_with_exit_one(self):
        """A hand-promoted confidence is caught -- the composer never emits one.

        ``model.claim`` derives ``confidence`` from ``status``, so the only way
        to get the contradiction the validator rejects is to edit the composed
        document, which is what this test does.  The rejection message it
        provokes is expected output, not a failure of the run.
        """
        self._inventory()
        self.assertEqual(self._compose(), 0)
        document = serialize.read_document(self.model_path)
        for block in document["blocks"]:
            for claim in block["claims"]:
                self.assertEqual(
                    claim["confidence"],
                    model.confidence_for_status(claim["status"]),
                    "the composed model must derive confidence from status",
                )
        promoted = [
            claim
            for block in document["blocks"]
            for claim in block["claims"]
            if claim["confidence"] == "heuristic"
        ]
        self.assertTrue(promoted, "the fixture must contain a heuristic claim")
        promoted[0]["confidence"] = "verified"
        serialize.write_document(document, self.model_path)
        self.assertEqual(cli_main(["--quiet", "validate", self.model_path]), 1)
        self.assertIn(
            "confidence is derived from the status and must be 'heuristic'",
            self.stderr(),
        )

    def test_prose_emits_a_bundle_and_calls_no_model(self):
        self._inventory()
        self.assertEqual(self._compose(), 0)
        bundle_path = os.path.join(self.tmp, "prose.json")
        self.assertEqual(
            cli_main(["--quiet", "prose", self.model_path, "-o", bundle_path]), 0
        )
        with open(bundle_path, "r", encoding="utf-8") as handle:
            bundle = json.load(handle)
        self.assertIn("Paraphrase ONLY the claims below", bundle["instructions"])
        self.assertTrue(bundle["blocks"])
        self.assertTrue(bundle["unknown_regions"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
