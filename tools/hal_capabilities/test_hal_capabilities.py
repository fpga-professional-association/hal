#!/usr/bin/env python3
"""Unit tests for hal_capabilities.

No HAL build, no netlist, no compiler: the netlist is a handful of stub objects
shaped like the ``hal_py`` bindings, exactly as the ``hal_findings`` adapter
tests do it.  What they cover is the part that has to be right before anything
else matters -- that an invalid declaration is rejected with a reason, and that
"supported", "partial" and "unsupported" mean three different things and are
told apart correctly.

    python3 -m unittest discover -s tools/hal_capabilities -t tools -p "test_*.py"
"""

import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_capabilities import cli, discover, support, validate  # noqa: E402
from hal_capabilities.schema import (  # noqa: E402
    CAPABILITIES_VERSION,
    SUPPORTED_CAPABILITIES_VERSIONS,
    load_schema,
    schema_path,
)
from hal_findings import jsonschema_mini  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLE_DECLARATION = os.path.join(
    REPO_ROOT, "plugins", "example_analysis", "capabilities.json"
)


def minimal_document(**overrides):
    document = {
        "capabilities_version": CAPABILITIES_VERSION,
        "plugin": {
            "name": "toy",
            "version": "0.1",
            "description": "A toy analysis.",
            "kind": "analysis",
        },
        "dependencies": {"plugins": []},
        "requires": {"gate_type_properties": {"all_of": ["sequential"]}},
        "supported_gate_libraries": {"mode": "any"},
        "findings": {"schema_version": "1.0.0", "statuses": ["heuristic"]},
    }
    document.update(overrides)
    return document


# ---------------------------------------------------------------------------
# stubs shaped like the hal_py bindings
# ---------------------------------------------------------------------------


class _Enum(object):
    def __init__(self, name):
        self.name = name


class StubPin(object):
    def __init__(self, name, pin_type, direction="input"):
        self._name = name
        self._type = _Enum(pin_type)
        self._direction = _Enum(direction)

    def get_name(self):
        return self._name

    def get_type(self):
        return self._type

    def get_direction(self):
        return self._direction


class StubGateType(object):
    def __init__(self, name, properties, pins=()):
        self._name = name
        self._properties = [_Enum(entry) for entry in properties]
        self._pins = list(pins)

    def get_name(self):
        return self._name

    def get_property_list(self):
        return list(self._properties)

    def get_pins(self, filter=None):
        return list(self._pins)


class StubGate(object):
    def __init__(self, gate_id, gate_type):
        self._id = gate_id
        self._type = gate_type

    def get_id(self):
        return self._id

    def get_type(self):
        return self._type


class StubLibrary(object):
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class StubNetlist(object):
    def __init__(self, gates, library_name="EXAMPLE_GATE_LIBRARY"):
        self._gates = list(gates)
        self._library = StubLibrary(library_name) if library_name else None

    def get_gates(self, filter=None):
        return list(self._gates)

    def get_gate_library(self):
        return self._library


DFF = StubGateType(
    "DFF", ["sequential", "ff"], [StubPin("CLK", "clock"), StubPin("D", "data")]
)
LATCH = StubGateType("LATCH", ["sequential", "latch"], [StubPin("EN", "enable")])
BUF = StubGateType("BUF", ["combinational", "c_buffer"], [StubPin("I", "none")])


def netlist_with(*type_counts, **kwargs):
    gates = []
    next_id = 1
    for gate_type, count in type_counts:
        for _ in range(count):
            gates.append(StubGate(next_id, gate_type))
            next_id += 1
    return StubNetlist(gates, **kwargs)


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


class SchemaTest(unittest.TestCase):
    def test_schema_file_exists_and_is_json(self):
        self.assertTrue(os.path.isfile(schema_path()))
        self.assertEqual(load_schema()["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_unknown_version_is_rejected_outright(self):
        with self.assertRaises(ValueError):
            schema_path("0.9.0")
        errors = validate.schema_errors(minimal_document(capabilities_version="9.9.9"))
        self.assertTrue(errors)
        self.assertIn("unsupported capabilities_version", errors[0])

    def test_schema_uses_only_keywords_the_builtin_validator_enforces(self):
        # Otherwise the schema could silently grow a constraint that is never checked.
        unsupported = jsonschema_mini.check_schema_support(load_schema())
        self.assertEqual(unsupported, [])

    def test_supported_versions_include_the_written_one(self):
        self.assertIn(CAPABILITIES_VERSION, SUPPORTED_CAPABILITIES_VERSIONS)


def _jsonschema_supports_2020_12():
    """The library validator is only exercised when it knows the draft."""
    try:
        import jsonschema
    except ImportError:
        return False
    return hasattr(jsonschema, "Draft202012Validator")


class ValidationTest(unittest.TestCase):
    def test_minimal_document_is_valid(self):
        self.assertEqual(validate.collect_errors(minimal_document()), [])

    def test_shipped_example_declaration_is_valid(self):
        with open(EXAMPLE_DECLARATION, "r") as handle:
            document = json.load(handle)
        self.assertEqual(validate.collect_errors(document), [])

    def test_missing_version(self):
        document = minimal_document()
        del document["capabilities_version"]
        self.assertIn("capabilities_version", validate.collect_errors(document)[0])

    def test_unknown_gate_type_property_is_rejected(self):
        document = minimal_document(
            requires={"gate_type_properties": {"all_of": ["flippy_flop"]}}
        )
        errors = validate.collect_errors(document)
        self.assertTrue(any("flippy_flop" in error for error in errors))

    def test_unknown_pin_type_is_rejected(self):
        document = minimal_document(
            requires={
                "gate_type_properties": {"all_of": ["sequential"]},
                "gate_type_pin_types": ["tick"],
            }
        )
        errors = validate.collect_errors(document)
        self.assertTrue(any("tick" in error for error in errors))

    def test_pin_type_without_a_property_is_meaningless(self):
        document = minimal_document(
            requires={"gate_type_properties": {}, "gate_type_pin_types": ["clock"]}
        )
        errors = validate.collect_errors(document)
        self.assertTrue(any("gate_type_pin_types" in error for error in errors))

    def test_empty_allow_list_is_rejected(self):
        document = minimal_document(
            supported_gate_libraries={"mode": "allow_list", "names": []}
        )
        errors = validate.collect_errors(document)
        self.assertTrue(any("allow list" in error for error in errors))

    def test_any_mode_with_names_is_rejected(self):
        document = minimal_document(
            supported_gate_libraries={"mode": "any", "names": ["LIB"]}
        )
        errors = validate.collect_errors(document)
        self.assertTrue(any("'any'" in error for error in errors))

    def test_analysis_must_declare_its_findings_statuses(self):
        document = minimal_document()
        del document["findings"]
        errors = validate.collect_errors(document)
        self.assertTrue(any("findings" in error for error in errors))

    def test_unknown_finding_status_is_rejected(self):
        document = minimal_document(
            findings={"schema_version": "1.0.0", "statuses": ["probably_fine"]}
        )
        self.assertTrue(validate.collect_errors(document))

    def test_additional_properties_are_rejected(self):
        document = minimal_document(surprise=True)
        self.assertTrue(validate.collect_errors(document))

    def test_validate_document_raises_with_every_problem(self):
        with self.assertRaises(validate.CapabilitiesValidationError) as context:
            validate.validate_document({"capabilities_version": CAPABILITIES_VERSION})
        self.assertTrue(context.exception.errors)

    @unittest.skipUnless(
        _jsonschema_supports_2020_12(), "jsonschema lacks draft 2020-12 support"
    )
    def test_builtin_validator_agrees_with_jsonschema(self):
        with open(EXAMPLE_DECLARATION, "r") as handle:
            good = json.load(handle)
        bad = copy.deepcopy(good)
        bad["plugin"]["kind"] = "nonsense"
        for document, expect_errors in ((good, False), (bad, True)):
            builtin = validate.schema_errors(document, prefer_jsonschema=False)
            library = validate.schema_errors(document, prefer_jsonschema=True)
            self.assertEqual(bool(builtin), expect_errors)
            self.assertEqual(bool(library), expect_errors)


# ---------------------------------------------------------------------------
# support
# ---------------------------------------------------------------------------


class NetlistProfileTest(unittest.TestCase):
    def test_profile_counts_types_properties_and_pin_types(self):
        profile = support.netlist_profile(netlist_with((DFF, 3), (BUF, 2)))
        self.assertEqual(profile["gate_count"], 5)
        self.assertEqual(profile["gate_library"], "EXAMPLE_GATE_LIBRARY")
        self.assertEqual(profile["gate_types"]["DFF"]["count"], 3)
        self.assertEqual(profile["gate_types"]["DFF"]["properties"], ["ff", "sequential"])
        self.assertEqual(profile["gate_types"]["DFF"]["pin_types"], ["clock", "data"])


class SupportEvaluationTest(unittest.TestCase):
    def setUp(self):
        self.document = minimal_document(
            requires={
                "gate_type_properties": {"all_of": ["sequential"]},
                "gate_type_pin_types": ["clock"],
                "netlist": {"min_gates": 1, "needs_gate_library": True},
            }
        )

    def evaluate(self, netlist, document=None):
        return support.evaluate(document or self.document, support.netlist_profile(netlist))

    def test_supported(self):
        report = self.evaluate(netlist_with((DFF, 4), (BUF, 1)))
        self.assertEqual(report.state, support.SUPPORTED)
        self.assertEqual(report.matched_gate_types, ["DFF"])
        self.assertEqual(report.unsupported_gate_types, [])
        self.assertTrue(report.ok)

    def test_partial_when_some_matched_types_lack_the_pin(self):
        report = self.evaluate(netlist_with((DFF, 4), (LATCH, 2)))
        self.assertEqual(report.state, support.PARTIAL)
        self.assertEqual(report.matched_gate_types, ["DFF"])
        self.assertEqual(len(report.unsupported_gate_types), 1)
        entry = report.unsupported_gate_types[0]
        self.assertEqual(entry["gate_type"], "LATCH")
        self.assertEqual(entry["count"], 2)
        self.assertEqual(entry["missing_pin_types"], ["clock"])
        self.assertIn("clock", entry["reason"])
        self.assertTrue(report.ok)

    def test_unsupported_when_no_matched_type_has_the_pin(self):
        report = self.evaluate(netlist_with((LATCH, 2)))
        self.assertEqual(report.state, support.UNSUPPORTED)
        self.assertFalse(report.ok)
        self.assertTrue(any("nothing this analysis can resolve" in r for r in report.reasons))

    def test_unsupported_when_the_property_is_absent_and_says_which(self):
        report = self.evaluate(netlist_with((BUF, 3)))
        self.assertEqual(report.state, support.UNSUPPORTED)
        self.assertTrue(any("'sequential'" in reason for reason in report.reasons))
        # The message has to be actionable: it names the library and the types present.
        self.assertTrue(any("BUF" in reason for reason in report.reasons))

    def test_any_of_is_satisfied_by_one_property(self):
        document = minimal_document(
            requires={"gate_type_properties": {"any_of": ["ram", "ff"]}}
        )
        self.assertEqual(self.evaluate(netlist_with((DFF, 1)), document).state, support.SUPPORTED)
        self.assertEqual(self.evaluate(netlist_with((BUF, 1)), document).state, support.UNSUPPORTED)

    def test_min_gates(self):
        document = minimal_document(
            requires={
                "gate_type_properties": {"all_of": ["sequential"]},
                "netlist": {"min_gates": 10},
            }
        )
        report = self.evaluate(netlist_with((DFF, 2)), document)
        self.assertEqual(report.state, support.UNSUPPORTED)
        self.assertTrue(any("at least 10" in reason for reason in report.reasons))

    def test_allow_list_rejects_a_foreign_library(self):
        document = minimal_document(
            supported_gate_libraries={"mode": "allow_list", "names": ["XILINX_UNISIM"]}
        )
        report = self.evaluate(netlist_with((DFF, 1)), document)
        self.assertEqual(report.state, support.UNSUPPORTED)
        self.assertTrue(any("allow list" in reason for reason in report.reasons))

    def test_deny_list_rejects_a_listed_library(self):
        document = minimal_document(
            supported_gate_libraries={"mode": "deny_list", "names": ["EXAMPLE_GATE_LIBRARY"]}
        )
        report = self.evaluate(netlist_with((DFF, 1)), document)
        self.assertEqual(report.state, support.UNSUPPORTED)

    def test_needs_gate_library(self):
        netlist = StubNetlist([StubGate(1, DFF)], library_name=None)
        report = self.evaluate(netlist)
        self.assertEqual(report.state, support.UNSUPPORTED)
        self.assertTrue(any("gate library" in reason for reason in report.reasons))

    def test_report_serializes(self):
        payload = self.evaluate(netlist_with((DFF, 1))).to_json()
        self.assertEqual(payload["state"], support.SUPPORTED)
        json.dumps(payload)

    def test_worst(self):
        self.assertEqual(support.worst([]), support.SUPPORTED)
        self.assertEqual(
            support.worst([support.SUPPORTED, support.PARTIAL]), support.PARTIAL
        )
        self.assertEqual(
            support.worst([support.UNSUPPORTED, support.PARTIAL]), support.UNSUPPORTED
        )


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="hal-capabilities-")
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_scans_the_real_plugin_tree(self):
        records = discover.scan_source_tree(REPO_ROOT)
        by_name = {record.name: record for record in records}
        self.assertIn("example_analysis", by_name)
        self.assertTrue(by_name["example_analysis"].declared)
        self.assertEqual(by_name["example_analysis"].capability_errors, [])
        self.assertEqual(by_name["example_analysis"].kind, "analysis")
        # A plugin that predates the format is listed, just not declared.
        self.assertIn("graph_algorithm", by_name)
        self.assertFalse(by_name["graph_algorithm"].declared)
        # Nothing is loadable until it has been probed, and that is a third state.
        self.assertIsNone(by_name["example_analysis"].loadable)
        self.assertEqual(by_name["example_analysis"].state_row()["loadable"], "not probed")

    def test_built_state_comes_from_the_build_tree(self):
        plugins = os.path.join(self.work, "lib", "hal_plugins")
        os.makedirs(plugins)
        open(os.path.join(plugins, "example_analysis.so"), "w").close()
        records = discover.scan_source_tree(REPO_ROOT, build_dir=self.work)
        by_name = {record.name: record for record in records}
        self.assertTrue(by_name["example_analysis"].built)
        self.assertFalse(by_name["graph_algorithm"].built)

    def test_installed_capabilities_can_be_read_without_a_source_tree(self):
        directory = os.path.join(self.work, discover.BUILD_CAPABILITY_SUBDIR)
        os.makedirs(directory)
        with open(os.path.join(directory, "toy.json"), "w") as handle:
            json.dump(minimal_document(), handle)
        document = discover.installed_capabilities(self.work, "toy")
        self.assertEqual(document["plugin"]["name"], "toy")
        self.assertIsNone(discover.installed_capabilities(self.work, "absent"))

    def test_broken_declaration_does_not_take_the_listing_down(self):
        path = os.path.join(self.work, "capabilities.json")
        with open(path, "w") as handle:
            handle.write("{not json")
        document, errors = discover.read_capabilities(path)
        self.assertIsNone(document)
        self.assertTrue(errors)

    def test_dependency_problems_name_a_missing_plugin(self):
        records = discover.scan_source_tree(REPO_ROOT)
        record = next(r for r in records if r.name == "example_analysis")
        record.capabilities = minimal_document(
            dependencies={"plugins": ["no_such_plugin"]}
        )
        problems = discover.dependency_problems(records)
        self.assertIn("example_analysis", problems)
        self.assertIn("no_such_plugin", problems["example_analysis"][0])

    def test_dependency_on_an_unbuilt_plugin_is_reported(self):
        records = discover.scan_source_tree(REPO_ROOT)
        by_name = {record.name: record for record in records}
        by_name["example_analysis"].capabilities = minimal_document(
            dependencies={"plugins": ["graph_algorithm"]}
        )
        by_name["example_analysis"].library_path = "/somewhere/example_analysis.so"
        problems = discover.dependency_problems(records)
        self.assertIn("not built", problems["example_analysis"][0])


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


class CommandLineTest(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        status = cli.main(argv, out=out, err=err)
        return status, out.getvalue(), err.getvalue()

    def test_list_defaults_to_the_repository(self):
        status, out, _err = self.run_cli(["list"])
        self.assertEqual(status, cli.EXIT_OK)
        self.assertIn("example_analysis", out)
        self.assertIn("declared", out)

    def test_list_json(self):
        status, out, _err = self.run_cli(["--json", "list"])
        self.assertEqual(status, cli.EXIT_OK)
        payload = json.loads(out)
        names = [entry["name"] for entry in payload["plugins"]]
        self.assertIn("example_analysis", names)
        self.assertEqual(payload["capabilities_version"], CAPABILITIES_VERSION)

    def test_list_declared_only(self):
        status, out, _err = self.run_cli(["list", "--declared-only"])
        self.assertEqual(status, cli.EXIT_OK)
        self.assertIn("example_analysis", out)
        self.assertNotIn("graph_algorithm", out)

    def test_show(self):
        status, out, _err = self.run_cli(["show", "example_analysis"])
        self.assertEqual(status, cli.EXIT_OK)
        self.assertEqual(json.loads(out)["plugin"]["name"], "example_analysis")

    def test_show_undeclared_plugin_explains_how_to_fix_it(self):
        status, _out, err = self.run_cli(["show", "graph_algorithm"])
        self.assertEqual(status, cli.EXIT_ERROR)
        self.assertIn("new_plugin.py", err)

    def test_show_unknown_plugin(self):
        status, _out, err = self.run_cli(["show", "not_a_plugin"])
        self.assertEqual(status, cli.EXIT_ERROR)
        self.assertIn("not_a_plugin", err)

    def test_validate_every_shipped_declaration(self):
        status, out, err = self.run_cli(["validate"])
        self.assertEqual(status, cli.EXIT_OK, err)
        self.assertIn("ok", out)

    def test_validate_reports_an_invalid_file(self):
        work = tempfile.mkdtemp(prefix="hal-capabilities-cli-")
        self.addCleanup(shutil.rmtree, work, True)
        path = os.path.join(work, "capabilities.json")
        with open(path, "w") as handle:
            json.dump({"capabilities_version": "1.0.0"}, handle)
        status, _out, err = self.run_cli(["validate", path])
        self.assertEqual(status, cli.EXIT_ERROR)
        self.assertIn("invalid", err)

    def test_schema_path(self):
        status, out, _err = self.run_cli(["schema", "--path"])
        self.assertEqual(status, cli.EXIT_OK)
        self.assertTrue(os.path.isfile(out.strip()))

    def test_schema_body(self):
        status, out, _err = self.run_cli(["schema"])
        self.assertEqual(status, cli.EXIT_OK)
        self.assertIn("capabilities_version", json.loads(out)["properties"])

    def test_check_without_hal_fails_with_a_message_not_a_traceback(self):
        status, _out, err = self.run_cli(
            ["--hal-lib", "/definitely/not/here", "check", "example_analysis",
             "--netlist", "/definitely/not/here/design.v"]
        )
        self.assertEqual(status, cli.EXIT_ERROR)
        self.assertIn("/definitely/not/here", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
