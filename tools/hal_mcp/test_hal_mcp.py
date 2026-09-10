#!/usr/bin/env python3
"""Tests for the HAL MCP server, driven as a real subprocess over its pipes.

Nothing here imports the server: it is spawned exactly the way an MCP client
spawns it (``python3 tools/hal_mcp``) and spoken to over stdin/stdout, because
the contract under test is the *stream*, not the Python functions behind it.

Two halves:

:class:`ProtocolContract`
    Handshake, ``tools/list``, error codes, notifications, pagination shape.
    These run on a bare checkout -- no build, no ``hal_py`` -- because the
    protocol must be right before HAL is even in the picture.
:class:`NetlistContract`, :class:`FileDescriptorGuard`, :class:`AnalysisContract`
    Real tool calls against ``examples/agilex3_walkthroughs/01_blinky_counter``
    (50 gates: 24 ``tennm_ff``, 24 ``tennm_lcell_comb``, ``HAL_GND``,
    ``HAL_VCC``; one clock domain; 24 two-gate SCCs).  They skip, with the
    reason, when ``hal_py`` cannot be imported.

Run against a build tree::

    HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib PYTHONPATH=<build>/lib \\
        python3 tools/hal_mcp/test_hal_mcp.py
"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
SERVER = TOOLS / "hal_mcp"

NETLIST = (
    REPO_ROOT / "examples" / "agilex3_walkthroughs" / "01_blinky_counter" / "netlist.hal.v"
)
GATE_LIBRARY = REPO_ROOT / "plugins" / "gate_libraries" / "definitions" / "AGILEX_TENNM.hgl"

#: Generous: a cold start loads 28 plugins, and DANA is slow by nature.
DEFAULT_TIMEOUT = 120
SLOW_TIMEOUT = 600

PROTOCOL_VERSION = "2025-06-18"


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------


def environment():
    env = dict(os.environ)
    search = [str(TOOLS)]
    for part in env.get("HAL_PY_PATH", "").split(os.pathsep):
        if part:
            search.append(part)
    if env.get("PYTHONPATH"):
        search.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(search)
    env["PYTHONIOENCODING"] = "utf-8"
    env["TERM"] = "dumb"
    return env


_PROBE = (
    "import os, sys\n"
    "for entry in os.environ.get('HAL_PY_PATH', '').split(os.pathsep):\n"
    "    if entry.strip():\n"
    "        sys.path.insert(0, entry.strip())\n"
    "import hal_py\n"
)

_HAL_REASON = None


def hal_unavailable_reason():
    """``None`` when ``hal_py`` and the fixtures are usable, else why not."""
    global _HAL_REASON
    if _HAL_REASON is not None:
        return _HAL_REASON or None
    if not NETLIST.is_file() or not GATE_LIBRARY.is_file():
        _HAL_REASON = "the 01_blinky_counter netlist or the AGILEX_TENNM gate library is missing"
        return _HAL_REASON
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _PROBE],
            env=environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=DEFAULT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _HAL_REASON = "could not probe for hal_py: {}".format(exc)
        return _HAL_REASON
    if completed.returncode == 0:
        _HAL_REASON = ""
        return None
    detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
    _HAL_REASON = "hal_py is not importable here ({})".format(
        detail[-1] if detail else "exit {}".format(completed.returncode)
    )
    return _HAL_REASON


def requires_hal(case):
    reason = hal_unavailable_reason()
    if reason:
        case.skipTest(reason)


# ---------------------------------------------------------------------------
# a minimal MCP client
# ---------------------------------------------------------------------------


class ServerClosed(AssertionError):
    pass


class McpClient(object):
    """Spawns ``python3 tools/hal_mcp serve`` and talks JSON-RPC to it.

    stdout is read by a background thread into a queue so that every wait has a
    timeout: a server that answers nothing must fail the test, not hang it.
    stderr goes to a temp file, which both keeps HAL's very chatty logging from
    filling a pipe and makes it available to the assertions.
    """

    def __init__(self, args=("serve",)):
        self.stderr_file = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER)] + list(args),
            cwd=str(REPO_ROOT),
            env=environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr_file,
        )
        self._queue = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self._next_id = 0

    def _pump(self):
        try:
            for line in iter(self.process.stdout.readline, b""):
                self._queue.put(line)
        finally:
            self._queue.put(None)

    # -- raw stream ---------------------------------------------------------

    def send_raw(self, text):
        self.process.stdin.write(text.encode("utf-8") + b"\n")
        self.process.stdin.flush()

    def send(self, message):
        self.send_raw(json.dumps(message))

    def read_line(self, timeout=DEFAULT_TIMEOUT):
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(
                "no protocol line within {}s.\n-- stderr --\n{}".format(
                    timeout, self.stderr_text()[-4000:]
                )
            )
        if item is None:
            raise ServerClosed(
                "the server closed its protocol stream.\n-- stderr --\n{}".format(
                    self.stderr_text()[-4000:]
                )
            )
        return item.decode("utf-8")

    def read_message(self, timeout=DEFAULT_TIMEOUT):
        line = self.read_line(timeout)
        try:
            return json.loads(line)
        except ValueError as exc:
            raise AssertionError(
                "the protocol stream is not parseable JSON ({}). Offending line:\n"
                "{!r}\nThis is the fd-1 leak the server exists to prevent.".format(exc, line)
            )

    def expect_silence(self, seconds=1.0):
        try:
            item = self._queue.get(timeout=seconds)
        except queue.Empty:
            return
        raise AssertionError("expected no response, got {!r}".format(item))

    # -- requests -----------------------------------------------------------

    def request(self, method, params=None, timeout=DEFAULT_TIMEOUT):
        self._next_id += 1
        identifier = self._next_id
        message = {"jsonrpc": "2.0", "id": identifier, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        while True:
            response = self.read_message(timeout)
            if response.get("id") == identifier:
                return response

    def handshake(self, protocol_version=PROTOCOL_VERSION):
        response = self.request(
            "initialize",
            {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "test_hal_mcp", "version": "1.0.0"},
            },
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    def call(self, name, arguments=None, timeout=DEFAULT_TIMEOUT):
        """``tools/call`` -> ``(payload, is_error)``; payload is parsed when it is JSON."""
        response = self.request(
            "tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout
        )
        if "error" in response:
            raise AssertionError(
                "tools/call returned a JSON-RPC error, which is reserved for "
                "protocol faults: {}".format(response["error"])
            )
        result = response["result"]
        text = "".join(
            block.get("text", "") for block in result.get("content", []) if block.get("type") == "text"
        )
        if result.get("isError"):
            return text, True
        try:
            return json.loads(text), False
        except ValueError:
            return text, False

    # -- teardown -----------------------------------------------------------

    def stderr_text(self):
        try:
            self.stderr_file.flush()
            self.stderr_file.seek(0)
            return self.stderr_file.read().decode("utf-8", "replace")
        except (OSError, ValueError):
            return "<stderr unavailable>"

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.stdin.close()
                self.process.wait(timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
            self.process.wait(timeout=30)
        finally:
            try:
                self.process.stdout.close()
            except OSError:
                pass
            self.stderr_file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# protocol -- no HAL required
# ---------------------------------------------------------------------------


class ProtocolContract(unittest.TestCase):
    """The transport and the MCP methods, on a checkout with no build."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.client = McpClient()
        cls.client.handshake()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_initialize_echoes_a_supported_protocol_version(self):
        with McpClient() as client:
            response = client.request(
                "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}
            )
            result = response["result"]
            self.assertEqual("2024-11-05", result["protocolVersion"])
            self.assertIn("tools", result["capabilities"])
            self.assertEqual("hal-mcp", result["serverInfo"]["name"])
            self.assertTrue(result["serverInfo"]["version"])

    def test_initialize_falls_back_to_its_newest_version(self):
        with McpClient() as client:
            response = client.request(
                "initialize", {"protocolVersion": "1999-01-01", "capabilities": {}}
            )
            self.assertEqual(PROTOCOL_VERSION, response["result"]["protocolVersion"])

    def test_ping(self):
        response = self.client.request("ping")
        self.assertEqual({}, response["result"])

    def test_notifications_get_no_response(self):
        """A message without an id must never be answered -- JSON-RPC 2.0."""
        with McpClient() as client:
            client.handshake()
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            client.send({"jsonrpc": "2.0", "method": "notifications/not-a-real-one"})
            client.expect_silence(1.0)
            # ... and the server is still alive and in order afterwards.
            self.assertEqual({}, client.request("ping")["result"])

    def test_malformed_json_is_a_parse_error(self):
        with McpClient() as client:
            client.send_raw("{this is not json")
            response = client.read_message()
            self.assertEqual(-32700, response["error"]["code"])
            self.assertIsNone(response["id"])
            # the stream survives a bad line
            self.assertEqual({}, client.request("ping")["result"])

    def test_unknown_method_is_method_not_found(self):
        response = self.client.request("netlist/teleport")
        self.assertEqual(-32601, response["error"]["code"])
        self.assertIn("netlist/teleport", response["error"]["message"])

    def test_batches_are_rejected_not_crashed(self):
        with McpClient() as client:
            client.send_raw(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]))
            response = client.read_message()
            self.assertEqual(-32600, response["error"]["code"])

    def test_tools_call_without_a_name_is_invalid_params(self):
        response = self.client.request("tools/call", {"arguments": {}})
        self.assertEqual(-32602, response["error"]["code"])

    # -- tools/list ---------------------------------------------------------

    def test_tools_list_shape(self):
        tools = self.client.request("tools/list")["result"]["tools"]
        self.assertTrue(tools, "the server advertises no tools")
        for tool in tools:
            self.assertIn("name", tool)
            self.assertTrue(tool.get("description"), "{} has no description".format(tool["name"]))
            schema = tool.get("inputSchema")
            self.assertIsInstance(schema, dict, "{} has no inputSchema".format(tool["name"]))
            self.assertEqual("object", schema.get("type"))
            self.assertIsInstance(schema.get("properties"), dict)
            for key, spec in schema["properties"].items():
                self.assertTrue(
                    spec.get("description"),
                    "{}.{} has no description; the schema is what an agent reads "
                    "to decide how to call the tool".format(tool["name"], key),
                )

    def test_every_documented_tool_is_present(self):
        names = {tool["name"] for tool in self.client.request("tools/list")["result"]["tools"]}
        expected = {
            "hal_load_netlist",
            "hal_load_project",
            "hal_list_sessions",
            "hal_close_session",
            "hal_netlist_stats",
            "hal_list_gates",
            "hal_gate_info",
            "hal_net_info",
            "hal_module_tree",
            "hal_fan_in",
            "hal_fan_out",
            "hal_shortest_path",
            "hal_boolean_function",
            "hal_sccs",
            "hal_dataflow_groups",
            "hal_clock_domains",
            "hal_render_graph",
        }
        self.assertEqual(expected, names)

    def test_paginated_tools_declare_bounded_limits(self):
        tools = self.client.request("tools/list")["result"]["tools"]
        paginated = [
            tool
            for tool in tools
            if "limit" in tool["inputSchema"]["properties"]
        ]
        self.assertTrue(paginated)
        for tool in paginated:
            properties = tool["inputSchema"]["properties"]
            self.assertEqual(100, properties["limit"]["default"], tool["name"])
            self.assertEqual(1000, properties["limit"]["maximum"], tool["name"])
            self.assertIn("offset", properties, tool["name"])

    # -- tool-level failures are results, not protocol errors ---------------

    def test_unknown_session_id_is_a_tool_error(self):
        payload, is_error = self.client.call("hal_netlist_stats", {"session_id": "s999"})
        self.assertTrue(is_error, "a bad session id must set isError")
        self.assertIn("s999", payload)
        self.assertIn("hal_load_netlist", payload)

    def test_unknown_tool_is_a_tool_error(self):
        payload, is_error = self.client.call("hal_do_something_impossible")
        self.assertTrue(is_error)
        self.assertIn("hal_load_netlist", payload)

    def test_unknown_argument_is_a_tool_error(self):
        payload, is_error = self.client.call(
            "hal_list_sessions", {"nonsense_argument": 1}
        )
        self.assertTrue(is_error)
        self.assertIn("nonsense_argument", payload)

    def test_list_sessions_starts_empty_and_reports_a_total(self):
        payload, is_error = self.client.call("hal_list_sessions")
        self.assertFalse(is_error, payload)
        self.assertEqual(0, payload["total"])
        self.assertEqual([], payload["sessions"])

    def test_limit_above_the_hard_maximum_is_refused(self):
        payload, is_error = self.client.call("hal_list_sessions", {"limit": 5000})
        self.assertTrue(is_error)
        self.assertIn("1000", payload)

    def test_loading_a_missing_netlist_is_a_tool_error(self):
        payload, is_error = self.client.call(
            "hal_load_netlist", {"netlist_path": "/nonexistent/nope.v"}
        )
        self.assertTrue(is_error, "a netlist that will not load must not be a crash")
        self.assertTrue(payload.strip())


# ---------------------------------------------------------------------------
# the fd guard -- the single most important property of this server
# ---------------------------------------------------------------------------


class FileDescriptorGuard(unittest.TestCase):
    """HAL's native logging must never reach the protocol stream.

    HAL's spdlog sinks write to file descriptor 1, and loading a netlist means
    28 ``loading plugin`` lines plus the parser's own output.  The server dups
    fd 1 away for itself and puts fd 2 in its place before ``hal_py`` is
    imported; if that ever regresses, the first netlist load turns the stream
    into garbage and every assertion below fails at once.
    """

    def test_a_netlist_load_leaves_the_stream_parseable(self):
        requires_hal(self)
        with McpClient() as client:
            client.handshake()
            payload, is_error = client.call(
                "hal_load_netlist",
                {"netlist_path": str(NETLIST), "gate_library": str(GATE_LIBRARY)},
            )
            self.assertFalse(is_error, payload)
            session_id = payload["session_id"]

            # Every one of these logs on the native side; every response must
            # still be exactly one JSON object on one line.
            for _ in range(3):
                stats, is_error = client.call("hal_netlist_stats", {"session_id": session_id})
                self.assertFalse(is_error, stats)
                self.assertEqual(50, stats["counts"]["gates"])

            self.assertEqual({}, client.request("ping")["result"])

            stderr = client.stderr_text()
            self.assertIn(
                "[core]",
                stderr,
                "HAL logged nothing to stderr, so this test proved nothing: either "
                "the netlist did not really load or the log went somewhere else "
                "(fd 1?).",
            )

    def test_stray_stdout_writes_do_not_corrupt_the_stream(self):
        """A print() inside a tool goes to stderr, not into the protocol."""
        requires_hal(self)
        with McpClient() as client:
            client.handshake()
            payload, _ = client.call(
                "hal_load_netlist",
                {"netlist_path": str(NETLIST), "gate_library": str(GATE_LIBRARY)},
            )
            session_id = payload["session_id"]
            # DANA is the loudest thing in the tree; if anything leaks, it does.
            groups, is_error = client.call(
                "hal_dataflow_groups", {"session_id": session_id}, timeout=SLOW_TIMEOUT
            )
            if is_error:
                self.skipTest("dataflow_analysis is unavailable: {}".format(groups))
            self.assertIn("groups", groups)
            self.assertEqual({}, client.request("ping")["result"])


# ---------------------------------------------------------------------------
# the tool surface against a real design
# ---------------------------------------------------------------------------


class _LoadedNetlist(unittest.TestCase):
    """One server, one loaded netlist, many questions -- the point of the thing."""

    client = None
    session_id = None

    @classmethod
    def setUpClass(cls):
        if hal_unavailable_reason():
            return
        cls.client = McpClient()
        cls.client.handshake()
        payload, is_error = cls.client.call(
            "hal_load_netlist",
            {"netlist_path": str(NETLIST), "gate_library": str(GATE_LIBRARY)},
        )
        if is_error:
            cls.client.close()
            cls.client = None
            raise AssertionError("could not load the fixture netlist: {}".format(payload))
        cls.session_id = payload["session_id"]

    @classmethod
    def tearDownClass(cls):
        if cls.client is not None:
            cls.client.close()
            cls.client = None

    def setUp(self):
        requires_hal(self)

    def call(self, name, arguments=None, timeout=DEFAULT_TIMEOUT):
        arguments = dict(arguments or {})
        arguments.setdefault("session_id", self.session_id)
        payload, is_error = self.client.call(name, arguments, timeout=timeout)
        self.assertFalse(is_error, "{} failed: {}".format(name, payload))
        return payload


class NetlistContract(_LoadedNetlist):
    """Inspection and traversal against 01_blinky_counter."""

    def test_load_reports_a_session_and_a_summary(self):
        payload, _ = self.client.call("hal_list_sessions")
        self.assertEqual(1, payload["total"])
        self.assertEqual(self.session_id, payload["sessions"][0]["session_id"])
        self.assertEqual(50, payload["sessions"][0]["gates"])

    def test_netlist_stats_matches_the_walkthrough(self):
        stats = self.call("hal_netlist_stats")
        self.assertEqual(50, stats["counts"]["gates"])
        histogram = {entry["type"]: entry["count"] for entry in stats["gate_types"]}
        self.assertEqual(24, histogram["tennm_ff"])
        self.assertEqual(24, histogram["tennm_lcell_comb"])
        self.assertEqual(1, histogram["HAL_GND"])
        self.assertEqual(1, histogram["HAL_VCC"])
        self.assertEqual(24, stats["counts"]["sequential_gates"])
        self.assertTrue(stats["top_module"]["name"])
        self.assertGreater(stats["global_inputs"]["total"], 0)

    def test_list_gates_pagination_reports_the_true_total(self):
        first = self.call("hal_list_gates", {"limit": 5})
        self.assertEqual(50, first["total"])
        self.assertEqual(5, first["returned"])
        self.assertTrue(first["truncated"])

        tail = self.call("hal_list_gates", {"limit": 5, "offset": 48})
        self.assertEqual(50, tail["total"])
        self.assertEqual(2, tail["returned"])
        self.assertFalse(tail["truncated"])

        self.assertNotEqual(
            [row["name"] for row in first["gates"]],
            [row["name"] for row in tail["gates"]],
        )

    def test_list_gates_filters(self):
        flops = self.call("hal_list_gates", {"type_contains": "ff", "limit": 1000})
        self.assertEqual(24, flops["total"])
        self.assertTrue(all(row["is_sequential"] for row in flops["gates"]))

        sequential = self.call("hal_list_gates", {"sequential_only": True, "limit": 1000})
        self.assertEqual(24, sequential["total"])

        named = self.call("hal_list_gates", {"name_contains": "zzz-no-such-gate"})
        self.assertEqual(0, named["total"])

    def test_gate_info_reports_pins_with_their_nets(self):
        flops = self.call("hal_list_gates", {"type_contains": "tennm_ff", "limit": 1})
        name = flops["gates"][0]["name"]
        info = self.call("hal_gate_info", {"gate": name})
        self.assertEqual("tennm_ff", info["type"])
        self.assertTrue(info["is_sequential"])
        self.assertIn("sequential", info["properties"])
        pins = {row["pin"]: row for row in info["input_pins"]}
        self.assertIn("clk", pins)
        self.assertEqual("clock", pins["clk"]["pin_type"])
        self.assertTrue(pins["clk"]["net"])
        self.assertTrue(any(row["pin"] == "q" for row in info["output_pins"]))

    def test_gate_info_on_a_missing_gate_is_a_tool_error(self):
        payload, is_error = self.client.call(
            "hal_gate_info", {"session_id": self.session_id, "gate": "no_such_gate_zzz"}
        )
        self.assertTrue(is_error)
        self.assertIn("hal_list_gates", payload)

    def test_net_info_finds_the_clock(self):
        stats = self.call("hal_netlist_stats")
        clock_candidates = [
            name for name in stats["global_inputs"]["names"] if "clk" in name.lower()
        ]
        self.assertTrue(clock_candidates, stats["global_inputs"])
        info = self.call("hal_net_info", {"net": clock_candidates[0], "limit": 1000})
        self.assertTrue(info["is_global_input"])
        self.assertEqual(24, info["destination_count"])
        self.assertEqual(24, info["destinations_page"]["total"])
        self.assertTrue(all(row["pin"] == "clk" for row in info["destinations"]))

    def test_net_info_paginates_destinations(self):
        stats = self.call("hal_netlist_stats")
        clock = [name for name in stats["global_inputs"]["names"] if "clk" in name.lower()][0]
        page = self.call("hal_net_info", {"net": clock, "limit": 4, "offset": 20})
        self.assertEqual(24, page["destinations_page"]["total"])
        self.assertEqual(4, page["destinations_page"]["returned"])
        self.assertFalse(page["destinations_page"]["truncated"])

    def test_module_tree_starts_at_the_top_module(self):
        tree = self.call("hal_module_tree")
        self.assertGreaterEqual(tree["total"], 1)
        root = tree["modules"][0]
        self.assertEqual(0, root["depth"])
        self.assertIsNone(root["parent_id"])
        self.assertEqual(tree["root"]["id"], root["id"])

    def test_fan_in_and_fan_out_from_a_flip_flop(self):
        flops = self.call("hal_list_gates", {"type_contains": "tennm_ff", "limit": 1})
        name = flops["gates"][0]["name"]

        back = self.call("hal_fan_in", {"gate": name, "depth": 1})
        self.assertGreater(back["total"], 0)
        self.assertTrue(all(row["distance"] == 1 for row in back["gates"]))

        forward = self.call("hal_fan_out", {"gate": name, "depth": 3})
        self.assertGreater(forward["total"], 0)
        self.assertLessEqual(max(row["distance"] for row in forward["gates"]), 3)

    def test_stop_at_sequential_bounds_the_cone(self):
        flops = self.call("hal_list_gates", {"type_contains": "tennm_ff", "limit": 1})
        name = flops["gates"][0]["name"]
        wide = self.call("hal_fan_out", {"gate": name, "depth": 8})
        bounded = self.call(
            "hal_fan_out", {"gate": name, "depth": 8, "stop_at_sequential": True}
        )
        self.assertLessEqual(bounded["total"], wide["total"])

    def test_fan_out_from_a_net(self):
        stats = self.call("hal_netlist_stats")
        clock = [name for name in stats["global_inputs"]["names"] if "clk" in name.lower()][0]
        forward = self.call("hal_fan_out", {"net": clock, "depth": 1, "limit": 1000})
        self.assertEqual(24, forward["total"])
        self.assertTrue(all(row["distance"] == 1 for row in forward["gates"]))

    def test_fan_in_needs_exactly_one_starting_point(self):
        payload, is_error = self.client.call("hal_fan_in", {"session_id": self.session_id})
        self.assertTrue(is_error)
        self.assertIn("gate", payload)

    def test_shortest_path_between_two_gates(self):
        cells = self.call("hal_list_gates", {"type_contains": "lcell", "limit": 1000})
        names = [row["name"] for row in cells["gates"]]
        result = self.call(
            "hal_shortest_path",
            {"from_gate": names[0], "to_gate": names[-1], "search_both_directions": True},
        )
        self.assertIn("found", result)
        if result["found"]:
            self.assertEqual(names[0], result["path"][0]["name"])
            self.assertEqual(0, result["path"][0]["position"])


class AnalysisContract(_LoadedNetlist):
    """The plugin-backed analyses, and the visual output."""

    def test_boolean_function_of_an_alm_output(self):
        cells = self.call("hal_list_gates", {"type_contains": "lcell", "limit": 1})
        name = cells["gates"][0]["name"]
        by_pin = self.call("hal_boolean_function", {"gate": name})
        self.assertEqual("input pin name", by_pin["variable_kind"])
        self.assertTrue(by_pin["functions"])

        by_net = self.call(
            "hal_boolean_function", {"gate": name, "use_net_variables": True}
        )
        self.assertEqual("net (net_<id>)", by_net["variable_kind"])

    def test_boolean_function_rejects_an_unknown_pin(self):
        cells = self.call("hal_list_gates", {"type_contains": "lcell", "limit": 1})
        payload, is_error = self.client.call(
            "hal_boolean_function",
            {
                "session_id": self.session_id,
                "gate": cells["gates"][0]["name"],
                "pin": "not_a_pin",
            },
        )
        self.assertTrue(is_error)
        self.assertIn("not_a_pin", payload)

    def test_sccs_find_the_twenty_four_register_loops(self):
        payload, is_error = self.client.call(
            "hal_sccs", {"session_id": self.session_id, "limit": 1000}, timeout=SLOW_TIMEOUT
        )
        if is_error:
            self.skipTest("graph_algorithm is unavailable: {}".format(payload))
        self.assertEqual(24, payload["total"])
        self.assertTrue(all(entry["size"] == 2 for entry in payload["components"]))

    def test_sccs_paginate(self):
        payload, is_error = self.client.call(
            "hal_sccs",
            {"session_id": self.session_id, "limit": 5, "offset": 20},
            timeout=SLOW_TIMEOUT,
        )
        if is_error:
            self.skipTest("graph_algorithm is unavailable: {}".format(payload))
        self.assertEqual(24, payload["total"])
        self.assertEqual(4, payload["returned"])

    def test_clock_domains_are_one_domain_on_the_clock(self):
        domains = self.call("hal_clock_domains", {"limit": 1000})
        self.assertEqual(24, domains["sequential_gates"])
        self.assertEqual(1, domains["total"])
        only = domains["domains"][0]
        self.assertEqual(24, only["size"])
        self.assertEqual(1, len(only["clock"]))
        self.assertIn("clk", only["clock"][0].lower())
        self.assertEqual(0, domains["unclocked_sequential_gates"]["total"])
        self.assertIn("issue #63", domains["method"])

    def test_dataflow_groups(self):
        payload, is_error = self.client.call(
            "hal_dataflow_groups",
            {"session_id": self.session_id, "limit": 1000},
            timeout=SLOW_TIMEOUT,
        )
        if is_error:
            self.skipTest("dataflow_analysis is unavailable: {}".format(payload))
        self.assertGreaterEqual(payload["total"], 1)
        self.assertEqual(24, payload["grouped_gates"])

    def test_render_graph_writes_a_module_tree(self):
        directory = tempfile.mkdtemp(prefix="hal_mcp_test_")
        output = os.path.join(directory, "tree.svg")
        payload = self.call(
            "hal_render_graph",
            {"kind": "module_tree", "output_path": output},
            timeout=SLOW_TIMEOUT,
        )
        self.assertEqual("module_tree", payload["kind"])
        self.assertTrue(os.path.isfile(payload["dot_path"]), payload)
        self.assertGreater(payload["nodes"], 0)
        if payload["rendered"]:
            self.assertTrue(os.path.isfile(payload["rendered"]))
        else:
            self.assertIn("note", payload)

    def test_render_graph_refuses_an_unknown_kind(self):
        payload, is_error = self.client.call(
            "hal_render_graph",
            {
                "session_id": self.session_id,
                "kind": "hairball",
                "output_path": os.path.join(tempfile.gettempdir(), "x.svg"),
            },
        )
        self.assertTrue(is_error)
        self.assertIn("module_tree", payload)

    def test_render_graph_refuses_an_oversized_scope(self):
        payload, is_error = self.client.call(
            "hal_render_graph",
            {
                "session_id": self.session_id,
                "kind": "netlist_graph",
                "output_path": os.path.join(tempfile.gettempdir(), "big.svg"),
                "max_gates": 5,
            },
        )
        self.assertTrue(is_error)
        self.assertIn("max_gates", payload)


class SessionLifecycle(unittest.TestCase):
    """Sessions are independent, and closing one keeps the others."""

    def test_two_sessions_and_a_close(self):
        requires_hal(self)
        with McpClient() as client:
            client.handshake()
            first, _ = client.call(
                "hal_load_netlist",
                {"netlist_path": str(NETLIST), "gate_library": str(GATE_LIBRARY)},
            )
            second, _ = client.call(
                "hal_load_netlist",
                {"netlist_path": str(NETLIST), "gate_library": str(GATE_LIBRARY)},
            )
            self.assertNotEqual(first["session_id"], second["session_id"])

            listing, _ = client.call("hal_list_sessions")
            self.assertEqual(2, listing["total"])

            closed, is_error = client.call(
                "hal_close_session", {"session_id": first["session_id"]}
            )
            self.assertFalse(is_error, closed)
            self.assertEqual([second["session_id"]], closed["open_sessions"])

            gone, is_error = client.call(
                "hal_netlist_stats", {"session_id": first["session_id"]}
            )
            self.assertTrue(is_error)

            still_there, is_error = client.call(
                "hal_netlist_stats", {"session_id": second["session_id"]}
            )
            self.assertFalse(is_error, still_there)
            self.assertEqual(50, still_there["counts"]["gates"])


class CommandLine(unittest.TestCase):
    """The CLI contract tests/cli_contract enforces, checked here too."""

    def _run(self, args):
        return subprocess.run(
            [sys.executable, str(SERVER)] + list(args),
            cwd=str(REPO_ROOT),
            env=environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=DEFAULT_TIMEOUT,
            universal_newlines=True,
        )

    def test_help_exits_zero_with_usage(self):
        completed = self._run(["--help"])
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("usage", completed.stdout.lower())

    def test_unknown_option_is_rejected_without_a_traceback(self):
        completed = self._run(["--not-a-real-option-zzz"])
        self.assertNotEqual(0, completed.returncode)
        self.assertTrue(completed.stderr.strip())
        self.assertNotIn("Traceback (most recent call last)", completed.stderr)

    def test_unknown_subcommand_is_rejected_without_a_traceback(self):
        completed = self._run(["not-a-real-subcommand-zzz"])
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("not-a-real-subcommand-zzz", completed.stdout + completed.stderr)
        self.assertNotIn("Traceback (most recent call last)", completed.stderr)

    def test_tools_subcommand_lists_the_surface_without_hal(self):
        completed = self._run(["tools"])
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("hal_load_netlist", completed.stdout)
        self.assertIn("hal_render_graph", completed.stdout)


if __name__ == "__main__":
    reason = hal_unavailable_reason()
    sys.stderr.write("repository: {}\n".format(REPO_ROOT))
    sys.stderr.write("hal_py:     {}\n".format(reason or "available"))
    sys.stderr.write("netlist:    {}\n\n".format(NETLIST))
    sys.stderr.flush()
    unittest.main(verbosity=2)
