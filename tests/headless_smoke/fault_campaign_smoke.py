#!/usr/bin/env python3
"""End-to-end check of ``tools/hal_fault_campaign`` against a built HAL.

``tools/hal_fault_campaign/test_hal_fault_campaign.py`` drives the orchestrator
with a stub executor and the classifier with hand-written traces, so it proves
the bookkeeping and none of the simulation: no HAL is started, no netlist is
instrumented, no simulator runs.  This script closes that gap, the same way
``runner_smoke.py`` does for ``hal_runner`` -- it needs a *built* HAL with the
simulator plugins and it asserts results, not exit codes:

  1. run the shipped short-window campaign over
     ``tools/hal_fault_campaign/fixtures/parity_counter.v`` and check it exits 0
     and writes a manifest
  2. check that HAL's simulation of the fixture reproduces the traces of the
     independent reference model **exactly**, cycle by cycle -- baseline first,
     then every injected fault.  This is the check that makes the rest mean
     something: if HAL and the model disagree, one of them is wrong and the run
     stops instead of trusting whichever produced the manifest
  3. check the verdicts against ``fixtures/ground_truth.json``: the four
     parity-protected registers detected with latency 0, the last two pipeline
     stages externally observable and undetected, and the toggle plus the first
     two pipeline stages unobserved in the one-cycle window
  4. check the findings document: it validates, the silent divergences are the
     only refutations, no finding claims an unbounded proof, and the coverage
     and limitations are on the summary
  5. ``recheck`` the manifest against the traces it shipped -- no HAL involved
  6. ``replay`` one fault of each class from the manifest and require the
     classification and both latencies to come back identical, with
     ``--verify-enumeration`` re-deriving the fault list from the recorded seed
  7. run the long-window campaign (seeded random sample) and check that the same
     registers are re-classified: unobserved in a one-cycle window, externally
     observable in a four-cycle one.  That contrast is the reason this tool
     never reports "unobserved" as "masked"
  8. replay against a *modified* netlist and require the tool to refuse

Nothing is skipped when something is missing.  A missing ``hal`` binary, a
missing simulator plugin or a wrong answer is a failure with an actionable
message, because a smoke test that quietly turns itself off is worse than none.

Run it against a build tree with::

    HAL_BASE_PATH=<build> python3 tests/headless_smoke/fault_campaign_smoke.py \\
        --hal-binary <build>/bin/hal --work-dir <build>/fault_campaign_smoke --keep
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
CAMPAIGN_DIR = TOOLS / "hal_fault_campaign"
FIXTURES = CAMPAIGN_DIR / "fixtures"

sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(FIXTURES))

from hal_findings import serialize as findings_serialize  # noqa: E402
from hal_findings import validate as findings_validate  # noqa: E402
from hal_findings.model import is_unbounded_proof  # noqa: E402

import reference_model  # noqa: E402
from hal_fault_campaign import faultmodel, manifest as manifest_module  # noqa: E402

SHORT_CONFIG = FIXTURES / "campaign_short_window.json"
LONG_CONFIG = FIXTURES / "campaign_long_window.json"

#: One fault of each class, to replay.
REPLAY_FAULTS = ("CNT_reg_0_inst@4", "PIPE_reg_3_inst@4", "PIPE_reg_0_inst@4")


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
# driving the tool
# ---------------------------------------------------------------------------


def run_campaign_tool(arguments, report, expect_exit=0, hal_binary=None):
    command = [sys.executable, str(CAMPAIGN_DIR)] + [str(part) for part in arguments]
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
        "hal_fault_campaign exited with {}, expected {}\n--- stdout ---\n{}\n"
        "--- stderr ---\n{}".format(completed.returncode, expect_exit, stdout, stderr),
    )
    return stdout, stderr


def write_config(source, path, output_dir, netlist=None):
    """A copy of a shipped configuration, retargeted at the work directory."""
    with open(str(source), "r", encoding="utf-8") as handle:
        document = json.load(handle)
    document["netlist"] = str(netlist or (FIXTURES / "parity_counter.v"))
    document["gate_library"] = str(
        REPO_ROOT / "plugins" / "gate_libraries" / "definitions" / "example_library.hgl"
    )
    document["output_dir"] = str(output_dir)
    with open(str(path), "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
    return path


def load_manifest(output_dir):
    path = Path(output_dir) / "manifest.json"
    require(path.is_file(), "the campaign did not write {}".format(path))
    return manifest_module.read(str(path))


def load_traces(output_dir):
    path = Path(output_dir) / "traces.json"
    require(path.is_file(), "the campaign did not write {}".format(path))
    with open(str(path), "r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def check_manifest(manifest, report, expected_faults):
    report.step("checking the campaign manifest")
    require(
        manifest["campaign"]["status"] == "success",
        "the campaign reports status {!r}: {}".format(
            manifest["campaign"]["status"], manifest.get("diagnostics")
        ),
    )
    report.ok("campaign status success")

    require(
        manifest["engine"] == "hal_simulator",
        "the campaign ran with engine {!r}, expected 'hal_simulator' (HAL's built-in "
        "event-driven engine, which needs no external tool)".format(manifest["engine"]),
    )
    report.ok("engine hal_simulator")

    require(
        len(manifest["faults"]) == expected_faults,
        "the manifest holds {} fault(s), expected {}".format(
            len(manifest["faults"]), expected_faults
        ),
    )
    report.ok("{} fault(s) recorded".format(expected_faults))

    sites = sorted(site["gate_name"] for site in manifest["sites"])
    require(
        sites == sorted(reference_model.REGISTERS),
        "the campaign instrumented {}, expected the nine fixture registers {}".format(
            sites, sorted(reference_model.REGISTERS)
        ),
    )
    report.ok("all nine registers instrumented")

    injector = manifest["instrumentation"].get("injector_gate_type")
    require(
        injector == "XOR",
        "the injector used gate type {!r}; example_library.hgl provides XOR".format(
            injector
        ),
    )
    report.ok("injector gate type XOR")


def check_traces_match_the_model(manifest, traces, report):
    report.step("checking HAL's simulation against the independent reference model")

    expected_baseline = reference_model.simulate(
        reference_model.STIMULUS, reference_model.WORKLOAD_CYCLES
    )
    for signal, expected in sorted(expected_baseline.items()):
        got = traces["baseline"].get(signal)
        require(
            got is not None,
            "HAL recorded no baseline trace for {!r}".format(signal),
        )
        require(
            got == expected,
            "the fault-free baseline of {!r} differs from the reference model:\n"
            "  model: {}\n  hal  : {}".format(
                signal, reference_model.encode(expected), reference_model.encode(got)
            ),
        )
    report.ok("fault-free baseline matches the model on all {} signal(s)".format(
        len(expected_baseline)))

    for record in manifest["faults"]:
        expected = reference_model.simulate(
            reference_model.STIMULUS,
            reference_model.WORKLOAD_CYCLES,
            (record["site"], record["cycle"], record["hold_cycles"]),
        )
        got = traces["faults"].get(record["id"])
        require(got is not None, "HAL recorded no trace for fault {}".format(record["id"]))
        for signal, series in sorted(expected.items()):
            require(
                got[signal] == series,
                "fault {} ({} at cycle {}): HAL and the reference model disagree on "
                "{!r}\n  model: {}\n  hal  : {}".format(
                    record["id"], record["site"], record["cycle"], signal,
                    reference_model.encode(series),
                    reference_model.encode(got[signal]),
                ),
            )
    report.ok(
        "all {} faulty simulations match the model cycle by cycle".format(
            len(manifest["faults"])
        )
    )


def check_verdicts(manifest, report, window_name):
    report.step("checking the verdicts against fixtures/ground_truth.json")
    truth = reference_model.load_ground_truth()["expected"][window_name]["verdicts"]
    for record in manifest["faults"]:
        key = "{}@{}".format(record["site"], record["cycle"])
        expected = truth[key]
        for field in ("classification", "detection_latency_cycles",
                      "divergence_latency_cycles"):
            require(
                record.get(field) == expected[field],
                "{}: {} is {!r}, the ground truth says {!r}".format(
                    key, field, record.get(field), expected[field]
                ),
            )
    report.ok("{} verdict(s) match the ground truth".format(len(manifest["faults"])))

    counts = manifest["summary"]["counts"]
    if window_name == "short_window":
        require(
            counts[faultmodel.CLASS_DETECTED] == 16,
            "expected 16 detected faults (4 protected registers x 4 cycles), got "
            "{}".format(counts[faultmodel.CLASS_DETECTED]),
        )
        require(
            counts[faultmodel.CLASS_SILENT] == 8,
            "expected 8 externally observable, undetected faults, got {}".format(
                counts[faultmodel.CLASS_SILENT]
            ),
        )
        require(
            counts[faultmodel.CLASS_UNOBSERVED] == 12,
            "expected 12 faults unobserved in the one-cycle window, got {}".format(
                counts[faultmodel.CLASS_UNOBSERVED]
            ),
        )
        require(
            counts[faultmodel.CLASS_INDETERMINATE] == 0,
            "the fixture is fully reset-initialised; an indeterminate result means some "
            "signal was X: {}".format(counts),
        )
        report.ok("class counts 16 detected / 8 silent / 12 unobserved / 0 indeterminate")

    latency = manifest["summary"]["detection_latency_cycles"]
    require(
        latency and latency["max"] == 0,
        "the parity check is combinational, so every detection latency must be 0; got "
        "{}".format(latency),
    )
    report.ok("detection latency 0 cycle(s) for every detected fault")


def check_findings(output_dir, report):
    report.step("checking the findings document")
    path = Path(output_dir) / "findings.json"
    require(path.is_file(), "the campaign wrote no findings document at {}".format(path))
    document = findings_serialize.read_document(str(path))
    errors = findings_validate.collect_errors(document)
    require(not errors, "the findings do not validate:\n  {}".format("\n  ".join(errors)))
    report.ok("findings validate against the {} schema".format(document["schema_version"]))

    for finding in document["findings"]:
        require(
            not is_unbounded_proof(finding),
            "finding {!r} claims an unbounded proof; every result here is bounded by the "
            "simulated cycle count".format(finding["id"]),
        )
    report.ok("no finding claims an unbounded proof")

    refutations = [
        finding for finding in document["findings"]
        if finding["status"] in ("counterexample", "bounded_counterexample")
    ]
    require(refutations, "no silent divergence was reported as a refutation")
    for finding in refutations:
        require(
            finding["status"] == "bounded_counterexample",
            "finding {!r} is an unbounded counterexample; a simulation cannot produce "
            "one".format(finding["id"]),
        )
        require(
            finding["data"]["classification"] == faultmodel.CLASS_SILENT,
            "finding {!r} is a refutation but is classified {!r}".format(
                finding["id"], finding["data"]["classification"]
            ),
        )
        require(finding["counterexample"]["witness"],
                "finding {!r} refutes without a witness".format(finding["id"]))
    report.ok("{} refutation(s), all bounded and all with a witness".format(len(refutations)))

    summary = next(
        finding for finding in document["findings"]
        if finding["id"] == "fault-campaign/summary"
    )
    require(
        summary["data"]["limitations"],
        "the campaign summary carries no limitations",
    )
    require(
        "not a failure rate" in summary["summary"],
        "the campaign summary does not disclaim a failure rate: {}".format(
            summary["summary"]
        ),
    )
    report.ok("campaign summary reports coverage {:.0%} and its limitations".format(
        summary["data"]["coverage_fraction"]))

    for finding in document["findings"]:
        if finding.get("data", {}).get("classification") == faultmodel.CLASS_UNOBSERVED:
            require(
                "NOT shown to be masked" in finding["summary"],
                "the unobserved finding {!r} does not say that unobserved is not "
                "masked".format(finding["id"]),
            )
    report.ok("every unobserved finding refuses to claim masking")
    return document


def check_recheck(output_dir, report):
    report.step("re-deriving every verdict from the recorded traces (no HAL)")
    stdout, _ = run_campaign_tool(
        ["recheck", str(Path(output_dir) / "manifest.json"), "--verify-enumeration"],
        report,
    )
    require(
        "reproduced exactly" in stdout,
        "recheck did not report an exact reproduction:\n{}".format(stdout),
    )
    require(
        "enumeration re-derived" in stdout,
        "recheck did not re-derive the enumeration from the recorded seed:\n{}".format(
            stdout
        ),
    )
    report.ok("recheck reproduced every verdict from the manifest's own evidence")


def check_replay(output_dir, hal_binary, report):
    report.step("replaying one fault of each class from the manifest")
    arguments = ["replay", str(Path(output_dir) / "manifest.json"),
                 "--verify-enumeration"]
    for name in REPLAY_FAULTS:
        arguments += ["--fault", name]
    stdout, _ = run_campaign_tool(arguments, report, hal_binary=hal_binary)
    require(
        "reproduced exactly" in stdout,
        "the replay did not reproduce the recorded verdicts:\n{}".format(stdout),
    )
    report.ok("{} replayed fault(s) reproduced classification and latency".format(
        len(REPLAY_FAULTS)))

    replay_manifest = load_manifest(Path(output_dir) / "replay")
    require(
        len(replay_manifest["faults"]) == len(REPLAY_FAULTS),
        "the replay injected {} fault(s), expected {}".format(
            len(replay_manifest["faults"]), len(REPLAY_FAULTS)
        ),
    )
    original = {record["id"]: record for record in load_manifest(output_dir)["faults"]}
    for record in replay_manifest["faults"]:
        expected = original[record["id"]]
        require(
            record["classification"] == expected["classification"],
            "replayed fault {} changed class from {!r} to {!r}".format(
                record["id"], expected["classification"], record["classification"]
            ),
        )
    report.ok("the replay manifest agrees with the original fault by fault")


def check_replay_refuses_a_changed_netlist(output_dir, hal_binary, report, work_dir):
    report.step("replaying against a modified netlist must be refused")
    modified = Path(work_dir) / "modified_parity_counter.v"
    with open(str(FIXTURES / "parity_counter.v"), "r", encoding="utf-8") as handle:
        text = handle.read()
    with open(str(modified), "w", encoding="utf-8") as handle:
        handle.write(text + "\n// a comment is enough to change the content hash\n")

    manifest_path = Path(output_dir) / "manifest.json"
    with open(str(manifest_path), "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    manifest["config_source"]["netlist"] = str(modified)
    tampered_dir = Path(work_dir) / "tampered"
    tampered_dir.mkdir(parents=True, exist_ok=True)
    with open(str(tampered_dir / "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)

    _, stderr = run_campaign_tool(
        ["replay", str(tampered_dir / "manifest.json")],
        report,
        expect_exit=2,
        hal_binary=hal_binary,
    )
    require(
        "changed" in stderr and "refusing" in stderr,
        "the replay did not refuse a netlist that no longer matches the manifest:\n"
        "{}".format(stderr),
    )
    report.ok("a replay against different bytes is refused, not silently accepted")


def check_window_reclassification(short_manifest, long_manifest, report):
    report.step("the same faults, a longer window, a different class")
    short = {
        "{}@{}".format(record["site"], record["cycle"]): record
        for record in short_manifest["faults"]
    }
    changed = 0
    for record in long_manifest["faults"]:
        key = "{}@{}".format(record["site"], record["cycle"])
        before = short.get(key)
        if before is None:
            continue
        if before["classification"] == faultmodel.CLASS_UNOBSERVED:
            require(
                record["classification"] == faultmodel.CLASS_SILENT,
                "{} is unobserved in the one-cycle window but {!r} rather than a silent "
                "divergence in the four-cycle one".format(key, record["classification"]),
            )
            changed += 1
    require(
        changed > 0,
        "no fault changed class between the two windows; the fixture is supposed to "
        "demonstrate exactly that",
    )
    report.ok(
        "{} fault(s) unobserved in a one-cycle window are externally observable in a "
        "four-cycle one -- unobserved is a property of the window".format(changed)
    )


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


def run(args, report):
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    report.step("running the short-window campaign on the parity_counter fixture")
    short_out = work_dir / "short-window"
    short_config = write_config(SHORT_CONFIG, work_dir / "short.json", short_out)
    run_campaign_tool(["run", str(short_config)], report, hal_binary=args.hal_binary)

    short_manifest = load_manifest(short_out)
    check_manifest(short_manifest, report, expected_faults=36)
    check_traces_match_the_model(short_manifest, load_traces(short_out), report)
    check_verdicts(short_manifest, report, "short_window")
    check_findings(short_out, report)
    check_recheck(short_out, report)
    check_replay(short_out, args.hal_binary, report)
    check_replay_refuses_a_changed_netlist(short_out, args.hal_binary, report, work_dir)

    report.step("running the long-window campaign (seeded random sample)")
    long_out = work_dir / "long-window"
    long_config = write_config(LONG_CONFIG, work_dir / "long.json", long_out)
    run_campaign_tool(["run", str(long_config)], report, hal_binary=args.hal_binary)
    long_manifest = load_manifest(long_out)
    check_manifest(long_manifest, report, expected_faults=12)
    check_traces_match_the_model(long_manifest, load_traces(long_out), report)
    check_verdicts(long_manifest, report, "long_window")
    check_window_reclassification(short_manifest, long_manifest, report)

    report.step("the sampled campaign must not be reported as a proof")
    document = findings_serialize.read_document(str(Path(long_out) / "findings.json"))
    summary = next(
        finding for finding in document["findings"]
        if finding["id"] == "fault-campaign/summary"
    )
    require(
        summary["status"] == "heuristic",
        "a seeded random sample must be reported as heuristic, not {!r}".format(
            summary["status"]
        ),
    )
    require(
        summary["data"]["sampling"]["seed"] == 20260907,
        "the summary does not record the seed the sample came from",
    )
    report.ok("sampled campaign summary is heuristic and records its seed")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Headless end-to-end test of tools/hal_fault_campaign: run the "
        "shipped fault-injection campaigns over the parity_counter fixture through a "
        "built HAL and check the traces, the verdicts, the findings, the replay and the "
        "window-dependence of 'unobserved'.",
    )
    parser.add_argument(
        "--hal-binary",
        metavar="PATH",
        help="the hal executable (default: $HAL_BASE_PATH/bin/hal, then 'hal' on PATH)",
    )
    parser.add_argument(
        "--work-dir",
        metavar="DIR",
        help="where to write campaigns and outputs (default: a temporary directory)",
    )
    parser.add_argument(
        "--keep", action="store_true",
        help="keep the work directory even when the run succeeds",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))

    temporary = None
    if not args.work_dir:
        temporary = tempfile.mkdtemp(prefix="hal_fault_campaign_smoke_")
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
