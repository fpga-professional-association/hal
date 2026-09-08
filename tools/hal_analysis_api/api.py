"""The API itself: one dispatch table, one envelope, one place that validates.

Every call goes through :meth:`AnalysisApi.call`, which

1. resolves the tool name (``unknown_tool`` if there is none),
2. validates the request against the tool's schema and fills in the defaults
   from :mod:`hal_analysis_api.limits` -- so a handler never has to ask whether
   a field was given,
3. runs the handler,
4. validates the *response* against the tool's schema, and
5. wraps whatever happened in the envelope.

Step 4 is the unusual one and it is deliberate.  An API whose answers an agent
cannot trust the shape of is worse than no API, so a response that does not
validate is turned into an ``internal`` error rather than returned: the bug
surfaces here, in the test suite, instead of three tool calls later in an
agent's reasoning.

``call`` never raises.  Everything -- a bad request, a missing project, a HAL
crash, a bug in this file -- comes back as an envelope with ``ok: false`` and a
code.
"""

import base64
import hashlib
import os

from hal_findings import serialize as findings_serialize
from hal_findings import validate as findings_validate
from hal_findings.schema import SCHEMA_VERSION as FINDINGS_SCHEMA_VERSION
from hal_runner import __version__ as RUNNER_VERSION
from hal_runner import analyses as runner_analyses

from . import API_VERSION, limits as limit_module, schemas
from .errors import (
    ERROR_CODES,
    ApiError,
    Internal,
    InvalidRequest,
    UnknownArtifact,
    UnknownTool,
    error_envelope,
    ok_envelope,
)
from .handles import ProjectStore, default_workspace
from .jobs import JobStore
from .queries import QueryRunner

__all__ = ["AnalysisApi", "analysis_info"]


def _type_name(python_type):
    if python_type is type(None):
        return "null"
    return {"bool": "boolean", "int": "integer", "float": "number", "str": "string",
            "list": "array", "dict": "object"}.get(python_type.__name__, python_type.__name__)


def analysis_info(analysis):
    """One :class:`hal_runner.analyses.Analysis` as the API reports it."""
    return {
        "name": analysis.name,
        "plugin": analysis.plugin,
        "entry_point": analysis.entry_point,
        "deterministic": bool(analysis.deterministic),
        "method_kind": analysis.method_kind,
        "method_name": analysis.method_name,
        "description": analysis.description,
        "options": [
            {
                "name": option.name,
                "types": [_type_name(entry) for entry in option.types],
                "default": option.default,
                "description": option.description,
                "choices": list(option.choices) if option.choices is not None else None,
                "minimum": option.minimum,
            }
            for option in sorted(analysis.options.values(), key=lambda item: item.name)
        ],
    }


def _optional_integrations():
    """Report neighbouring tooling without inventing an interface for it.

    ``tools/hal_capabilities`` is being added by a parallel change (issue #17).
    When it is importable, its inventory is offered through *its own* public
    entry point -- probed by name, never guessed at -- and when it is not, this
    says so instead of pretending the API knows what the build supports.
    """
    report = {}
    try:
        import hal_capabilities  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - absence is the normal case
        report["hal_capabilities"] = {
            "available": False,
            "reason": "not importable from the tools directory ({})".format(exc),
        }
        return report

    for entry_point in ("capabilities", "describe", "as_json", "inventory"):
        function = getattr(hal_capabilities, entry_point, None)
        if callable(function):
            try:
                value = function()
            except Exception as exc:  # noqa: BLE001 - a broken probe is not our failure
                report["hal_capabilities"] = {
                    "available": True,
                    "entry_point": entry_point,
                    "error": str(exc),
                }
                return report
            report["hal_capabilities"] = {
                "available": True,
                "entry_point": entry_point,
                "value": value if isinstance(value, dict) else {"value": str(value)},
            }
            return report
    report["hal_capabilities"] = {
        "available": True,
        "reason": "imported, but it exposes none of the entry points this build probes "
        "for (capabilities/describe/as_json/inventory)",
    }
    return report


class AnalysisApi(object):
    """The local API. The MCP adapter is a client of this class, not a peer."""

    def __init__(self, workspace=None, hal_binary=None, executor=None, spawn=None):
        self.workspace = os.path.abspath(workspace or default_workspace())
        self.projects = ProjectStore(self.workspace)
        self.jobs = JobStore(self.workspace, hal_binary=hal_binary, spawn=spawn)
        self.queries = QueryRunner(self.workspace, hal_binary=hal_binary, executor=executor)

    # -- entry point ---------------------------------------------------------

    def call(self, tool_name, request=None):
        """Run one tool and return its envelope. Never raises."""
        try:
            tool = schemas.get_tool(tool_name)
        except KeyError as exc:
            return error_envelope(
                tool_name,
                UnknownTool(
                    "no tool {!r} in this API version".format(tool_name),
                    detail=str(exc),
                    hint="call hal.capabilities for the tool list",
                ),
            )

        try:
            payload = self.invoke(tool.name, request)
        except ApiError as exc:
            return error_envelope(tool.name, exc)
        except Exception as exc:  # noqa: BLE001 - a bug is an envelope, not a traceback
            return error_envelope(tool.name, exc)
        return ok_envelope(tool.name, payload)

    def invoke(self, tool_name, request=None):
        """Run one tool and return its result payload, raising :class:`ApiError`."""
        tool = schemas.get_tool(tool_name)
        request = dict(request or {})
        errors = schemas.request_errors(tool.name, request)
        if errors:
            raise InvalidRequest(
                "the request for {!r} does not match its schema".format(tool.name),
                detail="\n".join(errors),
                hint="hal.schema returns the exact request schema for this tool",
                data={"violations": len(errors)},
            )
        handler = getattr(self, "_" + tool.key)
        result = handler(request)
        problems = schemas.response_errors(tool.name, result)
        if problems:
            raise Internal(
                "the response of {!r} does not match its own schema".format(tool.name),
                detail="\n".join(problems),
                hint="this is a bug in hal_analysis_api; the answer was withheld rather "
                "than returned in a shape the schema does not describe",
            )
        return result

    # -- discovery -----------------------------------------------------------

    def _hal_capabilities(self, request):
        return {
            "api_version": API_VERSION,
            "schema_version": schemas.SCHEMA_VERSION,
            "findings_schema_version": FINDINGS_SCHEMA_VERSION,
            "runner_version": RUNNER_VERSION,
            "tools": [schemas.TOOLS[name].as_json() for name in schemas.tool_names()],
            "analyses": [
                analysis_info(runner_analyses.ANALYSES[name])
                for name in runner_analyses.names()
            ],
            "limits": limit_module.as_json(),
            "error_codes": dict(ERROR_CODES),
            "hal": self.queries.availability(),
            "workspace": self.workspace,
            "optional_integrations": _optional_integrations(),
            "notes": [
                "Every netlist.* call is one 'hal --python-script' process: it loads the "
                "netlist, answers and exits without saving, which is how a read is kept "
                "from modifying a project. Prefer netlist.cone and filtered listings over "
                "a per-gate loop -- the process start dominates.",
                "Gate, net and module ids are only meaningful inside one project handle; "
                "quote the handle with any id you record.",
                "Analyses run through hal_runner: the manifest in the job directory pins "
                "the inputs, the tool versions and the configuration of every result.",
            ],
        }

    def _hal_schema(self, request):
        name = request["tool"]
        try:
            tool = schemas.get_tool(name)
        except KeyError as exc:
            raise UnknownTool(
                "no tool {!r} in this API version".format(name),
                detail=str(exc),
                hint="call hal.capabilities for the tool list",
            )
        return {
            "tool": tool.name,
            "summary": tool.summary,
            "request_schema": schemas.standalone_schema(tool.request_ref),
            "response_schema": schemas.standalone_schema(tool.response_ref),
        }

    def _analysis_list(self, request):
        return {
            "analyses": [
                analysis_info(runner_analyses.ANALYSES[name])
                for name in runner_analyses.names()
            ],
            "runner_version": RUNNER_VERSION,
            "registry_version": runner_analyses.REGISTRY_VERSION,
        }

    # -- projects ------------------------------------------------------------

    def _project_open(self, request):
        handle, reused = self.projects.open(
            request["path"],
            gate_library=request.get("gate_library"),
            label=request.get("label"),
        )
        return {"project": handle.id, "reused": reused, "info": handle.as_json()}

    def _project_list(self, request):
        handles = self.projects.list()
        return {"projects": [handle.as_json() for handle in handles], "count": len(handles)}

    def _project_describe(self, request):
        handle = self.projects.get(request["project"])
        return {
            "info": handle.as_json(),
            "still_matches_digest": self.projects.still_matches(handle),
        }

    def _project_close(self, request):
        handle = self.projects.close(request["project"])
        return {
            "project": handle.id,
            "closed": True,
            "note": "the handle is forgotten; the project files were not touched",
        }

    # -- netlist reads -------------------------------------------------------

    def _query(self, tool_name, request, params):
        handle = self.projects.get(request["project"])
        result = self.queries.run(
            tool_name, handle, params, timeout_s=request.get("timeout_s")
        )
        result["project"] = handle.id
        return result

    def _netlist_summary(self, request):
        return self._query(
            "netlist.summary",
            request,
            {
                "max_gate_types": int(
                    request.get("max_gate_types", limit_module.DEFAULT_GATE_TYPES)
                )
            },
        )

    def _netlist_gates(self, request):
        return self._query(
            "netlist.gates",
            request,
            {
                "name_contains": request.get("name_contains"),
                "gate_type": request.get("gate_type"),
                "module_id": request.get("module_id"),
                "offset": int(request.get("offset", 0)),
                "limit": int(request.get("limit", limit_module.DEFAULT_PAGE_LIMIT)),
            },
        )

    def _netlist_nets(self, request):
        return self._query(
            "netlist.nets",
            request,
            {
                "name_contains": request.get("name_contains"),
                "global_only": bool(request.get("global_only", False)),
                "offset": int(request.get("offset", 0)),
                "limit": int(request.get("limit", limit_module.DEFAULT_PAGE_LIMIT)),
            },
        )

    def _netlist_modules(self, request):
        return self._query(
            "netlist.modules",
            request,
            {
                "name_contains": request.get("name_contains"),
                "offset": int(request.get("offset", 0)),
                "limit": int(request.get("limit", limit_module.DEFAULT_PAGE_LIMIT)),
            },
        )

    def _netlist_gate(self, request):
        return self._query(
            "netlist.gate",
            request,
            {
                "gate_id": int(request["gate_id"]),
                "max_endpoints": int(
                    request.get("max_endpoints", limit_module.DEFAULT_ENDPOINTS)
                ),
            },
        )

    def _netlist_net(self, request):
        return self._query(
            "netlist.net",
            request,
            {
                "net_id": int(request["net_id"]),
                "max_endpoints": int(
                    request.get("max_endpoints", limit_module.DEFAULT_ENDPOINTS)
                ),
            },
        )

    def _netlist_cone(self, request):
        return self._query(
            "netlist.cone",
            request,
            {
                "seed_gate_ids": [int(value) for value in request["seed_gate_ids"]],
                "direction": request.get("direction", "both"),
                "depth": int(request.get("depth", limit_module.DEFAULT_CONE_DEPTH)),
                "max_gates": int(request.get("max_gates", limit_module.DEFAULT_CONE_GATES)),
                "include_edges": bool(request.get("include_edges", True)),
            },
        )

    # -- analyses ------------------------------------------------------------

    def _analysis_submit(self, request):
        handle = self.projects.get(request["project"])
        record = self.jobs.submit(
            handle,
            request["analysis"],
            config=request.get("config"),
            timeout_s=request.get("timeout_s"),
            memory_mb=request.get("memory_mb"),
            label=request.get("label"),
        )
        status = self.jobs.public(record)
        status["waited_s"] = None
        status["wait_timed_out"] = None
        return {"job": record["job"], "status": status}

    def _analysis_status(self, request):
        return {"status": self.jobs.status(request["job"], wait_s=request.get("wait_s"))}

    def _analysis_cancel(self, request):
        cancelled, reason, record = self.jobs.cancel(request["job"])
        status = self.jobs.public(record)
        status["waited_s"] = None
        status["wait_timed_out"] = None
        return {"job": record["job"], "cancelled": cancelled, "reason": reason, "status": status}

    def _analysis_jobs(self, request):
        records = self.jobs.list(
            project=request.get("project"), state=request.get("state")
        )
        window, page = limit_module.paginate(
            records,
            request.get("offset", 0),
            request.get("limit", limit_module.DEFAULT_PAGE_LIMIT),
        )
        statuses = []
        for record in window:
            status = self.jobs.public(record)
            status["waited_s"] = None
            status["wait_timed_out"] = None
            statuses.append(status)
        return {"jobs": statuses, "page": page}

    # -- results -------------------------------------------------------------

    def _findings_get(self, request):
        record = self.jobs.read(request["job"])
        path, source = self.jobs.findings_path(record)
        try:
            document = findings_serialize.read_document(path)
        except (OSError, ValueError) as exc:
            raise Internal("the findings document {} is unreadable: {}".format(path, exc))

        try:
            problems = findings_validate.collect_errors(document)
        except findings_validate.FindingsValidationError as exc:
            problems = exc.errors
        if problems:
            # hal_runner already refuses to call a step successful with an
            # invalid document; reaching this means something changed the file.
            raise Internal(
                "the stored findings document no longer validates",
                detail="\n".join(problems[:20]),
                data={"path": path},
            )

        findings = list(document.get("findings", []))
        counts = {}
        for finding in findings:
            counts[finding["status"]] = counts.get(finding["status"], 0) + 1
        wanted = request.get("status")
        if wanted:
            findings = [entry for entry in findings if entry.get("status") == wanted]
        window, page = limit_module.paginate(
            findings,
            request.get("offset", 0),
            request.get("limit", limit_module.DEFAULT_FINDINGS_LIMIT),
        )
        return {
            "job": record["job"],
            "source": source,
            "schema_version": document.get("schema_version"),
            "digest": findings_serialize.document_digest(document),
            "producer": document.get("producer"),
            "analysis": document.get("analysis"),
            "artifacts": list(document.get("artifacts", [])),
            "counts": counts,
            "findings": window,
            "page": page,
            "validated": True,
        }

    def _artifact_list(self, request):
        record = self.jobs.read(request["job"])
        return {
            "job": record["job"],
            "artifacts": self.jobs.artifacts(record),
            "job_dir": self.jobs.job_dir(record["job"]),
        }

    def _artifact_get(self, request):
        record = self.jobs.read(request["job"])
        job_dir = os.path.abspath(self.jobs.job_dir(record["job"]))
        relative = str(request["path"]).replace("\\", "/")
        absolute = os.path.abspath(os.path.join(job_dir, relative))
        try:
            escapes = os.path.commonpath([job_dir, absolute]) != job_dir
        except ValueError:
            # commonpath raises when the paths share no root at all (a different
            # drive on Windows); that is an escape by definition.
            escapes = True
        if escapes:
            raise InvalidRequest(
                "artifact paths are relative to the job directory and may not escape it",
                hint="use a path from artifact.list",
                data={"path": request["path"]},
            )
        if not os.path.isfile(absolute):
            known = [entry["path"] for entry in self.jobs.artifacts(record)]
            raise UnknownArtifact(
                "job {} has no artifact {!r}".format(record["job"], relative),
                detail="it wrote: {}".format(", ".join(known) if known else "(nothing yet)"),
                hint="call artifact.list first; a running job has not written its results",
            )

        budget = int(request.get("max_bytes", limit_module.DEFAULT_ARTIFACT_BYTES))
        size = os.path.getsize(absolute)
        with open(absolute, "rb") as handle:
            raw = handle.read(budget)
        digest = hashlib.sha256()
        with open(absolute, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)

        encoding = request.get("encoding", "text")
        if encoding == "base64":
            content = base64.b64encode(raw).decode("ascii")
        else:
            content = raw.decode("utf-8", errors="replace")
        return {
            "job": record["job"],
            "path": relative,
            "encoding": encoding,
            "content": content,
            "size_bytes": size,
            "returned_bytes": len(raw),
            "sha256": digest.hexdigest(),
            "truncation": limit_module.truncation(
                len(raw) < size,
                reason="the artifact is {} bytes; the first {} were returned".format(
                    size, len(raw)
                ),
                limit=budget,
                kind="bytes",
            ),
        }
