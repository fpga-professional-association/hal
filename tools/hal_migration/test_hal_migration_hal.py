"""Integration tests that need a built HAL; skipped everywhere else.

``test_hal_migration.py`` drives the inventory extractor with a stub netlist
built from ``ice40ultra.hgl``. That stub is a *model* of what HAL reports, and a
model can be wrong in exactly the way that matters here: if HAL assigns a pin
type this file does not expect, a migration report silently loses a clock or a
reset. These tests remove that doubt by running the same extraction through the
real bindings and comparing the result with the checked-in fixture:

1. parse ``fixtures/ice40_mixed.v`` with ``plugins/gate_libraries/definitions/
   ice40ultra.hgl`` and check the inventory against
   ``fixtures/ice40_mixed_inventory.json`` -- gate types, counts, categories,
   pin metadata, clock/reset signals, I/O ports and metadata gaps;
2. assess it against the bundled catalogue and check that the same five
   supported, six candidate and four unresolved gate types come out, that
   SB_RAM40_4K is downgraded because the gate library models no RAM component,
   and that SB_DFFNSR is unresolved because the catalogue does not list it;
3. load the shipped ``examples/uart.zip`` -- a netlist of a *different* gate
   library -- and check that assessing it against the iCE40 catalogue reports
   the library mismatch and resolves nothing, instead of quietly producing an
   empty-looking success.

Run inside the project's build/verification container, from the repo root::

    export HAL_PY_PATH=/path/to/hal/build/lib
    python -m unittest discover -s tools/hal_migration -t tools -p "test_*_hal.py"

Set ``HAL_MIGRATION_OUTPUT_DIR`` to keep the produced documents as CI artifacts.
"""

import json
import os
import sys
import tempfile
import unittest
import zipfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_findings import model as findings_model  # noqa: E402
from hal_findings import serialize as findings_serialize  # noqa: E402
from hal_findings import validate as findings_validate  # noqa: E402

from hal_migration import assess as assess_module  # noqa: E402
from hal_migration import catalogue as catalogue_module  # noqa: E402
from hal_migration import formats  # noqa: E402
from hal_migration import inventory as inventory_module  # noqa: E402
from hal_migration import report as report_module  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
FIXTURES = os.path.join(HERE, "fixtures")
NETLIST = os.path.join(FIXTURES, "ice40_mixed.v")
GATE_LIBRARY = os.path.join(
    REPO_ROOT, "plugins", "gate_libraries", "definitions", "ice40ultra.hgl"
)
CATALOGUE = os.path.join(HERE, "catalogues", "ice40ultra-to-generic-fpga-1.0.0.json")
UART_ARCHIVE = os.path.join(REPO_ROOT, "examples", "uart.zip")
OUTPUT_ENV = "HAL_MIGRATION_OUTPUT_DIR"


def _requirements():
    try:
        from hal_viz import halenv
    except ImportError as exc:
        return None, "tools/hal_viz is not importable: {}".format(exc)
    try:
        hal_py = halenv.import_hal_py()
    except Exception as exc:
        return None, "hal_py unavailable: {}".format(exc)
    if not os.path.isfile(GATE_LIBRARY):
        return None, "{} is missing".format(GATE_LIBRARY)
    return (hal_py, halenv), None


_REQUIREMENTS, _SKIP_REASON = _requirements()


def _fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


@unittest.skipIf(_REQUIREMENTS is None, _SKIP_REASON or "HAL is unavailable")
class MigrationAssessmentIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hal_py, cls.halenv = _REQUIREMENTS
        cls.output_dir = os.environ.get(OUTPUT_ENV)
        if cls.output_dir:
            os.makedirs(cls.output_dir, exist_ok=True)
            cls._temp = None
        else:
            cls._temp = tempfile.TemporaryDirectory()
            cls.output_dir = cls._temp.name

        # HAL's netlist and gate library parsers ship as plugins; nothing can be
        # read from a '.v' or '.hgl' file until they are registered.
        cls.halenv.load_all_plugins(cls.hal_py)
        netlist = cls.halenv.load_netlist(cls.hal_py, NETLIST, GATE_LIBRARY)
        cls.inventory = inventory_module.build_inventory(
            netlist,
            netlist_path=NETLIST,
            source_tool={"name": "hand-written fixture", "version": "1.0.0"},
            vendor="Lattice",
            family="iCE40 UltraPlus",
            device="iCE40UP5K",
        )
        formats.write_json(
            cls.inventory, os.path.join(cls.output_dir, "ice40_mixed.inventory.json")
        )
        cls.catalogue = catalogue_module.load(CATALOGUE)
        cls.document = assess_module.build_document(
            cls.inventory, cls.catalogue, catalogue_path=CATALOGUE
        )
        findings_serialize.write_document(
            cls.document, os.path.join(cls.output_dir, "ice40_mixed.assessment.json")
        )
        with open(
            os.path.join(cls.output_dir, "ice40_mixed.report.md"), "w",
            encoding="utf-8", newline="\n",
        ) as handle:
            handle.write(report_module.render_markdown(cls.document, inventory=cls.inventory))
        cls.expected = _fixture("ice40_mixed_inventory.json")

    @classmethod
    def tearDownClass(cls):
        if cls._temp is not None:
            cls._temp.cleanup()

    # ---- inventory -------------------------------------------------------

    def test_inventory_validates(self):
        formats.validate(self.inventory, "inventory")

    def test_gate_type_counts_match_the_fixture(self):
        actual = {
            entry["gate_type"]: entry["count"] for entry in self.inventory["primitives"]
        }
        expected = {
            entry["gate_type"]: entry["count"] for entry in self.expected["primitives"]
        }
        self.assertEqual(actual, expected)
        self.assertEqual(self.inventory["totals"]["gates"], 19)

    def test_categories_match_the_fixture(self):
        actual = {
            entry["gate_type"]: entry["category"] for entry in self.inventory["primitives"]
        }
        expected = {
            entry["gate_type"]: entry["category"] for entry in self.expected["primitives"]
        }
        self.assertEqual(actual, expected)

    def test_pin_metadata_matches_the_fixture(self):
        actual = inventory_module.primitive_by_type(self.inventory)
        expected = inventory_module.primitive_by_type(self.expected)
        for gate_type, entry in expected.items():
            for field in (
                "clock_pins",
                "reset_pins",
                "set_pins",
                "enable_pins",
                "address_pins",
                "io_pad_pins",
                "state_pins",
                "lut_input_count",
                "input_pin_count",
                "output_pin_count",
            ):
                self.assertEqual(
                    (actual[gate_type].get("metadata") or {}).get(field),
                    (entry.get("metadata") or {}).get(field),
                    "{}.{} differs between HAL and the fixture".format(gate_type, field),
                )

    def test_flip_flop_functions_come_from_the_gate_library(self):
        actual = inventory_module.primitive_by_type(self.inventory)
        # SB_DFFR models its reset with clear_on -> an asynchronous clear
        self.assertEqual(actual["SB_DFFR"]["metadata"].get("async_reset_function"), "R")
        # SB_DFFSR folds the reset into the next state -> no async clear at all
        self.assertIsNone(actual["SB_DFFSR"]["metadata"].get("async_reset_function"))
        self.assertIn("clock_function", actual["SB_DFF"]["metadata"])

    def test_memory_metadata_is_genuinely_absent(self):
        memory = inventory_module.primitive_by_type(self.inventory)["SB_RAM40_4K"]
        self.assertNotIn("bit_size", memory["metadata"])
        self.assertNotIn("ram_ports", memory["metadata"])
        self.assertIn("no RAMComponent", " ".join(memory["metadata_gaps"]))

    def test_clock_reset_and_io_structure_matches_the_fixture(self):
        for field in ("clock_signals", "reset_signals", "io_ports"):
            self.assertEqual(
                self.inventory["totals"][field],
                self.expected["totals"][field],
                "{} differs between HAL and the fixture".format(field),
            )
        clocks = {signal["net_name"] for signal in self.inventory["clock_signals"]}
        self.assertEqual(clocks, {"clk_g", "osc_clk"})
        resets = self.inventory["reset_signals"]
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0]["sink_count"], 4)
        directions = {port["direction"] for port in self.inventory["io_ports"]}
        self.assertIn("inout", directions)

    # ---- assessment ------------------------------------------------------

    def test_assessment_is_a_valid_findings_document_without_proofs(self):
        findings_validate.validate_document(self.document)
        for finding in self.document["findings"]:
            self.assertIn(
                finding["status"],
                (
                    findings_model.STATUS_HEURISTIC,
                    findings_model.STATUS_UNSUPPORTED,
                    findings_model.STATUS_UNKNOWN,
                ),
                finding["id"],
            )
            self.assertFalse(findings_model.is_unbounded_proof(finding))

    def test_categories_match_the_expected_assessment(self):
        summary = assess_module.summarize(self.document)
        self.assertEqual(summary["supported"], 5)
        self.assertEqual(summary["candidate"], 6)
        self.assertEqual(
            summary["unresolved_types"],
            ["SB_DFFNSR", "SB_HFOSC", "SB_I2C", "SB_RAM40_4K"],
        )
        self.assertEqual(summary["downgraded_types"], ["SB_RAM40_4K"])
        self.assertEqual(summary["not_in_catalogue"], ["SB_DFFNSR"])

    def test_design_level_obligations_are_open(self):
        design = [
            finding
            for finding in self.document["findings"]
            if finding["id"] == "migration/obligations/design-level"
        ][0]
        ids = {obligation["id"] for obligation in design["data"]["obligations"]}
        self.assertIn("physical/timing-closure", ids)
        self.assertIn("semantic/functional-equivalence", ids)
        self.assertIn("semantic/memory-collision-mode", ids)

    # ---- a netlist from a different gate library --------------------------

    def test_wrong_gate_library_is_reported_not_silently_assessed(self):
        if not os.path.isfile(UART_ARCHIVE):
            self.skipTest("{} is missing".format(UART_ARCHIVE))
        work_dir = os.path.join(self.output_dir, "uart")
        with zipfile.ZipFile(UART_ARCHIVE) as archive:
            for name in archive.namelist():
                self.assertFalse(os.path.isabs(name) or ".." in name.split("/"))
            archive.extractall(work_dir)
        project_dir = os.path.join(work_dir, "uart")
        self.assertTrue(os.path.isdir(project_dir))

        netlist = self.halenv.load_netlist(self.hal_py, project_dir)
        inventory = inventory_module.build_inventory(netlist, netlist_path=project_dir)
        formats.validate(inventory, "inventory")
        self.assertEqual(inventory["totals"]["gates"], 407)
        self.assertEqual(inventory["totals"]["gate_types"], 10)
        by_type = inventory_module.primitive_by_type(inventory)
        self.assertEqual(by_type["FFR"]["count"], 258)
        self.assertEqual(by_type["FFR"]["category"], "register")
        self.assertEqual(by_type["LUT4"]["category"], "combinational")

        document = assess_module.build_document(
            inventory, self.catalogue, catalogue_path=CATALOGUE
        )
        findings_validate.validate_document(document)
        summary = assess_module.summarize(document)
        # Only the two constant drivers match, and only because 'GND'/'VCC' are
        # the same *names* in both libraries. That accidental match is exactly
        # why the applicability finding below has to be emitted.
        self.assertEqual(summary["supported"], 2)
        self.assertEqual(summary["candidate"], 0)
        self.assertEqual(summary["unresolved"], 8)
        self.assertIn("FFR", summary["unresolved_types"])
        applicability = [
            finding
            for finding in document["findings"]
            if finding["id"] == "migration/catalogue/applicability"
        ]
        self.assertEqual(len(applicability), 1)
        self.assertEqual(applicability[0]["status"], findings_model.STATUS_UNKNOWN)
        self.assertIn("EXAMPLE_GATE_LIBRARY", applicability[0]["summary"])

        findings_serialize.write_document(
            document, os.path.join(self.output_dir, "uart.assessment.json")
        )


if __name__ == "__main__":
    unittest.main()
