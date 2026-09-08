#!/usr/bin/env python3
"""End-to-end check of ``tools/hal_secprop`` against a built HAL.

``tools/hal_secprop/test_hal_secprop.py`` runs the whole analysis on an offline
model of the fixtures, so it proves the policy language, the property
catalogue, the solver plumbing and the witness format -- and none of it against
a netlist HAL actually parsed. This script closes that gap the way
``fault_campaign_smoke.py`` does for ``hal_fault_campaign``: it needs a *built*
HAL and it asserts results, not exit codes.

  1. load all three fixtures through ``hal_py`` and require the ``hal_py`` and
     the offline transition systems to be **equivalent by a miter** -- for every
     register, the two next-state functions are asserted to differ and the
     solver must answer ``unsat``. If HAL and the offline reader disagree, one
     of them is wrong and the run stops instead of trusting whichever produced
     the report
  2. ``check`` the correct fixture: exit 0, every obligation ``proven_bounded``,
     none of them vacuous, and no finding claiming an unbounded proof
  3. ``check`` the faulty fixture: exit 1, both defects from
     ``fixtures/ground_truth.json`` reported as ``bounded_counterexample`` with
     a witness, and the exported transaction sequence really contains the
     lock write followed by the debug write that gets through
  4. ``replay`` every exported witness through ``hal_py`` -- against the netlist
     as HAL read it, not against the offline model
  5. ``replay`` against a *modified* netlist and require the tool to refuse
  6. ``check`` the latch fixture: no obligation checked, every finding
     ``unsupported``, ``DLH_X1`` named
  7. force a solver budget of zero and require ``timeout``, and ``--strict``
     to turn it into a non-zero exit -- an exhausted search must never look like
     a pass
  8. run the whole thing again through ``hal --python-script`` with
     ``scripts/check_in_hal.py`` so the verdict becomes HAL's exit code
  9. structural cones: the correct and the faulty fixture must produce the
     *same* cone, and every cone finding must be ``heuristic``. That is the
     acceptance criterion "graph connectivity is only a candidate path"

Nothing is skipped when something is missing. A missing ``hal_py``, a missing
``hal`` binary or a wrong answer is a failure with an actionable message,
because a smoke test that quietly turns itself off is worse than none.

Run it against a build tree with::

    HAL_BASE_PATH=<build> PYTHONPATH=<build>/lib \\
        python3 tests/headless_smoke/secprop_smoke.py \\
        --hal-binary <build>/bin/hal --work-dir <build>/secprop_smoke --keep
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
SECPROP_DIR = TOOLS / "hal_secprop"
FIXTURES = SECPROP_DIR / "fixtures"
LIBRARY = REPO_ROOT / "plugins" / "gate_libraries" / "definitions" / "NangateOpenCellLibrary.hgl"

sys.path.insert(0, str(TOOLS))

from hal_apb_check import expr, sat  # noqa: E402
from hal_findings import serialize as findings_serialize  # noqa: E402
from hal_findings import validate as findings_validate  # noqa: E402
from hal_findings.model import is_unbounded_proof  # noqa: E402

from hal_secprop import cones as cones_module  # noqa: E402
from hal_secprop import halsource, policy as policy_module, transitions  # noqa: E402
from hal_secprop.errors import UnsupportedPrimitives  # noqa: E402

VARIANTS = ("ok", "faulty", "blackbox")


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


def policy_path(variant):
    return FIXTURES / "secreg_{}.policy.json".format(variant)


def netlist_path(variant):
    return FIXTURES / "secreg_{}.v".format(variant)


def ground_truth():
    with open(str(FIXTURES / "ground_truth.json"), "r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# driving the tool
# ---------------------------------------------------------------------------


def run_tool(arguments, report, expect_exit=0):
    command = [sys.executable, str(SECPROP_DIR)] + [str(part) for part in arguments]
    report.note("$ {}".format(" ".join(command)))
    completed = subprocess.run(
        command, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    require(
        completed.returncode == expect_exit,
        "hal_secprop exited with {}, expected {}\n--- stdout ---\n{}\n--- stderr ---\n"
        "{}".format(completed.returncode, expect_exit, stdout, stderr),
    )
    return stdout, stderr


def load_document(path, report):
    require(Path(path).is_file(), "no findings document at {}".format(path))
    document = findings_serialize.read_document(str(path))
    errors = findings_validate.collect_errors(document)
    require(not errors, "the findings do not validate:\n  {}".format("\n  ".join(errors)))
    return document


def findings_by_id(document):
    return {finding["id"]: finding for finding in document["findings"]}


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def check_front_ends_agree(report, hal_lib):
    report.step("hal_py and the offline front end must build the same model")
    for variant in ("ok", "faulty"):
        offline, _ = transitions.load(str(netlist_path(variant)), str(LIBRARY))
        hal, _ = halsource.load(
            netlist_path=str(netlist_path(variant)),
            gate_library=str(LIBRARY),
            hal_libs=[hal_lib] if hal_lib else [],
        )
        require(
            sorted(offline.states) == sorted(hal.states),
            "{}: registers differ.\n  offline: {}\n  hal    : {}".format(
                variant, sorted(offline.states), sorted(hal.states)
            ),
        )
        require(
            sorted(offline.inputs) == sorted(hal.inputs),
            "{}: inputs differ.\n  offline: {}\n  hal    : {}".format(
                variant, sorted(offline.inputs), sorted(hal.inputs)
            ),
        )
        for state in sorted(offline.states):
            left = offline.flatten(offline.next_terms[state])
            right = hal.flatten(hal.next_terms[state])
            status, model = sat.solve_term([expr.xor_(left, right)])
            require(
                status == sat.UNSAT,
                "{}: the next-state functions of {} differ between hal_py and the "
                "offline reader (witness {})".format(variant, state, model),
            )
        report.ok(
            "{}: {} register(s) and {} input(s) identical, transition relations "
            "equivalent by miter".format(variant, len(hal.states), len(hal.inputs))
        )

    try:
        halsource.load(
            netlist_path=str(netlist_path("blackbox")),
            gate_library=str(LIBRARY),
            hal_libs=[hal_lib] if hal_lib else [],
        )
    except UnsupportedPrimitives as error:
        require(
            "DLH_X1" in str(error),
            "the latch fixture was refused, but not by naming DLH_X1: {}".format(error),
        )
        report.ok("hal_py refuses the latch fixture by name, exactly as the offline reader")
    else:
        raise SmokeError(
            "hal_py accepted the latch fixture; a transparent latch must be refused, not "
            "modelled as a flip-flop"
        )


def check_correct_fixture(work_dir, report):
    report.step("the correct fixture must come back proven (bounded) and exercised")
    output = work_dir / "ok" / "findings.json"
    run_tool(
        ["check", policy_path("ok"), "--source", "hal", "-o", output], report, expect_exit=0
    )
    document = load_document(output, report)
    by_id = findings_by_id(document)
    truth = ground_truth()["designs"]["secreg_ok"]["expected"]["obligations"]
    for identifier, expected in truth.items():
        finding = by_id.get(identifier)
        require(finding is not None, "no finding {}".format(identifier))
        require(
            finding["status"] == expected["status"],
            "{} is {!r}, the ground truth says {!r}".format(
                identifier, finding["status"], expected["status"]
            ),
        )
        require(
            finding["data"].get("exercised") is True,
            "{} passed without being exercised; a vacuous pass is not evidence".format(
                identifier
            ),
        )
        require(
            finding["bounds"]["unbounded"] is False and finding["bounds"]["cycle_bound"],
            "{} does not record the bound it holds under".format(identifier),
        )
    report.ok(
        "{} obligation(s) proven_bounded, each exercised, each carrying its cycle "
        "bound".format(len(truth))
    )

    for finding in document["findings"]:
        require(
            not is_unbounded_proof(finding),
            "finding {!r} claims an unbounded proof; this is bounded model checking".format(
                finding["id"]
            ),
        )
    report.ok("no finding claims an unbounded proof")

    assumptions = {
        entry["id"]
        for finding in document["findings"]
        for entry in finding.get("assumptions") or []
    }
    for required in (
        "secprop/tool/policy-is-trusted",
        "secprop/env/reset-sequence",
        "secprop/env/single-clock",
    ):
        require(required in assumptions, "the proof does not record {}".format(required))
    report.ok("the policy, the reset schedule and the single-clock model are recorded")
    return document


def check_faulty_fixture(work_dir, report):
    report.step("the faulty fixture must yield replayable violating sequences")
    output = work_dir / "faulty" / "findings.json"
    run_tool(
        ["check", policy_path("faulty"), "--source", "hal", "-o", output],
        report,
        expect_exit=1,
    )
    document = load_document(output, report)
    by_id = findings_by_id(document)
    truth = ground_truth()["designs"]["secreg_faulty"]["expected"]["obligations"]
    refuted = 0
    for identifier, expected in truth.items():
        finding = by_id[identifier]
        require(
            finding["status"] == expected["status"],
            "{} is {!r}, the ground truth says {!r}".format(
                identifier, finding["status"], expected["status"]
            ),
        )
        if expected["status"] != "bounded_counterexample":
            continue
        refuted += 1
        require(finding["counterexample"]["witness"], "{} refutes without a witness".format(identifier))
        require(
            finding["counterexample"]["cycle_bound"] <= finding["bounds"]["cycle_bound"],
            "{}: the witness claims more cycles than the run had".format(identifier),
        )
        require(
            finding["data"]["violation_cycle"]
            >= expected["witness"]["min_violation_cycle"],
            "{}: violation at cycle {}, which is earlier than the reset schedule "
            "allows".format(identifier, finding["data"]["violation_cycle"]),
        )
    require(refuted == 2, "expected both defects to be refuted, got {}".format(refuted))
    report.ok("both deliberate defects reported as bounded counterexamples with witnesses")

    reset = by_id["secprop/reset-clears/SECRET"]
    require(
        reset["data"]["failing_bits"]
        == truth["secprop/reset-clears/SECRET"]["witness"]["failing_bits"],
        "the reset defect names bit(s) {}, the ground truth says {}".format(
            reset["data"]["failing_bits"],
            truth["secprop/reset-clears/SECRET"]["witness"]["failing_bits"],
        ),
    )
    report.ok("the reset defect names exactly SECRET[3]")

    evidence = work_dir / "faulty" / "evidence"
    transactions = (
        evidence / "secprop_locked_write_blocked_SECRET.transactions.txt"
    )
    require(transactions.is_file(), "no transaction sequence at {}".format(transactions))
    with open(str(transactions), "r", encoding="utf-8") as handle:
        text = handle.read()
    require("lock_write" in text, "the witness does not show the lock being engaged")
    require(
        "LOCKED debug_write" in text,
        "the witness does not show a debug write while the lock is engaged:\n{}".format(
            text
        ),
    )
    report.ok("the exported witness reads as a transaction sequence: lock, then debug write")
    return document


def check_replay(work_dir, report):
    report.step("every exported witness must replay through hal_py")
    evidence = work_dir / "faulty" / "evidence"
    bundles = sorted(name for name in os.listdir(str(evidence)) if name.endswith(".replay.json"))
    require(len(bundles) == 2, "expected two witness bundles, found {}".format(bundles))
    for name in bundles:
        stdout, _ = run_tool(
            ["replay", evidence / name, "--source", "hal"], report, expect_exit=0
        )
        require("reproduced" in stdout, "replay of {} did not reproduce:\n{}".format(name, stdout))
        require(
            "transaction sequence" in stdout,
            "replay of {} printed no transaction sequence".format(name),
        )
    report.ok("{} witness(es) reproduced against the netlist as HAL read it".format(len(bundles)))


def check_replay_refuses_a_changed_netlist(work_dir, report):
    report.step("replaying against a modified netlist must be refused")
    evidence = work_dir / "faulty" / "evidence"
    name = sorted(n for n in os.listdir(str(evidence)) if n.endswith(".replay.json"))[0]
    modified = work_dir / "modified_secreg_faulty.v"
    with open(str(netlist_path("faulty")), "r", encoding="utf-8") as handle:
        text = handle.read()
    with open(str(modified), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n// a comment is enough to change the content hash\n")

    tampered_dir = work_dir / "tampered"
    tampered_dir.mkdir(parents=True, exist_ok=True)
    with open(str(evidence / name), "r", encoding="utf-8") as handle:
        bundle = json.load(handle)
    bundle["design_source"]["netlist"] = str(modified)
    tampered = tampered_dir / name
    with open(str(tampered), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(bundle, handle, indent=2, sort_keys=True)

    _, stderr = run_tool(["replay", tampered, "--source", "hal"], report, expect_exit=2)
    require(
        "REPLAY REFUSED" in stderr and "changed" in stderr,
        "the replay did not refuse a netlist that no longer matches the bundle:\n{}".format(
            stderr
        ),
    )
    report.ok("a replay against different bytes is refused, not silently accepted")


def check_unsupported(work_dir, report):
    report.step("an unmodelled primitive must produce unsupported, not a pass")
    output = work_dir / "blackbox" / "findings.json"
    _, stderr = run_tool(
        ["check", policy_path("blackbox"), "--source", "hal", "-o", output],
        report,
        expect_exit=0,
    )
    require("UNSUPPORTED" in stderr, "the run did not announce the coverage gap:\n{}".format(stderr))
    document = load_document(output, report)
    statuses = {finding["status"] for finding in document["findings"]}
    require(
        statuses == {"unsupported"},
        "the latch fixture produced statuses {}; every one of them must be "
        "unsupported".format(sorted(statuses)),
    )
    primitives = [
        primitive["gate_type"]
        for finding in document["findings"]
        for primitive in finding["unsupported"].get("primitives") or []
    ]
    require("DLH_X1" in primitives, "the report does not name the latch: {}".format(primitives))
    report.ok("every obligation reported unsupported, with DLH_X1 named")

    run_tool(
        ["check", policy_path("blackbox"), "--source", "hal", "-o", output, "--strict"],
        report,
        expect_exit=2,
    )
    report.ok("--strict turns the coverage gap into a non-zero exit")


def check_timeout(work_dir, report):
    report.step("an exhausted search must be a timeout, never a pass")
    output = work_dir / "timeout" / "findings.json"
    stdout, _ = run_tool(
        [
            "check",
            policy_path("ok"),
            "--source",
            "hal",
            "-o",
            output,
            "--conflict-limit",
            "0",
            "--no-evidence",
        ],
        report,
        expect_exit=0,
    )
    require("timeout" in stdout, "no obligation timed out:\n{}".format(stdout))
    document = load_document(output, report)
    statuses = {
        finding["status"]
        for finding in document["findings"]
        if finding["id"].startswith("secprop/locked-write")
        or finding["id"].startswith("secprop/reset-clears")
        or finding["id"] == "secprop/lock-integrity"
    }
    require(
        statuses == {"timeout"},
        "with a zero conflict budget the obligations came back {}".format(sorted(statuses)),
    )
    for finding in document["findings"]:
        if finding["status"] != "timeout":
            continue
        require(
            finding["limits"]["hit"] is True and finding["limits"]["timeout_s"] is not None,
            "the timeout finding {!r} does not record the limit it hit".format(finding["id"]),
        )
    report.ok("every obligation reported timeout, each recording the budget it hit")

    run_tool(
        [
            "check",
            policy_path("ok"),
            "--source",
            "hal",
            "-o",
            output,
            "--conflict-limit",
            "0",
            "--no-evidence",
            "--strict",
        ],
        report,
        expect_exit=2,
    )
    report.ok("--strict turns a timeout into a non-zero exit")


def check_cones_are_candidates_only(work_dir, report, ok_document, faulty_document):
    report.step("structural reachability must be a candidate, not a verdict")
    summaries = {}
    for variant in ("ok", "faulty"):
        system, _ = transitions.load(str(netlist_path(variant)), str(LIBRARY))
        parsed = policy_module.load(str(policy_path(variant)))
        cone_report = cones_module.analyse(system, parsed)
        controls = sorted(
            {signal for signals in parsed.interface_signals.values() for signal in signals}
        )
        data = cone_report.cones["SECRET"].to_dict(
            external_controls=controls, lock_signal=parsed.lock_signal
        )
        summaries[variant] = {
            "inputs": sorted(data["inputs"]),
            "controls": data["external_controls_in_cone"],
            "depths": data["external_control_depths"],
            "lock_in_cone": data["lock_in_cone"],
            "states": data["state_count"],
        }
    require(
        summaries["ok"] == summaries["faulty"],
        "the cones of the correct and the faulty fixture differ; the whole point of the "
        "pair is that structure cannot tell them apart:\n  ok    : {}\n  faulty: {}".format(
            summaries["ok"], summaries["faulty"]
        ),
    )
    report.ok(
        "the correct and the faulty design have identical cones -- only the bounded check "
        "separates them"
    )

    for document, name in ((ok_document, "ok"), (faulty_document, "faulty")):
        cone_findings = [
            finding
            for finding in document["findings"]
            if finding["id"].startswith("secprop/candidate-reachability/")
        ]
        require(cone_findings, "{}: no cone finding was emitted".format(name))
        for finding in cone_findings:
            require(
                finding["status"] == "heuristic",
                "{}: cone finding {!r} has status {!r}; connectivity must never be a "
                "verdict".format(name, finding["id"], finding["status"]),
            )
            require(
                "candidate" in finding["title"],
                "{}: cone finding {!r} does not say 'candidate'".format(name, finding["id"]),
            )
    report.ok("every cone finding is heuristic and labelled a candidate path")


def check_run_inside_hal(work_dir, report, hal_binary):
    report.step("the same check, driven by hal --python-script")
    require(
        hal_binary and Path(hal_binary).is_file(),
        "no hal binary at {!r}; pass --hal-binary <build>/bin/hal or set "
        "HAL_BASE_PATH".format(hal_binary),
    )
    output = work_dir / "in_hal" / "findings.json"
    environment = dict(os.environ)
    environment.update(
        {
            "HAL_SECPROP_TOOLS": str(TOOLS),
            "HAL_SECPROP_POLICY": str(policy_path("faulty")),
            "HAL_SECPROP_LIBRARY": str(LIBRARY),
            "HAL_SECPROP_OUTPUT": str(output),
            "HAL_SECPROP_REPLAY": "1",
        }
    )
    command = [
        str(hal_binary),
        "--python-script",
        str(SECPROP_DIR / "scripts" / "check_in_hal.py"),
    ]
    report.note("$ {}".format(" ".join(command)))
    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    require(
        completed.returncode == 1,
        "hal exited with {}, expected 1 (a policy violation).\n--- stdout ---\n{}\n"
        "--- stderr ---\n{}".format(completed.returncode, stdout, stderr),
    )
    document = load_document(output, report)
    refutations = [
        finding
        for finding in document["findings"]
        if finding["status"] == "bounded_counterexample"
    ]
    require(len(refutations) == 2, "the in-HAL run reported {} refutation(s)".format(len(refutations)))
    report.ok("hal --python-script exits 1 and writes the same two refutations")


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


def run(args, report):
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    check_front_ends_agree(report, args.hal_lib)
    ok_document = check_correct_fixture(work_dir, report)
    faulty_document = check_faulty_fixture(work_dir, report)
    check_replay(work_dir, report)
    check_replay_refuses_a_changed_netlist(work_dir, report)
    check_unsupported(work_dir, report)
    check_timeout(work_dir, report)
    check_cones_are_candidates_only(work_dir, report, ok_document, faulty_document)
    check_run_inside_hal(work_dir, report, args.hal_binary)


def default_hal_binary():
    base = os.environ.get("HAL_BASE_PATH")
    if base:
        candidate = Path(base) / "bin" / "hal"
        if candidate.is_file():
            return str(candidate)
        candidate = Path(base) / "bin" / "hal.exe"
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("hal")
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Headless end-to-end test of tools/hal_secprop: run the shipped "
        "security policies over the secreg fixtures through a built HAL and check the "
        "model, the verdicts, the witnesses, the replay, the coverage gaps and the "
        "candidate-only status of structural reachability.",
    )
    parser.add_argument(
        "--hal-binary",
        metavar="PATH",
        help="the hal executable (default: $HAL_BASE_PATH/bin/hal, then 'hal' on PATH)",
    )
    parser.add_argument(
        "--hal-lib",
        metavar="DIR",
        default=os.environ.get("HAL_PY_PATH"),
        help="directory holding hal_py (default: $HAL_PY_PATH, else PYTHONPATH)",
    )
    parser.add_argument(
        "--work-dir", metavar="DIR", help="where to write reports (default: a temporary directory)"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the work directory even when the run succeeds"
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    if not args.hal_binary:
        args.hal_binary = default_hal_binary()

    temporary = None
    if not args.work_dir:
        temporary = tempfile.mkdtemp(prefix="hal_secprop_smoke_")
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
