"""The stdio transport, the MCP dispatch, and the command line entry point.

Transport
---------
MCP over stdio is newline-delimited JSON-RPC 2.0: exactly one UTF-8 JSON object
per line, no embedded raw newlines.  That is few enough moving parts to
implement directly, which is what this module does -- hal_mcp has no
dependency outside the Python standard library.

The one thing that will destroy it
----------------------------------
**stdout is the protocol channel, and HAL's C++ logging writes to file
descriptor 1.**  A single ``[core] [info] loading plugin ...`` line landing
between two JSON objects makes the stream unparseable, and loading a netlist
emits dozens of them.  :func:`install_stdout_guard` therefore runs before
anything imports ``hal_py``: it dups fd 1 to a private handle that only the
protocol writer holds, then dups fd 2 *onto* fd 1, so every native log line,
every ``print()`` and every library that assumes stdout is free ends up on
stderr.  This is the same manoeuvre ``tools/hal_capabilities/cli.py`` performs
around its document output; here it is permanent, because the protocol stream
is the whole session.
"""

import argparse
import json
import os
import sys

from . import SERVER_NAME, __version__
from . import tools as tool_registry
from .session import SessionStore, ToolError

__all__ = ["Server", "install_stdout_guard", "build_parser", "main"]

#: Protocol revisions this server knows how to speak, newest first.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = PROTOCOL_VERSIONS[0]

INSTRUCTIONS = (
    "Load a netlist once with hal_load_netlist (or hal_load_project) and reuse "
    "the session_id it returns for every other tool: the parsed netlist stays "
    "in this server, so follow-up questions are instant. Start with "
    "hal_netlist_stats, then narrow down with hal_list_gates / hal_gate_info / "
    "hal_net_info, traverse with hal_fan_in / hal_fan_out, and reach for "
    "hal_sccs, hal_clock_domains and hal_dataflow_groups to separate state from "
    "logic. Every listing tool is paginated and reports the true total. Nothing "
    "here modifies the design."
)


# ---------------------------------------------------------------------------
# the file descriptor guard
# ---------------------------------------------------------------------------


def install_stdout_guard():
    """Reserve fd 1 for the protocol and send everything else to stderr.

    Returns a binary file object that writes to the *original* stdout.  Call
    this before importing ``hal_py`` -- and before importing anything that
    might import it -- because HAL installs its spdlog sinks on fd 1 at load
    time and never asks whether that is a good idea.
    """
    try:
        sys.stdout.flush()
    except (AttributeError, ValueError):  # pragma: no cover - detached stdout
        pass
    saved = os.dup(1)
    os.dup2(2, 1)
    protocol = os.fdopen(saved, "wb")
    # Belt and braces: a stray print() now goes to stderr explicitly rather
    # than relying on fd 1 having been redirected underneath it.
    sys.stdout = sys.stderr
    return protocol


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class ProtocolError(Exception):
    """A protocol-level fault: it becomes a JSON-RPC error object.

    Tool-level failures are *not* these -- see :class:`hal_mcp.session.ToolError`.
    """

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def _text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


class Server(object):
    """One MCP conversation over one pair of pipes."""

    def __init__(self, write, store=None):
        self._write = write
        self.store = store if store is not None else SessionStore()
        self.client_info = None
        self.protocol_version = LATEST_PROTOCOL_VERSION

    # -- outbound -----------------------------------------------------------

    def _send(self, message):
        self._write(message)

    def send_result(self, identifier, payload):
        self._send({"jsonrpc": "2.0", "id": identifier, "result": payload})

    def send_error(self, identifier, code, message):
        self._send(
            {
                "jsonrpc": "2.0",
                "id": identifier,
                "error": {"code": code, "message": message},
            }
        )

    # -- inbound ------------------------------------------------------------

    def handle_line(self, line):
        """Parse and dispatch one protocol line."""
        try:
            message = json.loads(line)
        except ValueError as exc:
            self.send_error(
                None,
                PARSE_ERROR,
                "parse error: {}. Each line must be exactly one JSON object.".format(exc),
            )
            return
        if isinstance(message, list):
            self.send_error(
                None,
                INVALID_REQUEST,
                "batched requests are not supported; send one JSON-RPC object per line.",
            )
            return
        if not isinstance(message, dict):
            self.send_error(
                None, INVALID_REQUEST, "a JSON-RPC message must be a JSON object."
            )
            return
        self.handle_message(message)

    def handle_message(self, message):
        identifier = message.get("id")
        is_notification = "id" not in message
        method = message.get("method")

        if not isinstance(method, str) or not method:
            if not is_notification:
                self.send_error(
                    identifier, INVALID_REQUEST, "invalid request: 'method' must be a string."
                )
            return

        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if not is_notification:
                self.send_error(
                    identifier, INVALID_PARAMS, "'params' must be an object when present."
                )
            return

        handler = _METHODS.get(method)
        if handler is None:
            if is_notification:
                # An unknown notification is ignored: the spec forbids a
                # response, and a client is allowed to send ones we do not know.
                return
            self.send_error(
                identifier,
                METHOD_NOT_FOUND,
                "unknown method {!r}. This server implements: {}.".format(
                    method, ", ".join(sorted(_METHODS))
                ),
            )
            return

        try:
            payload = handler(self, params)
        except ProtocolError as exc:
            if not is_notification:
                self.send_error(identifier, exc.code, exc.message)
            return
        except Exception as exc:  # pragma: no cover - defensive
            if not is_notification:
                self.send_error(
                    identifier,
                    INTERNAL_ERROR,
                    "internal error in {}: {}: {}".format(method, type(exc).__name__, exc),
                )
            return

        if is_notification:
            return
        self.send_result(identifier, {} if payload is None else payload)

    def serve(self, stream):
        """Read newline-delimited messages from a binary stream until EOF."""
        while True:
            try:
                raw = stream.readline()
            except KeyboardInterrupt:  # pragma: no cover
                return 130
            if not raw:
                return 0
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            self.handle_line(line)

    # -- methods ------------------------------------------------------------

    def _initialize(self, params):
        requested = params.get("protocolVersion")
        if isinstance(requested, str) and requested in PROTOCOL_VERSIONS:
            self.protocol_version = requested
        else:
            self.protocol_version = LATEST_PROTOCOL_VERSION
        client = params.get("clientInfo")
        if isinstance(client, dict):
            self.client_info = client
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def _initialized(self, params):
        return None

    def _ignore(self, params):
        return None

    def _ping(self, params):
        return {}

    def _tools_list(self, params):
        return {"tools": tool_registry.list_tools()}

    def _tools_call(self, params):
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise ProtocolError(
                INVALID_PARAMS, "tools/call requires a string 'name' naming the tool."
            )
        arguments = params.get("arguments")
        try:
            payload = tool_registry.call_tool(self.store, name, arguments)
        except ToolError as exc:
            # A tool-level failure is a normal result carrying isError, not a
            # JSON-RPC error: the caller is meant to read it and try again.
            return _text_result(str(exc), is_error=True)
        except Exception as exc:
            return _text_result(
                "{} while running {}: {}\nThis is a bug or an unsupported netlist; "
                "the server's stderr log has the traceback context. The session is "
                "still open -- hal_list_sessions confirms it.".format(
                    type(exc).__name__, name, exc
                ),
                is_error=True,
            )
        return _text_result(json.dumps(payload, indent=2, sort_keys=True, default=str))


_METHODS = {
    "initialize": Server._initialize,
    "notifications/initialized": Server._initialized,
    "notifications/cancelled": Server._ignore,
    "notifications/progress": Server._ignore,
    "ping": Server._ping,
    "tools/list": Server._tools_list,
    "tools/call": Server._tools_call,
}


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

_EPILOG = """\
serve (the default) speaks the Model Context Protocol on stdin/stdout: one
UTF-8 JSON-RPC 2.0 object per line. Register it with an MCP client rather than
running it by hand; tools/hal_mcp/README.md has the configuration stanza.

Loading a netlist needs a built HAL: export HAL_BASE_PATH=<build-or-install>
and HAL_PY_PATH=<build-or-install>/lib before launching the server. Without
HAL_BASE_PATH the hal_py import aborts the interpreter, so the server checks
for that in a throwaway process and reports it as a tool error instead of
dying.

tools prints the tool surface and needs no HAL at all.
"""


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python tools/hal_mcp",
        description="Model Context Protocol server for HAL: load a netlist once "
        "into a session, then answer many questions about it without reparsing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOG,
    )
    parser.add_argument("--version", action="version", version="hal_mcp " + __version__)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    subparsers.add_parser(
        "serve",
        help="speak MCP on stdin/stdout (the default when no command is given)",
        description="Speak the Model Context Protocol on stdin/stdout until EOF.",
    )

    tools_parser = subparsers.add_parser(
        "tools",
        help="print the tool surface and exit; needs no HAL",
        description="Print every tool this server exposes, with its description.",
    )
    tools_parser.add_argument(
        "--schemas",
        action="store_true",
        help="also print each tool's input schema",
    )

    return parser


def _command_tools(args, out):
    for tool in tool_registry.TOOLS:
        out.write("{}\n".format(tool.name))
        for line in _wrap(tool.description, 76):
            out.write("    {}\n".format(line))
        required = tool.schema.get("required") or []
        properties = tool.schema.get("properties") or {}
        optional = sorted(key for key in properties if key not in required)
        out.write("    required: {}\n".format(", ".join(required) or "none"))
        out.write("    optional: {}\n".format(", ".join(optional) or "none"))
        if args.schemas:
            for line in json.dumps(tool.schema, indent=2, sort_keys=True).splitlines():
                out.write("    {}\n".format(line))
        out.write("\n")
    out.write("{} tools\n".format(len(tool_registry.TOOLS)))
    return 0


def _wrap(text, width):
    words = str(text).split()
    lines = []
    current = ""
    for word in words:
        candidate = (current + " " + word).strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _command_serve():
    protocol = install_stdout_guard()

    def write(message):
        protocol.write(json.dumps(message, ensure_ascii=False).encode("utf-8"))
        protocol.write(b"\n")
        protocol.flush()

    server = Server(write)
    sys.stderr.write(
        "[hal_mcp] {} {} ready on stdio; protocol {}. HAL's log output is on this "
        "stream by design.\n".format(SERVER_NAME, __version__, LATEST_PROTOCOL_VERSION)
    )
    sys.stderr.flush()
    try:
        return server.serve(sys.stdin.buffer)
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except BrokenPipeError:  # pragma: no cover - the client went away
        return 0
    finally:
        try:
            protocol.close()
        except (OSError, ValueError):
            pass


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "serve"
    try:
        if command == "tools":
            return _command_tools(args, sys.stdout)
        return _command_serve()
    except Exception as exc:  # a message, never a traceback
        sys.stderr.write("hal_mcp: {}: {}\n".format(type(exc).__name__, exc))
        return 1
