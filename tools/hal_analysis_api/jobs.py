"""Analysis jobs: submission, status, artifacts -- all of it on top of hal_runner.

This module deliberately implements *no* analysis machinery.  Pinning inputs by
content hash, enforcing per-step time and memory limits, killing a step's whole
process group, validating the findings document, writing a diagnostic record
for a step that failed, and recording all of it in a manifest is exactly what
:mod:`hal_runner` already does.  Re-implementing any of that here would produce
a second, weaker set of guarantees.

So a submission is:

1. build a one-step run configuration for the requested analysis over the
   project handle's *source* path (the archive or directory the handle pinned,
   so the manifest records the same digest the handle did);
2. validate it with :mod:`hal_runner.config` -- **before** anything is spawned,
   which is why an unknown analysis or a mistyped option is an error at submit
   time rather than a job that fails two minutes later;
3. write it to the job directory (so the exact same run can be reproduced by
   hand with ``python tools/hal_runner run <job>/run_config.json``);
4. spawn a detached worker that executes it and records the outcome.

Status is read from the job record, and a job whose worker process has
disappeared without recording an outcome is reported as ``lost`` rather than
left as ``running`` for ever -- a status that can never resolve is worse than a
failure.
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid

from hal_findings.adapters.common import utc_now
from hal_runner import analyses as runner_analyses
from hal_runner import config as runner_config

from . import limits as limit_module
from .errors import Internal, InvalidRequest, NotReady, UnknownAnalysis, UnknownJob

__all__ = ["JOB_STATES", "TERMINAL_STATES", "STEP_STATE", "STEP_ID", "JobStore"]

#: Every state a job can be in. ``lost`` is not a HAL outcome: it is what this
#: API reports when the worker process vanished without writing one.
JOB_STATES = ("queued", "running", "succeeded", "failed", "timeout", "cancelled", "lost")

TERMINAL_STATES = ("succeeded", "failed", "timeout", "cancelled", "lost")

#: Keys of the job record that ``analysis.status`` returns; anything else
#: (the worker pid, bookkeeping) stays internal.
_PUBLIC_KEYS = (
    "job",
    "project",
    "analysis",
    "label",
    "state",
    "config",
    "limits",
    "submitted_at",
    "started_at",
    "finished_at",
    "duration_s",
    "exit_code",
    "step_status",
    "findings_counts",
    "findings_source",
    "run_dir",
    "run_config_path",
    "manifest_path",
    "error",
)

#: hal_runner step status -> job state.
STEP_STATE = {
    "success": "succeeded",
    "reused": "succeeded",
    "timeout": "timeout",
    "failed": "failed",
    "skipped": "failed",
}

_TOOLS_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_TOOLS_PATH)

#: The step id every job's single step uses; also its directory name.
STEP_ID = "analysis"


def _now():
    return utc_now()


def _process_alive(pid):
    """True when ``pid`` still exists. Conservative: unknown means alive.

    A worker started by *this* process is checked through its :class:`Popen`
    instead (see :attr:`JobStore.children`): on POSIX an exited child that
    nobody has waited for is a zombie, and ``kill(pid, 0)`` still succeeds for
    it -- so for a long-lived server this check alone would report a crashed
    worker as running for ever.
    """
    if not pid:
        return False
    if os.name == "nt":  # pragma: no cover - Windows
        try:
            output = subprocess.run(
                ["tasklist", "/FI", "PID eq {}".format(int(pid)), "/NH"],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return True
        return str(int(pid)) in output
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive but not ours
        return True
    except OSError:  # pragma: no cover - defensive
        return True
    return True


def _terminate(pid):
    """Stop a worker and the HAL process group below it. Returns True if it was signalled."""
    if not pid:
        return False
    if os.name == "nt":  # pragma: no cover - Windows
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(int(pid))],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False
    for sender in (
        lambda: os.killpg(os.getpgid(int(pid)), signal.SIGTERM),
        lambda: os.kill(int(pid), signal.SIGTERM),
    ):
        try:
            sender()
            return True
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return False


def _spawn_detached(job_dir, document):
    """Start the worker for ``job_dir`` and return the :class:`Popen` handle.

    Detached on purpose: the API call that submitted the job returns
    immediately, and the analysis outlives it -- an MCP session, a CLI
    invocation and a test all submit the same way.
    """
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        _TOOLS_PATH if not existing else _TOOLS_PATH + os.pathsep + existing
    )
    command = [sys.executable, "-m", "hal_analysis_api", "worker", job_dir]
    log_path = os.path.join(job_dir, "worker.log")
    handle = open(log_path, "ab")
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": handle, "stderr": handle}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:  # pragma: no cover - Windows
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
        kwargs["creationflags"] = flags
    try:
        process = subprocess.Popen(command, cwd=_REPO_ROOT, env=environment, **kwargs)
    finally:
        handle.close()
    return process


class JobStore(object):
    """The jobs of one workspace."""

    def __init__(self, workspace, hal_binary=None, spawn=None):
        self.workspace = os.path.abspath(workspace)
        self.directory = os.path.join(self.workspace, "jobs")
        self.hal_binary = hal_binary
        #: Injected in tests so a job can be executed inline instead of detached.
        #: May return a :class:`subprocess.Popen`, a pid, or nothing.
        self.spawn = spawn or _spawn_detached
        #: Workers this process started, so their exit can be observed directly.
        self.children = {}

    # -- paths ---------------------------------------------------------------

    def job_dir(self, job_id):
        return os.path.join(self.directory, job_id)

    def _record_path(self, job_id):
        return os.path.join(self.job_dir(job_id), "job.json")

    # -- submission ----------------------------------------------------------

    def build_run_config(self, handle, analysis, config, timeout_s, memory_mb, job_id, job_dir):
        """The one-step hal_runner configuration this job runs.

        Written to the job directory as ``run_config.json`` so that the run is
        reproducible outside the API: ``python tools/hal_runner run
        <job_dir>/run_config.json`` does exactly what the job did.
        """
        return {
            "config_version": runner_config.CONFIG_VERSION,
            "name": job_id,
            "description": "submitted through hal_analysis_api for project {}".format(handle.id),
            "netlist": handle.source_path,
            "gate_library": handle.gate_library,
            "output_dir": os.path.join(job_dir, "run"),
            "steps": [
                {
                    "id": STEP_ID,
                    "analysis": analysis,
                    "config": dict(config or {}),
                    "timeout_s": timeout_s,
                    "memory_mb": memory_mb,
                }
            ],
        }

    def submit(self, handle, analysis, config=None, timeout_s=None, memory_mb=None, label=None):
        """Validate, record and start one analysis. Returns the job record."""
        if analysis not in runner_analyses.ANALYSES:
            raise UnknownAnalysis(
                "no analysis {!r}".format(analysis),
                detail="hal_runner knows: {}".format(", ".join(runner_analyses.names())),
                hint="call analysis.list for the options each one takes",
                data={"analysis": analysis, "known": runner_analyses.names()},
            )

        timeout_s = int(timeout_s or limit_module.DEFAULT_ANALYSIS_TIMEOUT_S)
        job_id = "job-" + uuid.uuid4().hex[:12]
        job_dir = self.job_dir(job_id)
        os.makedirs(job_dir, exist_ok=True)

        document = self.build_run_config(
            handle, analysis, config, timeout_s, memory_mb, job_id, job_dir
        )
        try:
            # Validated here, before anything is spawned: an unknown option is a
            # typo in the request, not a job that dies two minutes from now.
            parsed = runner_config.parse(document, base_dir=job_dir)
        except runner_config.ConfigError as exc:
            raise InvalidRequest(
                "the analysis configuration is not usable",
                detail="\n".join(exc.errors),
                hint="analysis.list reports every option {!r} accepts".format(analysis),
                data={"analysis": analysis},
            )

        run_config_path = os.path.join(job_dir, "run_config.json")
        with open(run_config_path, "w", encoding="utf-8", newline="\n") as handle_out:
            handle_out.write(json.dumps(document, indent=2, sort_keys=True) + "\n")

        record = {
            "job": job_id,
            "project": handle.id,
            "analysis": analysis,
            "label": label,
            "state": "queued",
            "config": parsed.steps[0].config,
            "limits": {"timeout_s": timeout_s, "memory_mb": memory_mb},
            "submitted_at": _now(),
            "started_at": None,
            "finished_at": None,
            "duration_s": None,
            "exit_code": None,
            "step_status": None,
            "findings_counts": None,
            "findings_source": None,
            "findings_path": None,
            "run_dir": os.path.join(job_dir, "run"),
            "run_config_path": run_config_path,
            "manifest_path": None,
            "error": None,
            "hal_binary": self.hal_binary,
            "pid": None,
        }
        self.write(record)

        try:
            started = self.spawn(job_dir, record)
            pid = started.pid if hasattr(started, "pid") else started
            if hasattr(started, "pid"):
                self.children[job_id] = started
            # The pid goes in its own file, never back into job.json: from the
            # moment the worker starts, *it* owns that record. A second write
            # from here would race a fast worker and could overwrite a finished
            # job with the "queued" copy this function still holds.
            if pid:
                with open(os.path.join(job_dir, "worker.pid"), "w", encoding="utf-8") as pid_file:
                    pid_file.write("{}\n".format(int(pid)))
        except OSError as exc:
            record["state"] = "failed"
            record["finished_at"] = _now()
            record["error"] = {
                "code": "internal",
                "message": "could not start the analysis worker: {}".format(exc),
            }
            self.write(record)
            raise Internal(
                "could not start the analysis worker: {}".format(exc),
                detail="command: {} -m hal_analysis_api worker {}".format(sys.executable, job_dir),
            )
        return self.read(job_id)

    # -- reading -------------------------------------------------------------

    def read(self, job_id):
        """The raw job record, or ``unknown_job``."""
        path = self._record_path(str(job_id))
        if not os.path.isfile(path):
            raise UnknownJob(
                "no analysis job {!r} in this workspace".format(job_id),
                detail="workspace: {}".format(self.workspace),
                hint="submit one with analysis.submit, or list them with analysis.jobs",
            )
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError) as exc:
            raise Internal("the job record {} is unreadable: {}".format(path, exc))

    def write(self, record):
        path = self._record_path(record["job"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
        return path

    def status(self, job_id, wait_s=None):
        """The public status of a job, optionally waiting a bounded time for it.

        A wait that expires is **not** an error: the answer is the job's real
        state plus ``wait_timed_out: true``.  A caller that treated a still
        running job as a failure would be wrong, and one that could not tell
        the difference would be blind.
        """
        deadline = time.time() + min(float(wait_s or 0), limit_module.MAX_WAIT_S)
        started = time.time()
        record = self._reconcile(self.read(job_id))
        while record["state"] not in TERMINAL_STATES and time.time() < deadline:
            time.sleep(0.2)
            record = self._reconcile(self.read(job_id))
        waited = time.time() - started
        status = self.public(record)
        status["waited_s"] = round(waited, 3) if wait_s else None
        status["wait_timed_out"] = (
            (record["state"] not in TERMINAL_STATES) if wait_s else None
        )
        return status

    def pid_of(self, record):
        """The worker's pid: from the record the worker writes, else from ``worker.pid``.

        Two sources because there are two writers with one owner each: the
        worker owns ``job.json`` from the moment it starts, and the process
        that spawned it owns ``worker.pid``.  Between the spawn and the
        worker's first write, only the second exists.
        """
        if record.get("pid"):
            return record["pid"]
        path = os.path.join(self.job_dir(record["job"]), "worker.pid")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return int(handle.read().strip())
        except (OSError, ValueError):
            return None

    def _worker_gone(self, record):
        """True when the worker for ``record`` is definitely not running any more."""
        child = self.children.get(record["job"])
        if child is not None:
            return child.poll() is not None
        pid = self.pid_of(record)
        return bool(pid) and not _process_alive(pid)

    def _reconcile(self, record):
        """Turn a job whose worker died into ``lost`` instead of eternal ``running``."""
        if record["state"] in TERMINAL_STATES:
            return record
        pid = self.pid_of(record)
        if self._worker_gone(record):
            record["state"] = "lost"
            record["finished_at"] = _now()
            record["error"] = {
                "code": "internal",
                "message": "the analysis worker (pid {}) exited without recording an "
                "outcome".format(pid),
                "hint": "see worker.log in the job directory",
            }
            self.write(record)
        return record

    def public(self, record):
        """The job record as ``job_status`` in the API schema."""
        status = {key: record.get(key) for key in _PUBLIC_KEYS}
        status["terminal"] = record["state"] in TERMINAL_STATES
        return status

    def list(self, project=None, state=None):
        """Every job in this workspace, newest first."""
        if not os.path.isdir(self.directory):
            return []
        records = []
        for name in sorted(os.listdir(self.directory)):
            if not name.startswith("job-"):
                continue
            try:
                record = self._reconcile(self.read(name))
            except (UnknownJob, Internal):
                continue
            if project and record.get("project") != project:
                continue
            if state and record.get("state") != state:
                continue
            records.append(record)
        return sorted(records, key=lambda entry: entry.get("submitted_at") or "", reverse=True)

    # -- control -------------------------------------------------------------

    def cancel(self, job_id):
        """Stop a running job. Returns ``(cancelled, reason, record)``.

        Cancelling is a mutation of the *job*, never of the project, which is
        why it is its own operation and never a side effect of a status call.
        """
        record = self._reconcile(self.read(job_id))
        if record["state"] in TERMINAL_STATES:
            return False, "the job is already {}".format(record["state"]), record
        signalled = _terminate(self.pid_of(record))
        record["state"] = "cancelled"
        record["finished_at"] = _now()
        record["error"] = {
            "code": "internal" if not signalled else "timeout",
            "message": "cancelled through analysis.cancel"
            + ("" if signalled else " (the worker could not be signalled)"),
        }
        self.write(record)
        return True, None, record

    # -- results -------------------------------------------------------------

    def findings_path(self, record):
        """Where the job's findings (or diagnostic) document is, or raise."""
        if record["state"] not in TERMINAL_STATES:
            raise NotReady(
                "job {} is {}; it has produced no findings yet".format(
                    record["job"], record["state"]
                ),
                hint="poll analysis.status (optionally with wait_s) until terminal is true",
                data={"state": record["state"]},
            )
        path = record.get("findings_path")
        if not path or not os.path.isfile(path):
            raise NotReady(
                "job {} is {} but wrote no findings document".format(
                    record["job"], record["state"]
                ),
                detail=record.get("error", {}).get("message") if record.get("error") else None,
                hint="artifact.list shows what the job did write, including its logs",
                data={"state": record["state"]},
            )
        return path, record.get("findings_source") or "findings"

    def artifacts(self, record):
        """Everything the job wrote, with paths relative to the job directory."""
        job_dir = self.job_dir(record["job"])
        entries = []
        for relative, role, description in self._artifact_candidates(record):
            absolute = os.path.join(job_dir, relative)
            if not os.path.isfile(absolute):
                continue
            entries.append(
                {
                    "path": relative.replace(os.sep, "/"),
                    "role": role,
                    "description": description,
                    "sha256": None,
                    "size_bytes": os.path.getsize(absolute),
                }
            )
        return sorted(entries, key=lambda entry: entry["path"])

    def _artifact_candidates(self, record):
        job_dir = self.job_dir(record["job"])
        candidates = [
            ("run_config.json", "run_config", "the hal_runner configuration this job ran"),
            ("worker.log", "log", "stdout and stderr of the job worker"),
        ]
        manifest_path = record.get("manifest_path")
        if manifest_path:
            candidates.append(
                (
                    os.path.relpath(manifest_path, job_dir),
                    "manifest",
                    "the hal_runner manifest: inputs, digests, limits and outcome",
                )
            )
            step_dir = os.path.join(os.path.dirname(manifest_path), "steps", STEP_ID)
            for name, role, description in (
                ("findings.json", "findings", "the schema-validated findings document"),
                (
                    "diagnostic.json",
                    "diagnostic",
                    "the findings document hal_runner writes for a failed or timed-out step",
                ),
                ("result.json", "result", "what the step reported back"),
                ("request.json", "request", "exactly what the step was asked to do"),
                (os.path.join("logs", "stdout.log"), "log", "hal stdout of the step"),
                (os.path.join("logs", "stderr.log"), "log", "hal stderr of the step"),
                ("graph.dot", "dot", "the analysis' own Graphviz export"),
                ("groups.txt", "report", "the analysis' own textual report"),
            ):
                candidates.append(
                    (os.path.relpath(os.path.join(step_dir, name), job_dir), role, description)
                )
        return candidates
