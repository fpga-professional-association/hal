"""Unit tests for hal_migration. No HAL build required.

The inventory extractor is exercised against a stub netlist built from the real
``ice40ultra.hgl`` gate library (see ``fixtures/stub_netlist.py``), so these
tests check the actual primitive semantics HAL would report rather than a
hand-typed idea of them. ``test_hal_migration_hal.py`` closes the remaining gap
by running the same checks through real ``hal_py`` bindings.

Run from the repository root::

    python -m unittest discover -s tools/hal_migration -t tools -p "test_*.py"
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

from hal_findings import model as findings_model
from hal_findings import serialize as findings_serialize
from hal_findings import validate as findings_validate
from hal_findings import jsonschema_mini

from hal_migration import assess as assess_module
from hal_migration import catalogue as catalogue_module
from hal_migration import cli
from hal_migration import formats
from hal_migration import inventory as inventory_module
from hal_migration import obligations as obligations_module
from hal_migration import report as report_module
from hal_migration.fixtures import _generate
from hal_migration.fixtures import stub_netlist

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
GATE_LIBRARY = os.path.join(
    REPO_ROOT, "plugins", "gate_libraries", "definitions", "ice40ultra.hgl"
)
CATALOGUE_PATH = os.path.join(
    HERE, "catalogues", "ice40ultra-to-generic-fpga-1.0.0.json"
)

#: statuses an assessment is allowed to use. A migration assessment is
#: structural; it may never claim a proof of any kind.
ALLOWED_STATUSES = {
    findings_model.STATUS_HEURISTIC,
    findings_model.STATUS_UNSUPPORTED,
    findings_model.STATUS_UNKNOWN,
}


@contextlib.contextmanager
def in_repo_root():
    previous = os.getcwd()
    os.chdir(REPO_ROOT)
    try:
        yield
    finally:
        os.chdir(previous)


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


class SchemaTest(unittest.TestCase):
    def test_schemas_use_only_supported_keywords(self):
        for kind in ("inventory", "catalogue"):
            schema = formats.load_schema(kind)
            jsonschema_mini.check_schema_support(schema)

    def test_schema_path_rejects_unknown_version(self):
        with self.assertRaises(ValueError):
            formats.schema_path("inventory", "0.9.0")
        with self.assertRaises(ValueError):
            formats.schema_path("nonsense")

    def test_unknown_document_version_is_rejected_outright(self):
        document = load_fixture("ice40_mixed_inventory.json")
        document["inventory_version"] = "2.0.0"
        errors = formats.collect_errors(document)
        self.assertTrue(errors)
        self.assertIn("unsupported inventory format version", errors[0])

    def test_document_kind_detection(self):
        self.assertEqual(
            formats.document_kind(load_fixture("ice40_mixed_inventory.json")), "inventory"
        )
        with open(CATALOGUE_PATH, "r", encoding="utf-8") as handle:
            self.assertEqual(formats.document_kind(json.load(handle)), "catalogue")
        self.assertIsNone(formats.document_kind({"hello": "world"}))

    def test_shipped_fixtures_and_catalogue_validate(self):
        formats.validate(load_fixture("ice40_mixed_inventory.json"), "inventory")
        formats.validate(load_fixture("incomplete_metadata_inventory.json"), "inventory")
        with open(CATALOGUE_PATH, "r", encoding="utf-8") as handle:
            formats.validate(json.load(handle), "catalogue")

    def test_built_in_validator_agrees_with_jsonschema(self):
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("the optional jsonschema library is not installed")
        documents = [
            ("inventory", load_fixture("ice40_mixed_inventory.json")),
            ("inventory", load_fixture("incomplete_metadata_inventory.json")),
        ]
        with open(CATALOGUE_PATH, "r", encoding="utf-8") as handle:
            documents.append(("catalogue", json.load(handle)))
        for kind, document in documents:
            self.assertEqual(formats.collect_errors(document, kind=kind), [])
            self.assertEqual(
                formats.collect_errors(document, kind=kind, prefer_jsonschema=True), []
            )
        # and both must reject the same broken document
        broken = load_fixture("ice40_mixed_inventory.json")
        broken["primitives"][0]["category"] = "not-a-category"
        self.assertTrue(formats.collect_errors(broken, kind="inventory"))
        self.assertTrue(
            formats.collect_errors(broken, kind="inventory", prefer_jsonschema=True)
        )

    def test_write_json_is_deterministic(self):
        document = load_fixture("ice40_mixed_inventory.json")
        first = formats.dumps(document)
        second = formats.dumps(json.loads(first))
        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))


class CatalogueTest(unittest.TestCase):
    def setUp(self):
        self.catalogue = catalogue_module.load(CATALOGUE_PATH)

    def test_bundled_catalogue_is_discoverable_by_id(self):
        bundled = catalogue_module.bundled_catalogues()
        self.assertIn("ice40ultra-to-generic-fpga", bundled)
        self.assertEqual(
            os.path.abspath(catalogue_module.resolve_path("ice40ultra-to-generic-fpga")),
            os.path.abspath(CATALOGUE_PATH),
        )

    def test_catalogue_records_its_own_revision_and_review(self):
        self.assertTrue(self.catalogue["revision"])
        review = self.catalogue["review"]
        self.assertTrue(review["reviewed_by"])
        self.assertTrue(review["reviewed_at"])
        self.assertTrue(review["limitations"])
        self.assertTrue(self.catalogue["target"]["toolchain"]["version"])

    def test_every_proposed_mapping_carries_obligations(self):
        for mapping in self.catalogue["mappings"]:
            if mapping["category"] in ("supported", "candidate"):
                self.assertTrue(
                    mapping.get("obligations"),
                    "{} is {} without a single obligation".format(
                        mapping["source_type"], mapping["category"]
                    ),
                )
                self.assertTrue(mapping.get("target_primitive"))
            else:
                self.assertTrue(mapping.get("unresolved_reasons"))

    def test_duplicate_source_type_is_rejected(self):
        with open(CATALOGUE_PATH, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        document["mappings"].append(dict(document["mappings"][0]))
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(document, handle)
            path = handle.name
        try:
            with self.assertRaises(catalogue_module.CatalogueError):
                catalogue_module.load(path)
        finally:
            os.unlink(path)

    def test_dangling_obligation_id_is_rejected(self):
        document = {
            "catalogue_version": "1.0.0",
            "catalogue_id": "broken",
            "revision": "1",
            "review": {"reviewed_by": "x", "reviewed_at": "2026-01-01", "method": "y"},
            "source": {"vendor": "a", "family": "b"},
            "target": {"vendor": "c", "family": "d"},
            "mappings": [
                {
                    "source_type": "CELL",
                    "category": "supported",
                    "target_primitive": "TARGET",
                    "rationale": "because",
                    "obligations": ["semantic/does-not-exist"],
                }
            ],
        }
        formats.validate(document, "catalogue")
        with self.assertRaises(catalogue_module.CatalogueError):
            catalogue_module.check_obligation_ids(document)

    def test_library_mismatch_is_reported(self):
        inventory = load_fixture("ice40_mixed_inventory.json")
        self.assertIsNone(catalogue_module.library_mismatch(inventory, self.catalogue))
        inventory["source"]["gate_library"] = {"name": "XILINX_UNISIM"}
        message = catalogue_module.library_mismatch(inventory, self.catalogue)
        self.assertIn("XILINX_UNISIM", message)
        del inventory["source"]["gate_library"]
        self.assertIn("could not be checked", catalogue_module.library_mismatch(inventory, self.catalogue))


class InventoryExtractionTest(unittest.TestCase):
    """Drive the extractor with a stub netlist built from the real gate library."""

    @classmethod
    def setUpClass(cls):
        with in_repo_root():
            cls.inventory = _generate.build_stub_inventory()
        cls.by_type = inventory_module.primitive_by_type(cls.inventory)

    def test_inventory_validates(self):
        formats.validate(self.inventory, "inventory")

    def test_counts_match_the_fixture_netlist(self):
        totals = self.inventory["totals"]
        self.assertEqual(totals["gates"], 19)
        self.assertEqual(totals["gate_types"], 15)
        self.assertEqual(totals["clock_signals"], 2)
        self.assertEqual(totals["reset_signals"], 1)
        self.assertEqual(totals["io_ports"], 35)
        self.assertEqual(self.by_type["SB_DFF"]["count"], 3)
        self.assertEqual(self.by_type["SB_DFFR"]["count"], 2)
        self.assertEqual(self.by_type["SB_LUT4"]["count"], 2)

    def test_categories_come_from_gate_type_properties(self):
        expected = {
            "SB_LUT4": "combinational",
            "SB_DFF": "register",
            "SB_DFFR": "register",
            "SB_RAM40_4K": "memory",
            "SB_MAC16": "arithmetic",
            "SB_CARRY": "arithmetic",
            "SB_IO": "io",
            "SB_GB": "combinational",
            "SB_I2C": "sequential_other",
            "GND": "constant",
            "VCC": "constant",
        }
        for gate_type, category in expected.items():
            self.assertEqual(
                self.by_type[gate_type]["category"], category, gate_type
            )

    def test_categorize_never_reads_the_name(self):
        self.assertEqual(inventory_module.categorize([]), "black_box")
        self.assertEqual(inventory_module.categorize(["sequential", "ram"]), "memory")
        self.assertEqual(inventory_module.categorize(["sequential", "dsp"]), "arithmetic")
        self.assertEqual(inventory_module.categorize(["sequential"]), "sequential_other")
        self.assertEqual(inventory_module.categorize(["combinational"]), "combinational")
        self.assertEqual(inventory_module.categorize(["io"]), "io")

    def test_register_metadata_is_extracted(self):
        register = self.by_type["SB_DFFR"]
        metadata = register["metadata"]
        self.assertEqual(metadata["clock_pins"], ["C"])
        self.assertEqual(metadata["reset_pins"], ["R"])
        self.assertEqual(metadata["data_pins"], ["D"])
        self.assertEqual(metadata["clock_function"], "C")
        # the library models R with clear_on, i.e. as an asynchronous clear
        self.assertEqual(metadata["async_reset_function"], "R")
        self.assertEqual(metadata["init"]["identifiers"], ["INIT"])

    def test_synchronous_reset_is_reported_as_a_gap_not_as_a_fact(self):
        gaps = " ".join(self.by_type["SB_DFFSR"]["metadata_gaps"])
        self.assertIn("not modelled as an asynchronous clear", gaps)
        self.assertNotIn("asynchronous clear", " ".join(self.by_type["SB_DFFR"].get("metadata_gaps", [])))

    def test_memory_metadata_gaps_are_explicit(self):
        memory = self.by_type["SB_RAM40_4K"]
        self.assertNotIn("bit_size", memory["metadata"])
        self.assertNotIn("ram_ports", memory["metadata"])
        gaps = " ".join(memory["metadata_gaps"])
        self.assertIn("no RAMComponent", gaps)
        self.assertIn("read-during-write", gaps)
        self.assertEqual(len(memory["metadata"]["clock_pins"]), 2)
        self.assertEqual(len(memory["metadata"]["address_pins"]), 22)

    def test_clock_and_reset_signals(self):
        clocks = {signal["net_name"]: signal for signal in self.inventory["clock_signals"]}
        self.assertEqual(set(clocks), {"clk_g", "osc_clk"})
        self.assertEqual(clocks["clk_g"]["driver"]["gate_type"], "SB_GB")
        self.assertEqual(clocks["clk_g"]["sink_count"], 10)
        self.assertIn("clock_source", clocks["osc_clk"]["pin_types"])

        resets = self.inventory["reset_signals"]
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0]["net_name"], "rst")
        self.assertEqual(resets[0]["sink_count"], 4)
        self.assertTrue(resets[0]["is_global_input"])

    def test_inout_port_is_one_port_not_two(self):
        ports = {port["net_name"]: port for port in self.inventory["io_ports"]}
        self.assertEqual(ports["pad"]["direction"], "inout")
        self.assertTrue(ports["pad"]["through_io_primitive"])
        self.assertEqual(ports["clk_pin"]["direction"], "input")
        self.assertFalse(ports["clk_pin"]["through_io_primitive"])

    def test_missing_source_tool_is_recorded_as_a_gap(self):
        with in_repo_root():
            netlist = stub_netlist.build_netlist(
                GATE_LIBRARY,
                [("ff", "SB_DFF", {"C": "clk", "D": "d", "Q": "q"})],
                inputs=["clk", "d"],
                outputs=["q"],
            )
            document = inventory_module.build_inventory(netlist)
        formats.validate(document, "inventory")
        fields = {gap.get("field") for gap in document["metadata_gaps"]}
        self.assertIn("source.tool", fields)
        self.assertNotIn("tool", document["source"])

    def test_gate_type_without_properties_is_a_black_box(self):
        gate_type = stub_netlist.StubGateType(
            "CUSTOM_HARD_BLOCK",
            [],
            [stub_netlist.StubPin("A", "input", "none")],
            [],
        )
        gate = stub_netlist.StubGate(1, "u_custom", gate_type, None)
        netlist = stub_netlist.StubNetlist(None, "d", "", "")
        netlist.gates.append(gate)
        document = inventory_module.build_inventory(netlist)
        formats.validate(document, "inventory")
        primitive = document["primitives"][0]
        self.assertEqual(primitive["category"], "black_box")
        self.assertIn("no properties at all", " ".join(primitive["metadata_gaps"]))
        self.assertEqual(document["totals"]["black_box_types"], 1)

    def test_checked_in_inventory_fixture_is_up_to_date(self):
        self.assertEqual(
            formats.dumps(self.inventory),
            formats.dumps(load_fixture("ice40_mixed_inventory.json")),
            "fixtures/ice40_mixed_inventory.json is stale; regenerate it with "
            "python tools/hal_migration/fixtures/_generate.py",
        )


class MappingResolutionTest(unittest.TestCase):
    def setUp(self):
        self.catalogue = catalogue_module.load(CATALOGUE_PATH)
        self.mappings = catalogue_module.mappings_by_type(self.catalogue)
        self.inventory = load_fixture("ice40_mixed_inventory.json")
        self.by_type = inventory_module.primitive_by_type(self.inventory)

    def test_uncatalogued_primitive_is_unresolved(self):
        primitive = self.by_type["SB_DFFNSR"]
        self.assertNotIn("SB_DFFNSR", self.mappings)
        resolution = catalogue_module.resolve_mapping(primitive, None)
        self.assertEqual(resolution["category"], "unresolved")
        self.assertTrue(resolution["uncatalogued"])
        self.assertIn("no entry in this catalogue", " ".join(resolution["reasons"]))

    def test_missing_metadata_downgrades_a_candidate(self):
        primitive = self.by_type["SB_RAM40_4K"]
        resolution = catalogue_module.resolve_mapping(
            primitive, self.mappings["SB_RAM40_4K"]
        )
        self.assertEqual(resolution["declared_category"], "candidate")
        self.assertEqual(resolution["category"], "unresolved")
        self.assertTrue(resolution["downgraded"])
        self.assertEqual(resolution["missing_metadata"], ["bit_size", "ram_ports"])

    def test_metadata_present_keeps_the_declared_category(self):
        resolution = catalogue_module.resolve_mapping(
            self.by_type["SB_DFF"], self.mappings["SB_DFF"]
        )
        self.assertEqual(resolution["category"], "supported")
        self.assertFalse(resolution["downgraded"])
        self.assertEqual(resolution["missing_metadata"], [])

    def test_black_box_can_never_be_supported(self):
        primitive = {
            "gate_type": "CUSTOM_HARD_BLOCK",
            "count": 1,
            "category": "black_box",
            "properties": [],
        }
        mapping = {
            "source_type": "CUSTOM_HARD_BLOCK",
            "category": "supported",
            "target_primitive": "SOMETHING",
            "rationale": "someone was optimistic",
            "obligations": ["semantic/black-box-behaviour"],
        }
        resolution = catalogue_module.resolve_mapping(primitive, mapping)
        self.assertEqual(resolution["category"], "unresolved")
        self.assertTrue(resolution["downgraded"])
        self.assertIn("no properties at all", " ".join(resolution["reasons"]))

    def test_empty_metadata_value_counts_as_missing(self):
        primitive = {
            "gate_type": "X",
            "count": 1,
            "category": "memory",
            "properties": ["ram"],
            "metadata": {"bit_size": 0, "ram_ports": []},
        }
        self.assertIsNone(inventory_module.metadata_value(primitive, "bit_size"))
        self.assertIsNone(inventory_module.metadata_value(primitive, "ram_ports"))


class ObligationTest(unittest.TestCase):
    def test_every_builtin_obligation_is_complete(self):
        for obligation_id, entry in obligations_module.BUILTIN_OBLIGATIONS.items():
            self.assertEqual(entry["id"], obligation_id)
            self.assertIn(entry["kind"], ("semantic", "physical"))
            for field in ("title", "check", "severity", "target_assumption"):
                self.assertTrue(entry[field], "{} has no {}".format(obligation_id, field))

    def test_unknown_obligation_id_raises(self):
        with self.assertRaises(KeyError):
            obligations_module.obligation("semantic/invented")

    def test_catalogue_template_wins_over_builtin(self):
        templates = {
            "physical/timing-closure": {
                "id": "physical/timing-closure",
                "kind": "physical",
                "title": "overridden",
                "check": "do something else",
            }
        }
        resolved = obligations_module.resolve(["physical/timing-closure"], templates)
        self.assertEqual(resolved[0]["title"], "overridden")
        self.assertEqual(resolved[0]["status"], "open")

    def test_design_level_always_includes_timing_and_equivalence(self):
        derived = obligations_module.derive_design_level({"primitives": []})
        for obligation_id in obligations_module.ALWAYS_OPEN:
            self.assertIn(obligation_id, derived)


class AssessmentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = load_fixture("ice40_mixed_inventory.json")
        cls.catalogue = catalogue_module.load(CATALOGUE_PATH)
        cls.document = assess_module.build_document(
            cls.inventory,
            cls.catalogue,
            catalogue_path=CATALOGUE_PATH,
            generated_at="2026-09-08T00:00:00Z",
        )
        cls.findings = {finding["id"]: finding for finding in cls.document["findings"]}

    def test_document_is_a_valid_findings_document(self):
        findings_validate.validate_document(self.document)

    def test_no_finding_claims_a_proof(self):
        for finding in self.document["findings"]:
            self.assertIn(finding["status"], ALLOWED_STATUSES, finding["id"])
            self.assertFalse(findings_model.is_unbounded_proof(finding))
            self.assertNotIn("counterexample", finding)
            self.assertEqual(finding["method"]["kind"], "structural")
            self.assertFalse(finding["method"]["bounded"])

    def test_all_three_categories_are_present(self):
        summary = assess_module.summarize(self.document)
        self.assertEqual(summary["supported"], 5)
        self.assertEqual(summary["candidate"], 6)
        self.assertEqual(summary["unresolved"], 4)
        self.assertEqual(
            summary["unresolved_types"],
            ["SB_DFFNSR", "SB_HFOSC", "SB_I2C", "SB_RAM40_4K"],
        )
        self.assertEqual(summary["downgraded_types"], ["SB_RAM40_4K"])
        self.assertEqual(summary["not_in_catalogue"], ["SB_DFFNSR"])

    def test_unresolved_primitives_are_unsupported_findings_naming_the_gate_type(self):
        for gate_type in ("SB_DFFNSR", "SB_HFOSC", "SB_I2C", "SB_RAM40_4K"):
            finding = self.findings["migration/primitive/{}".format(gate_type)]
            self.assertEqual(finding["status"], findings_model.STATUS_UNSUPPORTED)
            self.assertEqual(finding["unsupported"]["kind"], "primitive")
            named = [entry["gate_type"] for entry in finding["unsupported"]["primitives"]]
            self.assertEqual(named, [gate_type])
            self.assertTrue(finding["unsupported"]["primitives"][0]["reason"])

    def test_every_proposed_mapping_states_checks_and_target_assumptions(self):
        proposed = [
            finding
            for finding in self.document["findings"]
            if finding["id"].startswith("migration/primitive/")
            and finding["status"] == findings_model.STATUS_HEURISTIC
        ]
        self.assertTrue(proposed)
        for finding in proposed:
            obligations = finding["data"]["obligations"]
            self.assertTrue(obligations, finding["id"])
            for obligation in obligations:
                self.assertTrue(obligation["check"])
                self.assertEqual(obligation["status"], "open")
            self.assertTrue(finding["assumptions"])
            for assumption in finding["assumptions"]:
                self.assertFalse(assumption["discharged"])
            self.assertTrue(finding["data"]["mapping"]["target_primitive"])

    def test_ram_collision_reset_clocking_and_io_are_retained_as_obligations(self):
        required = {
            "semantic/memory-collision-mode",
            "semantic/memory-init-contents",
            "semantic/async-set-reset-priority",
            "semantic/power-up-and-init-state",
            "physical/clock-network-and-buffering",
            "physical/io-standard-and-drive",
            "physical/io-pin-assignment",
        }
        present = set()
        for finding in self.document["findings"]:
            for obligation in finding.get("data", {}).get("obligations") or []:
                present.add(obligation["id"])
        self.assertTrue(required.issubset(present), sorted(required - present))

    def test_timing_closure_is_always_open_and_never_claimed(self):
        design = self.findings["migration/obligations/design-level"]
        self.assertEqual(design["status"], findings_model.STATUS_UNKNOWN)
        ids = {obligation["id"] for obligation in design["data"]["obligations"]}
        for obligation_id in obligations_module.ALWAYS_OPEN:
            self.assertIn(obligation_id, ids)
        text = json.dumps(self.document).lower()
        for phrase in ("conversion succeeded", "successfully converted", "meets timing", "timing closed"):
            self.assertNotIn(phrase, text)

    def test_catalogue_and_source_versions_are_recorded(self):
        artifacts = {artifact["artifact_id"]: artifact for artifact in self.document["artifacts"]}
        self.assertIn("ice40ultra-to-generic-fpga", artifacts)
        catalogue_artifact = artifacts["ice40ultra-to-generic-fpga"]
        self.assertIn(self.catalogue["revision"], catalogue_artifact["description"])
        self.assertTrue(catalogue_artifact["sha256"])
        configuration = self.document["analysis"]["configuration"]
        self.assertEqual(configuration["catalogue_revision"], self.catalogue["revision"])
        self.assertEqual(configuration["catalogue_version"], "1.0.0")
        self.assertEqual(configuration["inventory_version"], "1.0.0")
        self.assertEqual(configuration["source_tool"], self.inventory["source"]["tool"])
        self.assertTrue(configuration["target"]["toolchain"]["version"])

    def test_gate_references_are_scoped_to_the_source_artifact(self):
        source_id = self.inventory["source"]["artifact_id"]
        for finding in self.document["findings"]:
            for reference in finding["scope"].get("gates") or []:
                self.assertEqual(reference["artifact_id"], source_id)
                self.assertIn(source_id, finding["scope"]["artifact_ids"])

    def test_metadata_gaps_are_reported_as_unsupported_configuration(self):
        finding = self.findings["migration/metadata/gaps"]
        self.assertEqual(finding["status"], findings_model.STATUS_UNSUPPORTED)
        self.assertEqual(finding["unsupported"]["kind"], "configuration")
        self.assertTrue(finding["data"]["metadata_gaps"])

    def test_incomplete_metadata_fixture_yields_no_supported_mapping(self):
        document = assess_module.build_document(
            load_fixture("incomplete_metadata_inventory.json"),
            self.catalogue,
            catalogue_path=CATALOGUE_PATH,
            generated_at="2026-09-08T00:00:00Z",
        )
        findings_validate.validate_document(document)
        summary = assess_module.summarize(document)
        self.assertEqual(summary["supported"], 0)
        self.assertEqual(summary["candidate"], 0)
        self.assertEqual(summary["unresolved"], 4)
        self.assertIn("CUSTOM_HARD_BLOCK", summary["unresolved_types"])
        self.assertIn("SB_DFF", summary["downgraded_types"])
        applicability = [
            finding
            for finding in document["findings"]
            if finding["id"] == "migration/catalogue/applicability"
        ]
        self.assertFalse(applicability, "the gate libraries do match in this fixture")

    def test_assessment_is_deterministic(self):
        again = assess_module.build_document(
            self.inventory,
            self.catalogue,
            catalogue_path=CATALOGUE_PATH,
            generated_at="2026-09-08T00:00:00Z",
        )
        self.assertEqual(
            findings_serialize.document_digest(self.document),
            findings_serialize.document_digest(again),
        )

    def test_checked_in_assessment_fixture_is_up_to_date(self):
        with in_repo_root():
            expected = load_fixture("ice40_mixed_assessment.json")
            document = assess_module.build_document(
                self.inventory,
                catalogue_module.load(
                    "tools/hal_migration/catalogues/ice40ultra-to-generic-fpga-1.0.0.json"
                ),
                catalogue_path="tools/hal_migration/catalogues/"
                "ice40ultra-to-generic-fpga-1.0.0.json",
                generated_at="2026-09-08T00:00:00Z",
                producer_command=["python", "tools/hal_migration/fixtures/_generate.py"],
            )
        self.assertEqual(
            findings_serialize.document_digest(document),
            findings_serialize.document_digest(expected),
            "fixtures/ice40_mixed_assessment.json is stale; regenerate it with "
            "python tools/hal_migration/fixtures/_generate.py",
        )


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inventory = load_fixture("ice40_mixed_inventory.json")
        cls.document = assess_module.build_document(
            cls.inventory,
            catalogue_module.load(CATALOGUE_PATH),
            catalogue_path=CATALOGUE_PATH,
            generated_at="2026-09-08T00:00:00Z",
        )
        cls.text = report_module.render_markdown(cls.document, inventory=cls.inventory)

    def test_report_opens_with_what_it_is_not(self):
        self.assertIn("This is an assessment, not a conversion.", self.text)
        self.assertIn("no netlist was converted", self.text)

    def test_report_lists_every_category_and_the_versions(self):
        for heading in ("## Summary", "## Primitive mappings", "## Unresolved primitives",
                        "## Verification obligations", "## What this report does not say"):
            self.assertIn(heading, self.text)
        self.assertIn("revision `2026-09-08.1`", self.text)
        self.assertIn("| supported |", self.text)
        self.assertIn("| candidate |", self.text)
        self.assertIn("| unresolved |", self.text)

    def test_report_names_the_unresolved_primitives_and_their_reasons(self):
        self.assertIn("`SB_DFFNSR`", self.text)
        self.assertIn("Downgraded from `candidate`", self.text)
        self.assertIn("bit_size, ram_ports", self.text)

    def test_report_keeps_timing_closure_visible(self):
        self.assertIn("Timing closure in the target device", self.text)

    def test_checked_in_report_fixture_is_up_to_date(self):
        # regenerated with the repository-relative paths the fixture records, so
        # the check does not depend on where the checkout lives
        relative_catalogue = (
            "tools/hal_migration/catalogues/ice40ultra-to-generic-fpga-1.0.0.json"
        )
        with in_repo_root():
            document = assess_module.build_document(
                self.inventory,
                catalogue_module.load(relative_catalogue),
                catalogue_path=relative_catalogue,
                generated_at="2026-09-08T00:00:00Z",
                producer_command=["python", "tools/hal_migration/fixtures/_generate.py"],
            )
            text = report_module.render_markdown(document, inventory=self.inventory)
        with open(os.path.join(FIXTURES, "ice40_mixed_report.md"), "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), text)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.work_dir = tempfile.mkdtemp(prefix="hal_migration_test_")
        self.inventory_path = os.path.join(FIXTURES, "ice40_mixed_inventory.json")

    def tearDown(self):
        shutil.rmtree(self.work_dir, ignore_errors=True)

    def _run(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout, stderr
        try:
            code = cli.main(argv)
        finally:
            sys.stdout, sys.stderr = saved
        return code, stdout.getvalue(), stderr.getvalue()

    def test_assess_writes_a_valid_document_and_report(self):
        output = os.path.join(self.work_dir, "assessment.json")
        report_path = os.path.join(self.work_dir, "report.md")
        code, _stdout, _stderr = self._run(
            [
                "assess",
                "--inventory", self.inventory_path,
                "--catalogue", "ice40ultra-to-generic-fpga",
                "-o", output,
                "--report", report_path,
            ]
        )
        self.assertEqual(code, cli.EXIT_OK)
        document = findings_serialize.read_document(output)
        findings_validate.validate_document(document)
        with open(report_path, "r", encoding="utf-8") as handle:
            self.assertIn("This is an assessment, not a conversion.", handle.read())

    def test_fail_on_unresolved_exits_with_two(self):
        output = os.path.join(self.work_dir, "assessment.json")
        code, _stdout, stderr = self._run(
            [
                "assess",
                "--inventory", self.inventory_path,
                "--catalogue", "ice40ultra-to-generic-fpga",
                "-o", output,
                "--fail-on-unresolved",
            ]
        )
        self.assertEqual(code, cli.EXIT_UNRESOLVED)
        self.assertIn("SB_DFFNSR", stderr)

    def test_unresolved_alone_is_not_a_failure(self):
        output = os.path.join(self.work_dir, "assessment.json")
        code, _stdout, _stderr = self._run(
            [
                "assess",
                "--inventory", self.inventory_path,
                "--catalogue", "ice40ultra-to-generic-fpga",
                "-o", output,
            ]
        )
        self.assertEqual(code, cli.EXIT_OK)

    def test_validate_accepts_the_fixtures_and_rejects_a_broken_one(self):
        code, _stdout, _stderr = self._run(["validate", self.inventory_path, CATALOGUE_PATH])
        self.assertEqual(code, cli.EXIT_OK)

        broken = os.path.join(self.work_dir, "broken.json")
        document = load_fixture("ice40_mixed_inventory.json")
        del document["totals"]
        with open(broken, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        code, _stdout, stderr = self._run(["validate", broken])
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("invalid inventory", stderr)

    def test_missing_inventory_is_an_error_not_a_traceback(self):
        code, _stdout, stderr = self._run(
            ["assess", "--inventory", os.path.join(self.work_dir, "nope.json"),
             "--catalogue", "ice40ultra-to-generic-fpga"]
        )
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("hal_migration:", stderr)

    def test_unknown_catalogue_id_is_an_error(self):
        code, _stdout, stderr = self._run(
            ["assess", "--inventory", self.inventory_path, "--catalogue", "not-a-catalogue"]
        )
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("no catalogue", stderr)

    def test_catalogues_and_schema_commands(self):
        code, stdout, _stderr = self._run(["catalogues"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("ice40ultra-to-generic-fpga", stdout)

        code, stdout, _stderr = self._run(["schema", "--kind", "catalogue", "--path"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertTrue(os.path.isfile(stdout.strip()))

    def test_no_command_prints_help_and_fails(self):
        code, stdout, _stderr = self._run([])
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("usage:", stdout)


if __name__ == "__main__":
    unittest.main()
