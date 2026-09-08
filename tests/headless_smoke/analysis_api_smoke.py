#!/usr/bin/env python3
"""End-to-end check of ``tools/hal_analysis_api`` against real netlists.

``tools/hal_analysis_api/test_hal_analysis_api.py`` drives the API with a stub
netlist and a stub executor, so it proves the bounds, the schemas and the error
vocabulary and *nothing about HAL*: no netlist is parsed, no ``hal`` process is
started, no binding is exercised.  This script closes that gap the way
``runner_smoke.py`` does for hal_runner -- it needs a **built HAL** and it
asserts answers, not exit codes:

  1. ``hal.capabilities`` finds the hal binary and advertises every tool
  2. the hand-crafted fixture (``tools/hal_analysis_api/fixtures/cone_fixture.v``
     against ``example_library.hgl``) parses, and every cone recorded in
     ``cone_fixture.ground_truth.json`` comes back from HAL exactly as the
     pure-Python query layer produces it -- the two independent paths to the
     same hand-derived answer
  3. ``examples/uart.zip`` opens as a content-pinned handle, and
     ``netlist.summary`` reports the same 407/409 gate and net counts, and the
     same gate-type histogram, that ``real_netlist_smoke.py`` pins
  4. a filtered, paginated listing walks all 258 flip-flops without repeating
     one, and ``page.total`` describes the filter rather than the netlist
  5. an unknown gate id comes back as ``unknown_object`` -- across the process
     boundary, with its data intact -- and never as an empty answer
  6. a cone with a small budget is truncated *explicitly*, with the limit that
     cut it
  7. a query given a millisecond limit is killed and reported as ``timeout``
     carrying that limit
  8. a real analysis is submitted through hal_runner, waited for, and its
     findings come back schema-validated, with the artifact they are evidence
     about pinned to the same sha256 the project handle was opened with -- the
     acceptance criterion "an agent inspects a scoped circuit and receives a
     finding with evidence", end to end
  9. artifacts are listed and fetched under a byte budget, and a path that
     tries to leave the job directory is refused

Nothing is skipped when something is missing: a missing ``hal`` binary, a
missing plugin or a wrong answer is a failure with an actionable message.

Run it against a build tree with::

    HAL_BASE_PATH=<build> python3 tests/headless_smoke/analysis_api_smoke.py \\
        --hal-binary <build>/bin/hal --work-dir <build>/analysis_api_smoke --keep
"""

import argparse
import hashlib
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
FIXTURE_DIR = TOOLS / "hal_analysis_api" / "fixtures"
FIXTURE_NETLIST = FIXTURE_DIR / "cone_fixture.v"
FIXTURE_GROUND_TRUTH = FIXTURE_DIR / "cone_fixture.ground_truth.json"
GATE_LIBRARY = REPO_ROOT / "plugins" / "gate_libraries" / "definitions" / "example_library.hgl"

sys.path.insert(0, str(TOOLS))

from hal_analysis_api import limits, schemas  # noqa: E402
from hal_analysis_api.api import AnalysisApi  # noqa: E402

# ---------------------------------------------------------------------------
# Properties of examples/uart.zip, the same ones real_netlist_smoke.py and
# runner_smoke.py pin. Reaching them through the analysis API is the point.
# ---------------------------------------------------------------------------

EXPECTED_DESIGN_NAME = "test_ext_uart"
EXPECTED_GATE_COUNT = 407
EXPECTED_NET_COUNT = 409
EXPECTED_FFR_COUNT = 258
EXPECTED_GATE_TYPES = {
    "FFR": 258,
    "LUT4": 73,
    "LUT6": 20,
    "LUT5": 16,
    "LUT3": 15,
    "LUT2": 14,
    "LUT1": 5,
    "BUF": 4,
    "GND": 1,
    "VCC": 1,
}
#: The UART's three feedback loops, as connected_components reports them.
EXPECTED_STRONG_SIZES = [68, 50, 15]


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


def call(api, tool, request, report, expect_ok=True):
    """One API call, with the envelope checked against the schema either way."""
    envelope = api.call(tool, request)
    require(
        not schemas.envelope_errors(envelope),
        "the envelope of {} does not validate:\n{}\n{}".format(
            tool, "\n".join(schemas.envelope_errors(envelope)), json.dumps(envelope)[:2000]
        ),
    )
    if expect_ok:
        require(
            envelope["ok"],
            "{} failed: {}".format(tool, json.dumps(envelope.get("error"), indent=2)),
        )
        require(
            not schemas.response_errors(tool, envelope["result"]),
            "the result of {} does not match its schema:\n{}".format(
                tool, "\n".join(schemas.response_errors(tool, envelope["result"]))
            ),
        )
        return envelope["result"]
    require(
        not envelope["ok"],
        "{} was expected to fail but returned {}".format(tool, json.dumps(envelope)[:800]),
    )
    return envelope["error"]


def sha256_file(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def check_capabilities(api, report):
    report.step("hal.capabilities")
    result = call(api, "hal.capabilities", {}, report)
    require(
        result["hal"]["available"],
        "no hal binary found: {}. Pass --hal-binary <build>/bin/hal or set "
        "$HAL_BASE_PATH.".format(result["hal"]["reason"]),
    )
    report.ok("hal binary: {}".format(result["hal"]["binary"]))
    require(
        len(result["tools"]) == len(schemas.TOOLS),
        "capabilities advertises {} tools, the registry has {}".format(
            len(result["tools"]), len(schemas.TOOLS)
        ),
    )
    names = [entry["name"] for entry in result["analyses"]]
    require(
        "graph_algorithm.connected_components" in names,
        "the component analysis is not advertised; hal_runner registry: {}".format(names),
    )
    report.ok("{} tools, {} analyses".format(len(result["tools"]), len(result["analyses"])))
    return result


def check_fixture_cones(api, report):
    """The hand-derived ground truth, this time through HAL."""
    report.step("hand-crafted fixture: cones against the recorded ground truth")
    with open(str(FIXTURE_GROUND_TRUTH), "r", encoding="utf-8") as handle:
        ground_truth = json.load(handle)

    opened = call(
        api,
        "project.open",
        {"path": str(FIXTURE_NETLIST), "gate_library": str(GATE_LIBRARY)},
        report,
    )
    project = opened["project"]
    report.ok("{} -> {}".format(FIXTURE_NETLIST.name, project))

    summary = call(api, "netlist.summary", {"project": project}, report)
    expected = ground_truth["expected"]
    require(
        summary["counts"]["gates"] == expected["counts"]["gates"],
        "the fixture parsed to {} gates, the ground truth says {}".format(
            summary["counts"]["gates"], expected["counts"]["gates"]
        ),
    )
    histogram = {entry["type"]: entry["count"] for entry in summary["gate_types"]}
    require(
        histogram == expected["gate_types"],
        "the fixture's gate types are {}, expected {}".format(histogram, expected["gate_types"]),
    )
    report.ok(
        "{} gates, {} nets, types {}".format(
            summary["counts"]["gates"], summary["counts"]["nets"], histogram
        )
    )

    # Names -> ids, the same way an agent has to: ids are HAL's, not the fixture's.
    ids = {}
    offset = 0
    while True:
        page = call(
            api, "netlist.gates", {"project": project, "offset": offset, "limit": 50}, report
        )
        for entry in page["gates"]:
            ids[entry["name"]] = entry["id"]
        if not page["page"]["has_more"]:
            break
        offset = page["page"]["next_offset"]
    missing = [entry["name"] for entry in ground_truth["gates"] if entry["name"] not in ids]
    require(not missing, "the fixture is missing gates {}".format(missing))
    report.ok("resolved {} gate names to ids".format(len(ids)))

    for case in expected["cones"]:
        result = call(
            api,
            "netlist.cone",
            {
                "project": project,
                "seed_gate_ids": [ids[name] for name in case["seeds"]],
                "direction": case["direction"],
                "depth": case["depth"],
                "max_gates": case["max_gates"],
            },
            report,
        )
        names = sorted(entry["name"] for entry in result["gates"])
        if "gates" in case:
            require(
                names == sorted(case["gates"]),
                "cone {}: HAL returned {}, the ground truth says {}".format(
                    case["id"], names, sorted(case["gates"])
                ),
            )
        if "gate_count" in case:
            require(
                len(names) == case["gate_count"],
                "cone {}: {} gates, expected {}".format(case["id"], len(names), case["gate_count"]),
            )
        for name in case.get("gates_contain", []):
            require(name in names, "cone {}: {} is missing from {}".format(case["id"], name, names))
        require(
            result["truncation"]["truncated"] == case["truncated"]
            and result["truncation"]["kind"] == case["truncation_kind"],
            "cone {}: truncation {} does not match the ground truth ({}, {})".format(
                case["id"], result["truncation"], case["truncated"], case["truncation_kind"]
            ),
        )
        report.ok("cone {}: {}".format(case["id"], ", ".join(names)))

    # The same queries against the same fixture, in process, with no HAL: the
    # unit tests' stub netlist is only worth something if HAL agrees with it.
    edges = call(
        api,
        "netlist.cone",
        {
            "project": project,
            "seed_gate_ids": [ids["out_buf"]],
            "direction": "fan_in",
            "depth": 6,
        },
        report,
    )
    by_id = {entry["id"]: entry["name"] for entry in edges["gates"]}
    seen = sorted(
        (by_id[edge["from_gate_id"]], by_id[edge["to_gate_id"]], edge["net_name"])
        for edge in edges["edges"]
    )
    require(
        seen == sorted(tuple(entry) for entry in expected["gate_edges"]),
        "the fixture's gate-level edges are {}, expected {}".format(
            seen, expected["gate_edges"]
        ),
    )
    report.ok("{} gate-level edges match the ground truth".format(len(seen)))
    call(api, "project.close", {"project": project}, report)


def check_uart_reads(api, report):
    report.step("examples/uart.zip: handle, summary, listings, cones")
    opened = call(api, "project.open", {"path": str(EXAMPLE_ARCHIVE)}, report)
    project = opened["project"]
    digest = sha256_file(EXAMPLE_ARCHIVE)
    require(
        opened["info"]["digest"]["value"] == digest,
        "the handle pinned {} but the archive hashes to {}".format(
            opened["info"]["digest"]["value"], digest
        ),
    )
    report.ok("{} pinned as sha256 {}".format(project, digest[:16]))

    summary = call(api, "netlist.summary", {"project": project}, report)
    require(
        summary["design_name"] == EXPECTED_DESIGN_NAME,
        "design name is {!r}, expected {!r}".format(summary["design_name"], EXPECTED_DESIGN_NAME),
    )
    require(
        summary["counts"]["gates"] == EXPECTED_GATE_COUNT
        and summary["counts"]["nets"] == EXPECTED_NET_COUNT,
        "uart.zip reports {} gates / {} nets, expected {} / {}".format(
            summary["counts"]["gates"],
            summary["counts"]["nets"],
            EXPECTED_GATE_COUNT,
            EXPECTED_NET_COUNT,
        ),
    )
    histogram = {entry["type"]: entry["count"] for entry in summary["gate_types"]}
    require(
        histogram == EXPECTED_GATE_TYPES,
        "the gate-type histogram is {}, expected {}".format(histogram, EXPECTED_GATE_TYPES),
    )
    report.ok(
        "{}: {} gates, {} nets, {} gate types".format(
            summary["design_name"],
            summary["counts"]["gates"],
            summary["counts"]["nets"],
            len(histogram),
        )
    )

    # 4 -- a filtered, paginated walk
    seen = []
    offset = 0
    while True:
        page = call(
            api,
            "netlist.gates",
            {"project": project, "gate_type": "FFR", "offset": offset, "limit": 100},
            report,
        )
        require(
            page["page"]["total"] == EXPECTED_FFR_COUNT,
            "the filtered total is {}, expected {}".format(
                page["page"]["total"], EXPECTED_FFR_COUNT
            ),
        )
        seen.extend(entry["id"] for entry in page["gates"])
        if not page["page"]["has_more"]:
            require(
                page["page"]["next_offset"] is None,
                "a last page must not offer a next_offset",
            )
            break
        offset = page["page"]["next_offset"]
    require(
        len(seen) == EXPECTED_FFR_COUNT and len(set(seen)) == EXPECTED_FFR_COUNT,
        "walking the pages produced {} ids ({} unique), expected {}".format(
            len(seen), len(set(seen)), EXPECTED_FFR_COUNT
        ),
    )
    report.ok("paginated through {} FFR gates without a repeat".format(len(seen)))

    # 5 -- an unknown id is an error, not an empty answer
    error = call(
        api, "netlist.gate", {"project": project, "gate_id": 999999}, report, expect_ok=False
    )
    require(
        error["code"] == "unknown_object",
        "an unknown gate id returned {!r}, expected unknown_object".format(error["code"]),
    )
    require(
        error["data"]["id"] == 999999,
        "the unknown_object error lost its data: {}".format(error),
    )
    report.ok("unknown gate id -> {} ({})".format(error["code"], error["message"]))

    # 6 -- a bounded cone, and a truncated one
    seed = seen[0]
    cone = call(
        api,
        "netlist.cone",
        {
            "project": project,
            "seed_gate_ids": [seed],
            "direction": "fan_in",
            "depth": 2,
            "max_gates": 200,
        },
        report,
    )
    require(
        seed in [entry["id"] for entry in cone["gates"]],
        "the cone does not contain its own seed",
    )
    report.ok(
        "fan-in cone of gate {}: {} gates, {} edges".format(
            seed, cone["counts"]["gates"], cone["counts"]["edges"]
        )
    )

    cut = call(
        api,
        "netlist.cone",
        {
            "project": project,
            "seed_gate_ids": [seed],
            "direction": "both",
            "depth": 8,
            "max_gates": 5,
        },
        report,
    )
    require(
        cut["truncation"]["truncated"] and cut["truncation"]["kind"] == "gates",
        "a cone cut by its budget must say so: {}".format(cut["truncation"]),
    )
    require(
        cut["counts"]["gates"] == 5,
        "the truncated cone has {} gates, the budget was 5".format(cut["counts"]["gates"]),
    )
    require(cut["counts"]["frontier"] > 0, "a truncated cone must report its frontier")
    report.ok("budgeted cone: {}".format(cut["truncation"]["reason"]))
    return project


def check_query_timeout(api, project, report):
    """A limit that actually bites, at the transport level.

    The API's own schema will not accept ``timeout_s`` below one second, so this
    goes through the query runner directly -- the point is that the *mechanism*
    kills a HAL process and reports it as ``timeout``, not that a caller can ask
    for a millisecond.
    """
    report.step("a query given a 1 ms limit is killed and reported as a timeout")
    from hal_analysis_api.errors import Timeout

    handle = api.projects.get(project)
    try:
        api.queries.run(
            "netlist.summary",
            handle,
            {"max_gate_types": limits.DEFAULT_GATE_TYPES},
            timeout_s=0.001,
        )
    except Timeout as exc:
        require(
            exc.data.get("timeout_s") == 0.001,
            "the timeout error lost its limit: {}".format(exc.data),
        )
        report.ok("timeout: {}".format(exc.message))
        return
    raise SmokeError("a 1 ms limit did not stop the query; the limit is not enforced")


def check_analysis(api, project, report, wait_s):
    report.step("analysis.submit -> status -> findings.get, through hal_runner")
    submitted = call(
        api,
        "analysis.submit",
        {
            "project": project,
            "analysis": "graph_algorithm.connected_components",
            "config": {"strong": True, "min_size": 2},
            "timeout_s": 900,
            "label": "uart feedback loops",
        },
        report,
    )
    job = submitted["job"]
    report.ok("submitted {} ({})".format(job, submitted["status"]["state"]))

    waited = 0.0
    status = submitted["status"]
    while waited < wait_s and not status["terminal"]:
        step = min(30.0, wait_s - waited)
        status = call(api, "analysis.status", {"job": job, "wait_s": step}, report)["status"]
        waited += step
        if not status["terminal"]:
            report.note("still {} after {:.0f}s".format(status["state"], waited))
    require(
        status["terminal"],
        "the analysis did not finish within {}s (state {})".format(wait_s, status["state"]),
    )
    require(
        status["state"] == "succeeded",
        "the analysis ended as {}: {}".format(status["state"], json.dumps(status.get("error"))),
    )
    report.ok("job {} in {}s".format(status["state"], status["duration_s"]))

    findings = call(api, "findings.get", {"job": job, "limit": 50}, report)
    require(findings["source"] == "findings", "a successful job must return findings")
    require(findings["validated"], "the findings document was not validated")

    sizes = []
    with_evidence = 0
    for finding in findings["findings"]:
        if finding.get("metrics", {}).get("component_size"):
            sizes.append(finding["metrics"]["component_size"])
        if finding.get("scope", {}).get("gates"):
            with_evidence += 1
    require(
        sorted(sizes, reverse=True) == EXPECTED_STRONG_SIZES,
        "the UART's feedback loops are {}, expected {}".format(
            sorted(sizes, reverse=True), EXPECTED_STRONG_SIZES
        ),
    )
    require(with_evidence >= 1, "no finding carries gate-level evidence")
    report.ok(
        "{} findings, feedback loops {}, {} with gate-level evidence".format(
            findings["page"]["total"], sorted(sizes, reverse=True), with_evidence
        )
    )

    # The evidence has to be about the artifact the handle pinned, or it is
    # evidence about nothing in particular.
    digest = sha256_file(EXAMPLE_ARCHIVE)
    pinned = [
        artifact
        for artifact in findings["artifacts"]
        if artifact.get("sha256") == digest
    ]
    require(
        pinned,
        "no finding artifact carries the sha256 the project handle was opened with "
        "({}); artifacts: {}".format(digest[:16], findings["artifacts"]),
    )
    scoped = [
        finding
        for finding in findings["findings"]
        if pinned[0]["artifact_id"] in finding.get("scope", {}).get("artifact_ids", [])
    ]
    require(scoped, "no finding is scoped to the pinned artifact")
    report.ok(
        "evidence bound to artifact {!r}, pinned as the archive's own sha256".format(
            pinned[0]["artifact_id"]
        )
    )

    # A page of findings is a page, not the document.
    first = call(api, "findings.get", {"job": job, "limit": 1}, report)
    require(
        first["page"]["total"] == findings["page"]["total"] and len(first["findings"]) == 1,
        "findings pagination is inconsistent: {}".format(first["page"]),
    )
    report.ok("findings paginate: 1 of {}".format(first["page"]["total"]))
    return job


def check_artifacts(api, job, report):
    report.step("artifact.list / artifact.get")
    listing = call(api, "artifact.list", {"job": job}, report)
    paths = [entry["path"] for entry in listing["artifacts"]]
    for expected in ("run_config.json", "run/manifest.json", "run/steps/analysis/findings.json"):
        require(expected in paths, "{} is missing from {}".format(expected, paths))
    report.ok("{} artifacts: {}".format(len(paths), ", ".join(paths[:5])))

    fetched = call(
        api,
        "artifact.get",
        {"job": job, "path": "run/manifest.json", "max_bytes": 512},
        report,
    )
    require(
        fetched["truncation"]["truncated"] and fetched["returned_bytes"] == 512,
        "a 512-byte budget on the manifest must truncate: {}".format(fetched["truncation"]),
    )
    require(
        fetched["size_bytes"] > fetched["returned_bytes"],
        "the truncated artifact does not report its full size",
    )
    report.ok(
        "manifest: {} of {} bytes, sha256 {}".format(
            fetched["returned_bytes"], fetched["size_bytes"], fetched["sha256"][:16]
        )
    )

    error = call(
        api, "artifact.get", {"job": job, "path": "../../../etc/passwd"}, report, expect_ok=False
    )
    require(
        error["code"] == "invalid_request",
        "a path escaping the job directory returned {!r}".format(error["code"]),
    )
    report.ok("path traversal refused: {}".format(error["message"]))


def check_cli(work_dir, hal_binary, report):
    """The same API through the documented command line, as an agent's fallback."""
    report.step("python tools/hal_analysis_api call ...")
    command = [
        sys.executable,
        str(TOOLS / "hal_analysis_api"),
        "--workspace",
        str(work_dir / "cli_workspace"),
    ]
    if hal_binary:
        command += ["--hal-binary", str(hal_binary)]
    completed = subprocess.run(
        command + ["call", "project.open", "--arg", "path={}".format(EXAMPLE_ARCHIVE)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = completed.stdout.decode("utf-8", "replace")
    require(
        completed.returncode == 0,
        "the CLI exited with {}:\n{}\n{}".format(
            completed.returncode, stdout, completed.stderr.decode("utf-8", "replace")
        ),
    )
    envelope = json.loads(stdout)
    require(envelope["ok"], "the CLI returned {}".format(stdout[:500]))
    project = envelope["result"]["project"]

    completed = subprocess.run(
        command + ["call", "netlist.summary", "--arg", "project={}".format(project)],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = completed.stdout.decode("utf-8", "replace")
    require(
        completed.returncode == 0,
        "the CLI summary exited with {}:\n{}".format(completed.returncode, stdout),
    )
    result = json.loads(stdout)["result"]
    require(
        result["counts"]["gates"] == EXPECTED_GATE_COUNT,
        "the CLI reported {} gates".format(result["counts"]["gates"]),
    )
    report.ok("CLI: {} gates through {}".format(result["counts"]["gates"], project))

    # An error envelope is exit code 1, and still a JSON document.
    completed = subprocess.run(
        command + ["call", "netlist.gate", "--arg", "project={}".format(project),
                   "--arg", "gate_id=999999"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    require(
        completed.returncode == 1,
        "an error envelope must exit 1, got {}".format(completed.returncode),
    )
    envelope = json.loads(completed.stdout.decode("utf-8", "replace"))
    require(envelope["error"]["code"] == "unknown_object", "unexpected: {}".format(envelope))
    report.ok("CLI error path: exit 1, code {}".format(envelope["error"]["code"]))


def check_example(work_dir, hal_binary, report):
    report.step("examples/scoped_investigation.py")
    command = [
        sys.executable,
        str(TOOLS / "hal_analysis_api" / "examples" / "scoped_investigation.py"),
        "--workspace",
        str(work_dir / "example_workspace"),
    ]
    if hal_binary:
        command += ["--hal-binary", str(hal_binary)]
    completed = subprocess.run(
        command, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout = completed.stdout.decode("utf-8", "replace")
    require(
        completed.returncode == 0,
        "the example workflow exited with {}:\n{}\n{}".format(
            completed.returncode, stdout[-4000:], completed.stderr.decode("utf-8", "replace")[-2000:]
        ),
    )
    require(
        "evidence:" in stdout,
        "the example did not report a finding with evidence:\n{}".format(stdout[-2000:]),
    )
    report.ok("the documented agent workflow ran end to end")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hal-binary", help="path to the hal executable")
    parser.add_argument("--work-dir", help="where to put the workspace (default: a temp dir)")
    parser.add_argument("--keep", action="store_true", help="keep the work directory")
    parser.add_argument(
        "--wait",
        type=float,
        default=900.0,
        help="how long to wait for the analysis job (default 900s)",
    )
    parser.add_argument("--skip-example", action="store_true")
    args = parser.parse_args(argv)

    require(EXAMPLE_ARCHIVE.is_file(), "missing {}".format(EXAMPLE_ARCHIVE))
    require(FIXTURE_NETLIST.is_file(), "missing {}".format(FIXTURE_NETLIST))
    require(GATE_LIBRARY.is_file(), "missing {}".format(GATE_LIBRARY))

    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="hal_api_"))
    work_dir.mkdir(parents=True, exist_ok=True)
    report = Report()
    print("work directory: {}".format(work_dir), flush=True)

    api = AnalysisApi(workspace=str(work_dir / "workspace"), hal_binary=args.hal_binary)
    try:
        check_capabilities(api, report)
        check_fixture_cones(api, report)
        project = check_uart_reads(api, report)
        check_query_timeout(api, project, report)
        job = check_analysis(api, project, report, args.wait)
        check_artifacts(api, job, report)
        check_cli(work_dir, args.hal_binary, report)
        if not args.skip_example:
            check_example(work_dir, args.hal_binary, report)
    except SmokeError as exc:
        print("\nFAILED: {}".format(exc), file=sys.stderr, flush=True)
        print("the work directory is kept at {}".format(work_dir), file=sys.stderr)
        return 1
    finally:
        sys.stdout.flush()

    print("\n{} checks passed.".format(report.passed), flush=True)
    if not args.keep and not args.work_dir:
        shutil.rmtree(str(work_dir), ignore_errors=True)
    else:
        print("work directory kept at {}".format(work_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
