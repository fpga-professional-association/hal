"""The detached process that executes one analysis job.

Started by :meth:`hal_analysis_api.jobs.JobStore.submit` as
``python -m hal_analysis_api worker <job_dir>`` and never called directly by a
caller of the API.  It is a separate process for the same reason a hal_runner
step is: ``analysis.submit`` has to return immediately, the analysis has to
survive the process that submitted it (an MCP session, a CLI call), and a
cancel has to be able to kill something.

What it does is small, because :mod:`hal_runner` does the work:

1. mark the job ``running`` and record when it started;
2. load the job's ``run_config.json`` and execute it with
   :class:`hal_runner.runner.Runner`;
3. read the outcome out of the *manifest* rather than out of its own memory --
   the manifest is what a later reader will see, so the job record must agree
   with it;
4. write the terminal state.

Every failure path ends in a written job record.  A worker that dies before it
can write one is detected by ``analysis.status`` and reported as ``lost``; this
module's job is to make that rare, not to pretend it cannot happen.
"""

import json
import os
import time
import traceback

from hal_findings.adapters.common import utc_now
from hal_runner import config as runner_config
from hal_runner import runner as runner_module

from .jobs import STEP_ID, STEP_STATE, JobStore

__all__ = ["run_job", "main"]


def _load_record(job_dir):
    with open(os.path.join(job_dir, "job.json"), "r", encoding="utf-8") as handle:
        return json.load(handle)


def run_job(job_dir):
    """Execute the job in ``job_dir``. Returns the process exit code.

    ``0`` when the analysis succeeded, ``1`` when it failed or timed out, ``2``
    when the job could not be started at all -- the same vocabulary
    ``hal_runner`` uses, because a caller reading exit codes should not have to
    learn a second one.
    """
    job_dir = os.path.abspath(job_dir)
    record = _load_record(job_dir)
    store = JobStore(
        os.path.dirname(os.path.dirname(job_dir)), hal_binary=record.get("hal_binary")
    )

    record["state"] = "running"
    record["started_at"] = utc_now()
    record["pid"] = os.getpid()
    store.write(record)
    started = time.time()

    def finish(state, error=None, exit_code=None):
        record["state"] = state
        record["finished_at"] = utc_now()
        record["duration_s"] = round(time.time() - started, 3)
        record["exit_code"] = exit_code
        if error is not None:
            record["error"] = error
        store.write(record)
        return 0 if state == "succeeded" else (2 if state == "failed" and exit_code is None else 1)

    try:
        configuration = runner_config.load(record["run_config_path"])
    except runner_config.ConfigError as exc:
        return finish(
            "failed",
            {"code": "invalid_request", "message": str(exc), "detail": "\n".join(exc.errors)},
        )

    try:
        runner = runner_module.Runner(
            configuration,
            hal_binary=record.get("hal_binary"),
            reporter=runner_module.Reporter(quiet=True),
            command=["python", "-m", "hal_analysis_api", "worker", job_dir],
        )
        runner.prepare()
    except runner_module.RunnerError as exc:
        return finish(
            "failed",
            {
                "code": "hal_unavailable",
                "message": str(exc),
                "hint": "hal.capabilities reports whether a hal binary was found",
            },
        )
    except Exception as exc:  # noqa: BLE001 - a job that cannot start is a failed job
        return finish(
            "failed",
            {
                "code": "internal",
                "message": "could not prepare the run: {}: {}".format(type(exc).__name__, exc),
                "detail": traceback.format_exc(),
            },
        )

    try:
        exit_code, manifest = runner.run()
    except Exception as exc:  # noqa: BLE001 - the runner itself broke
        return finish(
            "failed",
            {
                "code": "internal",
                "message": "the runner raised {}: {}".format(type(exc).__name__, exc),
                "detail": traceback.format_exc(),
            },
        )

    record["manifest_path"] = os.path.join(runner.output_dir, "manifest.json")
    step = (manifest.get("steps") or [{}])[0]
    step_status = step.get("status")
    record["step_status"] = step_status
    state = STEP_STATE.get(step_status, "failed")

    findings = step.get("findings") or {}
    diagnostic = step.get("diagnostic") or {}
    if findings.get("path"):
        record["findings_path"] = os.path.join(
            runner.output_dir, step.get("output_dir", os.path.join("steps", STEP_ID)),
            findings["path"],
        )
        record["findings_source"] = "findings"
        record["findings_counts"] = findings.get("counts")
    elif diagnostic.get("path"):
        # A timed-out or failed step still produced a real findings document;
        # labelling it 'diagnostic' is what keeps it from being mistaken for a
        # result about the design.
        record["findings_path"] = os.path.join(runner.output_dir, diagnostic["path"])
        record["findings_source"] = "diagnostic"

    error = None
    if state != "succeeded":
        error = {
            "code": "timeout" if state == "timeout" else "hal_error",
            "message": diagnostic.get("message")
            or "the analysis step ended as {!r}".format(step_status),
            "hint": "artifact.get the step's logs, or findings.get for the diagnostic record",
        }
    return finish(state, error=error, exit_code=exit_code)


def main(argv=None):
    """``python -m hal_analysis_api worker <job_dir>``."""
    argv = list(argv if argv is not None else [])
    if len(argv) != 1:
        import sys

        sys.stderr.write("usage: python -m hal_analysis_api worker <job-directory>\n")
        return 2
    return run_job(argv[0])
