"""Unit tests for hal_analysis_api. No HAL build, no netlist, no network.

    python -m unittest discover -s tools/hal_analysis_api -t tools -p "test_*.py"

What makes that possible is the same split hal_viz and hal_runner use: the
query layer is duck-typed against the ``hal_py`` bindings, so a stub netlist
built from ``fixtures/cone_fixture.ground_truth.json`` exercises pagination,
filtering, cones and truncation for real; and the API reaches HAL through
exactly one object (``QueryRunner.executor``), so a stub executor exercises
timeouts, crashes and typed errors crossing the process boundary.

The cases that matter are the negative ones: an unknown id that must not come
back as an empty answer, a cut that must be visible, a wait that expires
without becoming a failure, a worker that vanished, an artifact path that tries
to leave the job directory.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_findings import jsonschema_mini
from hal_findings import model as findings_model
from hal_findings import serialize as findings_serialize
from hal_runner.execute import ExecutionResult

from hal_analysis_api import errors, limits, netlist_query, query_protocol, schemas
from hal_analysis_api.api import AnalysisApi
from hal_analysis_api.inhal import dispatch as inhal_dispatch
from hal_analysis_api.jobs import JobStore

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
GROUND_TRUTH = os.path.join(FIXTURE_DIR, "cone_fixture.ground_truth.json")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_ground_truth():
    with open(GROUND_TRUTH, "r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# a stub netlist built from the fixture's ground truth
# ---------------------------------------------------------------------------


class StubPin(object):
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class StubEndpoint(object):
    def __init__(self, gate, net, pin):
        self._gate = gate
        self._net = net
        self._pin = StubPin(pin)

    def get_gate(self):
        return self._gate

    def get_net(self):
        return self._net

    def get_pin(self):
        return self._pin


class StubGateType(object):
    def __init__(self, name, properties):
        self._name = name
        self._properties = properties

    def get_name(self):
        return self._name

    def get_properties(self):
        return set(self._properties)


class StubModule(object):
    def __init__(self, module_id, name, gates):
        self._id = module_id
        self._name = name
        self._gates = gates

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_type(self):
        return ""

    def get_gates(self):
        return list(self._gates)

    def get_submodules(self):
        return []

    def get_parent_module(self):
        return None


class StubNet(object):
    def __init__(self, net_id, name, global_input=False, global_output=False):
        self._id = net_id
        self._name = name
        self._global_input = global_input
        self._global_output = global_output
        self.sources = []
        self.destinations = []

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_sources(self):
        return list(self.sources)

    def get_destinations(self):
        return list(self.destinations)

    def is_global_input_net(self):
        return self._global_input

    def is_global_output_net(self):
        return self._global_output


class StubGate(object):
    _PROPERTIES = {
        "BUF": ("combinational", "c_buffer"),
        "INV": ("combinational", "c_inverter"),
        "AND2": ("combinational", "c_and"),
        "FF": ("sequential", "ff"),
    }

    def __init__(self, gate_id, name, gate_type):
        self._id = gate_id
        self._name = name
        self._type = StubGateType(gate_type, self._PROPERTIES.get(gate_type, ()))
        self.module = None
        self.fan_in = []
        self.fan_out = []

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_type(self):
        return self._type

    def get_module(self):
        return self.module

    def get_fan_in_endpoints(self):
        return list(self.fan_in)

    def get_fan_out_endpoints(self):
        return list(self.fan_out)

    def get_unique_predecessors(self):
        seen = {}
        for endpoint in self.fan_in:
            for source in endpoint.get_net().get_sources():
                gate = source.get_gate()
                if gate is not None and gate.get_id() != self._id:
                    seen[gate.get_id()] = gate
        return list(seen.values())

    def get_unique_successors(self):
        seen = {}
        for endpoint in self.fan_out:
            for destination in endpoint.get_net().get_destinations():
                gate = destination.get_gate()
                if gate is not None and gate.get_id() != self._id:
                    seen[gate.get_id()] = gate
        return list(seen.values())

    def is_gnd_gate(self):
        return False

    def is_vcc_gate(self):
        return False


class StubNetlist(object):
    """A netlist with exactly the shape ``fixtures/cone_fixture.v`` describes."""

    def __init__(self, ground_truth):
        self.design_name = ground_truth["design_name"]
        self._nets = {}
        for index, entry in enumerate(ground_truth["nets"], start=1):
            self._nets[entry["name"]] = StubNet(
                index, entry["name"], entry["global_input"], entry["global_output"]
            )
        self._gates = {}
        for index, entry in enumerate(ground_truth["gates"], start=1):
            gate = StubGate(index, entry["name"], entry["type"])
            self._gates[entry["name"]] = gate
            for pin, net_name in sorted(entry["inputs"].items()):
                net = self._nets[net_name]
                endpoint = StubEndpoint(gate, net, pin)
                gate.fan_in.append(endpoint)
                net.destinations.append(endpoint)
            for pin, net_name in sorted(entry["outputs"].items()):
                net = self._nets[net_name]
                endpoint = StubEndpoint(gate, net, pin)
                gate.fan_out.append(endpoint)
                net.sources.append(endpoint)
        self._module = StubModule(1, "top_module", list(self._gates.values()))
        for gate in self._gates.values():
            gate.module = self._module

    # -- the hal_py Netlist surface the queries use --------------------------

    def get_gates(self):
        return list(self._gates.values())

    def get_nets(self):
        return list(self._nets.values())

    def get_modules(self):
        return [self._module]

    def get_gate_by_id(self, gate_id):
        for gate in self._gates.values():
            if gate.get_id() == gate_id:
                return gate
        return None

    def get_net_by_id(self, net_id):
        for net in self._nets.values():
            if net.get_id() == net_id:
                return net
        return None

    def get_module_by_id(self, module_id):
        return self._module if module_id == 1 else None

    def get_design_name(self):
        return self.design_name

    def get_id(self):
        return 1

    def get_device_name(self):
        return ""

    def get_top_module(self):
        return self._module

    def get_gate_library(self):
        return StubGateType("EXAMPLE_GATE_LIBRARY", ())

    # -- helpers for the tests ----------------------------------------------

    def id_of(self, name):
        return self._gates[name].get_id()

    def names(self, gate_dicts):
        return sorted(entry["name"] for entry in gate_dicts)


# ---------------------------------------------------------------------------
# a stub executor: what a hal subprocess would have written
# ---------------------------------------------------------------------------


class StubExecutor(object):
    """Stands in for ``hal --python-script``.

    It answers by calling the *real* in-HAL dispatch table over the stub
    netlist, so the request/response protocol, the typed errors and the host's
    interpretation of them are all exercised; only HAL itself is missing.
    """

    def __init__(self, netlist, mode="ok"):
        self.netlist = netlist
        self.mode = mode
        self.calls = []

    def execute(self, command, cwd=None, env=None, timeout_s=None, memory_mb=None,
                stdout_path=None, stderr_path=None):
        request_path = env[query_protocol.REQUEST_ENV]
        request = query_protocol.read_request(request_path)
        self.calls.append(request)
        response_path = os.path.join(request["output_dir"], request["response_file"])

        def result(exit_code, timed_out=False):
            for path in (stdout_path, stderr_path):
                if path:
                    with open(path, "a", encoding="utf-8") as handle:
                        handle.write("stub executor: mode={}\n".format(self.mode))
            return ExecutionResult(
                command, exit_code, timed_out, 0.01, stdout_path, stderr_path,
                timeout_s=timeout_s,
            )

        if self.mode == "timeout":
            return result(-9, timed_out=True)
        if self.mode == "crash":
            return result(139)
        if self.mode == "silent":
            return result(0)

        handler = inhal_dispatch.QUERIES[request["query"]]
        try:
            payload = handler(self.netlist, request["params"])
        except errors.ApiError as exc:
            query_protocol.write_json(
                query_protocol.response(
                    "error",
                    request["query"],
                    error=exc.as_json(),
                    duration_s=0.01,
                    hal_version="stub",
                ),
                response_path,
            )
            return result(1)
        query_protocol.write_json(
            query_protocol.response(
                "ok", request["query"], result=payload, duration_s=0.01, hal_version="stub"
            ),
            response_path,
        )
        return result(0)


class ApiTestCase(unittest.TestCase):
    """Base case: a temporary workspace, a stub netlist, an API wired to both."""

    mode = "ok"

    def setUp(self):
        self.ground_truth = load_ground_truth()
        self.netlist = StubNetlist(self.ground_truth)
        self.workspace = tempfile.mkdtemp(prefix="hal_analysis_api_test_")
        self.addCleanup(shutil.rmtree, self.workspace, True)
        self.executor = StubExecutor(self.netlist, mode=self.mode)
        self.api = AnalysisApi(
            workspace=self.workspace,
            hal_binary=self._fake_hal_binary(),
            executor=self.executor,
            spawn=lambda job_dir, record: None,
        )

    def _fake_hal_binary(self):
        path = os.path.join(self.workspace, "hal")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        return path

    def open_fixture(self):
        envelope = self.api.call(
            "project.open",
            {
                "path": os.path.join(FIXTURE_DIR, "cone_fixture.v"),
                "gate_library": os.path.join(
                    REPO_ROOT, "plugins", "gate_libraries", "definitions", "example_library.hgl"
                ),
            },
        )
        self.assertTrue(envelope["ok"], envelope)
        return envelope["result"]["project"]


# ---------------------------------------------------------------------------
# 1. the schema is the contract
# ---------------------------------------------------------------------------


class SchemaContractTests(unittest.TestCase):
    def test_validator_supports_every_keyword_the_schema_uses(self):
        # Tripwire: jsonschema_mini silently ignores keywords it does not know,
        # so a schema that drifts past it would validate nothing.
        self.assertEqual([], jsonschema_mini.check_schema_support(schemas.load_schema()))

    def test_every_tool_has_a_request_and_a_response_schema(self):
        definitions = schemas.load_schema()["$defs"]
        for name in schemas.tool_names():
            tool = schemas.TOOLS[name]
            self.assertIn("req_" + tool.key, definitions, name)
            self.assertIn("res_" + tool.key, definitions, name)

    def test_requests_reject_unknown_fields(self):
        for name in schemas.tool_names():
            tool = schemas.TOOLS[name]
            definition = schemas.load_schema()["$defs"]["req_" + tool.key]
            self.assertIs(
                definition.get("additionalProperties"),
                False,
                "{} must reject unknown request fields".format(name),
            )
        errors_found = schemas.request_errors("netlist.gates", {"project": "prj-" + "0" * 12,
                                                               "limitt": 5})
        self.assertTrue(any("limitt" in entry for entry in errors_found), errors_found)

    def test_standalone_schema_is_self_contained_and_an_object(self):
        schema = schemas.standalone_schema(schemas.TOOLS["netlist.cone"].request_ref)
        self.assertEqual("object", schema["type"])
        self.assertIn("$defs", schema)
        self.assertIn("seed_gate_ids", schema["properties"])
        # It has to validate a real request on its own, with no outer document.
        self.assertEqual(
            [],
            list(
                jsonschema_mini.iter_errors(
                    {"project": "prj-" + "a" * 12, "seed_gate_ids": [1]}, schema
                )
            ),
        )

    def test_envelopes_validate(self):
        ok = errors.ok_envelope("hal.capabilities", {"whatever": 1})
        bad = errors.error_envelope("hal.capabilities", errors.UnknownProject("nope"))
        self.assertEqual([], schemas.envelope_errors(ok))
        self.assertEqual([], schemas.envelope_errors(bad))
        # ok:true without a result is not a valid envelope
        self.assertTrue(
            schemas.envelope_errors({"ok": True, "api_version": "1.0.0", "tool": "x"})
        )

    def test_error_codes_in_schema_match_the_module(self):
        enum = schemas.load_schema()["$defs"]["error"]["properties"]["code"]["enum"]
        self.assertEqual(sorted(errors.ERROR_CODES), sorted(enum))


# ---------------------------------------------------------------------------
# 2. the query layer against the hand-checked fixture
# ---------------------------------------------------------------------------


class NetlistQueryTests(unittest.TestCase):
    def setUp(self):
        self.ground_truth = load_ground_truth()
        self.netlist = StubNetlist(self.ground_truth)
        self.expected = self.ground_truth["expected"]

    def test_summary_matches_the_ground_truth(self):
        result = netlist_query.summary(self.netlist)
        self.assertEqual(self.expected["counts"]["gates"], result["counts"]["gates"])
        self.assertEqual(self.expected["counts"]["nets"], result["counts"]["nets"])
        self.assertEqual(self.expected["global_inputs"], result["counts"]["global_inputs"])
        self.assertEqual(self.expected["global_outputs"], result["counts"]["global_outputs"])
        self.assertEqual(
            self.expected["gate_types"],
            {entry["type"]: entry["count"] for entry in result["gate_types"]},
        )
        self.assertFalse(result["truncation"]["truncated"])

    def test_summary_truncates_the_histogram_explicitly(self):
        result = netlist_query.summary(self.netlist, max_gate_types=2)
        self.assertEqual(2, len(result["gate_types"]))
        self.assertTrue(result["truncation"]["truncated"])
        self.assertEqual("gate_types", result["truncation"]["kind"])
        self.assertEqual(4, result["counts"]["gate_types"])

    def test_pagination_walks_every_gate_exactly_once(self):
        seen = []
        offset = 0
        while True:
            page = netlist_query.gates(self.netlist, offset=offset, limit=3)
            seen.extend(entry["id"] for entry in page["gates"])
            self.assertEqual(7, page["page"]["total"])
            if not page["page"]["has_more"]:
                self.assertIsNone(page["page"]["next_offset"])
                break
            offset = page["page"]["next_offset"]
        self.assertEqual(sorted(seen), sorted(set(seen)))
        self.assertEqual(7, len(seen))

    def test_pagination_past_the_end_is_an_empty_page_not_an_error(self):
        page = netlist_query.gates(self.netlist, offset=999, limit=10)
        self.assertEqual([], page["gates"])
        self.assertEqual(7, page["page"]["total"])
        self.assertFalse(page["page"]["has_more"])

    def test_filters_narrow_the_total_not_just_the_page(self):
        page = netlist_query.gates(self.netlist, gate_type="FF")
        self.assertEqual(2, page["page"]["total"])
        self.assertEqual(["ff0", "ff1"], sorted(entry["name"] for entry in page["gates"]))
        page = netlist_query.gates(self.netlist, name_contains="buf")
        self.assertEqual(["clk_buf", "out_buf"], sorted(e["name"] for e in page["gates"]))

    def test_unknown_ids_are_errors_never_empty_answers(self):
        with self.assertRaises(errors.UnknownObject) as caught:
            netlist_query.gate(self.netlist, 9999)
        self.assertEqual("unknown_object", caught.exception.code)
        with self.assertRaises(errors.UnknownObject):
            netlist_query.net(self.netlist, 9999)
        with self.assertRaises(errors.UnknownObject):
            netlist_query.cone(self.netlist, [9999])
        with self.assertRaises(errors.UnknownObject):
            netlist_query.gates(self.netlist, module_id=77)

    def test_gate_detail_reports_pins_and_neighbours(self):
        detail = netlist_query.gate(self.netlist, self.netlist.id_of("and0"))["gate"]
        self.assertEqual("AND2", detail["type"])
        self.assertEqual(
            {"I0", "I1"}, {endpoint["pin"] for endpoint in detail["fan_in"]}
        )
        self.assertEqual(
            {"ff0", "en_inv"},
            {endpoint["gate"]["name"] for endpoint in detail["fan_in"] if endpoint["gate"]},
        )
        self.assertFalse(detail["truncation"]["truncated"])

    def test_gate_detail_truncates_endpoints_explicitly(self):
        detail = netlist_query.gate(
            self.netlist, self.netlist.id_of("ff0"), max_endpoints=1
        )["gate"]
        self.assertEqual(1, len(detail["fan_in"]))
        self.assertEqual(3, detail["fan_in_total"])
        self.assertTrue(detail["truncation"]["truncated"])
        self.assertEqual("endpoints", detail["truncation"]["kind"])

    def test_net_detail_names_its_driver_and_loads(self):
        net_id = None
        for entry in netlist_query.nets(self.netlist)["nets"]:
            if entry["name"] == "q1":
                net_id = entry["id"]
        detail = netlist_query.net(self.netlist, net_id)["net"]
        self.assertEqual(["ff1"], [e["gate"]["name"] for e in detail["sources"]])
        self.assertEqual(
            {"inv0", "out_buf"}, {e["gate"]["name"] for e in detail["destinations"]}
        )

    def test_cones_match_the_recorded_ground_truth(self):
        for case in self.expected["cones"]:
            result = netlist_query.cone(
                self.netlist,
                [self.netlist.id_of(name) for name in case["seeds"]],
                direction=case["direction"],
                depth=case["depth"],
                max_gates=case["max_gates"],
            )
            names = sorted(entry["name"] for entry in result["gates"])
            if "gates" in case:
                self.assertEqual(sorted(case["gates"]), names, case["id"])
            if "gate_count" in case:
                self.assertEqual(case["gate_count"], len(names), case["id"])
            for name in case.get("gates_contain", []):
                self.assertIn(name, names, case["id"])
            self.assertEqual(
                case["truncated"], result["truncation"]["truncated"], case["id"]
            )
            self.assertEqual(
                case["truncation_kind"], result["truncation"]["kind"], case["id"]
            )

    def test_cone_edges_are_the_recorded_edges(self):
        result = netlist_query.cone(
            self.netlist, [self.netlist.id_of("out_buf")], direction="fan_in", depth=6
        )
        by_id = {entry["id"]: entry["name"] for entry in result["gates"]}
        edges = sorted(
            (by_id[edge["from_gate_id"]], by_id[edge["to_gate_id"]], edge["net_name"])
            for edge in result["edges"]
        )
        self.assertEqual(
            sorted(tuple(entry) for entry in self.expected["gate_edges"]), edges
        )

    def test_a_cone_that_cannot_hold_its_seeds_is_an_explicit_limit_error(self):
        with self.assertRaises(errors.LimitExceeded) as caught:
            netlist_query.cone(
                self.netlist,
                [self.netlist.id_of("ff0"), self.netlist.id_of("ff1")],
                max_gates=1,
            )
        self.assertEqual("limit_exceeded", caught.exception.code)

    def test_cone_is_deterministic(self):
        first = netlist_query.cone(self.netlist, [self.netlist.id_of("ff1")], depth=2)
        second = netlist_query.cone(self.netlist, [self.netlist.id_of("ff1")], depth=2)
        self.assertEqual(first, second)


# ---------------------------------------------------------------------------
# 3. project handles
# ---------------------------------------------------------------------------


class ProjectHandleTests(ApiTestCase):
    def test_opening_the_same_content_twice_reuses_the_handle(self):
        first = self.api.call("project.open", {"path": os.path.join(FIXTURE_DIR,
                                                                   "cone_fixture.v"),
                                               "gate_library": os.path.join(
                                                   REPO_ROOT, "plugins", "gate_libraries",
                                                   "definitions", "example_library.hgl")})
        second = self.api.call("project.open", {"path": os.path.join(FIXTURE_DIR,
                                                                    "cone_fixture.v"),
                                                "gate_library": os.path.join(
                                                    REPO_ROOT, "plugins", "gate_libraries",
                                                    "definitions", "example_library.hgl")})
        self.assertFalse(first["result"]["reused"])
        self.assertTrue(second["result"]["reused"])
        self.assertEqual(first["result"]["project"], second["result"]["project"])

    def test_an_hdl_netlist_without_a_gate_library_is_refused_with_a_hint(self):
        envelope = self.api.call(
            "project.open", {"path": os.path.join(FIXTURE_DIR, "cone_fixture.v")}
        )
        self.assertFalse(envelope["ok"])
        self.assertEqual("invalid_request", envelope["error"]["code"])
        self.assertIn("gate_library", envelope["error"]["hint"])

    def test_a_missing_path_is_invalid_request_not_a_crash(self):
        envelope = self.api.call("project.open", {"path": "/nowhere/at/all.hal"})
        self.assertEqual("invalid_request", envelope["error"]["code"])

    def test_an_unknown_handle_is_unknown_project_everywhere(self):
        for tool, request in (
            ("project.describe", {"project": "prj-" + "0" * 12}),
            ("project.close", {"project": "prj-" + "0" * 12}),
            ("netlist.summary", {"project": "prj-" + "0" * 12}),
            ("analysis.submit", {"project": "prj-" + "0" * 12, "analysis": "dataflow.groups"}),
        ):
            envelope = self.api.call(tool, request)
            self.assertFalse(envelope["ok"], tool)
            self.assertEqual("unknown_project", envelope["error"]["code"], tool)

    def test_a_malformed_handle_is_rejected_by_the_schema(self):
        envelope = self.api.call("project.describe", {"project": "not-a-handle"})
        self.assertEqual("invalid_request", envelope["error"]["code"])

    def test_close_forgets_the_handle_and_leaves_the_project_alone(self):
        project = self.open_fixture()
        source = os.path.join(FIXTURE_DIR, "cone_fixture.v")
        before = os.path.getsize(source)
        self.assertTrue(self.api.call("project.close", {"project": project})["ok"])
        self.assertEqual(before, os.path.getsize(source))
        self.assertEqual(
            "unknown_project",
            self.api.call("project.describe", {"project": project})["error"]["code"],
        )

    def test_describe_reports_a_changed_file(self):
        copied = os.path.join(self.workspace, "copy.hal")
        shutil.copyfile(os.path.join(FIXTURE_DIR, "cone_fixture.v"), copied)
        project = self.api.call("project.open", {"path": copied})["result"]["project"]
        self.assertTrue(
            self.api.call("project.describe", {"project": project})["result"][
                "still_matches_digest"
            ]
        )
        with open(copied, "a", encoding="utf-8") as handle:
            handle.write("\n// changed\n")
        self.assertFalse(
            self.api.call("project.describe", {"project": project})["result"][
                "still_matches_digest"
            ]
        )


# ---------------------------------------------------------------------------
# 4. netlist reads through the (stubbed) subprocess boundary
# ---------------------------------------------------------------------------


class NetlistToolTests(ApiTestCase):
    def test_a_scoped_investigation_end_to_end(self):
        project = self.open_fixture()

        summary = self.api.call("netlist.summary", {"project": project})
        self.assertTrue(summary["ok"], summary)
        self.assertEqual(7, summary["result"]["counts"]["gates"])
        self.assertEqual(project, summary["result"]["project"])

        gates = self.api.call(
            "netlist.gates", {"project": project, "gate_type": "FF", "limit": 1}
        )
        self.assertEqual(2, gates["result"]["page"]["total"])
        self.assertTrue(gates["result"]["page"]["has_more"])
        self.assertEqual(1, gates["result"]["page"]["next_offset"])

        seed = gates["result"]["gates"][0]["id"]
        cone = self.api.call(
            "netlist.cone",
            {"project": project, "seed_gate_ids": [seed], "direction": "fan_in", "depth": 2},
        )
        self.assertTrue(cone["ok"], cone)
        self.assertIn(seed, [entry["id"] for entry in cone["result"]["gates"]])
        self.assertIn("truncation", cone["result"])

    def test_every_netlist_response_validates_against_its_schema(self):
        project = self.open_fixture()
        for tool, request in (
            ("netlist.summary", {}),
            ("netlist.gates", {}),
            ("netlist.nets", {}),
            ("netlist.modules", {}),
            ("netlist.gate", {"gate_id": 1}),
            ("netlist.net", {"net_id": 1}),
            ("netlist.cone", {"seed_gate_ids": [1]}),
        ):
            request = dict(request, project=project)
            envelope = self.api.call(tool, request)
            self.assertTrue(envelope["ok"], (tool, envelope))
            self.assertEqual([], schemas.response_errors(tool, envelope["result"]), tool)
            self.assertEqual([], schemas.envelope_errors(envelope), tool)

    def test_an_unknown_gate_id_survives_the_process_boundary_as_unknown_object(self):
        project = self.open_fixture()
        envelope = self.api.call("netlist.gate", {"project": project, "gate_id": 9999})
        self.assertFalse(envelope["ok"])
        self.assertEqual("unknown_object", envelope["error"]["code"])
        self.assertEqual({"kind": "gate", "id": 9999}, envelope["error"]["data"])

    def test_over_large_requests_are_rejected_before_hal_runs(self):
        project = self.open_fixture()
        envelope = self.api.call(
            "netlist.cone",
            {"project": project, "seed_gate_ids": [1], "max_gates": 10 ** 6},
        )
        self.assertEqual("invalid_request", envelope["error"]["code"])
        self.assertEqual([], self.executor.calls, "no hal process should have started")


class QueryFailureTests(ApiTestCase):
    def test_a_timeout_is_reported_as_timeout_with_its_limit(self):
        self.executor.mode = "timeout"
        project = self.open_fixture()
        envelope = self.api.call(
            "netlist.summary", {"project": project, "timeout_s": 7}
        )
        self.assertFalse(envelope["ok"])
        self.assertEqual("timeout", envelope["error"]["code"])
        self.assertEqual(7, envelope["error"]["data"]["timeout_s"])

    def test_a_crashed_hal_is_hal_error_with_its_exit_code(self):
        self.executor.mode = "crash"
        project = self.open_fixture()
        envelope = self.api.call("netlist.summary", {"project": project})
        self.assertEqual("hal_error", envelope["error"]["code"])
        self.assertEqual(139, envelope["error"]["data"]["exit_code"])

    def test_a_clean_exit_with_no_answer_is_still_a_failure(self):
        self.executor.mode = "silent"
        project = self.open_fixture()
        envelope = self.api.call("netlist.summary", {"project": project})
        self.assertFalse(envelope["ok"])
        self.assertEqual("hal_error", envelope["error"]["code"])

    def test_without_a_hal_binary_netlist_tools_say_so(self):
        api = AnalysisApi(workspace=self.workspace, hal_binary=os.path.join(
            self.workspace, "does-not-exist"))
        api.queries.executor = self.executor
        os.environ.pop("HAL_RUNNER_HAL_BINARY", None)
        old_path = os.environ.get("PATH")
        old_base = os.environ.pop("HAL_BASE_PATH", None)
        os.environ["PATH"] = ""
        try:
            project = self.open_fixture()
            envelope = api.call("netlist.summary", {"project": project})
        finally:
            if old_path is not None:
                os.environ["PATH"] = old_path
            if old_base is not None:
                os.environ["HAL_BASE_PATH"] = old_base
        self.assertFalse(envelope["ok"])
        self.assertEqual("hal_unavailable", envelope["error"]["code"])


# ---------------------------------------------------------------------------
# 5. discovery
# ---------------------------------------------------------------------------


class DiscoveryTests(ApiTestCase):
    def test_capabilities_describes_every_tool_and_analysis(self):
        envelope = self.api.call("hal.capabilities", {})
        self.assertTrue(envelope["ok"], envelope)
        result = envelope["result"]
        self.assertEqual(sorted(schemas.tool_names()),
                         sorted(entry["name"] for entry in result["tools"]))
        self.assertIn("graph_algorithm.connected_components",
                      [entry["name"] for entry in result["analyses"]])
        self.assertEqual(sorted(errors.ERROR_CODES), sorted(result["error_codes"]))
        self.assertEqual([], schemas.response_errors("hal.capabilities", result))

    def test_capabilities_marks_read_only_tools(self):
        result = self.api.call("hal.capabilities", {})["result"]
        mutates = {entry["name"]: entry["mutates"] for entry in result["tools"]}
        for name in ("netlist.summary", "netlist.cone", "findings.get", "artifact.get"):
            self.assertEqual("nothing", mutates[name], name)
        self.assertEqual("session", mutates["project.close"])
        self.assertEqual("job", mutates["analysis.submit"])

    def test_schema_tool_returns_usable_schemas(self):
        envelope = self.api.call("hal.schema", {"tool": "analysis.submit"})
        self.assertTrue(envelope["ok"])
        request_schema = envelope["result"]["request_schema"]
        self.assertEqual([], list(jsonschema_mini.iter_errors(
            {"project": "prj-" + "b" * 12, "analysis": "dataflow.groups"}, request_schema)))

    def test_schema_of_an_unknown_tool_is_unknown_tool(self):
        envelope = self.api.call("hal.schema", {"tool": "netlist.everything"})
        self.assertEqual("unknown_tool", envelope["error"]["code"])

    def test_analysis_list_matches_the_runner_registry(self):
        from hal_runner import analyses as runner_analyses

        result = self.api.call("analysis.list", {})["result"]
        self.assertEqual(runner_analyses.names(),
                         sorted(entry["name"] for entry in result["analyses"]))
        options = {
            entry["name"]: entry["options"]
            for entry in result["analyses"]
        }["graph_algorithm.connected_components"]
        self.assertIn("min_size", [option["name"] for option in options])


# ---------------------------------------------------------------------------
# 6. jobs
# ---------------------------------------------------------------------------


def _findings_document(job_label="stub"):
    """A small, valid findings document, built with the hal_findings builders."""
    artifact = findings_model.artifact(
        "netlist", kind="netlist", path="cone_fixture.v", sha256="0" * 64
    )
    finding = findings_model.finding(
        "loop-1",
        "feedback loop through ff0 and ff1",
        findings_model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
        findings_model.method("connected components", "structural", False),
        findings_model.scope(["netlist"], description=job_label),
        bounds_dict=findings_model.unbounded("a structural claim about connectivity"),
        assumptions=[
            findings_model.assumption(
                "graph", "the netlist graph is derived from gate connectivity", kind="tool"
            )
        ],
        evidence_list=[
            findings_model.evidence("inline", description="component", inline="ff0, ff1")
        ],
    )
    second = findings_model.finding(
        "gap-1",
        "one gate type was not covered",
        findings_model.STATUS_UNSUPPORTED,
        findings_model.method("connected components", "structural", False),
        findings_model.scope(["netlist"]),
        unsupported_dict=findings_model.unsupported(
            "primitive",
            "the analysis does not model this cell",
            primitives=[findings_model.unsupported_primitive("FF", "sequential")],
        ),
    )
    return findings_model.document(
        {"name": "stub", "version": "1.0.0"},
        [artifact],
        {
            "plugin": {"name": "graph_algorithm", "version": "1.0.0"},
            "entry_point": "graph_algorithm.get_connected_components",
        },
        [finding, second],
    )


class JobTests(ApiTestCase):
    def _complete_job(self, job_id, state="succeeded"):
        """Write what a finished worker would have written."""
        store = self.api.jobs
        record = store.read(job_id)
        step_dir = os.path.join(record["run_dir"], "steps", "analysis")
        os.makedirs(os.path.join(step_dir, "logs"), exist_ok=True)
        findings_path = os.path.join(step_dir, "findings.json")
        findings_serialize.write_document(_findings_document(job_id), findings_path)
        with open(os.path.join(step_dir, "logs", "stdout.log"), "w", encoding="utf-8") as handle:
            handle.write("x" * 5000)
        manifest_path = os.path.join(record["run_dir"], "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump({"manifest_version": "1.0.0"}, handle)
        record.update(
            {
                "state": state,
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:10Z",
                "duration_s": 10.0,
                "exit_code": 0,
                "step_status": "success",
                "findings_path": findings_path,
                "findings_source": "findings",
                "findings_counts": {"proven_under_assumptions": 1, "unsupported": 1},
                "manifest_path": manifest_path,
                "pid": None,
            }
        )
        store.write(record)
        return record

    def test_submitting_an_unknown_analysis_is_unknown_analysis(self):
        project = self.open_fixture()
        envelope = self.api.call(
            "analysis.submit", {"project": project, "analysis": "does.not.exist"}
        )
        self.assertEqual("unknown_analysis", envelope["error"]["code"])
        self.assertIn("known", envelope["error"]["data"])

    def test_a_mistyped_option_is_rejected_at_submit_time(self):
        project = self.open_fixture()
        envelope = self.api.call(
            "analysis.submit",
            {
                "project": project,
                "analysis": "graph_algorithm.connected_components",
                "config": {"min_sizee": 2},
            },
        )
        self.assertEqual("invalid_request", envelope["error"]["code"])
        self.assertIn("min_sizee", envelope["error"]["detail"])

    def test_submission_records_a_reproducible_run_configuration(self):
        project = self.open_fixture()
        envelope = self.api.call(
            "analysis.submit",
            {
                "project": project,
                "analysis": "graph_algorithm.connected_components",
                "config": {"min_size": 3},
                "label": "loops",
            },
        )
        self.assertTrue(envelope["ok"], envelope)
        status = envelope["result"]["status"]
        self.assertEqual("queued", status["state"])
        self.assertEqual("loops", status["label"])
        self.assertFalse(status["terminal"])
        # the resolved configuration carries the analysis defaults, not just the override
        self.assertEqual(3, status["config"]["min_size"])
        self.assertIn("max_components", status["config"])
        with open(status["run_config_path"], "r", encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual("graph_algorithm.connected_components",
                         document["steps"][0]["analysis"])
        self.assertEqual([], schemas.response_errors("analysis.submit", envelope["result"]))

    def test_a_worker_that_vanished_becomes_lost_not_running_for_ever(self):
        project = self.open_fixture()
        self.api.jobs.spawn = lambda job_dir, record: 0x7FFFFFF0  # a pid that is not alive
        job = self.api.call(
            "analysis.submit",
            {"project": project, "analysis": "graph_algorithm.connected_components"},
        )["result"]["job"]
        status = self.api.call("analysis.status", {"job": job})["result"]["status"]
        self.assertEqual("lost", status["state"])
        self.assertTrue(status["terminal"])
        self.assertEqual("internal", status["error"]["code"])

    def test_waiting_for_a_running_job_times_out_explicitly(self):
        project = self.open_fixture()
        job = self.api.call(
            "analysis.submit",
            {"project": project, "analysis": "graph_algorithm.connected_components"},
        )["result"]["job"]
        envelope = self.api.call("analysis.status", {"job": job, "wait_s": 0.5})
        status = envelope["result"]["status"]
        self.assertTrue(envelope["ok"], "a wait that expires is not an error")
        self.assertTrue(status["wait_timed_out"])
        self.assertFalse(status["terminal"])
        self.assertGreaterEqual(status["waited_s"], 0.4)

    def test_an_unknown_job_is_unknown_job(self):
        for tool, request in (
            ("analysis.status", {"job": "job-" + "0" * 12}),
            ("analysis.cancel", {"job": "job-" + "0" * 12}),
            ("findings.get", {"job": "job-" + "0" * 12}),
            ("artifact.list", {"job": "job-" + "0" * 12}),
            ("artifact.get", {"job": "job-" + "0" * 12, "path": "x"}),
        ):
            envelope = self.api.call(tool, request)
            self.assertEqual("unknown_job", envelope["error"]["code"], tool)

    def test_cancelling_a_finished_job_says_so_instead_of_pretending(self):
        project = self.open_fixture()
        job = self.api.call(
            "analysis.submit",
            {"project": project, "analysis": "graph_algorithm.connected_components"},
        )["result"]["job"]
        self._complete_job(job)
        envelope = self.api.call("analysis.cancel", {"job": job})
        self.assertTrue(envelope["ok"])
        self.assertFalse(envelope["result"]["cancelled"])
        self.assertIn("already", envelope["result"]["reason"])
        self.assertEqual("succeeded", envelope["result"]["status"]["state"])

    def test_cancelling_a_pending_job_makes_it_terminal(self):
        project = self.open_fixture()
        job = self.api.call(
            "analysis.submit",
            {"project": project, "analysis": "graph_algorithm.connected_components"},
        )["result"]["job"]
        envelope = self.api.call("analysis.cancel", {"job": job})
        self.assertTrue(envelope["result"]["cancelled"])
        self.assertEqual("cancelled", envelope["result"]["status"]["state"])

    def test_findings_before_the_job_finished_are_not_ready(self):
        project = self.open_fixture()
        job = self.api.call(
            "analysis.submit",
            {"project": project, "analysis": "graph_algorithm.connected_components"},
        )["result"]["job"]
        envelope = self.api.call("findings.get", {"job": job})
        self.assertEqual("not_ready", envelope["error"]["code"])

    def test_findings_are_validated_paginated_and_filterable(self):
        project = self.open_fixture()
        job = self.api.call(
            "analysis.submit",
            {"project": project, "analysis": "graph_algorithm.connected_components"},
        )["result"]["job"]
        self._complete_job(job)

        envelope = self.api.call("findings.get", {"job": job, "limit": 1})
        self.assertTrue(envelope["ok"], envelope)
        result = envelope["result"]
        self.assertEqual("findings", result["source"])
        self.assertTrue(result["validated"])
        self.assertEqual(1, len(result["findings"]))
        self.assertEqual(2, result["page"]["total"])
        self.assertEqual({"proven_under_assumptions": 1, "unsupported": 1}, result["counts"])
        self.assertEqual([], schemas.response_errors("findings.get", result))

        filtered = self.api.call("findings.get", {"job": job, "status": "unsupported"})
        self.assertEqual(1, filtered["result"]["page"]["total"])
        self.assertEqual("gap-1", filtered["result"]["findings"][0]["id"])
        # the counts still describe the whole document, not the filtered page
        self.assertEqual(2, sum(filtered["result"]["counts"].values()))

    def test_a_corrupted_findings_document_is_withheld_not_returned(self):
        project = self.open_fixture()
        job = self.api.call(
            "analysis.submit",
            {"project": project, "analysis": "graph_algorithm.connected_components"},
        )["result"]["job"]
        record = self._complete_job(job)
        document = _findings_document()
        document["findings"][0]["status"] = "not-a-status"
        with open(record["findings_path"], "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        envelope = self.api.call("findings.get", {"job": job})
        self.assertEqual("internal", envelope["error"]["code"])

    def test_artifacts_are_listed_and_bounded(self):
        project = self.open_fixture()
        job = self.api.call(
            "analysis.submit",
            {"project": project, "analysis": "graph_algorithm.connected_components"},
        )["result"]["job"]
        self._complete_job(job)

        listing = self.api.call("artifact.list", {"job": job})["result"]
        paths = [entry["path"] for entry in listing["artifacts"]]
        self.assertIn("run_config.json", paths)
        self.assertIn("run/steps/analysis/findings.json", paths)
        self.assertEqual([], schemas.response_errors("artifact.list", listing))

        envelope = self.api.call(
            "artifact.get",
            {"job": job, "path": "run/steps/analysis/logs/stdout.log", "max_bytes": 100},
        )
        result = envelope["result"]
        self.assertEqual(100, result["returned_bytes"])
        self.assertEqual(5000, result["size_bytes"])
        self.assertTrue(result["truncation"]["truncated"])
        self.assertEqual("bytes", result["truncation"]["kind"])
        self.assertEqual(64, len(result["sha256"]))

    def test_artifact_paths_cannot_escape_the_job_directory(self):
        project = self.open_fixture()
        job = self.api.call(
            "analysis.submit",
            {"project": project, "analysis": "graph_algorithm.connected_components"},
        )["result"]["job"]
        envelope = self.api.call("artifact.get", {"job": job, "path": "../../../etc/passwd"})
        self.assertEqual("invalid_request", envelope["error"]["code"])

    def test_an_unknown_artifact_lists_what_there_is(self):
        project = self.open_fixture()
        job = self.api.call(
            "analysis.submit",
            {"project": project, "analysis": "graph_algorithm.connected_components"},
        )["result"]["job"]
        self._complete_job(job)
        envelope = self.api.call("artifact.get", {"job": job, "path": "nope.txt"})
        self.assertEqual("unknown_artifact", envelope["error"]["code"])
        self.assertIn("run_config.json", envelope["error"]["detail"])

    def test_the_worker_records_a_failed_run_as_a_diagnostic(self):
        """Run the real worker over a hal binary that cannot execute.

        This is the whole worker path -- job record, hal_runner configuration,
        manifest, diagnostic document, terminal state -- without needing a HAL:
        the 'hal binary' is a plain file, so the executor cannot start it, the
        step fails, and hal_runner writes the diagnostic findings document that
        ``findings.get`` must label as such rather than pass off as a result.
        """
        from hal_analysis_api import worker

        project = self.open_fixture()
        job = self.api.call(
            "analysis.submit",
            {"project": project, "analysis": "graph_algorithm.connected_components"},
        )["result"]["job"]

        import contextlib
        import io

        # The runner reports the failed step on stderr; in a real job that ends
        # up in worker.log, here it would only clutter the test output.
        with contextlib.redirect_stderr(io.StringIO()):
            exit_code = worker.run_job(self.api.jobs.job_dir(job))
        self.assertEqual(1, exit_code, "a failed analysis must not exit 0")

        status = self.api.call("analysis.status", {"job": job})["result"]["status"]
        self.assertEqual("failed", status["state"])
        self.assertTrue(status["terminal"])
        self.assertEqual("failed", status["step_status"])
        self.assertTrue(os.path.isfile(status["manifest_path"]))

        findings = self.api.call("findings.get", {"job": job})
        self.assertTrue(findings["ok"], findings)
        self.assertEqual("diagnostic", findings["result"]["source"])
        self.assertEqual(
            ["error"], sorted(findings["result"]["counts"]), findings["result"]["counts"]
        )
        paths = [entry["path"] for entry in
                 self.api.call("artifact.list", {"job": job})["result"]["artifacts"]]
        self.assertIn("run/manifest.json", paths)
        self.assertIn("run/steps/analysis/logs/stderr.log", paths)

    def test_jobs_are_listed_and_filtered(self):
        project = self.open_fixture()
        first = self.api.call(
            "analysis.submit",
            {"project": project, "analysis": "graph_algorithm.connected_components"},
        )["result"]["job"]
        self.api.call("analysis.submit", {"project": project, "analysis": "dataflow.groups"})
        self._complete_job(first)
        listing = self.api.call("analysis.jobs", {})["result"]
        self.assertEqual(2, listing["page"]["total"])
        succeeded = self.api.call("analysis.jobs", {"state": "succeeded"})["result"]
        self.assertEqual(1, succeeded["page"]["total"])
        self.assertEqual([], schemas.response_errors("analysis.jobs", succeeded))


# ---------------------------------------------------------------------------
# 7. dispatch and envelopes
# ---------------------------------------------------------------------------


class DispatchTests(ApiTestCase):
    def test_an_unknown_tool_is_an_envelope_not_an_exception(self):
        envelope = self.api.call("netlist.everything", {})
        self.assertFalse(envelope["ok"])
        self.assertEqual("unknown_tool", envelope["error"]["code"])
        self.assertIn("hal.capabilities", envelope["error"]["hint"])

    def test_every_tool_has_a_handler(self):
        for name in schemas.tool_names():
            self.assertTrue(
                hasattr(self.api, "_" + schemas.TOOLS[name].key),
                "no handler for {}".format(name),
            )

    def test_a_response_that_breaks_its_own_schema_is_withheld(self):
        self.api._analysis_list = lambda request: {"analyses": "not a list"}
        envelope = self.api.call("analysis.list", {})
        self.assertFalse(envelope["ok"])
        self.assertEqual("internal", envelope["error"]["code"])

    def test_a_handler_that_raises_becomes_an_internal_envelope(self):
        def explode(request):
            raise ZeroDivisionError("boom")

        self.api._analysis_list = explode
        envelope = self.api.call("analysis.list", {})
        self.assertEqual("internal", envelope["error"]["code"])
        self.assertIn("ZeroDivisionError", envelope["error"]["message"])

    def test_limits_are_reported_and_enforced_consistently(self):
        reported = self.api.call("hal.capabilities", {})["result"]["limits"]
        self.assertEqual(limits.MAX_PAGE_LIMIT, reported["page"]["max"])
        envelope = self.api.call(
            "netlist.gates", {"project": "prj-" + "0" * 12, "limit": limits.MAX_PAGE_LIMIT + 1}
        )
        self.assertEqual("invalid_request", envelope["error"]["code"])


# ---------------------------------------------------------------------------
# 8. the optional MCP adapter
# ---------------------------------------------------------------------------


class McpAdapterTests(ApiTestCase):
    def test_tool_descriptions_are_built_from_the_registry(self):
        from hal_analysis_api import mcp_adapter

        entries = mcp_adapter.describe_tools()
        self.assertEqual(sorted(schemas.tool_names()),
                         sorted(entry["name"] for entry in entries))
        for entry in entries:
            self.assertEqual("object", entry["inputSchema"]["type"], entry["name"])
            self.assertIn("read-only", " ".join(
                e["description"] for e in entries if e["name"] == "netlist.cone"))

    def test_calls_are_forwarded_and_errors_stay_structured(self):
        from hal_analysis_api import mcp_adapter

        text, is_error = mcp_adapter.handle_call(self.api, "hal.capabilities", {})
        self.assertFalse(is_error)
        self.assertTrue(json.loads(text)["ok"])

        text, is_error = mcp_adapter.handle_call(
            self.api, "netlist.summary", {"project": "prj-" + "0" * 12}
        )
        self.assertTrue(is_error)
        self.assertEqual("unknown_project", json.loads(text)["error"]["code"])

        text, is_error = mcp_adapter.handle_call(self.api, "no.such.tool", {})
        self.assertTrue(is_error)
        self.assertEqual("unknown_tool", json.loads(text)["error"]["code"])

    def test_availability_is_explicit_either_way(self):
        from hal_analysis_api import mcp_adapter

        state = mcp_adapter.availability()
        self.assertIn("available", state)
        if not state["available"]:
            self.assertIn("pip install", state["reason"])

    def test_server_construction_against_the_installed_sdk(self):
        from hal_analysis_api import mcp_adapter

        state = mcp_adapter.availability()
        if not state["available"]:
            self.skipTest("the optional 'mcp' package is not installed: " + state["reason"])
        server = mcp_adapter.build_server(self.api)
        self.assertIsNotNone(server)

    def test_tool_objects_build_against_the_installed_sdk(self):
        from hal_analysis_api import mcp_adapter

        state = mcp_adapter.availability()
        if not state["available"]:
            self.skipTest("the optional 'mcp' package is not installed")
        import mcp.types as types_module

        tools = mcp_adapter._tool_objects(types_module)
        self.assertEqual(len(schemas.TOOLS), len(tools))
        result = mcp_adapter._call_result(types_module, "{}", True)
        payload = result.model_dump(by_alias=True)
        self.assertTrue(payload["isError"])


# ---------------------------------------------------------------------------
# 9. the command line
# ---------------------------------------------------------------------------


class CliTests(ApiTestCase):
    def _run(self, argv):
        from hal_analysis_api import cli
        import io
        import contextlib

        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_tools_listing(self):
        code, out, _ = self._run(["tools", "--json"])
        self.assertEqual(0, code)
        self.assertEqual(len(schemas.TOOLS), len(json.loads(out)["tools"]))

    def test_schema_command(self):
        code, out, _ = self._run(["schema", "netlist.cone"])
        self.assertEqual(0, code)
        self.assertEqual("object", json.loads(out)["request_schema"]["type"])

    def test_schema_of_unknown_tool_is_a_usage_error(self):
        code, _, err = self._run(["schema", "nope"])
        self.assertEqual(2, code)
        self.assertIn("unknown tool", err)

    def test_call_returns_the_envelope_and_exit_code_one_on_error(self):
        code, out, _ = self._run(
            ["--workspace", self.workspace, "call", "project.describe",
             "--arg", "project=prj-000000000000"]
        )
        self.assertEqual(1, code)
        envelope = json.loads(out)
        self.assertFalse(envelope["ok"])
        self.assertEqual("unknown_project", envelope["error"]["code"])

    def test_call_parses_json_arguments(self):
        code, out, _ = self._run(
            ["--workspace", self.workspace, "call", "hal.schema", "--json",
             '{"tool": "netlist.gate"}']
        )
        self.assertEqual(0, code)
        self.assertEqual("netlist.gate", json.loads(out)["result"]["tool"])

    def test_capabilities_command_runs_without_hal(self):
        code, out, _ = self._run(["--workspace", self.workspace, "capabilities"])
        self.assertEqual(0, code)
        self.assertTrue(json.loads(out)["ok"])


if __name__ == "__main__":
    unittest.main()
