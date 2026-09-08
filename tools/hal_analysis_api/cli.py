"""Command line front end -- the same API the MCP adapter serves, on a terminal.

    python tools/hal_analysis_api tools
    python tools/hal_analysis_api capabilities
    python tools/hal_analysis_api schema netlist.cone
    python tools/hal_analysis_api call project.open --arg path=examples/uart.zip
    python tools/hal_analysis_api call netlist.cone \\
        --json '{"project": "prj-...", "seed_gate_ids": [12], "depth": 2}'
    python tools/hal_analysis_api mcp        # stdio MCP server over the same API

Having a CLI is not decoration.  It is how the API is exercised in CI without
an MCP client, how a human reproduces what an agent did, and the reason the
adapter can stay thin: the transport is never the only way in.

Exit codes are part of the interface:

===  =========================================================================
0    the tool answered (``ok: true``)
1    the tool returned an error envelope -- a real answer, just not a result
2    the invocation was wrong: no such tool, unparsable ``--json``, bad usage
===  =========================================================================

stdout carries exactly one JSON document (the envelope) so it can be piped into
``jq``; progress and diagnostics go to stderr.
"""

import argparse
import json
import os
import sys

from . import API_VERSION, __version__, schemas
from .api import AnalysisApi
from .handles import default_workspace

__all__ = ["build_parser", "main"]


def build_parser():
    parser = argparse.ArgumentParser(
        prog="hal_analysis_api",
        description="Bounded, structured HAL analysis tools for agents: capability "
        "discovery, scoped read-only netlist queries, analysis submission and artifact "
        "retrieval, all schema-validated.",
        epilog="netlist.* tools need a built HAL (see --hal-binary / $HAL_BASE_PATH); "
        "every other tool runs on a plain interpreter.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="hal_analysis_api {} (API {})".format(__version__, API_VERSION),
    )
    parser.add_argument(
        "--workspace",
        metavar="DIR",
        help="where handles, jobs and query scratch files live (default: "
        "$HAL_ANALYSIS_API_WORKSPACE or build/hal_analysis_api)",
    )
    parser.add_argument(
        "--hal-binary",
        metavar="PATH",
        help="the hal executable to run netlist queries and analyses with",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    listing = subparsers.add_parser("tools", help="list the tools and what they touch")
    listing.add_argument("--json", action="store_true", help="machine-readable output")

    schema = subparsers.add_parser(
        "schema", help="print the request and response schema of one tool"
    )
    schema.add_argument("tool", help="tool name, e.g. netlist.cone")

    subparsers.add_parser("capabilities", help="run the hal.capabilities tool")

    call = subparsers.add_parser("call", help="call one tool with a JSON request")
    call.add_argument("tool", help="tool name")
    call.add_argument("--json", metavar="JSON", help="the request as a JSON object")
    call.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="one request field; VALUE is parsed as JSON when it can be, else kept as a "
        "string. Repeatable.",
    )
    call.add_argument(
        "--stdin", action="store_true", help="read the request as JSON from stdin"
    )

    subparsers.add_parser(
        "mcp", help="serve the same tools over MCP on stdio (needs the 'mcp' package)"
    )

    worker = subparsers.add_parser(
        "worker", help=argparse.SUPPRESS, description="internal: execute one analysis job"
    )
    worker.add_argument("job_dir")

    return parser


def _parse_request(args, parser):
    request = {}
    if args.stdin:
        try:
            text = sys.stdin.read()
            request.update(json.loads(text) if text.strip() else {})
        except ValueError as exc:
            parser.error("stdin is not a JSON object: {}".format(exc))
    if args.json:
        try:
            document = json.loads(args.json)
        except ValueError as exc:
            parser.error("--json is not valid JSON: {}".format(exc))
        if not isinstance(document, dict):
            parser.error("--json must be a JSON object")
        request.update(document)
    for entry in args.arg:
        if "=" not in entry:
            parser.error("--arg expects KEY=VALUE, got {!r}".format(entry))
        key, _, raw = entry.partition("=")
        try:
            request[key] = json.loads(raw)
        except ValueError:
            request[key] = raw
    return request


def _emit(document):
    sys.stdout.write(json.dumps(document, indent=2, sort_keys=True) + "\n")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    if args.command == "worker":
        from .worker import run_job

        return run_job(args.job_dir)

    workspace = args.workspace or default_workspace()

    if args.command == "tools":
        entries = [schemas.TOOLS[name].as_json() for name in schemas.tool_names()]
        if args.json:
            _emit({"api_version": API_VERSION, "tools": entries})
            return 0
        width = max(len(entry["name"]) for entry in entries)
        for entry in entries:
            sys.stdout.write(
                "{name:<{width}}  {hal}{mutates}  {summary}\n".format(
                    name=entry["name"],
                    width=width,
                    hal="hal " if entry["requires_hal"] else "    ",
                    mutates="[{}]".format(entry["mutates"]),
                    summary=entry["summary"].split(".")[0],
                )
            )
        sys.stdout.write(
            "\n{} tools; 'hal' marks the ones that need a built HAL, [nothing] that the "
            "tool changes nothing.\n".format(len(entries))
        )
        return 0

    if args.command == "schema":
        try:
            tool = schemas.get_tool(args.tool)
        except KeyError as exc:
            sys.stderr.write("{}\n".format(exc))
            return 2
        _emit(
            {
                "tool": tool.name,
                "summary": tool.summary,
                "request_schema": schemas.standalone_schema(tool.request_ref),
                "response_schema": schemas.standalone_schema(tool.response_ref),
            }
        )
        return 0

    if args.command == "mcp":
        from .mcp_adapter import serve_stdio

        return serve_stdio(workspace=workspace, hal_binary=args.hal_binary)

    api = AnalysisApi(workspace=workspace, hal_binary=args.hal_binary)

    if args.command == "capabilities":
        envelope = api.call("hal.capabilities", {})
    else:
        if args.tool not in schemas.TOOLS:
            sys.stderr.write(
                "unknown tool {!r}; run 'python tools/hal_analysis_api tools' for the "
                "list\n".format(args.tool)
            )
            return 2
        envelope = api.call(args.tool, _parse_request(args, parser))

    _emit(envelope)
    return 0 if envelope.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
