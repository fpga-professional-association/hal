"""Turning a step request into a validated findings document.

This is the code that actually runs inside HAL.  It does five things and
refuses to guess at any of them:

1. imports ``hal_py`` and loads the plugins (``--python-script`` hands control
   to the python shell *before* HAL loads plugins, so a step that does not load
   them itself finds no parsers and no analyses);
2. loads the netlist through :mod:`hal_viz.halenv`, the repository's existing
   loader for project directories, ``.hal`` files and HDL + gate library --
   there is no second implementation of that here;
3. calls the analysis module's ``run()``;
4. re-pins the input artifact to the digest the *runner* computed, so the
   document says what the run manifest says;
5. validates the document against the findings schema before writing it, and
   writes the result record last.

A failure at any point produces a result record with an error and a nonzero
exit code.  Never a partial success, and never an exit code that says the step
worked when it did not.
"""

import importlib
import os
import time
import traceback

from hal_findings import serialize as findings_serialize
from hal_findings import validate as findings_validate
from hal_findings.adapters.common import utc_now

from .. import analyses as analysis_registry
from .. import protocol

__all__ = ["StepError", "StepContext", "StepOutcome", "execute", "apply_pin"]


class StepError(RuntimeError):
    """A failure of the analysis itself, with a typed kind for the findings record."""

    def __init__(self, message, kind="plugin_error", detail=None):
        RuntimeError.__init__(self, message)
        self.kind = kind
        self.detail = detail


class StepContext(object):
    """Everything an analysis module needs that is not the netlist itself."""

    def __init__(self, request, analysis, plugin_module, plugin_version):
        self.request = request
        self.analysis = analysis
        self.plugin_module = plugin_module
        self.plugin_version = plugin_version
        self.step_id = request.get("step_id")
        self.run_id = request.get("run", {}).get("id")
        self.artifact_id = request.get("artifact_id", "netlist")
        self.output_dir = request["output_dir"]
        self.netlist_path = request["netlist"]
        self.gate_library_path = request.get("gate_library")
        self.pin = request.get("pin") or {}
        self.hal_version = request.get("hal_version")
        self.limits = request.get("limits") or {}

    def artifact_path(self, name):
        return os.path.join(self.output_dir, name)


class StepOutcome(object):
    """What an analysis module returns: a document plus the files it wrote."""

    def __init__(self, document, artifacts=None, metrics=None, notes=None):
        self.document = document
        #: ``{"path": <relative to the step directory>, "role": ..., "description": ...}``
        self.artifacts = list(artifacts or [])
        self.metrics = dict(metrics or {})
        self.notes = list(notes or [])


def apply_pin(document, artifact_id, pin):
    """Make the document's input artifact carry the runner's content pin.

    The adapters describe the artifact from the *loaded netlist*, which for a
    project directory means "no file to hash".  The runner knows better -- it
    hashed the archive, or computed a tree digest over the directory -- so its
    answer wins.  A real ``sha256`` replaces an ``unhashed_reason``; where there
    is none, the reason is rewritten to name the digest that does exist.
    """
    if not pin:
        return document
    for artifact in document.get("artifacts", []):
        if artifact.get("artifact_id") != artifact_id:
            continue
        if pin.get("path"):
            artifact["path"] = pin["path"]
        if pin.get("kind"):
            artifact["kind"] = pin["kind"]
        if pin.get("sha256"):
            artifact["sha256"] = pin["sha256"]
            artifact.pop("unhashed_reason", None)
            if pin.get("size_bytes") is not None:
                artifact["size_bytes"] = pin["size_bytes"]
        elif pin.get("unhashed_reason"):
            artifact.pop("sha256", None)
            artifact["unhashed_reason"] = pin["unhashed_reason"]
        if pin.get("description"):
            artifact["description"] = pin["description"]
    return document


def _plugin_version(hal_py, name):
    """Best-effort plugin version; 'unknown' is an honest answer, a wrong one is not."""
    try:
        instance = hal_py.plugin_manager.get_plugin_instance(name)
    except Exception:
        return "unknown"
    if instance is None:
        return "unknown"
    try:
        version = instance.get_version()
    except Exception:
        return "unknown"
    return str(version) if version else "unknown"


def _stamp(document, started_at, finished_at, duration_s, hal_version):
    """Record the wall-clock facts of this run on the document's analysis block."""
    analysis = document.setdefault("analysis", {})
    analysis["started_at"] = started_at
    analysis["finished_at"] = finished_at
    analysis["duration_s"] = round(float(duration_s), 3)
    if hal_version:
        analysis.setdefault("hal", {})["version"] = str(hal_version)
    return document


def execute(request):
    """Run one step request. Returns the process exit code."""
    started_at = utc_now()
    started = time.time()
    output_dir = request["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    result_path = os.path.join(output_dir, request.get("result_file", "result.json"))
    findings_name = request.get("findings_file", "findings.json")
    analysis_name = request.get("analysis", "unknown")

    def fail(message, kind="plugin_error", detail=None):
        protocol.write_json(
            protocol.result(
                "error",
                analysis_name,
                error={"kind": kind, "message": message, "detail": detail},
                started_at=started_at,
                finished_at=utc_now(),
                duration_s=time.time() - started,
            ),
            result_path,
        )
        return 1

    try:
        analysis = analysis_registry.get(analysis_name)
    except analysis_registry.UnknownAnalysis as exc:
        return fail(str(exc), kind="invalid_input")

    try:
        from hal_viz.halenv import (
            HalUnavailable,
            NetlistLoadError,
            import_hal_py,
            import_plugin,
            load_all_plugins,
            load_netlist,
        )
    except ImportError as exc:  # pragma: no cover - a broken checkout
        return fail(
            "could not import hal_viz.halenv from the tools directory: {}".format(exc),
            kind="internal",
            detail=traceback.format_exc(),
        )

    try:
        hal_py = import_hal_py()
        # --python-script takes over main() before HAL loads plugins, so every parser
        # and every analysis has to be loaded here or nothing is registered.
        load_all_plugins(hal_py)
        plugin_module = import_plugin(analysis.plugin)
        plugin_version = _plugin_version(hal_py, analysis.plugin)
        netlist = load_netlist(hal_py, request["netlist"], request.get("gate_library"))
    except HalUnavailable as exc:
        return fail(str(exc), kind="resource", detail=traceback.format_exc())
    except NetlistLoadError as exc:
        return fail(str(exc), kind="invalid_input", detail=traceback.format_exc())
    except Exception as exc:  # noqa: BLE001 - any failure here is a failed step
        return fail(
            "could not set up the analysis: {}: {}".format(type(exc).__name__, exc),
            kind="internal",
            detail=traceback.format_exc(),
        )

    context = StepContext(request, analysis, plugin_module, plugin_version)

    try:
        module = importlib.import_module(analysis.module)
        outcome = module.run(hal_py, netlist, request.get("config") or {}, context)
    except StepError as exc:
        return fail(str(exc), kind=exc.kind, detail=exc.detail)
    except Exception as exc:  # noqa: BLE001 - the analysis raised
        return fail(
            "{} raised {}: {}".format(analysis.name, type(exc).__name__, exc),
            kind="exception",
            detail=traceback.format_exc(),
        )

    finished_at = utc_now()
    duration = time.time() - started

    document = apply_pin(outcome.document, context.artifact_id, context.pin)
    _stamp(document, started_at, finished_at, duration, context.hal_version)

    try:
        findings_validate.validate_document(document)
    except findings_validate.FindingsValidationError as exc:
        return fail(
            "the analysis produced a findings document that does not validate",
            kind="internal",
            detail="\n".join(exc.errors[:20]),
        )

    findings_path = os.path.join(output_dir, findings_name)
    try:
        findings_serialize.write_document(document, findings_path)
    except OSError as exc:
        return fail("could not write {}: {}".format(findings_path, exc), kind="io")

    artifacts = [
        {
            "path": findings_name,
            "role": "findings",
            "description": "findings document written through hal_findings",
        }
    ] + outcome.artifacts

    protocol.write_json(
        protocol.result(
            "ok",
            analysis.name,
            artifacts=artifacts,
            metrics=outcome.metrics,
            started_at=started_at,
            finished_at=finished_at,
            duration_s=duration,
            notes=outcome.notes,
        ),
        result_path,
    )
    return 0
