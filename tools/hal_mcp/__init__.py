"""hal_mcp -- a Model Context Protocol server that keeps a HAL netlist loaded.

Every ``hal`` CLI invocation reloads all 28 plugins and re-parses the netlist,
which costs one to three seconds *per question*.  An agent that asks twenty
questions about one design pays that twenty times.  This server pays it once:
``hal_load_netlist`` returns a **session id**, the loaded ``Netlist`` stays in
this process, and every later tool call answers against it.

The package is split so that the protocol can be tested without a built HAL:

``hal_mcp.server``
    The stdio JSON-RPC 2.0 transport, the MCP method dispatch, the file
    descriptor guard, and the command line entry point.  Imports no HAL.
``hal_mcp.session``
    The session registry and the only module that reaches for ``hal_py`` (via
    ``hal_viz.halenv``).  It imports HAL lazily, on the first tool call that
    needs it.
``hal_mcp.tools``
    The tool surface: one JSON Schema plus one handler per tool.

Run it with ``python3 tools/hal_mcp`` (or ``python3 -m hal_mcp`` with
``tools/`` on ``PYTHONPATH``).  See ``tools/hal_mcp/README.md`` for the tool
reference and the client configuration stanza.
"""

__version__ = "1.0.0"

#: Advertised in the ``initialize`` response as ``serverInfo.name``.
SERVER_NAME = "hal-mcp"

__all__ = ["__version__", "SERVER_NAME"]
