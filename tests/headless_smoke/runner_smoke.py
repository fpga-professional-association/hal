#!/usr/bin/env python3
"""End-to-end check of ``tools/hal_runner`` against a real, shipped example netlist.

``tools/hal_runner/test_hal_runner.py`` drives the orchestrator with a stub
executor, so it proves the bookkeeping and none of the analysis: no HAL is
started, no netlist is loaded, no plugin runs.  This script closes that gap the
same way ``real_netlist_smoke.py`` does for hal_viz -- it needs a *built* HAL
and it asserts results, not exit codes:

  1. run the documented command on ``examples/uart.zip`` and check that it
     exits 0 and writes a manifest
  2. check the manifest: the input pinned by the archive's real ``sha256``, the
     HAL version read from the binary, both steps successful, every artifact
     hashed
  3. check the findings: the strongly connected components of the UART netlist
     graph are the three known feedback loops (68/50/15 gates), the graph has
     407 vertices and 1604 edges, and the weak decomposition is one block of
     407 -- the same numbers ``real_netlist_smoke.py`` pins, arrived at through
     the runner instead of by hand
  4. run it a second time: both steps must be *reused*, and the reused findings
     must be byte-identical to the computed ones
  5. corrupt the cached artifact and run again: the checkpoint must be rejected
     and the step re-run, never served stale
  6. run a step with a 1 ms limit: the run must exit non-zero, the step must be
     recorded as a timeout, its logs must survive, and its diagnostic record
     must be a valid findings document with status ``timeout``

Nothing is skipped when something is missing. A missing ``hal`` binary, a
missing plugin or a wrong answer is a failure with an actionable message.

Run it against a build tree with::

    HAL_BASE_PATH=<build> python3 tests/headless_smoke/runner_smoke.py \\
        --hal-binary <build>/bin/hal --work-dir <build>/runner_smoke --keep
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
EXAMPLE_ARCHIVE = REPO_ROOT / "examples" / "uart.zip"
EXAMPLE_CONFIG = TOOLS / "hal_runner" / "examples" / "uart_components.json"

sys.path.insert(0, str(TOOLS))

from hal_findings import serialize as findings_serialize  # noqa: E402
from hal_findings import validate as findings_validate  # noqa: E402
from hal_runner import checkpoint, hashing, manifest as manifest_module  # noqa: E402

# ---------------------------------------------------------------------------
# Properties of examples/uart.zip, identical to the ones tests/headless_smoke/
# real_netlist_smoke.py pins. Reaching them through hal_runner is the point: if
# these change, either the example was replaced or the runner is not running the
# analysis it says it is.
# ---------------------------------------------------------------------------

EXPECTED_VERTICES = 407
EXPECTED_EDGES = 1604
EXPECTED_STRONG_SIZES = [68, 50, 15]
EXPECTED_WEAK_SIZES = [407]


class SmokeError(RuntimeError):
    """A check failed, or a prerequisite for one is missing."""


def require(condition, message):
    if not condition:
        raise SmokeError(message)


class Report(object):
    """Prints each check as it happens so a CI log shows where a run stopped."""

    def __init__(self):
        self.passed = 0

    def step(self, message):
        print("\n== {}".format(message), flush=True)

    def ok(self, message):
        self.passed += 1
        print("   ok: {}".format(message), flush=True)

    def note(self, message):
        print("   -- {}".format(message), flush=True)


# ---------------------------------------------------------------------------
# driving the runner
# ---------------------------------------------------------------------------


def run_runner(arguments, hal_binary, report, expect_exit=0):
    """Run ``python -m hal_runner`` as a subprocess and check its exit code."""
    command = [sys.executable, str(TOOLS / "hal_runner")] + list(arguments)
    if hal_binary:
        command += ["--hal-binary", str(hal_binary)]
    report.note("$ {}".format(" ".join(command)))
    completed = subprocess.run(
        command, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    require(
        completed.returncode == expect_exit,
        "hal_runner exited with {}, expected {}\n--- stdout ---\n{}\n--- stderr ---\n{}".format(
            completed.returncode, expect_exit, stdout, stderr
        ),
    )
    return stdout, stderr


def write_config(path, output_dir, timeout_s=None):
    """A copy of the shipped example, retargeted at the work directory."""
    with open(str(EXAMPLE_CONFIG), "r", encoding="utf-8") as handle:
        document = json.load(handle)
    document["netlist"] = str(EXAMPLE_ARCHIVE)
    document["output_dir"] = str(output_dir)
    if timeout_s is not None:
        for step in document["steps"]:
            step["timeout_s"] = timeout_s
    with open(str(path), "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
    return path


def load_manifest(output_dir):
    path = Path(output_dir) / "manifest.json"
    require(path.is_file(), "hal_runner did not write {}".format(path))
    return manifest_module.read(str(path))


def step_record(manifest, step_id):
    for step in manifest.get("steps", []):
        if step["id"] == step_id:
            return step
    raise SmokeError(
        "the manifest has no step {!r}; it has {}".format(
            step_id, [step["id"] for step in manifest.get("steps", [])]
        )
    )


def findings_of(output_dir, step_id):
    path = Path(output_dir) / "steps" / step_id / "findings.json"
    require(path.is_file(), "step {!r} produced no findings document at {}".format(step_id, path))
    document = findings_serialize.read_document(str(path))
    errors = findings_validate.collect_errors(document)
    require(not errors, "the findings of step {!r} do not validate:\n  {}".format(step_id, "\n  ".join(errors)))
    return document


def finding_by_id(document, finding_id):
    for finding in document.get("findings", []):
        if finding["id"] == finding_id:
            return finding
    raise SmokeError(
        "no finding {!r} in the document; it has {}".format(
            finding_id, [finding["id"] for finding in document.get("findings", [])]
        )
    )


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def check_manifest(manifest, report, expected_status="success"):
    report.step("checking the run manifest")
    require(
        manifest["run"]["status"] == expected_status,
        "the run reports status {!r}, expected {!r}".format(
            manifest["run"]["status"], expected_status
        ),
    )
    report.ok("run status {!r}".format(manifest["run"]["status"]))

    netlist_input = manifest["inputs"][0]
    expected_hash = hashing.sha256_file(str(EXAMPLE_ARCHIVE))
    require(
        netlist_input.get("sha256") == expected_hash,
        "the manifest pins the netlist as {}, expected the archive's sha256 {}".format(
            netlist_input.get("sha256"), expected_hash
        ),
    )
    report.ok("input pinned by sha256 {}".format(expected_hash[:16]))

    hal = manifest["tool"]["hal"]
    require(
        hal["source"] == "hal --version",
        "the manifest recorded the HAL version from {!r}; with a real binary it must "
        "come from 'hal --version'".format(hal["source"]),
    )
    require(hal["version"] and hal["version"] != "unknown", "no HAL version was recorded")
    require(hal.get("binary_sha256"), "the hal binary itself was not hashed")
    report.ok("hal {} (from {})".format(hal["version"], hal["source"]))

    require(
        len(manifest_module.digest(manifest)) == 64,
        "the manifest has no reproducible digest",
    )
    report.ok("manifest digest {}".format(manifest_module.digest(manifest)[:16]))


def check_step_artifacts(manifest, output_dir, step_id, report):
    record = step_record(manifest, step_id)
    require(
        record["artifacts"],
        "step {!r} recorded no artifacts".format(step_id),
    )
    for artifact in record["artifacts"]:
        path = Path(output_dir) / "steps" / step_id / artifact["path"]
        require(path.is_file(), "step {!r} artifact {} is missing".format(step_id, path))
        actual = hashing.sha256_file(str(path))
        require(
            actual == artifact["sha256"],
            "step {!r} artifact {} hashes to {}, the manifest says {}".format(
                step_id, artifact["path"], actual, artifact["sha256"]
            ),
        )
    report.ok(
        "step {!r}: {} artifact(s), every hash matches the manifest".format(
            step_id, len(record["artifacts"])
        )
    )
    return record


def check_feedback_loops(output_dir, report):
    report.step("checking the strongly connected components of the UART netlist graph")
    document = findings_of(output_dir, "feedback-loops")

    summary = finding_by_id(document, "graph_algorithm/graph/summary")
    require(
        summary["metrics"]["vertices"] == EXPECTED_VERTICES,
        "the netlist graph has {} vertices, expected {}".format(
            summary["metrics"]["vertices"], EXPECTED_VERTICES
        ),
    )
    require(
        summary["metrics"]["edges"] == EXPECTED_EDGES,
        "the netlist graph has {} edges, expected {}".format(
            summary["metrics"]["edges"], EXPECTED_EDGES
        ),
    )
    report.ok("netlist graph: {} vertices, {} edges".format(EXPECTED_VERTICES, EXPECTED_EDGES))

    sizes = summary["data"]["component_sizes"]
    require(
        sizes == EXPECTED_STRONG_SIZES,
        "the strongly connected components of size >= 2 are {}, expected {}. These are "
        "the sequential feedback loops of the design; a different answer means the "
        "analysis changed, not just the plumbing.".format(sizes, EXPECTED_STRONG_SIZES),
    )
    report.ok("feedback loops: {}".format(sizes))

    components = [
        finding
        for finding in document["findings"]
        if finding["id"].startswith("graph_algorithm/component/")
    ]
    require(
        len(components) == len(EXPECTED_STRONG_SIZES),
        "expected {} component findings, got {}".format(len(EXPECTED_STRONG_SIZES), len(components)),
    )
    for finding, expected_size in zip(
        sorted(components, key=lambda entry: entry["id"]), EXPECTED_STRONG_SIZES
    ):
        require(
            finding["status"] == "proven_under_assumptions",
            "component finding {} has status {!r}; a graph-theoretic component is proved "
            "under stated assumptions".format(finding["id"], finding["status"]),
        )
        require(
            finding["bounds"]["unbounded"] is True,
            "component finding {} is not marked unbounded".format(finding["id"]),
        )
        require(
            len(finding.get("assumptions", [])) >= 2,
            "component finding {} does not state the assumptions its claim rests "
            "on".format(finding["id"]),
        )
        require(
            finding["metrics"]["component_size"] == expected_size,
            "component finding {} has size {}, expected {}".format(
                finding["id"], finding["metrics"]["component_size"], expected_size
            ),
        )
        require(
            len(finding["scope"]["gates"]) == expected_size,
            "component finding {} lists {} gates for a component of {}".format(
                finding["id"], len(finding["scope"]["gates"]), expected_size
            ),
        )
        for gate in finding["scope"]["gates"]:
            require(
                gate["artifact_id"] == "netlist",
                "a gate reference is not scoped to the declared artifact",
            )
    report.ok("{} component findings, each proved under stated assumptions".format(len(components)))

    artifact = document["artifacts"][0]
    require(
        artifact.get("sha256") == hashing.sha256_file(str(EXAMPLE_ARCHIVE)),
        "the findings document pins the input as {}, expected the archive's sha256".format(
            artifact.get("sha256")
        ),
    )
    report.ok("the findings document pins the same input as the manifest")


def check_connectivity(output_dir, report):
    report.step("checking the weakly connected components")
    document = findings_of(output_dir, "connectivity")
    summary = finding_by_id(document, "graph_algorithm/graph/summary")
    require(
        summary["data"]["component_sizes"] == EXPECTED_WEAK_SIZES,
        "the weakly connected component sizes are {}, expected {}".format(
            summary["data"]["component_sizes"], EXPECTED_WEAK_SIZES
        ),
    )
    report.ok("one weakly connected block of {} gates".format(EXPECTED_WEAK_SIZES[0]))


def check_reuse(config_path, output_dir, hal_binary, first_digests, report):
    report.step("running the same configuration again: every step must be reused")
    run_runner(["run", str(config_path), "-q"], hal_binary, report)
    manifest = load_manifest(output_dir)
    for step_id in ("feedback-loops", "connectivity"):
        record = step_record(manifest, step_id)
        require(
            record["status"] == "reused",
            "step {!r} was recomputed ({}) instead of reusing its checkpoint; reason: "
            "{}".format(step_id, record["status"], record.get("cache", {}).get("reason")),
        )
        document = findings_of(output_dir, step_id)
        require(
            findings_serialize.document_digest(document) == first_digests[step_id],
            "the reused findings of step {!r} differ from the computed ones".format(step_id),
        )
    report.ok("both steps reused, with identical findings digests")
    return manifest


def check_stale_cache_rejection(config_path, output_dir, hal_binary, manifest, report):
    report.step("corrupting a cached artifact: the checkpoint must be rejected")
    key = step_record(manifest, "feedback-loops")["cache"]["key"]
    cached = Path(output_dir) / "cache" / key / checkpoint.ARTIFACT_DIR / "findings.json"
    require(cached.is_file(), "no cached findings artifact at {}".format(cached))
    with open(str(cached), "a", encoding="utf-8") as handle:
        handle.write("\n")

    run_runner(["run", str(config_path), "-q"], hal_binary, report)
    rerun = load_manifest(output_dir)
    record = step_record(rerun, "feedback-loops")
    require(
        record["status"] == "success",
        "a corrupted checkpoint was served as {!r}; it must be rejected and the step "
        "re-run".format(record["status"]),
    )
    require(
        "sha256" in (record.get("cache", {}).get("reason") or ""),
        "the manifest does not say why the checkpoint was rejected (reason: {!r})".format(
            record.get("cache", {}).get("reason")
        ),
    )
    report.ok("the corrupted checkpoint was rejected: {}".format(record["cache"]["reason"]))

    # And the freshly computed result is the same one, so the rejection cost time, not truth.
    check_feedback_loops(output_dir, report)


def check_timeout(work_dir, hal_binary, report):
    report.step("running a step with a 1 ms limit: it must be killed and recorded")
    output_dir = Path(work_dir) / "timeout"
    config_path = write_config(Path(work_dir) / "timeout.json", output_dir, timeout_s=0.001)
    run_runner(["run", str(config_path), "-q"], hal_binary, report, expect_exit=1)

    manifest = load_manifest(output_dir)
    require(
        manifest["run"]["status"] == "failure",
        "a timed-out step left the run reporting {!r}".format(manifest["run"]["status"]),
    )
    record = step_record(manifest, "feedback-loops")
    require(
        record["status"] == "timeout",
        "the step is recorded as {!r}, expected 'timeout'".format(record["status"]),
    )
    require(
        step_record(manifest, "connectivity")["status"] == "skipped",
        "the step after a failure must be recorded as skipped",
    )
    report.ok("run failed, step recorded as a timeout, the following step as skipped")

    for name in ("stdout.log", "stderr.log"):
        path = output_dir / "steps" / "feedback-loops" / "logs" / name
        require(path.is_file(), "the retained log {} is missing".format(path))
    report.ok("the step's logs were retained")

    diagnostic_path = output_dir / "steps" / "feedback-loops" / "diagnostic.json"
    require(diagnostic_path.is_file(), "no diagnostic record at {}".format(diagnostic_path))
    diagnostic = findings_serialize.read_document(str(diagnostic_path))
    errors = findings_validate.collect_errors(diagnostic)
    require(not errors, "the diagnostic record does not validate:\n  {}".format("\n  ".join(errors)))
    finding = diagnostic["findings"][0]
    require(
        finding["status"] == "timeout",
        "the diagnostic finding has status {!r}, expected 'timeout'".format(finding["status"]),
    )
    require(
        finding["limits"]["timeout_s"] == 0.001 and finding["limits"]["hit"] is True,
        "the diagnostic finding does not record the limit it hit: {}".format(finding["limits"]),
    )
    report.ok("the diagnostic record is a valid findings document with status 'timeout'")


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def resolve_hal_binary(explicit):
    if explicit:
        path = Path(explicit).resolve()
        require(path.is_file(), "--hal-binary does not exist: {}".format(explicit))
        return path
    base = os.environ.get("HAL_BASE_PATH")
    if base:
        candidate = Path(base) / "bin" / "hal"
        if candidate.is_file():
            return candidate.resolve()
    found = shutil.which("hal")
    require(
        found,
        "no 'hal' binary found. Pass --hal-binary <build>/bin/hal or set HAL_BASE_PATH; "
        "this test needs a built HAL and does not skip without one.",
    )
    return Path(found).resolve()


def run(args, report):
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    report.step("locating the inputs")
    require(EXAMPLE_ARCHIVE.is_file(), "the example archive {} is missing".format(EXAMPLE_ARCHIVE))
    require(EXAMPLE_CONFIG.is_file(), "the example run configuration {} is missing".format(EXAMPLE_CONFIG))
    hal_binary = resolve_hal_binary(args.hal_binary)
    report.ok("hal binary {}".format(hal_binary))

    output_dir = work_dir / "uart-baseline"
    if output_dir.exists():
        shutil.rmtree(str(output_dir))
    config_path = write_config(work_dir / "uart_components.json", output_dir)

    report.step("running the documented command")
    stdout, _ = run_runner(["run", str(config_path)], hal_binary, report)
    printed = [line.strip() for line in stdout.splitlines() if line.strip()]
    require(
        printed == [str(output_dir / "manifest.json")],
        "hal_runner should print exactly the manifest path, printed {}".format(printed),
    )
    report.ok("wrote {}".format(printed[0]))

    manifest = load_manifest(output_dir)
    check_manifest(manifest, report)
    for step_id in ("feedback-loops", "connectivity"):
        record = check_step_artifacts(manifest, output_dir, step_id, report)
        require(
            record["status"] == "success",
            "step {!r} is recorded as {!r}".format(step_id, record["status"]),
        )

    check_feedback_loops(output_dir, report)
    check_connectivity(output_dir, report)

    digests = {
        step_id: findings_serialize.document_digest(findings_of(output_dir, step_id))
        for step_id in ("feedback-loops", "connectivity")
    }
    reused = check_reuse(config_path, output_dir, hal_binary, digests, report)
    check_stale_cache_rejection(config_path, output_dir, hal_binary, reused, report)
    check_timeout(work_dir, hal_binary, report)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Headless end-to-end test of tools/hal_runner: run the shipped "
        "example configuration against examples/uart.zip through a built HAL and check "
        "the manifest, the findings, checkpoint reuse, stale-cache rejection and "
        "timeout handling.",
    )
    parser.add_argument(
        "--hal-binary",
        metavar="PATH",
        help="the hal executable (default: $HAL_BASE_PATH/bin/hal, then 'hal' on PATH)",
    )
    parser.add_argument(
        "--work-dir",
        metavar="DIR",
        help="where to write runs and outputs (default: a temporary directory)",
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the work directory even when the run succeeds"
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))

    temporary = None
    if not args.work_dir:
        temporary = tempfile.mkdtemp(prefix="hal_runner_smoke_")
        args.work_dir = temporary

    report = Report()
    try:
        run(args, report)
    except SmokeError as exc:
        print("\nFAILED: {}".format(exc), file=sys.stderr)
        print("work directory kept at {}".format(args.work_dir), file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - an unexpected failure is still a failure
        import traceback

        traceback.print_exc()
        print("\nFAILED: unexpected exception, see the traceback above", file=sys.stderr)
        print("work directory kept at {}".format(args.work_dir), file=sys.stderr)
        return 1

    print("\n{} checks passed".format(report.passed))
    if temporary and not args.keep:
        shutil.rmtree(temporary, ignore_errors=True)
    else:
        print("work directory kept at {}".format(args.work_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
