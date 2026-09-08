"""The tool registry, and request/response validation against the schema file.

A tool is not a Python callable a caller has to know about; it is an entry in
:data:`TOOLS` with a name, a request schema and a response schema.  That
indirection is what makes the API describable (``hal.capabilities``,
``hal.schema``), what lets the MCP adapter be *thin* -- it advertises exactly
these schemas and forwards -- and what makes an unknown tool or a mistyped
argument a typed error instead of a ``TypeError`` from somewhere inside.

Two properties are deliberate:

* **Requests are closed.**  Every request schema sets
  ``additionalProperties: false``, so ``{"max_gate": 10}`` is rejected with the
  list of options rather than silently ignored -- the same rule
  :mod:`hal_runner.config` applies to step configuration.
* **Responses are validated too.**  Before any answer leaves
  :class:`hal_analysis_api.api.AnalysisApi`, it is checked against its own
  schema.  A response that does not validate is an ``internal`` error, because
  an agent that cannot trust the shape of an answer cannot use it at all.

Validation uses :mod:`hal_findings.jsonschema_mini`, the same dependency-free
validator the findings schema uses, so the API works inside a bare HAL build
container with no ``jsonschema`` package.
"""

import json
import os

from hal_findings import jsonschema_mini

from . import API_VERSION

__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SCHEMA_DIR",
    "Tool",
    "TOOLS",
    "tool_names",
    "get_tool",
    "load_schema",
    "schema_path",
    "standalone_schema",
    "request_errors",
    "response_errors",
    "validate_request",
    "validate_response",
]

#: The schema version this build writes and reads.
SCHEMA_VERSION = API_VERSION

SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema")

_SCHEMA_CACHE = {}


def schema_path(version=SCHEMA_VERSION):
    """Absolute path of the schema file for ``version``."""
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ValueError(
            "unsupported analysis API schema version {!r}; this build understands "
            "{}".format(version, ", ".join(SUPPORTED_SCHEMA_VERSIONS))
        )
    return os.path.join(SCHEMA_DIR, "analysis-api-{}.schema.json".format(version))


def load_schema(version=SCHEMA_VERSION):
    """Load (and cache) the API schema document."""
    if version not in _SCHEMA_CACHE:
        with open(schema_path(version), "r", encoding="utf-8") as handle:
            _SCHEMA_CACHE[version] = json.load(handle)
    return _SCHEMA_CACHE[version]


class Tool(object):
    """One operation: what it does, what it may touch, what it needs.

    ``mutates`` is part of the contract rather than a comment.  The issue this
    API answers requires that reads cannot modify a project, so every read
    declares ``nothing`` and the two operations that do change something --
    closing a handle, cancelling a job -- say so and are separate operations,
    never a side effect of a query.
    """

    MUTATION_KINDS = ("nothing", "session", "job")

    def __init__(self, name, summary, mutates="nothing", requires_hal=False, paginated=False):
        if mutates not in self.MUTATION_KINDS:
            raise ValueError("unknown mutation kind {!r}".format(mutates))
        self.name = name
        self.summary = summary
        self.mutates = mutates
        self.requires_hal = requires_hal
        self.paginated = paginated
        self.key = name.replace(".", "_")
        self.request_ref = "#/$defs/req_{}".format(self.key)
        self.response_ref = "#/$defs/res_{}".format(self.key)

    def as_json(self):
        return {
            "name": self.name,
            "summary": self.summary,
            "mutates": self.mutates,
            "requires_hal": self.requires_hal,
            "paginated": self.paginated,
        }


_TOOLS = [
    Tool(
        "hal.capabilities",
        "What this build can answer: the tool list, the analyses hal_runner knows, "
        "every limit, and whether a usable hal binary was found.",
    ),
    Tool(
        "hal.schema",
        "The request and response JSON Schema of one tool, self-contained.",
    ),
    Tool(
        "project.open",
        "Open a HAL project, netlist or project archive read-only and return a handle "
        "pinned to its content digest. Every other read names that handle.",
    ),
    Tool("project.list", "The project handles this workspace currently holds."),
    Tool(
        "project.describe",
        "What a handle points at, and whether that content still hashes to what it did "
        "when the handle was created.",
    ),
    Tool(
        "project.close",
        "Forget a handle. The project files are never touched.",
        mutates="session",
    ),
    Tool(
        "netlist.summary",
        "Design name, gate/net/module counts and the gate-type histogram.",
        requires_hal=True,
    ),
    Tool(
        "netlist.gates",
        "Gates, optionally filtered by name substring, gate type or module, one page "
        "at a time.",
        requires_hal=True,
        paginated=True,
    ),
    Tool("netlist.nets", "Nets, one page at a time.", requires_hal=True, paginated=True),
    Tool(
        "netlist.modules",
        "Modules of the hierarchy, one page at a time.",
        requires_hal=True,
        paginated=True,
    ),
    Tool(
        "netlist.gate",
        "One gate with its fan-in and fan-out endpoints (pin, net, neighbour).",
        requires_hal=True,
    ),
    Tool("netlist.net", "One net with its sources and destinations.", requires_hal=True),
    Tool(
        "netlist.cone",
        "The bounded fan-in/fan-out cone around seed gates: the scoped view an "
        "investigation actually needs, with an explicit record when the budget cut it.",
        requires_hal=True,
    ),
    Tool("analysis.list", "The analyses that can be submitted, with their options."),
    Tool(
        "analysis.submit",
        "Start one hal_runner analysis over a project handle and return a job id. "
        "Non-blocking: HAL runs in a detached process with the declared limits.",
        mutates="job",
    ),
    Tool(
        "analysis.status",
        "Where a job stands, optionally waiting a bounded time for it to finish.",
    ),
    Tool("analysis.cancel", "Stop a running job and its HAL process group.", mutates="job"),
    Tool("analysis.jobs", "The jobs in this workspace.", paginated=True),
    Tool(
        "findings.get",
        "The job's findings document, schema-validated, one page of findings at a time. "
        "A failed or timed-out job returns its diagnostic document, explicitly labelled.",
        paginated=True,
    ),
    Tool("artifact.list", "Everything the job wrote, with sizes and hashes."),
    Tool(
        "artifact.get",
        "The contents of one artifact, up to a byte budget, with an explicit truncation "
        "record.",
    ),
]

#: Every tool, keyed by name.
TOOLS = {tool.name: tool for tool in _TOOLS}


def tool_names():
    """Tool names in the order the registry declares them (discovery first)."""
    return [tool.name for tool in _TOOLS]


def get_tool(name):
    """Look up a tool; raises :class:`KeyError` with the alternatives."""
    try:
        return TOOLS[name]
    except KeyError:
        raise KeyError(
            "unknown tool {!r}; this API version has {}".format(name, ", ".join(tool_names()))
        )


def standalone_schema(ref, version=SCHEMA_VERSION):
    """A self-contained schema document for one ``#/$defs/...`` reference.

    The definition is *inlined at the top level* rather than referenced, so the
    result is a plain ``{"type": "object", ...}`` schema -- what an MCP client
    expects in ``inputSchema``, and what a caller that does not follow ``$ref``
    can still use.  The whole ``$defs`` block travels with it (a few kilobytes)
    so nested references still resolve, which removes an entire class of "my
    copy of the schema cannot resolve this pointer" bugs.
    """
    document = load_schema(version)
    if not ref.startswith("#/$defs/"):
        raise ValueError("expected a local $defs reference, got {!r}".format(ref))
    name = ref[len("#/$defs/") :]
    if name not in document["$defs"]:
        raise ValueError("the schema has no definition {!r}".format(name))
    schema = dict(document["$defs"][name])
    schema["$schema"] = document["$schema"]
    schema["$defs"] = document["$defs"]
    return schema


def _errors(instance, ref, version=SCHEMA_VERSION):
    document = load_schema(version)
    schema = {"$ref": ref}
    return [str(error) for error in jsonschema_mini.iter_errors(instance, dict(document, **schema))]


def request_errors(tool_name, request):
    """Every schema violation in ``request``, as human readable strings."""
    tool = get_tool(tool_name)
    if request is None:
        request = {}
    if not isinstance(request, dict):
        return ["a request must be a JSON object, got {}".format(type(request).__name__)]
    return _errors(request, tool.request_ref)


def response_errors(tool_name, result):
    """Every schema violation in a tool's ``result`` payload."""
    tool = get_tool(tool_name)
    return _errors(result, tool.response_ref)


def envelope_errors(envelope):
    """Every schema violation in a full response envelope."""
    return _errors(envelope, "#/$defs/envelope")


def validate_request(tool_name, request):
    """Raise :class:`ValueError` listing every problem with ``request``."""
    errors = request_errors(tool_name, request)
    if errors:
        raise ValueError("\n".join(errors))
    return request


def validate_response(tool_name, result):
    """Raise :class:`ValueError` listing every problem with ``result``."""
    errors = response_errors(tool_name, result)
    if errors:
        raise ValueError("\n".join(errors))
    return result
