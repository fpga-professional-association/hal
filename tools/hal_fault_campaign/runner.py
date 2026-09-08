"""The host side: build a request, run HAL once, check what came back.

This module never imports ``hal_py``.  It writes a request, executes

    hal --python-script tools/hal_fault_campaign/steps/campaign_runner.py

with :class:`hal_runner.execute.ProcessExecutor` (so a campaign inherits the
same wall-clock kill, process-group teardown and retained logs the analysis
runner has), and then reads files back.  That is also why the whole orchestrator
can be unit tested with a stub executor and no HAL build in sight.

The whole campaign is **one** subprocess rather than one per fault: loading the
netlist and instrumenting it is the expensive part and it is identical for every
injection, while the process boundary is there for cancellation and blast
radius, which one campaign-sized process still provides.

Nothing is trusted.  A campaign has failed if HAL exits nonzero, if it is killed
at its limit, if it exits 0 but writes no result, if it declares an artifact it
did not write, or if its findings document does not validate.  Every one of
those produces a manifest -- a failed run needs a record more than a successful
one does.
"""

import os
import time

from hal_findings import serialize as findings_serialize
from hal_findings import validate as findings_validate
from hal_findings.adapters.common import utc_now
from hal_runner.execute import ProcessExecutor, tail
from hal_runner.hashing import describe_input, sha256_file
from hal_runner.manifest import tool_info
from hal_runner.runner import resolve_hal_binary

from . import MANIFEST_VERSION, __version__
from . import manifest as manifest_module
from . import protocol

__all__ = [
    "CampaignError",
    "CampaignRunner",
    "CAMPAIGN_RUNNER_SCRIPT",
    "TOOLS_PATH",
    "resolve_hal_binary",
]

#: The script HAL is pointed at. A plain file on purpose: ``--python-script``
#: takes a path to a ``.py`` file, not a module.
CAMPAIGN_RUNNER_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "steps", "campaign_runner.py"
)

TOOLS_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TOOLS_PATH)


class CampaignError(RuntimeError):
    """The campaign could not be started at all (bad input, no hal binary)."""


class CampaignRunner(object):
    """Runs one campaign configuration and writes its manifest."""

    def __init__(self, config, hal_binary=None, executor=None, reporter=None,
                 keep_simulation_dirs=False, verify_instrumentation=True):
        self.config = config
        self.executor = executor or ProcessExecutor()
        self.reporter = reporter
        self.keep_simulation_dirs = keep_simulation_dirs
        self.verify_instrumentation = verify_instrumentation
        self.hal_binary = resolve_hal_binary(hal_binary, config.hal_binary)
        self.tool = tool_info(self.hal_binary, REPO_ROOT)
        self.tool["hal_fault_campaign"] = {
            "version": __version__,
            "manifest_version": MANIFEST_VERSION,
            "request_version": protocol.REQUEST_VERSION,
        }

    # -- plumbing ---------------------------------------------------------

    def _say(self, message):
        if self.reporter is not None:
            self.reporter(message)

    def inputs(self):
        """Pin every input by content. Raises when one is missing."""
        entries = []
        try:
            entries.append(describe_input(self.config.netlist, role="netlist"))
            if self.config.gate_library:
                entries.append(
                    describe_input(self.config.gate_library, role="gate_library")
                )
        except FileNotFoundError as exc:
            raise CampaignError(str(exc))
        return entries

    def pin_for(self, inputs):
        """The artifact pin the step stamps onto the findings document."""
        netlist = inputs[0]
        pin = {
            "path": os.path.basename(self.config.netlist),
            "kind": "hal_project" if netlist["kind"] == "directory" else "netlist",
        }
        if netlist["kind"] == "file":
            pin["sha256"] = netlist["digest"]
            pin["size_bytes"] = netlist["size_bytes"]
        else:
            pin["unhashed_reason"] = (
                "the input is a HAL project directory; it is pinned in the campaign "
                "manifest by a {} digest, which is not a plain file sha256".format(
                    netlist["digest_algorithm"]
                )
            )
        for entry in inputs[1:]:
            if entry["role"] == "gate_library" and entry.get("sha256"):
                pin["gate_library_sha256"] = entry["sha256"]
        return pin

    def request(self, inputs, faults=None, enumeration=None):
        return {
            "request_version": protocol.REQUEST_VERSION,
            "campaign": self.config.name,
            "tools_path": TOOLS_PATH,
            "output_dir": self.config.output_dir,
            "result_file": "result.json",
            "findings_file": "findings.json",
            "traces_file": "traces.json",
            "netlist": self.config.netlist,
            "gate_library": self.config.gate_library,
            "artifact_id": "netlist",
            "pin": self.pin_for(inputs),
            "config": self.config.document,
            "engine": self.config.engine,
            "hal_version": self.tool["hal"]["version"],
            "verify_instrumentation": bool(self.verify_instrumentation),
            "keep_simulation_dirs": bool(self.keep_simulation_dirs),
            "engine_timeout_s": self.config.timeout_s,
            "faults": faults,
            "enumeration": enumeration,
        }

    def command(self):
        return [self.hal_binary] + self.config.hal_args + [
            "--python-script",
            CAMPAIGN_RUNNER_SCRIPT,
        ]

    def plan(self):
        """What :meth:`run` would do, without doing any of it."""
        return {
            "campaign": self.config.name,
            "netlist": self.config.netlist,
            "output_dir": self.config.output_dir,
            "engine": self.config.engine,
            "command": self.command(),
            "hal": self.tool["hal"],
            "cycles": self.config.cycles,
            "window": self.config.window,
            "sampling": self.config.sampling,
        }

    # -- the run ----------------------------------------------------------

    def run(self, faults=None, enumeration=None):
        """Execute the campaign. Returns ``(exit_code, manifest)``."""
        started_at = utc_now()
        started = time.time()
        output_dir = self.config.output_dir
        os.makedirs(output_dir, exist_ok=True)
        logs_dir = os.path.join(output_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)

        inputs = self.inputs()
        request = self.request(inputs, faults=faults, enumeration=enumeration)
        request_path = protocol.write_json(request, os.path.join(output_dir, "request.json"))

        environment = dict(os.environ)
        environment[protocol.REQUEST_ENV] = request_path

        stdout_path = os.path.join(logs_dir, "stdout.log")
        stderr_path = os.path.join(logs_dir, "stderr.log")

        self._say(
            "running {} injection campaign step: {}".format(
                self.config.name, " ".join(self.command())
            )
        )
        execution = self.executor.execute(
            self.command(),
            cwd=output_dir,
            env=environment,
            timeout_s=self.config.timeout_s,
            memory_mb=self.config.memory_mb,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

        diagnostics = []
        result = None
        result_path = os.path.join(output_dir, "result.json")
        if os.path.isfile(result_path):
            try:
                result = protocol.read_result(result_path)
            except protocol.ProtocolError as exc:
                diagnostics.append({"kind": "protocol", "message": str(exc)})

        status = "success"
        if execution.timed_out:
            status = "timeout"
            diagnostics.append(
                {
                    "kind": "timeout",
                    "message": "hal was killed after {}s".format(self.config.timeout_s),
                    "timeout_s": self.config.timeout_s,
                    "stderr_tail": tail(stderr_path),
                }
            )
        elif execution.exit_code != 0:
            status = "failure"
            diagnostics.append(
                {
                    "kind": "exit_code",
                    "message": "hal exited with {}".format(execution.exit_code),
                    "stderr_tail": tail(stderr_path),
                }
            )
        elif result is None:
            status = "failure"
            diagnostics.append(
                {
                    "kind": "missing_result",
                    "message": (
                        "hal exited 0 but wrote no result.json; a campaign that does not "
                        "say what it did has not proved that it did anything"
                    ),
                    "stderr_tail": tail(stderr_path),
                }
            )
        elif result.get("status") != "ok":
            status = "failure"
            diagnostics.append(
                {
                    "kind": "step_error",
                    "message": (result.get("error") or {}).get("message", "unknown error"),
                    "error": result.get("error"),
                }
            )

        artifacts = []
        if result is not None:
            for declared in result.get("artifacts", []):
                path = os.path.join(output_dir, declared["path"])
                if not os.path.isfile(path):
                    status = "failure"
                    diagnostics.append(
                        {
                            "kind": "missing_artifact",
                            "message": "the campaign declared {!r} but did not write "
                            "it".format(declared["path"]),
                        }
                    )
                    continue
                artifacts.append(
                    {
                        "path": declared["path"],
                        "role": declared.get("role", "artifact"),
                        "sha256": sha256_file(path),
                        "size_bytes": os.path.getsize(path),
                    }
                )

        findings_status_counts = None
        findings_path = os.path.join(output_dir, "findings.json")
        if status == "success" and os.path.isfile(findings_path):
            try:
                document = findings_serialize.read_document(findings_path)
                findings_validate.validate_document(document)
                findings_status_counts = _status_counts(document)
            except (OSError, ValueError) as exc:
                status = "failure"
                diagnostics.append(
                    {
                        "kind": "invalid_findings",
                        "message": "the findings document does not validate: {}".format(exc),
                    }
                )

        finished_at = utc_now()
        duration = time.time() - started

        manifest = manifest_module.build(
            self.config,
            inputs,
            self.tool,
            (result or {}).get("enumeration") or (enumeration or {}),
            (result or {}).get("sites") or [],
            (result or {}).get("faults") or [],
            None,
            (result or {}).get("summary") or {},
            status,
            (result or {}).get("engine") or self.config.engine,
            instrumentation=(result or {}).get("instrumentation"),
            skipped=((result or {}).get("instrumentation") or {}).get("skipped", []),
            artifacts=artifacts,
            logs={
                "stdout": os.path.relpath(stdout_path, output_dir).replace(os.sep, "/"),
                "stderr": os.path.relpath(stderr_path, output_dir).replace(os.sep, "/"),
            },
            exit_code=execution.exit_code,
            started_at=started_at,
            finished_at=finished_at,
            duration_s=duration,
            diagnostics=diagnostics,
            output_dir=output_dir,
        )
        if findings_status_counts is not None:
            manifest["findings"] = {
                "path": "findings.json",
                "status_counts": findings_status_counts,
                "digest": findings_serialize.document_digest(
                    findings_serialize.read_document(findings_path)
                ),
            }
        manifest["execution"] = {
            "exit_code": execution.exit_code,
            "timed_out": bool(execution.timed_out),
            "duration_s": round(execution.duration_s, 3),
            "memory_limit_enforced": bool(execution.memory_limit_enforced),
            "timeout_s": self.config.timeout_s,
            "memory_mb": self.config.memory_mb,
        }

        manifest_path = manifest_module.write(
            manifest, os.path.join(output_dir, "manifest.json")
        )
        self._say("manifest: {}".format(manifest_path))
        return (0 if status == "success" else 1), manifest


def _status_counts(document):
    counts = {}
    for finding in document.get("findings", []):
        counts[finding["status"]] = counts.get(finding["status"], 0) + 1
    return counts
