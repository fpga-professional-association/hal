"""Standalone unit tests for hal_findings; no HAL build required.

They cover the schema (positive and negative cases), deterministic
serialization, the cross-reference rules, the model builders, both analysis
adapters (driven by stubs that mimic the ``hal_py`` accessors) and the CLI.
Run them with a plain interpreter from the repository root::

    python -m unittest discover -s tools/hal_findings -t tools -p "test_*.py"

or simply::

    python tools/hal_findings/test_hal_findings.py
"""

import copy
import io
import json
import os
import sys
import tempfile
import unittest

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_findings import cli, jsonschema_mini, model, serialize, validate  # noqa: E402
from hal_findings.adapters import dataflow as dataflow_adapter  # noqa: E402
from hal_findings.adapters import netlist_comparison as comparison_adapter  # noqa: E402
from hal_findings.examples import _generate  # noqa: E402
from hal_findings.schema import SCHEMA_VERSION, load_schema  # noqa: E402

EXAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples")

try:
    import jsonschema as _jsonschema
except ImportError:  # optional; the built-in validator is the fallback
    _jsonschema = None


# ---------------------------------------------------------------------------
# stub netlist objects (duck-typed against the hal_py bindings)
# ---------------------------------------------------------------------------


class StubGateType(object):
    def __init__(self, name, properties):
        self._name = name
        self._properties = list(properties)

    def get_name(self):
        return self._name

    def get_property_list(self):
        return list(self._properties)


class StubModule(object):
    def __init__(self, module_id, name):
        self._id = module_id
        self._name = name

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name


class StubGate(object):
    def __init__(self, gate_id, name, gate_type, module=None):
        self._id = gate_id
        self._name = name
        self._type = gate_type
        self._module = module

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_type(self):
        return self._type

    def get_module(self):
        return self._module


class StubNet(object):
    def __init__(self, net_id, name):
        self._id = net_id
        self._name = name

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name


class StubNetlist(object):
    def __init__(self, gates, nets=(), design="stub_design", netlist_id=1, filename=""):
        self._gates = list(gates)
        self._nets = list(nets)
        self._design = design
        self._id = netlist_id
        self._filename = filename

    def get_gates(self):
        return list(self._gates)

    def get_nets(self):
        return list(self._nets)

    def get_design_name(self):
        return self._design

    def get_device_name(self):
        return ""

    def get_id(self):
        return self._id

    def get_input_filename(self):
        return self._filename

    def get_gate_library(self):
        return None


class StubDataflowResult(object):
    """Mimics ``dataflow.Result`` as exposed by its python bindings."""

    def __init__(self, netlist, groups, successors=None, control_nets=None):
        self._netlist = netlist
        self._groups = groups
        self._successors = successors or {}
        self._control_nets = control_nets or {}

    def get_netlist(self):
        return self._netlist

    def get_groups(self):
        return {key: set(value) for key, value in self._groups.items()}

    def get_group_successors(self, group_id):
        return self._successors.get(group_id)

    def get_group_predecessors(self, group_id):
        return None

    def get_group_control_nets(self, group_id, pin_type):
        return self._control_nets.get((group_id, pin_type))


FF = StubGateType("FDRE", ["ff", "sequential"])
RAM = StubGateType("RAMB18E1", ["ram", "sequential"])
LUT = StubGateType("LUT4", ["combinational", "c_lut"])
DSP = StubGateType("DSP48E1", ["dsp"])


def find_by_status(document, status):
    """First finding with ``status``; documents are sorted by id, not by status."""
    matches = [finding for finding in document["findings"] if finding["status"] == status]
    if not matches:
        raise AssertionError("no {} finding in the document".format(status))
    return matches[0]


def _flip_flops(count, offset=100, prefix="state_reg"):
    module = StubModule(3, "top/core")
    return [
        StubGate(offset + index, "{}_{}".format(prefix, index), FF, module)
        for index in range(count)
    ]


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


class SchemaTest(unittest.TestCase):
    def test_schema_only_uses_supported_keywords(self):
        # Tripwire: if the schema starts using a keyword jsonschema_mini does
        # not implement, validation would silently weaken.
        self.assertEqual([], jsonschema_mini.check_schema_support(load_schema()))

    @unittest.skipIf(_jsonschema is None, "jsonschema not installed")
    def test_schema_is_valid_draft_2020_12(self):
        _jsonschema.Draft202012Validator.check_schema(load_schema())

    def test_schema_version_is_pinned_in_documents(self):
        document = _generate.equivalence_proof()
        self.assertEqual(SCHEMA_VERSION, document["schema_version"])


class ExamplesTest(unittest.TestCase):
    def _examples(self):
        for name in sorted(os.listdir(EXAMPLE_DIR)):
            if name.endswith(".json"):
                yield name, serialize.read_document(os.path.join(EXAMPLE_DIR, name))

    def test_every_shipped_example_validates(self):
        names = []
        for name, document in self._examples():
            names.append(name)
            self.assertEqual([], validate.collect_errors(document), name)
        self.assertEqual(sorted(_generate.EXAMPLES), names)

    @unittest.skipIf(_jsonschema is None, "jsonschema not installed")
    def test_examples_validate_with_the_real_jsonschema_library(self):
        for name, document in self._examples():
            self.assertEqual(
                [], validate.collect_errors(document, prefer_jsonschema=True), name
            )

    def test_examples_cover_every_status(self):
        seen = set()
        for _, document in self._examples():
            for finding in document["findings"]:
                seen.add(finding["status"])
        self.assertEqual(set(model.STATUSES), seen)

    def test_examples_come_from_two_different_analyses(self):
        plugins = {
            document["analysis"]["plugin"]["name"] for _, document in self._examples()
        }
        self.assertTrue({"z3_utils", "dataflow_analysis"} <= plugins, plugins)

    def test_checked_in_examples_are_byte_identical_to_regenerated_ones(self):
        for name, document in sorted(_generate.build_all().items()):
            with open(os.path.join(EXAMPLE_DIR, name), "r", encoding="utf-8") as handle:
                on_disk = handle.read()
            self.assertEqual(on_disk, serialize.dumps(document), name)


# ---------------------------------------------------------------------------
# negative schema cases
# ---------------------------------------------------------------------------


class SchemaNegativeTest(unittest.TestCase):
    """Every case must be rejected by the built-in *and* the real validator."""

    def setUp(self):
        self.base = _generate.equivalence_proof()
        self.assertEqual([], validate.collect_errors(self.base))

    def _reject(self, document, needle=None):
        errors = validate.collect_errors(document)
        self.assertTrue(errors, "document was accepted but should not be")
        if needle:
            self.assertTrue(
                any(needle in error for error in errors),
                "expected {!r} in {}".format(needle, errors),
            )
        if _jsonschema is not None:
            self.assertTrue(validate.collect_errors(document, prefer_jsonschema=True))

    def _mutate(self, mutation):
        document = copy.deepcopy(self.base)
        mutation(document["findings"][0], document)
        return document

    def test_unbounded_proof_may_not_carry_a_cycle_bound(self):
        def mutate(finding, _document):
            finding["bounds"]["cycle_bound"] = 8

        self._reject(self._mutate(mutate))

    def test_proof_may_not_be_marked_bounded(self):
        def mutate(finding, _document):
            finding["bounds"]["unbounded"] = False
            finding["bounds"]["cycle_bound"] = 8

        self._reject(self._mutate(mutate))

    def test_proof_requires_assumptions(self):
        def mutate(finding, _document):
            del finding["assumptions"]

        self._reject(self._mutate(mutate), "assumptions")

    def test_proof_may_not_carry_a_counterexample(self):
        def mutate(finding, _document):
            finding["counterexample"] = {"description": "nope"}

        self._reject(self._mutate(mutate))

    def test_bounded_counterexample_requires_a_cycle_bound(self):
        document = serialize.read_document(os.path.join(EXAMPLE_DIR, "bounded-results.json"))
        self.assertEqual([], validate.collect_errors(document))
        finding = [
            entry
            for entry in document["findings"]
            if entry["status"] == model.STATUS_BOUNDED_COUNTEREXAMPLE
        ][0]
        del finding["counterexample"]["cycle_bound"]
        self._reject(document, "cycle_bound")

    def test_bounded_result_may_not_claim_to_be_unbounded(self):
        document = serialize.read_document(os.path.join(EXAMPLE_DIR, "bounded-results.json"))
        finding = [
            entry
            for entry in document["findings"]
            if entry["status"] == model.STATUS_PROVEN_BOUNDED
        ][0]
        finding["bounds"]["unbounded"] = True
        self._reject(document)

    def test_unbounded_counterexample_may_not_carry_a_cycle_bound(self):
        document = serialize.read_document(os.path.join(EXAMPLE_DIR, "counterexample.json"))
        document["findings"][0]["counterexample"]["cycle_bound"] = 3
        self._reject(document)

    def test_heuristic_may_not_claim_unbounded_validity(self):
        document = serialize.read_document(
            os.path.join(EXAMPLE_DIR, "dataflow-heuristic.json")
        )
        find_by_status(document, model.STATUS_HEURISTIC)["bounds"] = {"unbounded": True}
        self._reject(document)

    def test_heuristic_may_not_use_a_formal_method(self):
        document = serialize.read_document(
            os.path.join(EXAMPLE_DIR, "dataflow-heuristic.json")
        )
        find_by_status(document, model.STATUS_HEURISTIC)["method"]["kind"] = "formal"
        self._reject(document)

    def test_timeout_requires_a_recorded_limit(self):
        document = serialize.read_document(
            os.path.join(EXAMPLE_DIR, "inconclusive-timeout-unknown-error.json")
        )
        finding = [
            entry for entry in document["findings"] if entry["status"] == model.STATUS_TIMEOUT
        ][0]
        del finding["limits"]["timeout_s"]
        self._reject(document, "timeout_s")

    def test_error_status_requires_an_error_object(self):
        document = serialize.read_document(
            os.path.join(EXAMPLE_DIR, "inconclusive-timeout-unknown-error.json")
        )
        finding = [
            entry for entry in document["findings"] if entry["status"] == model.STATUS_ERROR
        ][0]
        del finding["error"]
        self._reject(document, "error")

    def test_unsupported_primitive_kind_requires_a_primitive(self):
        document = serialize.read_document(
            os.path.join(EXAMPLE_DIR, "unsupported-primitives.json")
        )
        document["findings"][0]["unsupported"]["primitives"] = []
        self._reject(document)

    def test_unknown_status_string_is_rejected(self):
        def mutate(finding, _document):
            finding["status"] = "probably_fine"

        self._reject(self._mutate(mutate))

    def test_unknown_property_is_rejected(self):
        def mutate(finding, _document):
            finding["proof"] = True

        self._reject(self._mutate(mutate), "unknown property")

    def test_artifact_needs_a_hash_or_an_explicit_reason(self):
        document = copy.deepcopy(self.base)
        del document["artifacts"][0]["sha256"]
        self._reject(document)

    def test_gate_reference_without_an_artifact_is_rejected(self):
        document = serialize.read_document(
            os.path.join(EXAMPLE_DIR, "dataflow-heuristic.json")
        )
        finding = find_by_status(document, model.STATUS_HEURISTIC)
        del finding["scope"]["gates"][0]["artifact_id"]
        self._reject(document, "artifact_id")

    def test_schema_version_must_be_supported(self):
        document = copy.deepcopy(self.base)
        document["schema_version"] = "0.9.0"
        with self.assertRaises(validate.FindingsValidationError):
            validate.validate_document(document)


# ---------------------------------------------------------------------------
# cross-reference (semantic) rules
# ---------------------------------------------------------------------------


class SemanticTest(unittest.TestCase):
    def setUp(self):
        self.document = serialize.read_document(
            os.path.join(EXAMPLE_DIR, "dataflow-heuristic.json")
        )

    def test_gate_reference_must_resolve_to_a_declared_artifact(self):
        finding = find_by_status(self.document, model.STATUS_HEURISTIC)
        finding["scope"]["gates"][0]["artifact_id"] = "other_netlist"
        errors = validate.collect_errors(self.document)
        self.assertTrue(any("undeclared artifact" in error for error in errors), errors)

    def test_gate_reference_must_be_inside_the_finding_scope(self):
        self.document["artifacts"].append(
            model.artifact("second_netlist", sha256="0" * 64, path="designs/other.v")
        )
        finding = find_by_status(self.document, model.STATUS_HEURISTIC)
        finding["scope"]["gates"][0]["artifact_id"] = "second_netlist"
        errors = validate.collect_errors(self.document)
        self.assertTrue(any("scope.artifact_ids" in error for error in errors), errors)

    def test_duplicate_finding_ids_are_rejected(self):
        self.document["findings"].append(copy.deepcopy(self.document["findings"][-1]))
        errors = validate.collect_errors(self.document)
        self.assertTrue(any("duplicate finding id" in error for error in errors), errors)

    def test_counterexample_bound_may_not_exceed_the_analysis_bound(self):
        document = serialize.read_document(os.path.join(EXAMPLE_DIR, "bounded-results.json"))
        finding = [
            entry
            for entry in document["findings"]
            if entry["status"] == model.STATUS_BOUNDED_COUNTEREXAMPLE
        ][0]
        finding["counterexample"]["cycle_bound"] = 99
        errors = validate.collect_errors(document)
        self.assertTrue(any("exceeds bounds.cycle_bound" in error for error in errors), errors)


# ---------------------------------------------------------------------------
# deterministic serialization
# ---------------------------------------------------------------------------


class SerializationTest(unittest.TestCase):
    def setUp(self):
        self.document = _generate.heuristic_groups()

    def test_round_trip_is_stable(self):
        text = serialize.dumps(self.document)
        self.assertEqual(text, serialize.dumps(serialize.loads(text)))
        self.assertEqual(
            serialize.document_digest(self.document),
            serialize.document_digest(serialize.loads(text)),
        )

    def test_serialization_is_independent_of_insertion_order(self):
        shuffled = copy.deepcopy(self.document)
        shuffled["findings"].reverse()
        find_by_status(shuffled, model.STATUS_HEURISTIC)["scope"]["gates"].reverse()
        shuffled["artifacts"] = list(reversed(shuffled["artifacts"]))
        self.assertEqual(serialize.dumps(self.document), serialize.dumps(shuffled))

    def test_keys_are_sorted_and_output_ends_with_a_newline(self):
        text = serialize.dumps(self.document)
        self.assertTrue(text.endswith("\n"))
        keys = list(json.loads(text).keys())
        self.assertEqual(sorted(keys), keys)

    def test_digest_ignores_volatile_fields_only(self):
        digest = serialize.document_digest(self.document)
        other = copy.deepcopy(self.document)
        other["generated_at"] = "2030-12-31T23:59:59Z"
        other["analysis"]["duration_s"] = 999.0
        self.assertEqual(digest, serialize.document_digest(other))

        changed = copy.deepcopy(self.document)
        changed["findings"][0]["title"] = "a different claim"
        self.assertNotEqual(digest, serialize.document_digest(changed))

    def test_write_and_read_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "findings.json")
            serialize.write_document(self.document, path)
            self.assertEqual(
                serialize.document_digest(self.document),
                serialize.document_digest(serialize.read_document(path)),
            )


# ---------------------------------------------------------------------------
# model builders
# ---------------------------------------------------------------------------


class ModelTest(unittest.TestCase):
    def _method(self):
        return model.method("m", "formal", False)

    def _scope(self):
        return model.scope(["a"])

    def test_unbounded_bounds_reject_a_cycle_bound(self):
        with self.assertRaises(ValueError):
            model.bounds(True, cycle_bound=4)

    def test_bounded_bounds_require_a_bound(self):
        with self.assertRaises(ValueError):
            model.bounds(False)

    def test_proof_requires_unbounded_bounds(self):
        with self.assertRaises(ValueError):
            model.finding(
                "f",
                "t",
                model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
                self._method(),
                self._scope(),
                assumptions=[],
                bounds_dict=model.bounded(4),
            )

    def test_bounded_counterexample_requires_a_witness_bound(self):
        with self.assertRaises(ValueError):
            model.finding(
                "f",
                "t",
                model.STATUS_BOUNDED_COUNTEREXAMPLE,
                model.method("m", "bounded_formal", True),
                self._scope(),
                bounds_dict=model.bounded(4),
                counterexample_dict=model.counterexample("boom"),
            )

    def test_counterexample_bound_may_not_exceed_the_analysis_bound(self):
        with self.assertRaises(ValueError):
            model.finding(
                "f",
                "t",
                model.STATUS_BOUNDED_COUNTEREXAMPLE,
                model.method("m", "bounded_formal", True),
                self._scope(),
                bounds_dict=model.bounded(4),
                counterexample_dict=model.counterexample("boom", cycle_bound=9),
            )

    def test_timeout_requires_limits(self):
        with self.assertRaises(ValueError):
            model.finding("f", "t", model.STATUS_TIMEOUT, self._method(), self._scope())

    def test_only_refutations_may_carry_a_counterexample(self):
        with self.assertRaises(ValueError):
            model.finding(
                "f",
                "t",
                model.STATUS_UNKNOWN,
                self._method(),
                self._scope(),
                counterexample_dict=model.counterexample("boom"),
            )

    def test_unsupported_primitive_kind_requires_entries(self):
        with self.assertRaises(ValueError):
            model.unsupported("primitive", "because")

    def test_artifact_requires_hash_or_reason(self):
        with self.assertRaises(ValueError):
            model.artifact("a", path="x.v")

    def test_is_unbounded_proof_distinguishes_bounded_results(self):
        document = serialize.read_document(os.path.join(EXAMPLE_DIR, "bounded-results.json"))
        for finding in document["findings"]:
            self.assertFalse(model.is_unbounded_proof(finding), finding["id"])
            self.assertTrue(model.is_bounded_claim(finding), finding["id"])
        proof = serialize.read_document(
            os.path.join(EXAMPLE_DIR, "equivalence-proof.json")
        )["findings"][0]
        self.assertTrue(model.is_unbounded_proof(proof))
        self.assertFalse(model.is_bounded_claim(proof))


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------


class DataflowAdapterTest(unittest.TestCase):
    def _result(self):
        grouped = _flip_flops(4)
        ungrouped_ram = StubGate(900, "mem/bram_0", RAM)
        combinational = StubGate(901, "lut_0", LUT)
        netlist = StubNetlist(grouped + [ungrouped_ram, combinational], nets=[StubNet(1, "clk")])
        return StubDataflowResult(
            netlist,
            {1: grouped[:2], 2: grouped[2:]},
            successors={1: {2}},
            control_nets={(1, "clock"): [StubNet(12, "clk")]},
        )

    def test_document_validates(self):
        document = dataflow_adapter.build_document(
            self._result(), plugin_version="1.3.0", control_pin_types=[("clock", "clock")]
        )
        self.assertEqual([], validate.collect_errors(document))

    def test_groups_become_heuristic_findings(self):
        document = dataflow_adapter.build_document(self._result())
        groups = [
            finding
            for finding in document["findings"]
            if finding["id"].startswith("dataflow/group/")
        ]
        self.assertEqual(2, len(groups))
        for finding in groups:
            self.assertEqual(model.STATUS_HEURISTIC, finding["status"])
            self.assertFalse(model.is_unbounded_proof(finding))
            self.assertEqual("heuristic", finding["method"]["kind"])
            self.assertFalse(finding["method"]["bounded"])
            for gate in finding["scope"]["gates"]:
                self.assertEqual("netlist", gate["artifact_id"])

    def test_ungrouped_sequential_types_are_reported_as_unsupported(self):
        document = dataflow_adapter.build_document(self._result())
        coverage = [
            finding
            for finding in document["findings"]
            if finding["status"] == model.STATUS_UNSUPPORTED
        ]
        self.assertEqual(1, len(coverage))
        primitives = coverage[0]["unsupported"]["primitives"]
        self.assertEqual(["RAMB18E1"], [entry["gate_type"] for entry in primitives])
        self.assertEqual(1, primitives[0]["count"])

    def test_full_coverage_is_stated_in_the_notes(self):
        gates = _flip_flops(2)
        result = StubDataflowResult(StubNetlist(gates), {1: gates})
        document = dataflow_adapter.build_document(result)
        self.assertEqual([], validate.collect_errors(document))
        self.assertFalse(
            [
                finding
                for finding in document["findings"]
                if finding["status"] == model.STATUS_UNSUPPORTED
            ]
        )
        self.assertTrue(any("no unsupported primitives" in note for note in document["notes"]))

    def test_control_nets_are_recorded_when_pin_types_are_supplied(self):
        document = dataflow_adapter.build_document(
            self._result(), control_pin_types=[("clock", "clock")]
        )
        group_one = [
            finding for finding in document["findings"] if finding["id"].endswith("0001")
        ][0]
        self.assertEqual(["clk"], [net["name"] for net in group_one["scope"]["nets"]])
        self.assertEqual(["clock"], group_one["data"]["control_net_roles"])

    def test_netlist_without_a_source_file_is_flagged_as_unhashed(self):
        document = dataflow_adapter.build_document(self._result())
        artifact = document["artifacts"][0]
        self.assertNotIn("sha256", artifact)
        self.assertIn("no readable source file", artifact["unhashed_reason"])


class NetlistComparisonAdapterTest(unittest.TestCase):
    def _netlists(self, same_names=True, with_dsp=False):
        gates_a = _flip_flops(2) + [StubGate(300, "lut_a", LUT)]
        names = "state_reg" if same_names else "renamed_reg"
        gates_b = _flip_flops(2, offset=200, prefix=names) + [StubGate(400, "lut_b", LUT)]
        if with_dsp:
            gates_a.append(StubGate(500, "alu/dsp_0", DSP))
        return StubNetlist(gates_a, design="a"), StubNetlist(gates_b, design="b")

    def test_status_mapping(self):
        cases = {
            (True, True): model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
            (False, True): model.STATUS_UNKNOWN,
            (True, False): model.STATUS_UNKNOWN,
            (False, False): model.STATUS_COUNTEREXAMPLE,
            (None, True): model.STATUS_ERROR,
            (None, False): model.STATUS_ERROR,
        }
        for (equivalent, fail_on_unknown), expected in cases.items():
            status, rationale = comparison_adapter.classify(equivalent, fail_on_unknown)
            self.assertEqual(expected, status, (equivalent, fail_on_unknown))
            self.assertTrue(rationale)

    def test_every_outcome_produces_a_valid_document(self):
        netlist_a, netlist_b = self._netlists()
        for equivalent in (True, False, None):
            for fail_on_unknown in (True, False):
                document = comparison_adapter.build_document(
                    netlist_a, netlist_b, equivalent, fail_on_unknown=fail_on_unknown
                )
                self.assertEqual(
                    [], validate.collect_errors(document), (equivalent, fail_on_unknown)
                )

    def test_proof_is_only_claimed_with_fail_on_unknown(self):
        netlist_a, netlist_b = self._netlists()
        proof = comparison_adapter.build_document(
            netlist_a, netlist_b, True, fail_on_unknown=True
        )["findings"][-1]
        self.assertTrue(model.is_unbounded_proof(proof))
        self.assertTrue(proof["assumptions"])

        swallowed = comparison_adapter.build_document(
            netlist_a, netlist_b, True, fail_on_unknown=False
        )["findings"][-1]
        self.assertEqual(model.STATUS_UNKNOWN, swallowed["status"])
        self.assertFalse(model.is_unbounded_proof(swallowed))

    def test_counterexample_records_that_no_witness_is_available(self):
        netlist_a, netlist_b = self._netlists()
        finding = comparison_adapter.build_document(
            netlist_a, netlist_b, False, fail_on_unknown=False
        )["findings"][-1]
        self.assertEqual(model.STATUS_COUNTEREXAMPLE, finding["status"])
        self.assertFalse(finding["counterexample"]["witness_available"])

    def test_timeout_is_reported_when_the_caller_aborts(self):
        netlist_a, netlist_b = self._netlists()
        document = comparison_adapter.build_document(
            netlist_a, netlist_b, None, timed_out=True, solver_timeout=5
        )
        self.assertEqual([], validate.collect_errors(document))
        finding = document["findings"][-1]
        self.assertEqual(model.STATUS_TIMEOUT, finding["status"])
        self.assertEqual(5.0, finding["limits"]["timeout_s"])
        self.assertTrue(finding["limits"]["hit"])

    def test_name_mismatch_is_reported_as_an_unsupported_precondition(self):
        netlist_a, netlist_b = self._netlists(same_names=False)
        document = comparison_adapter.build_document(netlist_a, netlist_b, False)
        self.assertEqual([], validate.collect_errors(document))
        precondition = [
            finding
            for finding in document["findings"]
            if finding["id"].endswith("sequential-gate-names")
        ]
        self.assertEqual(1, len(precondition))
        self.assertEqual(model.STATUS_UNSUPPORTED, precondition[0]["status"])
        self.assertEqual("construct", precondition[0]["unsupported"]["kind"])
        self.assertEqual(2, precondition[0]["data"]["unmatched_count_a"])

    def test_gate_types_outside_the_boolean_model_are_reported(self):
        netlist_a, netlist_b = self._netlists(with_dsp=True)
        document = comparison_adapter.build_document(netlist_a, netlist_b, True)
        self.assertEqual([], validate.collect_errors(document))
        coverage = [
            finding
            for finding in document["findings"]
            if finding["id"].endswith("unmodelled-primitives")
        ][0]
        self.assertEqual(
            ["DSP48E1"],
            [entry["gate_type"] for entry in coverage["unsupported"]["primitives"]],
        )

    def test_gate_references_of_both_netlists_stay_distinguishable(self):
        netlist_a, netlist_b = self._netlists(same_names=False)
        document = comparison_adapter.build_document(netlist_a, netlist_b, False)
        artifact_ids = {artifact["artifact_id"] for artifact in document["artifacts"]}
        self.assertEqual({"netlist_a", "netlist_b"}, artifact_ids)
        precondition = [
            finding
            for finding in document["findings"]
            if finding["id"].endswith("sequential-gate-names")
        ][0]
        referenced = {gate["artifact_id"] for gate in precondition["scope"]["gates"]}
        self.assertEqual({"netlist_a", "netlist_b"}, referenced)

    def test_run_wrapper_turns_an_exception_into_an_error_finding(self):
        class Exploding(object):
            @staticmethod
            def compare_netlists(*args, **kwargs):
                raise RuntimeError("z3 exploded")

        netlist_a, netlist_b = self._netlists()
        document = comparison_adapter.run_compare_netlists(Exploding, netlist_a, netlist_b)
        self.assertEqual([], validate.collect_errors(document))
        finding = document["findings"][-1]
        self.assertEqual(model.STATUS_ERROR, finding["status"])
        self.assertIn("z3 exploded", finding["error"]["message"])


# ---------------------------------------------------------------------------
# command line interface
# ---------------------------------------------------------------------------


class CliTest(unittest.TestCase):
    def _run(self, argv):
        stream = io.StringIO()
        code = cli.main(argv, stream=stream)
        return code, stream.getvalue()

    def test_validate_accepts_the_examples(self):
        paths = [
            os.path.join(EXAMPLE_DIR, name) for name in sorted(_generate.EXAMPLES)
        ]
        code, output = self._run(["validate"] + paths)
        self.assertEqual(0, code, output)
        self.assertEqual(len(paths), output.count("ok  "))

    def test_validate_rejects_a_broken_document(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "broken.json")
            document = _generate.equivalence_proof()
            document["findings"][0]["bounds"]["cycle_bound"] = 4
            serialize.write_document(document, path, normalize=False)
            code, output = self._run(["validate", path])
        self.assertEqual(1, code)
        self.assertIn("FAIL", output)

    def test_summary_is_strict_about_counterexamples(self):
        path = os.path.join(EXAMPLE_DIR, "counterexample.json")
        self.assertEqual(0, self._run(["summary", path])[0])
        code, output = self._run(["summary", "--strict", path])
        self.assertEqual(1, code)
        self.assertIn("counterexample", output)

    def test_schema_path_is_printed(self):
        code, output = self._run(["schema", "--path"])
        self.assertEqual(0, code)
        self.assertTrue(output.strip().endswith("findings-{}.schema.json".format(SCHEMA_VERSION)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
