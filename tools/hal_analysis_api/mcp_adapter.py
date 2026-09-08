"""The optional MCP transport. A thin adapter over :mod:`hal_analysis_api.api`.

*Thin* is the whole design.  This module contains no analysis logic, no
bounds, no error handling of its own: it advertises the tools the local API
already describes, forwards the arguments, and returns
:meth:`~hal_analysis_api.api.AnalysisApi.call`'s envelope as JSON text.  If MCP
disappeared tomorrow the API would be untouched, and everything the adapter
exposes is reachable through ``python tools/hal_analysis_api call`` today.

**Which SDK.**  The official Python SDK is the ``mcp`` package
(``modelcontextprotocol/python-sdk``).  Its interface changed in a way that
matters, so both were checked against the real packages rather than from
memory:

* ``mcp`` **1.x** (verified against 1.29.0): low-level server at
  ``mcp.server.lowlevel.Server``, handlers registered with the
  ``@server.list_tools()`` / ``@server.call_tool()`` decorators, ``Tool`` uses
  the wire field name ``inputSchema``.
* ``mcp`` **2.x** (verified against 2.2.0): ``mcp.server.Server`` takes its
  handlers as constructor arguments (``on_list_tools=``, ``on_call_tool=``)
  with the signature ``(ctx, params) -> Result``; the high-level ``FastMCP``
  class was renamed ``MCPServer``.  ``Tool`` accepts ``inputSchema`` as an
  alias of ``input_schema``, so tool construction is shared.

Both majors still transport over ``mcp.server.stdio.stdio_server()`` and run
with ``Server.run(read, write, initialization_options)``, which is what makes
one adapter possible.  Anything outside those two shapes is refused with the
installed version named, rather than half-driven.

**When ``mcp`` is not installed** -- the normal case in a HAL build container --
importing this module still works.  :func:`availability` says so, and
:func:`serve_stdio` exits 2 with the install command.  The API itself never
imports it.
"""

import json

from . import API_VERSION, __version__, schemas
from .api import AnalysisApi

__all__ = [
    "SUPPORTED_MAJORS",
    "availability",
    "describe_tools",
    "handle_call",
    "build_server",
    "serve_stdio",
]

#: Major versions of the ``mcp`` package this adapter was verified against.
SUPPORTED_MAJORS = (1, 2)

_INSTALL_HINT = (
    "the optional 'mcp' package is not installed. Install it with\n"
    "    python -m pip install 'mcp>=1,<3'\n"
    "or use the same tools without MCP:\n"
    "    python tools/hal_analysis_api call <tool> --json '{...}'"
)


def _import_mcp():
    """Import the SDK, or return ``(None, reason)``."""
    try:
        import mcp  # noqa: F401
        import mcp.types as mcp_types  # noqa: F401
    except ImportError as exc:
        return None, "{} ({})".format(_INSTALL_HINT, exc)
    return mcp, None


def _installed_version():
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - Python < 3.8
        return None
    try:
        return metadata.version("mcp")
    except Exception:  # noqa: BLE001 - installed from a checkout, say so honestly
        return None


def availability():
    """Whether MCP can be served here, and if not, exactly why.

    Reported by ``hal.capabilities`` under ``optional_integrations`` and by the
    CLI, so "the MCP server does not start" never has to be diagnosed by
    reading a traceback.
    """
    module, reason = _import_mcp()
    if module is None:
        return {"available": False, "version": None, "reason": reason}
    version = _installed_version()
    major = None
    if version:
        try:
            major = int(str(version).split(".")[0])
        except ValueError:  # pragma: no cover - a non-numeric version
            major = None
    if major is not None and major not in SUPPORTED_MAJORS:
        return {
            "available": False,
            "version": version,
            "reason": "mcp {} is installed; this adapter was written and verified "
            "against major versions {}. Refusing to guess at its interface -- pin "
            "'mcp>=1,<3' or update the adapter.".format(
                version, " and ".join(str(entry) for entry in SUPPORTED_MAJORS)
            ),
        }
    return {
        "available": True,
        "version": version,
        "api_style": "v2-constructor-handlers" if major == 2 else "v1-decorators",
        "reason": None,
    }


def describe_tools():
    """The tool list an MCP client sees: name, description, input schema.

    The description is assembled from the API's own registry -- there is no
    second, hand-maintained list to drift out of sync -- and states what the
    tool touches, because "does calling this change my project" is the first
    thing an agent has to know.
    """
    entries = []
    for name in schemas.tool_names():
        tool = schemas.TOOLS[name]
        notes = []
        if tool.requires_hal:
            notes.append("needs a built HAL")
        if tool.paginated:
            notes.append("paginated: see result.page")
        notes.append(
            {
                "nothing": "read-only: changes nothing",
                "session": "changes only this workspace's handles",
                "job": "changes analysis jobs, never the project",
            }[tool.mutates]
        )
        entries.append(
            {
                "name": tool.name,
                "description": "{} ({}). Answers are wrapped in "
                "{{'ok': true, 'result': ...}} or {{'ok': false, 'error': {{'code': ...}}}}."
                .format(tool.summary, "; ".join(notes)),
                "inputSchema": schemas.standalone_schema(tool.request_ref),
            }
        )
    return entries


def handle_call(api, name, arguments):
    """Forward one tool call. Returns ``(text, is_error)``.

    Errors are *not* raised at the transport: an agent gets the same envelope
    it would get from the CLI, with the code intact, and ``is_error`` set so an
    MCP client can also see it failed without parsing the body.
    """
    envelope = api.call(name, arguments or {})
    return json.dumps(envelope, indent=2, sort_keys=True), not envelope.get("ok", False)


def _tool_objects(types_module):
    """Build the SDK's ``Tool`` objects. ``inputSchema`` works on 1.x and 2.x."""
    return [
        types_module.Tool(
            name=entry["name"],
            description=entry["description"],
            inputSchema=entry["inputSchema"],
        )
        for entry in describe_tools()
    ]


def _call_result(types_module, text, is_error):
    """Build a ``CallToolResult``; ``isError`` is the wire name in both majors."""
    content = [types_module.TextContent(type="text", text=text)]
    return types_module.CallToolResult(content=content, isError=is_error)


def build_server(api, name="hal-analysis-api"):
    """Construct the SDK server object for the installed major version.

    Returns ``(server, run_coroutine_factory)``: the caller runs
    ``run_coroutine_factory()`` inside an event loop.  Split this way so the
    wiring can be constructed -- and tested -- without opening stdio.
    """
    module, reason = _import_mcp()
    if module is None:
        raise RuntimeError(reason)
    state = availability()
    if not state["available"]:
        raise RuntimeError(state["reason"])

    import mcp.types as types_module

    tools = _tool_objects(types_module)
    version = "{} (API {})".format(__version__, API_VERSION)

    if state["api_style"] == "v2-constructor-handlers":
        from mcp.server import Server  # mcp 2.x

        async def on_list_tools(ctx, params):
            return types_module.ListToolsResult(tools=tools)

        async def on_call_tool(ctx, params):
            text, is_error = handle_call(api, params.name, params.arguments)
            return _call_result(types_module, text, is_error)

        server = Server(
            name,
            version=version,
            instructions=_INSTRUCTIONS,
            on_list_tools=on_list_tools,
            on_call_tool=on_call_tool,
        )
    else:
        from mcp.server.lowlevel import Server  # mcp 1.x

        server = Server(name, version=version, instructions=_INSTRUCTIONS)

        @server.list_tools()
        async def _list_tools():
            return tools

        # validate_input=False: the request is validated against the same schema
        # inside the API, which produces a typed 'invalid_request' envelope. Two
        # validators would give the caller two different error shapes.
        @server.call_tool(validate_input=False)
        async def _call_tool(tool_name, arguments):
            text, is_error = handle_call(api, tool_name, arguments)
            return _call_result(types_module, text, is_error)

    return server


_INSTRUCTIONS = (
    "HAL netlist analysis, bounded and structured. Start with hal.capabilities, open a "
    "design with project.open, then use netlist.summary / netlist.gates / netlist.cone "
    "to scope an investigation, and analysis.submit + analysis.status + findings.get to "
    "run an analysis. Every id is relative to the project handle it came from. Every "
    "listing is paginated (result.page) and every traversal reports whether it was cut "
    "(result.truncation). Read tools never modify the project."
)


def serve_stdio(workspace=None, hal_binary=None, api=None):
    """Serve the API over MCP on stdio. Returns a process exit code."""
    import sys

    state = availability()
    if not state["available"]:
        sys.stderr.write("[hal_analysis_api] {}\n".format(state["reason"]))
        return 2

    try:
        import anyio
        from mcp.server.stdio import stdio_server
    except ImportError as exc:  # pragma: no cover - a broken mcp install
        sys.stderr.write(
            "[hal_analysis_api] the installed mcp package is missing its stdio "
            "transport: {}\n".format(exc)
        )
        return 2

    api = api or AnalysisApi(workspace=workspace, hal_binary=hal_binary)
    server = build_server(api)
    sys.stderr.write(
        "[hal_analysis_api] serving {} tools over MCP {} on stdio (workspace {})\n".format(
            len(schemas.TOOLS), state["version"] or "?", api.workspace
        )
    )

    async def _run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(_run)
    return 0
