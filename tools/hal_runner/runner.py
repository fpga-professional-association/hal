"""The orchestrator: configuration in, manifest out.

The rules this module implements, in the order they matter:

1. **Nothing is trusted.**  A step that exits 0 but writes no result record has
   failed.  A step that declares an artifact it did not write has failed.  A
   step whose findings document does not validate against the findings schema
   has failed.  Success has to be demonstrated, not assumed.
2. **A failure is recorded, not just returned.**  Every unsuccessful step gets a
   diagnostic findings document (``timeout`` or ``error``) next to its retained
   logs, and the manifest is written whether the run succeeded or not.
3. **A reused result is a verified result.**  See :mod:`hal_runner.checkpoint`.
4. **Order is honoured.**  Steps run in the order the configuration lists them;
   after a failure the remaining steps are recorded as ``skipped``, never
   silently dropped and never reported as successful.

The runner never imports ``hal_py``.  It builds a request, runs
``hal --python-script`` on :mod:`hal_runner.steps.step_runner`, and reads back
files -- which is also why the whole orchestrator can be unit tested against a
stub executor with no HAL build in sight.
"""

import os
import shutil
import sys
import time
import zipfile
from pathlib import PurePosixPath

from hal_findings import serialize as findings_serialize
from hal_findings import validate as findings_validate
from hal_findings.adapters.common import utc_now

from . import __version__, checkpoint, diagnostics, manifest as manifest_module, protocol
from .execute import ProcessExecutor, tail
from .hashing import describe_input, json_digest, sha256_file

__all__ = ["Runner", "RunnerError", "Reporter", "resolve_hal_binary", "STEP_RUNNER"]

#: The script every step is executed as. It is a plain file on purpose: HAL's
#: ``--python-script`` takes a path to a ``.py`` file, not a module.
STEP_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steps", "step_runner.py")

TOOLS_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TOOLS_PATH)

#: Findings artifacts are validated; there must be exactly one per step.
FINDINGS_ROLE = "findings"


class RunnerError(RuntimeError):
    """A problem with the run itself: a missing input, no hal binary, a bad request."""


class Reporter(object):
    """Progress on stderr so stdout stays free for machine-readable output."""

    def __init__(self, quiet=False, stream=None):
        self.quiet = quiet
        self.stream = stream if stream is not None else sys.stderr

    def info(self, message):
        if not self.quiet:
            self.stream.write("[hal_runner] {}\n".format(message))

    def warn(self, message):
        self.stream.write("[hal_runner] warning: {}\n".format(message))

    def error(self, message):
        self.stream.write("[hal_runner] error: {}\n".format(message))


def resolve_hal_binary(explicit=None, config_value=None):
    """Find the ``hal`` executable, preferring the most explicit answer.

    Order: ``--hal-binary``, the configuration's ``hal_binary``,
    ``$HAL_RUNNER_HAL_BINARY``, ``$HAL_BASE_PATH/bin/hal``, then ``hal`` on
    ``PATH``.
    """
    candidates = [explicit, config_value, os.environ.get("HAL_RUNNER_HAL_BINARY")]
    base = os.environ.get("HAL_BASE_PATH")
    if base:
        candidates.append(os.path.join(base, "bin", "hal"))
    for candidate in candidates:
        if not candidate:
            continue
        expanded = os.path.abspath(os.path.expanduser(str(candidate)))
        if os.path.isfile(expanded):
            return expanded
        found = shutil.which(str(candidate))
        if found:
            return os.path.abspath(found)
    found = shutil.which("hal")
    if found:
        return os.path.abspath(found)
    raise RunnerError(
        "no 'hal' executable found. Pass --hal-binary <build>/bin/hal, set "
        "$HAL_RUNNER_HAL_BINARY or $HAL_BASE_PATH, or put hal on PATH. This fork "
        "builds on Linux/macOS only; see the Build Instructions in the README."
    )


def _relative(path, start):
    """``path`` relative to ``start`` when that is meaningful, else the absolute path."""
    path = os.path.abspath(str(path))
    try:
        relative = os.path.relpath(path, os.path.abspath(str(start)))
    except ValueError:  # pragma: no cover - different drives on Windows
        return path
    if relative.startswith(".."):
        return path
    return relative.replace(os.sep, "/")


class Runner(object):
    """Executes one :class:`hal_runner.config.RunConfig`."""

    def __init__(
        self,
        config,
        output_dir=None,
        hal_binary=None,
        cache_dir=None,
        use_cache=True,
        refresh_cache=False,
        executor=None,
        reporter=None,
        repo_root=None,
        tools_path=None,
        only=None,
        command=None,
    ):
        self.config = config
        self.output_dir = os.path.abspath(output_dir or config.output_dir)
        self.hal_binary = hal_binary
        self.executor = executor if executor is not None else ProcessExecutor()
        self.reporter = reporter if reporter is not None else Reporter()
        self.repo_root = os.path.abspath(repo_root or REPO_ROOT)
        self.tools_path = os.path.abspath(tools_path or TOOLS_PATH)
        self.command = list(command or [])
        self.only = list(only) if only else None

        self.cache = checkpoint.CheckpointStore(
            cache_dir or os.path.join(self.output_dir, "cache"),
            enabled=use_cache,
            refresh=refresh_cache,
        )

        self.run_id = None
        self.inputs = []
        self.tool = None
        self.pin = None
        self.netlist_for_steps = None
        self.gate_library_for_steps = None

    # -- preparation --------------------------------------------------------

    @property
    def steps(self):
        """The steps this invocation will run, honouring ``--step``."""
        if self.only is None:
            return list(self.config.steps)
        selected = [step for step in self.config.steps if step.id in self.only]
        missing = sorted(set(self.only) - {step.id for step in selected})
        if missing:
            raise RunnerError(
                "no step named {} in run {!r}; it has {}".format(
                    ", ".join(repr(name) for name in missing),
                    self.config.name,
                    ", ".join(step.id for step in self.config.steps),
                )
            )
        return selected

    def prepare(self, require_hal=True, materialize=True):
        """Resolve the hal binary, pin every input, and lay out the run directory.

        With ``materialize=False`` nothing is written -- no output directory, no
        unpacked archive -- which is what makes ``--dry-run`` a read-only
        operation that still computes real digests and real cache keys.
        """
        if require_hal:
            self.hal_binary = resolve_hal_binary(self.hal_binary, self.config.hal_binary)
        if materialize:
            os.makedirs(self.output_dir, exist_ok=True)

        if not os.path.exists(self.config.netlist):
            raise RunnerError(
                "the netlist declared by the run configuration does not exist: {}".format(
                    self.config.netlist
                )
            )
        if self.config.gate_library and not os.path.isfile(self.config.gate_library):
            raise RunnerError(
                "the gate library declared by the run configuration does not exist: "
                "{}".format(self.config.gate_library)
            )

        self.inputs = []
        netlist_input = describe_input(self.config.netlist, role="netlist")
        netlist_input["path"] = self.config.document["netlist"]
        self.inputs.append(netlist_input)
        self.netlist_for_steps, extracted = self._materialize_netlist(netlist_input, materialize)
        if extracted:
            netlist_input["extracted_to"] = extracted

        if self.config.gate_library:
            library_input = describe_input(self.config.gate_library, role="gate_library")
            library_input["path"] = self.config.document.get("gate_library")
            self.inputs.append(library_input)
            self.gate_library_for_steps = self.config.gate_library

        self.tool = manifest_module.tool_info(self.hal_binary, self.repo_root)
        if self.hal_binary and os.path.isfile(self.hal_binary):
            self.tool["hal"]["binary_sha256"] = sha256_file(self.hal_binary)

        self.pin = self._build_pin(netlist_input)
        self.run_id = "{}-{}".format(
            self.config.name, json_digest({"inputs": self.inputs, "at": time.time()})[:8]
        )
        return self

    def _materialize_netlist(self, netlist_input, materialize=True):
        """Return the path the steps load, unpacking a project archive if needed.

        HAL's examples ship as ``.zip`` project archives, and an archive is the
        *better* input: a single file with a real ``sha256``, unlike a directory
        that can only be pinned by a digest of our own construction.  So the
        archive is what is hashed and recorded, and what the steps get is the
        directory it was unpacked into, below the run's output directory.

        The destination is derived from the archive's *listing*, so a dry run
        can report where the project would land without writing anything.
        """
        source = self.config.netlist
        if os.path.isdir(source) or not source.lower().endswith(".zip"):
            return source, None

        target = os.path.join(
            self.output_dir, "inputs", os.path.splitext(os.path.basename(source))[0]
        )
        with zipfile.ZipFile(source) as archive:
            names = archive.namelist()
            for name in names:
                parts = PurePosixPath(name).parts
                if os.path.isabs(name) or ".." in parts or (parts and parts[0].endswith(":")):
                    raise RunnerError(
                        "refusing to extract {!r} from {}: the archive escapes its "
                        "destination".format(name, source)
                    )
            if materialize:
                if os.path.isdir(target):
                    shutil.rmtree(target)
                os.makedirs(target, exist_ok=True)
                archive.extractall(target)

        # A HAL project archive holds the project in a single top-level directory
        # (examples/uart.zip holds 'uart/'); anything else is unpacked flat.
        roots = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
        root_files = {name for name in names if len(PurePosixPath(name).parts) == 1 and not name.endswith("/")}
        project_dir = os.path.join(target, sorted(roots)[0]) if (len(roots) == 1 and not root_files) else target
        if materialize:
            self.reporter.info("unpacked {} -> {}".format(os.path.basename(source), project_dir))
        return project_dir, project_dir

    def _build_pin(self, netlist_input):
        """How the netlist is described in every findings document this run writes."""
        path = netlist_input.get("path") or netlist_input["resolved_path"]
        if netlist_input["kind"] == "file":
            kind = "hal_project" if str(path).lower().endswith(".zip") else "netlist"
            return {
                "path": path,
                "kind": kind,
                "sha256": netlist_input["sha256"],
                "size_bytes": netlist_input.get("size_bytes"),
                "description": "pinned by hal_runner run {!r}".format(self.config.name),
            }
        return {
            "path": path,
            "kind": "hal_project",
            "sha256": None,
            "unhashed_reason": (
                "HAL project directory: no single file to hash. hal_runner pinned it as "
                "{}:{} over its {} files; see the run manifest.".format(
                    netlist_input["digest_algorithm"],
                    netlist_input["digest"],
                    netlist_input.get("file_count", "?"),
                )
            ),
            "description": "pinned by hal_runner run {!r}".format(self.config.name),
        }

    # -- planning -----------------------------------------------------------

    def step_dir(self, step):
        return os.path.join(self.output_dir, "steps", step.id)

    def cache_key_for(self, step):
        components = checkpoint.key_components(step.as_json(), self.inputs, self.tool)
        return checkpoint.cache_key(components), components

    def build_request(self, step):
        """The request handed to the step process."""
        step_dir = self.step_dir(step)
        return {
            "request_version": protocol.REQUEST_VERSION,
            "run": {"id": self.run_id, "name": self.config.name},
            "step_id": step.id,
            "analysis": step.analysis.name,
            "module": step.analysis.module,
            "plugin": step.analysis.plugin,
            "config": step.config,
            "tools_path": self.tools_path,
            "netlist": self.netlist_for_steps,
            "gate_library": self.gate_library_for_steps,
            "pin": self.pin,
            "artifact_id": "netlist",
            "output_dir": step_dir,
            "findings_file": "findings.json",
            "result_file": "result.json",
            "hal_version": self.tool["hal"]["version"],
            "limits": {"timeout_s": step.timeout_s, "memory_mb": step.memory_mb},
        }

    def build_command(self, step):
        return [self.hal_binary or "hal"] + self.config.hal_args + ["--python-script", STEP_RUNNER]

    def plan(self):
        """What :meth:`run` would do, without doing any of it."""
        plan = []
        for step in self.steps:
            key, _ = self.cache_key_for(step)
            plan.append(
                {
                    "id": step.id,
                    "analysis": step.analysis.name,
                    "config": step.config,
                    "timeout_s": step.timeout_s,
                    "memory_mb": step.memory_mb,
                    "cache_key": key,
                    "output_dir": self.step_dir(step),
                    "command": self.build_command(step),
                }
            )
        return plan

    # -- execution ----------------------------------------------------------

    def run(self):
        """Execute the run. Returns ``(exit_code, manifest)``; never raises for a failed step."""
        started_at = utc_now()
        started = time.time()
        records = []
        failed = False
        stop = False

        for step in self.steps:
            if stop:
                records.append(self._skipped(step, "an earlier step failed"))
                self.reporter.warn("skipping step {!r}: an earlier step failed".format(step.id))
                continue
            record = self._run_step(step)
            records.append(record)
            if record["status"] not in manifest_module.SUCCESS_STATUSES:
                # A failure always makes the run a failure. Whether it also ends the
                # run is the configuration's decision, per step.
                failed = True
                stop = self.config.stop_on_failure and not step.continue_on_failure

        finished_at = utc_now()
        duration = time.time() - started
        document = self._build_manifest(records, started_at, finished_at, duration, failed)
        manifest_module.write(document, os.path.join(self.output_dir, "manifest.json"))
        return (1 if failed else 0), document

    def _skipped(self, step, reason):
        return {
            "id": step.id,
            "analysis": step.analysis.name,
            "status": "skipped",
            "reason": reason,
            "config": step.config,
            "config_digest": json_digest(step.as_json()),
            "timeout_s": step.timeout_s,
            "memory_mb": step.memory_mb,
            "artifacts": [],
        }

    def _run_step(self, step):
        step_dir = self.step_dir(step)
        key, components = self.cache_key_for(step)
        record = {
            "id": step.id,
            "analysis": step.analysis.name,
            "description": step.description,
            "config": step.config,
            "config_digest": json_digest(step.as_json()),
            "timeout_s": step.timeout_s,
            "memory_mb": step.memory_mb,
            "output_dir": _relative(step_dir, self.output_dir),
            "artifacts": [],
        }

        entry, reason = self.cache.lookup(key, components)
        record["cache"] = {"key": key, "status": "hit" if entry else "miss", "reason": reason}
        if entry is not None:
            reused = self._reuse(step, entry, step_dir, record)
            if reused:
                return record
            # The entry looked fine but its findings no longer validate; fall
            # through and run the step for real rather than reporting a result
            # nobody can stand behind.
            record["cache"]["status"] = "rejected"

        os.makedirs(step_dir, exist_ok=True)
        logs_dir = os.path.join(step_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        for stale in ("result.json", "findings.json", "diagnostic.json"):
            path = os.path.join(step_dir, stale)
            if os.path.isfile(path):
                os.remove(path)

        request = self.build_request(step)
        request_path = os.path.join(step_dir, "request.json")
        protocol.write_json(request, request_path)

        environment = os.environ.copy()
        environment[protocol.REQUEST_ENV] = request_path
        command = self.build_command(step)
        stdout_path = os.path.join(logs_dir, "stdout.log")
        stderr_path = os.path.join(logs_dir, "stderr.log")

        record["command"] = command
        record["logs"] = {
            "stdout": _relative(stdout_path, self.output_dir),
            "stderr": _relative(stderr_path, self.output_dir),
        }
        record["started_at"] = utc_now()
        self.reporter.info(
            "step {!r}: {}{}".format(
                step.id,
                step.analysis.name,
                "" if step.timeout_s is None else " (limit {}s)".format(step.timeout_s),
            )
        )

        execution = self.executor.execute(
            command,
            cwd=step_dir,
            env=environment,
            timeout_s=step.timeout_s,
            memory_mb=step.memory_mb,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        record["finished_at"] = utc_now()
        record["duration_s"] = round(float(execution.duration_s), 3)
        record["execution"] = execution.as_json()

        log_paths = [
            ("hal stdout of this step", "logs/stdout.log"),
            ("hal stderr of this step", "logs/stderr.log"),
        ]

        if execution.timed_out:
            record["status"] = "timeout"
            document = diagnostics.timeout_document(
                step,
                self.pin,
                execution,
                log_paths=log_paths,
                hal_version=self.tool["hal"]["version"],
                started_at=record["started_at"],
                finished_at=record["finished_at"],
            )
            self._write_diagnostic(record, step_dir, document, "the step exceeded its time limit")
            self.reporter.warn(
                "step {!r} timed out after {}s; logs kept in {}".format(
                    step.id, step.timeout_s, logs_dir
                )
            )
            return record

        failure = self._collect(step, step_dir, execution, record)
        if failure is None:
            record["status"] = "success"
            self.cache.store(key, components, step_dir, record["artifacts"], record.get("result", {}))
            self.reporter.info(
                "step {!r} finished in {:.1f}s".format(step.id, execution.duration_s)
            )
            return record

        message, kind, detail = failure
        record["status"] = "failed"
        document = diagnostics.error_document(
            step,
            self.pin,
            message,
            detail=detail,
            kind=kind,
            log_paths=log_paths,
            command=command,
            hal_version=self.tool["hal"]["version"],
            started_at=record["started_at"],
            finished_at=record["finished_at"],
            duration_s=execution.duration_s,
        )
        self._write_diagnostic(record, step_dir, document, message)
        self.reporter.error(
            "step {!r} failed: {} (logs in {})".format(step.id, message, logs_dir)
        )
        return record

    def _collect(self, step, step_dir, execution, record):
        """Turn a finished process into a verdict.

        Returns ``None`` when the step really succeeded, else
        ``(message, error_kind, detail)``.
        """
        stderr_tail = tail(os.path.join(step_dir, "logs", "stderr.log"))
        result_path = os.path.join(step_dir, "result.json")

        if execution.exit_code != 0:
            return (
                "hal exited with {}".format(execution.exit_code),
                "plugin_error",
                stderr_tail or None,
            )
        if not os.path.isfile(result_path):
            return (
                "the step exited successfully but wrote no result record",
                "internal",
                "expected {}; a step that does not report what it did is not a step "
                "that succeeded.\n{}".format(result_path, stderr_tail),
            )
        try:
            result = protocol.read_result(result_path)
        except protocol.ProtocolError as exc:
            return (str(exc), "internal", stderr_tail or None)

        record["result"] = result
        if result.get("status") != "ok":
            error = result.get("error") or {}
            return (
                error.get("message", "the step reported a failure"),
                error.get("kind", "plugin_error"),
                error.get("detail") or stderr_tail or None,
            )

        artifacts, problem = self._collect_artifacts(step_dir, result)
        if problem:
            return problem
        record["artifacts"] = artifacts

        findings = [entry for entry in artifacts if entry.get("role") == FINDINGS_ROLE]
        if len(findings) != 1:
            return (
                "the step declared {} findings artifacts, expected exactly one".format(
                    len(findings)
                ),
                "internal",
                None,
            )
        summary, problem = self._read_findings(os.path.join(step_dir, findings[0]["path"]))
        if problem:
            return problem
        summary["path"] = findings[0]["path"]
        record["findings"] = summary
        return None

    def _collect_artifacts(self, step_dir, result):
        artifacts = []
        for declared in result.get("artifacts", []):
            relative = declared.get("path")
            if not relative:
                return [], ("the step declared an artifact without a path", "internal", None)
            absolute = os.path.join(step_dir, relative)
            if not os.path.isfile(absolute):
                return (
                    [],
                    (
                        "the step declared the artifact {!r} but did not write it".format(
                            relative
                        ),
                        "internal",
                        None,
                    ),
                )
            artifacts.append(
                {
                    "path": relative,
                    "role": declared.get("role", "other"),
                    "description": declared.get("description"),
                    "sha256": sha256_file(absolute),
                    "size_bytes": os.path.getsize(absolute),
                }
            )
        return sorted(artifacts, key=lambda entry: entry["path"]), None

    @staticmethod
    def _read_findings(path):
        """Validate a findings document and summarize it, or explain why it is unusable."""
        try:
            document = findings_serialize.read_document(path)
        except (OSError, ValueError) as exc:
            return None, ("the findings document is unreadable: {}".format(exc), "internal", None)
        try:
            errors = findings_validate.collect_errors(document)
        except findings_validate.FindingsValidationError as exc:
            errors = exc.errors
        if errors:
            return None, (
                "the step wrote a findings document that does not validate",
                "internal",
                "\n".join(errors[:20]),
            )
        counts = {}
        for finding in document.get("findings", []):
            counts[finding["status"]] = counts.get(finding["status"], 0) + 1
        return (
            {
                "schema_version": document.get("schema_version"),
                "digest": findings_serialize.document_digest(document),
                "count": len(document.get("findings", [])),
                "counts": counts,
            },
            None,
        )

    def _reuse(self, step, entry, step_dir, record):
        """Restore a verified checkpoint into ``step_dir``; False if it still fails validation."""
        self.cache.restore(entry, step_dir)
        artifacts = list(entry.artifacts)
        findings = [item for item in artifacts if item.get("role") == FINDINGS_ROLE]
        if len(findings) != 1:
            record["cache"]["reason"] = "the entry does not hold exactly one findings artifact"
            return False
        summary, problem = self._read_findings(os.path.join(step_dir, findings[0]["path"]))
        if problem:
            record["cache"]["reason"] = "the cached findings document no longer validates"
            return False
        summary["path"] = findings[0]["path"]
        record["status"] = "reused"
        record["artifacts"] = artifacts
        record["findings"] = summary
        record["result"] = entry.result
        record["cache"]["path"] = entry.path
        self.reporter.info(
            "step {!r}: reusing checkpoint {}".format(step.id, entry.key[:12])
        )
        return True

    def _write_diagnostic(self, record, step_dir, document, message):
        path = os.path.join(step_dir, "diagnostic.json")
        findings_validate.validate_document(document)
        findings_serialize.write_document(document, path)
        record["diagnostic"] = {
            "path": _relative(path, self.output_dir),
            "message": message,
            "digest": findings_serialize.document_digest(document),
        }

    # -- manifest -----------------------------------------------------------

    def _build_manifest(self, records, started_at, finished_at, duration, failed):
        counts = {status: 0 for status in manifest_module.STEP_STATUSES}
        for record in records:
            counts[record["status"]] = counts.get(record["status"], 0) + 1
        document = {
            "manifest_version": manifest_module.MANIFEST_VERSION,
            "generated_at": finished_at,
            "producer": {
                "name": "hal_runner",
                "version": __version__,
                "command": self.command or [sys.executable, "-m", "hal_runner"],
            },
            "run": {
                "id": self.run_id,
                "name": self.config.name,
                "description": self.config.description,
                "status": "failure" if failed else "success",
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_s": round(float(duration), 3),
            },
            "tool": self.tool,
            "environment": manifest_module.environment_info(),
            "output_dir": self.output_dir,
            "inputs": self.inputs,
            "configuration": self.config.as_json(),
            "configuration_digest": json_digest(self.config.as_json()),
            "steps": records,
            "outcome": {
                "status": "failure" if failed else "success",
                "steps_total": len(records),
                "counts": counts,
            },
        }
        if self.config.path and os.path.isfile(self.config.path):
            document["configuration_file"] = {
                "path": self.config.path,
                "sha256": sha256_file(self.config.path),
            }
        return document
