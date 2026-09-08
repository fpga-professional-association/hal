#!/usr/bin/env python3
"""End-to-end check of ``tools/hal_fsm`` against a hand-written FSM fixture.

``tools/hal_fsm/test_hal_fsm.py`` drives the whole workflow against a stub
netlist and a stub ``solve_fsm``, so it proves the bookkeeping, the encodings
and the claim separation -- and none of the bindings.  This script closes that
gap the way ``runner_smoke.py`` does for hal_runner: it needs a *built* HAL and
it asserts results, not exit codes.

The design under test is ``tests/fixtures/fsm_controller/controller.v``: a
3-state controller whose state bits are named ``nx_a1_reg``/``nx_b2_reg``, a
3-bit counter as a decoy, and a feedback-free pipeline register.  Its transition
relation is recorded independently in ``ground_truth.json``.

  1. discovery: hal_fsm proposes the controller first, the counter second, and
     never the pipeline register -- with the counter's lower score explained by
     the absence of mutual feedback
  2. recovery: the transitions solve_fsm returns, permuted into the reference's
     bit order, are exactly the reference's reachable part -- this is the
     "renamed state bits" check, and it only passes if the bit order the tool
     recorded is the one it actually used
  3. claim separation: the candidate is ``heuristic`` with a confidence, the
     transition relation is ``proven_under_assumptions`` and names the candidate
     as an assumption, and the assumptions that *can* be discharged (reset tied
     off, initial state from INIT, cone closed) are
  4. witness: state 2 is reached from the initial state under a recorded input
     sequence, and every step's condition holds under it
  5. exhaustive mode: ``solver: brute_force`` additionally reports state 3 --
     encodable, present in the reference, and unreachable
  6. the counter, chosen by an override: its asynchronous reset is driven by
     ``i_rst``, which solve_fsm does not model, so the run must report that gap
     and must not discharge the assumption
  7. a wrong candidate (one controller bit + one counter bit): the run must fail
     or contradict itself visibly, never present a recovered controller
  8. limits: a 1-second wall clock on a run must produce a ``timeout`` findings
     document and exit non-zero -- and no transition relation

Nothing is skipped when something is missing: a missing ``hal`` binary, a
missing ``solve_fsm`` plugin or a wrong answer is a failure with an actionable
message.

Run it against a build tree with::

    HAL_BASE_PATH=<build> python3 tests/headless_smoke/fsm_discovery_smoke.py \\
        --hal-binary <build>/bin/hal --work-dir <build>/fsm_smoke --keep
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
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "fsm_controller"
NETLIST = FIXTURE_DIR / "controller.v"
GROUND_TRUTH = FIXTURE_DIR / "ground_truth.json"
GATE_LIBRARY = REPO_ROOT / "plugins" / "gate_libraries" / "definitions" / "example_library.hgl"

sys.path.insert(0, str(TOOLS))

from hal_findings import serialize as findings_serialize  # noqa: E402
from hal_findings import validate as findings_validate  # noqa: E402
from hal_fsm import reference as reference_module  # noqa: E402
from hal_fsm import transitions as transitions_module  # noqa: E402

#: Properties of the fixture, taken from ground_truth.json rather than restated.
CONTROLLER_REGISTER = ["nx_a1_reg", "nx_b2_reg"]
COUNTER_REGISTER = ["cnt_r0", "cnt_r1", "cnt_r2"]
PIPELINE_REGISTER = ["dp_x0", "dp_x1"]


class SmokeError(RuntimeError):
    """A check failed, or a prerequisite for one is missing."""


def require(condition, message):
    if not condition:
        raise SmokeError(message)


class Report(object):
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
# driving hal_fsm
# ---------------------------------------------------------------------------


def run_hal_fsm(arguments, hal_binary, report, expect_exit=0):
    command = [sys.executable, str(TOOLS / "hal_fsm")] + [str(part) for part in arguments]
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
        "hal_fsm exited with {}, expected {}\n--- stdout ---\n{}\n--- stderr ---\n{}".format(
            completed.returncode, expect_exit, stdout, stderr
        ),
    )
    return stdout, stderr


def analyze(output_dir, hal_binary, report, extra=(), expect_exit=0):
    """Run ``hal_fsm analyze`` on the fixture and return the findings document."""
    if output_dir.exists():
        shutil.rmtree(str(output_dir))
    arguments = [
        "analyze",
        str(NETLIST),
        "--gate-library",
        str(GATE_LIBRARY),
        "-o",
        str(output_dir),
    ] + list(extra)
    run_hal_fsm(arguments, hal_binary, report, expect_exit=expect_exit)
    findings_path = output_dir / "findings.json"
    require(
        findings_path.is_file(),
        "hal_fsm wrote no findings document at {}; logs are in {}".format(
            findings_path, output_dir / "logs"
        ),
    )
    document = findings_serialize.read_document(str(findings_path))
    findings_validate.validate_document(document)
    return document


def by_id(document):
    return {finding["id"]: finding for finding in document["findings"]}


def candidate_names(finding):
    return sorted(finding["data"]["gate_names"])


def assumptions_of(finding):
    return {entry["id"]: entry for entry in finding.get("assumptions", [])}


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------


def check_discovery(document, report):
    report.step("candidate discovery")
    findings = by_id(document)
    require("fsm/candidate/001" in findings, "no candidate was proposed at all")
    first = findings["fsm/candidate/001"]
    require(
        candidate_names(first) == CONTROLLER_REGISTER,
        "the highest scoring candidate is {}, expected the controller {}".format(
            candidate_names(first), CONTROLLER_REGISTER
        ),
    )
    require(
        first["status"] == "heuristic",
        "a candidate must be heuristic, got {!r}".format(first["status"]),
    )
    require("confidence" in first, "a candidate must carry a confidence")
    report.ok(
        "controller proposed first with confidence {:.2f} via {}".format(
            first["confidence"], ", ".join(first["data"]["sources"])
        )
    )

    second = findings.get("fsm/candidate/002")
    require(second is not None, "the decoy counter was not proposed at all")
    require(
        candidate_names(second) == COUNTER_REGISTER,
        "the second candidate is {}, expected the counter {}".format(
            candidate_names(second), COUNTER_REGISTER
        ),
    )
    require(
        second["confidence"] < first["confidence"],
        "the counter scores {} and the controller {}; the controller must rank "
        "higher".format(second["confidence"], first["confidence"]),
    )
    require(
        first["data"]["features"]["mutual_feedback"]
        > second["data"]["features"]["mutual_feedback"],
        "the ranking must come from mutual feedback: controller {} vs counter {}".format(
            first["data"]["features"], second["data"]["features"]
        ),
    )
    report.ok(
        "counter ranked second at {:.2f}; the difference is mutual feedback".format(
            second["confidence"]
        )
    )

    for finding in document["findings"]:
        if not finding["id"].startswith("fsm/candidate/"):
            continue
        overlap = set(candidate_names(finding)) & set(PIPELINE_REGISTER)
        require(
            not overlap,
            "the feedback-free pipeline register {} must never be proposed as a state "
            "register, but {} contains {}".format(
                PIPELINE_REGISTER, finding["id"], sorted(overlap)
            ),
        )
    require(
        "fsm/candidates/ambiguous" not in findings,
        "the controller and the counter must not be reported as ambiguous at the "
        "default margin",
    )
    report.ok("pipeline register excluded; the selection is not ambiguous")


def check_recovery(document, output_dir, report):
    report.step("recovered transition relation")
    findings = by_id(document)
    transitions_finding = findings.get("fsm/machine01/transitions")
    require(
        transitions_finding is not None,
        "no transition relation was produced; findings are {}".format(sorted(findings)),
    )
    require(
        transitions_finding["status"] == "proven_under_assumptions",
        "the transition relation is {!r}; solve_fsm succeeded, so it must be "
        "proven_under_assumptions".format(transitions_finding["status"]),
    )
    bit_order = transitions_finding["data"]["bit_order"]
    require(
        sorted(bit_order) == sorted(CONTROLLER_REGISTER),
        "the transition relation is over {}, expected the controller".format(bit_order),
    )
    report.ok(
        "{} states, {} transitions over bit order {}".format(
            len(transitions_finding["data"]["states"]),
            len(transitions_finding["data"]["transitions"]),
            bit_order,
        )
    )

    comparison = findings.get("fsm/machine01/reference-comparison")
    require(comparison is not None, "the reference comparison finding is missing")
    require(
        comparison["status"] == "proven_under_assumptions",
        "the recovered relation does not match the reference: {}".format(
            json.dumps(comparison.get("data", {}), indent=2)
        ),
    )
    report.ok(
        "matches the reference machine {!r} after permuting into bit order {}".format(
            comparison["data"]["machine"], comparison["data"]["bit_order"]
        )
    )

    # the same check again, through the offline CLI and the file a run leaves
    # behind: what the report claims must be reproducible from the artifacts
    table_path = output_dir / "transitions-machine01.json"
    require(table_path.is_file(), "the run wrote no transition table at {}".format(table_path))
    with open(str(table_path), "r", encoding="utf-8") as handle:
        table_document = json.load(handle)
    machine = reference_module.load(str(GROUND_TRUTH)).get("controller")
    table = transitions_module.TransitionTable(
        table_document["bit_order"], initial_state=table_document["initial_state"]
    )
    for entry in table_document["transitions"]:
        table.add(transitions_module.Transition(entry["source"], entry["target"]))
    offline = machine.compare(table, restrict_to_reachable=True)
    require(offline["matches"], "the written table does not match the reference: {}".format(offline))
    report.ok("the written transition table reproduces the comparison offline")


def check_claim_separation(document, report):
    report.step("candidate confidence vs solver-verified transitions")
    findings = by_id(document)
    candidate = findings["fsm/candidate/001"]
    relation = findings["fsm/machine01/transitions"]
    require(
        candidate["status"] == "heuristic" and "confidence" in candidate,
        "the candidate must stay a scored heuristic",
    )
    require(
        "confidence" not in relation,
        "a solver-verified relation must not carry a candidate confidence",
    )
    assumptions = assumptions_of(relation)
    require(
        "hal_fsm.state-register" in assumptions,
        "the transition relation must name the state-register choice as an assumption; "
        "it has {}".format(sorted(assumptions)),
    )
    require(
        assumptions["hal_fsm.state-register"].get("discharged") is False,
        "the state-register assumption is a heuristic and must not be marked discharged",
    )
    report.ok("the heuristic is an explicit, undischarged assumption of the proof")

    for key, expected in (
        ("hal_fsm.asynchronous-control-inactive", True),
        ("hal_fsm.initial-state", True),
        ("hal_fsm.transition-cone-complete", True),
        ("hal_fsm.single-clock-domain", True),
        ("hal_fsm.closed-machine", True),
        ("hal_fsm.library-next-state", False),
    ):
        require(key in assumptions, "assumption {!r} is missing".format(key))
        require(
            assumptions[key].get("discharged") is expected,
            "assumption {!r} is discharged={!r}, expected {!r}: {}".format(
                key, assumptions[key].get("discharged"), expected, assumptions[key]["description"]
            ),
        )
    report.ok(
        "reset tied off, initial state from INIT, cone closed and one clock domain are "
        "discharged; the library model is not"
    )

    consistency = findings.get("fsm/machine01/relation-consistency")
    require(consistency is not None, "the relation was never checked for consistency")
    require(
        consistency["status"] == "proven_under_assumptions",
        "the recovered relation is not a deterministic total function: {}".format(
            json.dumps(consistency.get("data", {}), indent=2)
        ),
    )
    report.ok(
        "relation checked deterministic and total over {} input assignments".format(
            consistency["metrics"]["checked_assignments"]
        )
    )


def check_witness(document, output_dir, report):
    report.step("bounded reachability witness")
    findings = by_id(document)
    witness = findings.get("fsm/machine01/witness/2")
    require(witness is not None, "no witness was produced for state 2")
    require(
        witness["status"] == "proven_under_assumptions",
        "the witness for state 2 is {!r}: {}".format(witness["status"], witness["summary"]),
    )
    data = witness["data"]
    require(data["path"][0] == 0, "the witness must start at the initial state")
    require(data["path"][-1] == 2, "the witness must end at state 2")
    require(data["cycles"] == 2, "state 2 is two transitions away, got {}".format(data["cycles"]))
    signals = findings["fsm/machine01/transitions"]["data"]["signals"]
    named = [
        {signals[name]["name"]: value for name, value in step["inputs"].items()}
        for step in data["steps"]
    ]
    require(
        named == [{"i_go": 1}, {"i_fin": 1}],
        "the witness inputs are {}, expected i_go then i_fin".format(named),
    )
    report.ok("state 2 reached in 2 cycles under i_go=1 then i_fin=1")

    diagram_path = output_dir / "state-diagram-machine01.dot"
    require(diagram_path.is_file(), "no state diagram was written")
    dot = diagram_path.read_text(encoding="utf-8")
    require(
        "state bit order (bit 0 first): nx_a1_reg, nx_b2_reg" in dot,
        "the state diagram must name the bit order its state values are relative to",
    )
    require("doublecircle" in dot, "the diagram must mark the initial state")
    require("#b30000" in dot, "the diagram must highlight the witness path")
    require(
        'label="i_go"' in dot,
        "condition labels must show net names, not HAL's net_<id> variables",
    )
    report.ok("state diagram records the bit order, the initial state and the witness")


def check_exhaustive_mode(work_dir, hal_binary, report):
    report.step("exhaustive mode reports the unreachable state")
    output_dir = work_dir / "brute-force"
    document = analyze(
        output_dir,
        hal_binary,
        report,
        extra=[
            "--reference",
            str(GROUND_TRUTH),
            "--solver",
            "brute_force",
            "--targets",
            "3",
        ],
    )
    findings = by_id(document)
    relation = findings["fsm/machine01/transitions"]
    require(
        relation["data"]["solver_mode"] == "brute_force",
        "expected the brute force solver, got {!r}".format(relation["data"]["solver_mode"]),
    )
    require(
        relation["data"]["states"] == [0, 1, 2, 3],
        "an exhaustive run must cover every encodable state, got {}".format(
            relation["data"]["states"]
        ),
    )
    reachability = findings["fsm/machine01/reachable-states"]
    require(
        reachability["data"]["reachable"] == [0, 1, 2],
        "reachable set is {}, expected [0, 1, 2]".format(reachability["data"]["reachable"]),
    )
    require(
        reachability["data"]["unreachable_in_encoding"] == [3],
        "state 3 must be reported as unreachable, got {}".format(
            reachability["data"].get("unreachable_in_encoding")
        ),
    )
    witness = findings["fsm/machine01/witness/3"]
    require(
        witness["status"] == "proven_under_assumptions"
        and "not reachable" in witness["title"],
        "state 3 must be reported as unreachable, not as a failed search: {}".format(
            witness["title"]
        ),
    )
    comparison = findings["fsm/machine01/reference-comparison"]
    require(
        comparison["status"] == "proven_under_assumptions"
        and comparison["data"]["restricted_to_reachable"] is False,
        "an exhaustive run must match the reference's *total* relation: {}".format(
            json.dumps(comparison["data"], indent=2)
        ),
    )
    report.ok("state 3 recovered, reported unreachable, and matched against the reference")


def check_counter_override(work_dir, hal_binary, report):
    report.step("user override: the counter, whose reset is not modelled")
    output_dir = work_dir / "counter"
    document = analyze(
        output_dir,
        hal_binary,
        report,
        extra=[
            "--config",
            str(FIXTURE_DIR / "counter_override.json"),
            "--reference",
            str(GROUND_TRUTH),
        ],
    )
    findings = by_id(document)
    candidate = findings["fsm/candidate/001"]
    require(
        candidate["data"]["origin"] == "user_override",
        "an override must be recorded as such, not scored: {}".format(candidate["data"]),
    )
    require(
        "confidence" not in candidate,
        "a user-selected register must not be given a confidence score",
    )

    gap = findings.get("fsm/machine01/asynchronous-control")
    require(
        gap is not None,
        "the counter's asynchronous reset is driven by i_rst and solve_fsm does not "
        "model it; that gap must be reported",
    )
    require(gap["status"] == "unsupported", "the gap is {!r}".format(gap["status"]))
    require(
        "i_rst" in json.dumps(gap["data"]),
        "the reported gap must name the net that drives the reset",
    )
    relation = findings["fsm/machine01/transitions"]
    assumptions = assumptions_of(relation)
    require(
        assumptions["hal_fsm.asynchronous-control-inactive"].get("discharged") is False,
        "with a driven reset the inactive-reset assumption must NOT be discharged",
    )
    report.ok("asynchronous reset reported as unsupported and left undischarged")

    comparison = findings.get("fsm/machine01/reference-comparison")
    require(comparison is not None, "the counter was not compared against the reference")
    require(
        comparison["status"] == "proven_under_assumptions",
        "the recovered counter does not match its reference: {}".format(
            json.dumps(comparison.get("data", {}), indent=2)
        ),
    )
    witness = findings["fsm/machine01/witness/7"]
    require(
        witness["status"] == "proven_under_assumptions" and witness["data"]["cycles"] == 7,
        "reaching counter state 7 takes 7 ticks, got {}".format(witness.get("data"))
    )
    report.ok("counter matches its reference; state 7 witnessed in 7 cycles")


def check_wrong_candidate(work_dir, hal_binary, report):
    report.step("a deliberately wrong candidate")
    output_dir = work_dir / "wrong"
    document = analyze(
        output_dir,
        hal_binary,
        report,
        extra=["--config", str(FIXTURE_DIR / "wrong_candidate.json")],
        expect_exit=0,
    )
    findings = by_id(document)
    relation = findings.get("fsm/machine01/transitions")
    require(relation is not None, "the run produced no finding at all for the candidate")

    if relation["status"] in ("error", "unsupported", "timeout"):
        report.ok(
            "solve_fsm refused the mixed register and the run reported {!r}".format(
                relation["status"]
            )
        )
        return

    # If it did produce a relation, it must not be presented as the controller:
    # the two flip-flops do not influence each other, so either the consistency
    # check or the reference comparison has to say so.
    require(
        sorted(relation["data"]["bit_order"]) == sorted(["cnt_r2", "nx_a1_reg"]),
        "the relation must be over the register that was actually asked for",
    )
    require(
        "fsm/machine01/reference-comparison" not in findings,
        "there is no reference for this register; nothing may claim it matches one",
    )
    consistency = findings.get("fsm/machine01/relation-consistency")
    require(consistency is not None, "a recovered relation must always be checked")
    external = findings.get("fsm/machine01/external-state-dependence")
    require(
        external is not None,
        "the two flip-flops do not influence each other, so their transitions must read "
        "nets driven by flip-flops outside the register -- that must be reported",
    )
    require(external["status"] == "unsupported", "status is {!r}".format(external["status"]))
    assumptions = assumptions_of(relation)
    require(
        assumptions["hal_fsm.closed-machine"].get("discharged") is False,
        "a register that reads other registers' outputs is not a closed machine",
    )
    report.ok(
        "the mixed register produced a relation over {} that is reported as not closed "
        "(consistency: {})".format(relation["data"]["bit_order"], consistency["status"])
    )


def check_timeout(work_dir, hal_binary, report):
    report.step("a wall-clock limit produces a timeout record, not a state machine")
    output_dir = work_dir / "timeout"
    if output_dir.exists():
        shutil.rmtree(str(output_dir))
    run_hal_fsm(
        [
            "analyze",
            str(NETLIST),
            "--gate-library",
            str(GATE_LIBRARY),
            "-o",
            str(output_dir),
            "--timeout",
            "0.05",
        ],
        hal_binary,
        report,
        expect_exit=1,
    )
    findings_path = output_dir / "findings.json"
    require(
        findings_path.is_file(),
        "a killed run must still leave a findings document behind; nothing at {}".format(
            findings_path
        ),
    )
    document = findings_serialize.read_document(str(findings_path))
    findings_validate.validate_document(document)
    findings = by_id(document)
    require(
        "fsm/run/timeout" in findings,
        "the killed run must be recorded as a timeout, findings are {}".format(sorted(findings)),
    )
    timeout = findings["fsm/run/timeout"]
    require(timeout["status"] == "timeout", "status is {!r}".format(timeout["status"]))
    require(
        timeout["limits"]["hit"] is True and timeout["limits"]["timeout_s"] == 0.05,
        "the timeout record must carry the limit it hit: {}".format(timeout["limits"]),
    )
    for finding in document["findings"]:
        require(
            finding["status"] not in ("proven_under_assumptions", "proven_bounded"),
            "a killed run must claim nothing, but {} is {!r}".format(
                finding["id"], finding["status"]
            ),
        )
    require(
        not (output_dir / "transitions-machine01.json").is_file(),
        "a killed run must not leave a transition table claiming a recovered machine",
    )
    report.ok("timeout recorded with its limit; no transition relation, no proof")


def check_logs_survive(output_dir, report):
    report.step("logs")
    for name in ("stdout.log", "stderr.log"):
        path = output_dir / "logs" / name
        require(path.is_file(), "HAL's {} was not kept at {}".format(name, path))
    report.ok("HAL's own output kept next to the findings")


# ---------------------------------------------------------------------------
# entry point
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
    for path in (NETLIST, GROUND_TRUTH, GATE_LIBRARY):
        require(path.is_file(), "missing input: {}".format(path))
    hal_binary = resolve_hal_binary(args.hal_binary)
    report.ok("hal binary {}".format(hal_binary))

    output_dir = work_dir / "controller"
    document = analyze(
        output_dir,
        hal_binary,
        report,
        extra=["--reference", str(GROUND_TRUTH), "--targets", "2", "--print-summary"],
    )
    check_discovery(document, report)
    check_recovery(document, output_dir, report)
    check_claim_separation(document, report)
    check_witness(document, output_dir, report)
    check_logs_survive(output_dir, report)

    check_exhaustive_mode(work_dir, hal_binary, report)
    check_counter_override(work_dir, hal_binary, report)
    check_wrong_candidate(work_dir, hal_binary, report)
    check_timeout(work_dir, hal_binary, report)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Headless end-to-end test of tools/hal_fsm: discover, solve and "
        "explain the FSM of tests/fixtures/fsm_controller/controller.v through a built "
        "HAL and check the result against the recorded ground truth.",
    )
    parser.add_argument(
        "--hal-binary", metavar="PATH", help="path to the hal executable (default: $HAL_BASE_PATH)"
    )
    parser.add_argument(
        "--work-dir", metavar="DIR", help="where to write runs (default: a temporary directory)"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the work directory even when the run succeeds"
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))

    temporary = None
    if not args.work_dir:
        temporary = tempfile.mkdtemp(prefix="hal_fsm_smoke_")
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
