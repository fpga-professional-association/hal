"""Diagnostic records for steps that did not produce a result.

A step that times out or crashes still has to say something, and what it says
must not be confusable with an analysis result.  The findings schema already
has exactly the right vocabulary for this: ``timeout`` (aborted at a resource
limit, which the finding must carry) and ``error`` (the analysis itself failed
-- says nothing about the design).  So a failed step gets a real findings
document too, written by the runner rather than by the step that could not
write one, and pointing at the retained logs as evidence.

The alternative -- a bare log file and a nonzero exit code -- is what makes
pipelines lose failures: nothing downstream reads logs, and nothing can tell a
step that found nothing from a step that never ran.
"""

from hal_findings import model
from hal_findings.adapters.common import utc_now

from . import __version__

__all__ = ["netlist_artifact", "timeout_document", "error_document"]

_ARTIFACT_ID = "netlist"


def netlist_artifact(pin, artifact_id=_ARTIFACT_ID):
    """Build the input artifact from the runner's own content pin.

    A file input (including the ``.zip`` archives the examples ship as) carries
    its real ``sha256``.  A project *directory* has no single file to hash, so
    it is recorded as unhashed with the runner's tree digest named explicitly in
    the reason -- an honest "pinned, but not by a hash you can reproduce with
    sha256sum" rather than a hash-shaped value in a field called ``sha256``.
    """
    return model.artifact(
        artifact_id,
        kind=pin.get("kind", "netlist"),
        path=pin.get("path"),
        sha256=pin.get("sha256"),
        unhashed_reason=None if pin.get("sha256") else pin.get("unhashed_reason"),
        size_bytes=pin.get("size_bytes"),
        description=pin.get("description"),
    )


def _analysis_block(step, hal_version, started_at, finished_at, duration_s):
    analysis = {
        "plugin": {
            "name": step.analysis.plugin,
            "version": "unknown",
            "description": step.analysis.description,
        },
        "entry_point": step.analysis.entry_point,
        "configuration": step.config,
    }
    if hal_version:
        analysis["hal"] = {"version": str(hal_version)}
    if started_at:
        analysis["started_at"] = started_at
    if finished_at:
        analysis["finished_at"] = finished_at
    if duration_s is not None:
        analysis["duration_s"] = round(float(duration_s), 3)
    return analysis


def _method(step):
    return model.method(
        step.analysis.method_name,
        step.analysis.method_kind,
        False,
        description=step.analysis.description,
    )


def _evidence(log_paths, command):
    evidence = []
    for description, path in log_paths:
        if path:
            evidence.append(model.evidence("log", description=description, path=path))
    if command:
        evidence.append(
            model.evidence(
                "command",
                description="the exact invocation, re-runnable as-is",
                command=[str(part) for part in command],
            )
        )
    return evidence


def _document(step, pin, finding, hal_version, started_at, finished_at, duration_s, notes):
    return model.document(
        {"name": "hal_runner", "version": __version__},
        [netlist_artifact(pin)],
        _analysis_block(step, hal_version, started_at, finished_at, duration_s),
        [finding],
        generated_at=utc_now(),
        notes=notes,
    )


def timeout_document(
    step,
    pin,
    execution,
    log_paths=(),
    hal_version=None,
    started_at=None,
    finished_at=None,
):
    """A findings document recording that ``step`` hit its wall-clock limit."""
    finding = model.finding(
        "hal_runner/step/{}/timeout".format(step.id),
        "Step {!r} was stopped at its {}s time limit".format(step.id, execution.timeout_s),
        model.STATUS_TIMEOUT,
        _method(step),
        model.scope(
            [_ARTIFACT_ID],
            description="the netlist the step was analysing when it was stopped",
        ),
        summary=(
            "{} did not finish within {}s and its process was {}. Nothing was learned "
            "about the design: a timeout is a statement about this run, not about the "
            "netlist.".format(
                step.analysis.name,
                execution.timeout_s,
                "killed" if execution.killed else "terminated",
            )
        ),
        severity="medium",
        limits_dict=model.limits(
            timeout_s=execution.timeout_s,
            wall_time_s=round(float(execution.duration_s), 3),
            memory_mb=execution.memory_mb,
            hit=True,
            description=(
                "wall-clock limit enforced by hal_runner by killing the step process"
                + ("" if execution.memory_limit_enforced else "; the memory limit was not "
                   "enforced on this platform")
            ),
        ),
        evidence_list=_evidence(log_paths, execution.command),
        tags=["hal_runner", "timeout"],
    )
    return _document(
        step,
        pin,
        finding,
        hal_version,
        started_at,
        finished_at,
        execution.duration_s,
        notes=[
            "written by hal_runner, not by the analysis: the step never reported a result"
        ],
    )


def error_document(
    step,
    pin,
    message,
    detail=None,
    kind="plugin_error",
    log_paths=(),
    command=None,
    hal_version=None,
    started_at=None,
    finished_at=None,
    duration_s=None,
):
    """A findings document recording that ``step`` failed."""
    finding = model.finding(
        "hal_runner/step/{}/error".format(step.id),
        "Step {!r} failed".format(step.id),
        model.STATUS_ERROR,
        _method(step),
        model.scope(
            [_ARTIFACT_ID],
            description="the netlist the step was given",
        ),
        summary=(
            "{} did not complete: {} A failed analysis says nothing about the "
            "design.".format(step.analysis.name, message)
        ),
        severity="high",
        error_dict=model.error(kind, message, detail=detail),
        evidence_list=_evidence(log_paths, command),
        tags=["hal_runner", "error"],
    )
    return _document(
        step,
        pin,
        finding,
        hal_version,
        started_at,
        finished_at,
        duration_s,
        notes=[
            "written by hal_runner, not by the analysis: the step did not report a "
            "usable result"
        ],
    )
