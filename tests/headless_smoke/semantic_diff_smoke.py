#!/usr/bin/env python3
"""End-to-end headless smoke test for ``tools/hal_semantic_diff``.

``tools/hal_semantic_diff/test_hal_semantic_diff.py`` drives the whole
classification pipeline with stub netlists and a brute-force solver, which is
what makes it runnable anywhere -- and exactly why it proves nothing about
``hal_py``.  It never parses Verilog, never touches
``SubgraphNetlistDecorator``, never calls an SMT solver and never finds out
whether ``SMT.SolverResult.model`` still carries a model.

This script closes that gap.  It needs a *built* HAL and it asserts results:

  1. parse the five hand-written fixtures in
     ``tools/hal_semantic_diff/fixtures`` against the shipped
     ``example_library.hgl`` and check their gate counts and gate types
  2. run ``hal_semantic_diff compare`` as a subprocess for every case in
     ``fixtures/ground_truth.json`` and check the exit code, the per-point
     verdicts, the correspondence gaps and the localization
  3. check that the changed-decoder case produces a witness that satisfies the
     recorded constraints (``EN = 1`` and ``A1 = 1``), that the witness
     replayed through both cone functions and that the counterexample is
     unbounded rather than dressed up as a bounded one
  4. check that the findings document validates against the ``hal_findings``
     schema and that the report and the changed-cone diagrams were written
  5. record the measured runtimes of every case in ``runtimes.json``

Nothing is skipped when something is missing: a missing ``hal_py``, a missing
plugin or a missing binding is a failure with an actionable message.  Graphviz
is the one exception, for the same reason as in ``real_netlist_smoke.py``.

Run it against a build tree with::

    HAL_BASE_PATH=<build> PYTHONPATH=<build>/lib \\
        python3 tests/headless_smoke/semantic_diff_smoke.py

or point it at the libraries explicitly with ``--hal-lib <build>/lib``.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"
FIXTURES = TOOLS / "hal_semantic_diff" / "fixtures"
GATE_LIBRARY = REPO_ROOT / "plugins" / "gate_libraries" / "definitions" / "example_library.hgl"
SEMANTIC_DIFF = TOOLS / "hal_semantic_diff"

EXIT_VERDICT = 3


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
# the fixtures, through hal_py
# ---------------------------------------------------------------------------


def import_hal(hal_libs, report):
    report.step("importing hal_py")
    for entry in hal_libs:
        path = os.path.abspath(os.path.expanduser(entry))
        require(os.path.isdir(path), "--hal-lib directory does not exist: {}".format(entry))
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        import hal_py
    except ImportError as exc:
        raise SmokeError(
            "could not import hal_py ({}).\n"
            "This test needs a built HAL. Point it at one with --hal-lib <build>/lib or\n"
            "PYTHONPATH=<build>/lib, and set HAL_BASE_PATH=<build> so the plugins and\n"
            "gate libraries are found.".format(exc)
        )
    hal_py.plugin_manager.load_all_plugins()
    report.ok("hal_py from {}".format(getattr(hal_py, "__file__", "<unknown>")))
    return hal_py


def check_fixture_netlists(hal_py, ground_truth, report):
    report.step("parsing the fixtures against example_library.hgl")
    require(GATE_LIBRARY.is_file(), "the gate library {} is missing".format(GATE_LIBRARY))

    for name, expected in sorted(ground_truth["designs"].items()):
        path = FIXTURES / name
        require(path.is_file(), "fixture {} is missing".format(path))
        netlist = hal_py.NetlistFactory.load_netlist(str(path), str(GATE_LIBRARY))
        require(
            netlist is not None,
            "HAL could not parse {} against {}. The fixture and the gate library must "
            "match; see the HAL log above.".format(path.name, GATE_LIBRARY.name),
        )
        gates = netlist.get_gates()
        require(
            len(gates) == expected["gate_count"],
            "{} has {} gates, ground_truth.json says {}".format(
                name, len(gates), expected["gate_count"]
            ),
        )
        if "gate_types" in expected:
            histogram = dict(Counter(gate.get_type().get_name() for gate in gates))
            require(
                histogram == expected["gate_types"],
                "{} has gate types {}, ground_truth.json says {}".format(
                    name, histogram, expected["gate_types"]
                ),
            )
        sequential = sorted(
            gate.get_name()
            for gate in gates
            if "sequential" in {str(p).rsplit(".", 1)[-1] for p in gate.get_type().get_property_list()}
        )
        require(
            sequential == sorted(expected["sequential_gates"]),
            "{} has sequential gates {}, ground_truth.json says {}".format(
                name, sequential, sorted(expected["sequential_gates"])
            ),
        )
        top = netlist.get_top_module()
        require(top is not None, "{} has no top module".format(name))
        report.ok(
            "{}: {} gates, registers {}".format(name, len(gates), ", ".join(sequential))
        )


# ---------------------------------------------------------------------------
# the tool, as a subprocess
# ---------------------------------------------------------------------------


def run_compare(case, out_dir, hal_libs, extra_args=()):
    command = [
        sys.executable,
        str(SEMANTIC_DIFF),
        "compare",
        str(FIXTURES / case["netlist_a"]),
        str(FIXTURES / case["netlist_b"]),
        "--gate-library",
        str(GATE_LIBRARY),
        "--correspondence",
        str(FIXTURES / case["correspondence"]),
        "-o",
        str(out_dir),
    ] + list(extra_args)
    for entry in hal_libs:
        command += ["--hal-lib", entry]
    started = time.time()
    completed = subprocess.run(
        command, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    elapsed = time.time() - started
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", "replace"),
        completed.stderr.decode("utf-8", "replace"),
        elapsed,
        command,
    )


def _findings_by_id(document):
    return {finding["id"]: finding for finding in document["findings"]}


def _point_status(document, key):
    finding = _findings_by_id(document).get("semantic_diff/point/" + key)
    return finding["status"] if finding else None


def check_case(case, work_dir, hal_libs, report):
    name = case["name"]
    expected = case["expect"]
    report.step("case {}: {} vs {}".format(name, case["netlist_a"], case["netlist_b"]))

    out_dir = work_dir / name
    code, stdout, stderr, elapsed, command = run_compare(case, out_dir, hal_libs)
    report.note("$ {}".format(" ".join(command)))
    require(
        code == expected["exit_code_fail_on_difference"],
        "exit code was {}, expected {} (--fail-on difference)\n"
        "--- stdout ---\n{}\n--- stderr ---\n{}".format(
            code, expected["exit_code_fail_on_difference"], stdout, stderr
        ),
    )
    report.ok("exit code {} in {:.2f} s".format(code, elapsed))

    findings_path = out_dir / "findings.json"
    require(findings_path.is_file(), "no findings.json in {}".format(out_dir))

    sys.path.insert(0, str(TOOLS))
    from hal_findings import model as findings_model
    from hal_findings import serialize as findings_serialize
    from hal_findings import validate as findings_validate

    document = findings_serialize.read_document(str(findings_path))
    findings_validate.validate_document(document)
    report.ok("findings.json validates against the schema")

    by_id = _findings_by_id(document)
    summary = by_id.get("semantic_diff/summary")
    require(summary is not None, "the document has no semantic_diff/summary finding")
    require(
        summary["status"] == expected["summary_status"],
        "summary status is {!r}, ground truth says {!r}".format(
            summary["status"], expected["summary_status"]
        ),
    )
    report.ok("summary status {!r}".format(summary["status"]))

    data = summary["data"]
    require(
        data["coverage"]["observation_points"] == expected["observation_points"],
        "{} observation points, ground truth says {}".format(
            data["coverage"]["observation_points"], expected["observation_points"]
        ),
    )
    require(
        data["correspondence_gaps"] == expected["correspondence_gaps"],
        "{} correspondence gaps, ground truth says {}".format(
            data["correspondence_gaps"], expected["correspondence_gaps"]
        ),
    )
    require(
        data["counts"] == expected["counts"],
        "per-point counts are {}, ground truth says {}".format(
            data["counts"], expected["counts"]
        ),
    )
    report.ok(
        "{} observation points, counts {}".format(
            expected["observation_points"], expected["counts"]
        )
    )

    if "different_points" in expected:
        require(
            sorted(data["changed_points"]) == sorted(expected["different_points"]),
            "the changed points are {}, ground truth says {}".format(
                sorted(data["changed_points"]), sorted(expected["different_points"])
            ),
        )
        report.ok("changed points: {}".format(sorted(data["changed_points"]) or "none"))

    for key in expected.get("equivalent_points_include", []):
        status = _point_status(document, key)
        require(
            status == findings_model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
            "observation point {} is {!r}, ground truth says it must be proven".format(
                key, status
            ),
        )
    if expected.get("equivalent_points_include"):
        report.ok(
            "still proven: {}".format(", ".join(expected["equivalent_points_include"]))
        )

    for key in expected.get("unsupported_points", []):
        status = _point_status(document, key)
        require(
            status == findings_model.STATUS_UNSUPPORTED,
            "observation point {} is {!r}, ground truth says 'unsupported' -- a missing "
            "correspondence must be neither equivalence nor a counterexample".format(
                key, status
            ),
        )
    if expected.get("unsupported_points"):
        report.ok("unsupported (not 'different'): {}".format(", ".join(expected["unsupported_points"])))

    # No finding may claim a proof unless the whole run reached one.
    proofs = [
        finding
        for finding in document["findings"]
        if findings_model.is_unbounded_proof(finding)
    ]
    if expected["summary_status"] != findings_model.STATUS_PROVEN_UNDER_ASSUMPTIONS:
        require(
            summary not in proofs,
            "the summary claims an unbounded proof but the run did not reach one",
        )
    for finding in document["findings"]:
        require(
            finding["status"] != "bounded_counterexample",
            "finding {} is a bounded_counterexample, but nothing is unrolled here; an "
            "invented cycle bound would misrepresent the claim".format(finding["id"]),
        )

    if expected.get("witness_available"):
        counterexample = summary["counterexample"]
        require(
            counterexample.get("witness_available"),
            "the summary counterexample has no witness; z3_utils cannot produce one, "
            "but hal_py.SMT can -- check QueryConfig.with_model_generation() and "
            "SolverResult.model",
        )
        witness = {entry["signal"]: entry["value"] for entry in counterexample["witness"]}
        for signal, value in expected["witness_constraints"].items():
            require(
                witness.get(signal) == value,
                "the witness has {}={!r}, ground truth requires {!r}. The difference "
                "function of this point is EN & A1, so any other value means the "
                "witness does not actually distinguish the two builds.".format(
                    signal, witness.get(signal), value
                ),
            )
        report.ok("witness {} satisfies the recorded constraints".format(witness))

        for key in expected["different_points"]:
            finding = by_id["semantic_diff/point/" + key]
            replay = finding["data"].get("replay") or {}
            require(
                replay.get("confirmed"),
                "the witness for {} was not replayed successfully; a model that does "
                "not reproduce the difference must be downgraded, not reported".format(key),
            )
            require(
                replay.get("value_a") != replay.get("value_b"),
                "replaying the witness for {} gave {} on both sides".format(
                    key, replay.get("value_a")
                ),
            )
            dot = out_dir / "cone_{}.dot".format(key.replace(":", "_"))
            require(
                dot.is_file(),
                "no changed-cone diagram for {} (expected {})".format(key, dot.name),
            )
            text = dot.read_text(encoding="utf-8")
            require("cluster_a" in text and "cluster_b" in text, "{} draws only one build".format(dot.name))
        report.ok("every changed point was replayed and has a cone diagram")

    report_path = out_dir / "report.html"
    require(report_path.is_file(), "no report.html in {}".format(out_dir))
    html = report_path.read_text(encoding="utf-8")
    require("<html" in html and "</html>" in html, "report.html is not an HTML document")
    require(len(html) > 2000, "report.html is suspiciously small ({} bytes)".format(len(html)))
    report.ok("report.html: {} bytes".format(len(html)))

    measurements = json.loads((out_dir / "measurements.json").read_text(encoding="utf-8"))
    report.ok(
        "{} solver queries in {:.3f} s".format(
            measurements["solver_queries"], measurements["solver_wall_time_s"]
        )
    )
    return {
        "case": name,
        "exit_code": code,
        "wall_time_s": round(elapsed, 3),
        "solver_queries": measurements["solver_queries"],
        "solver_wall_time_s": measurements["solver_wall_time_s"],
        "observation_points": measurements["observation_points"],
        "status_counts": measurements["status_counts"],
    }


def check_fail_on_inconclusive(case, work_dir, hal_libs, report):
    """``--fail-on inconclusive`` must gate on anything short of a proof."""
    expected = case["expect"]["exit_code_fail_on_inconclusive"]
    report.step("case {}: --fail-on inconclusive".format(case["name"]))
    code, stdout, stderr, _, _ = run_compare(
        case,
        work_dir / (case["name"] + "_strict"),
        hal_libs,
        ["--fail-on", "inconclusive", "--diagrams", "none", "--no-html", "-q"],
    )
    require(
        code == expected,
        "exit code was {}, expected {}\n--- stderr ---\n{}".format(code, expected, stderr),
    )
    report.ok("exit code {}".format(code))


def check_determinism(case, work_dir, hal_libs, report):
    """Two runs with ``--no-timings`` must produce byte-identical findings."""
    report.step("case {}: reproducibility with --no-timings".format(case["name"]))
    digests = []
    for index in (1, 2):
        out_dir = work_dir / "{}_repro{}".format(case["name"], index)
        code, _, stderr, _, _ = run_compare(
            case,
            out_dir,
            hal_libs,
            ["--no-timings", "--diagrams", "none", "--no-html", "--fail-on", "never", "-q"],
        )
        require(code == 0, "the reproducibility run failed with {}\n{}".format(code, stderr))
        digests.append((out_dir / "findings.json").read_bytes())
    require(
        digests[0] == digests[1],
        "two runs on identical inputs produced different findings.json; with "
        "--no-timings the document must be byte-identical",
    )
    report.ok("two runs produced byte-identical findings.json")


def check_bad_correspondence(work_dir, hal_libs, report):
    """A malformed correspondence must be a handled error, not a verdict."""
    report.step("a malformed correspondence is an error, not a result")
    bad = work_dir / "bad_correspondence.json"
    bad.write_text('{"version": "1.0", "registers": {}}', encoding="utf-8")
    command = [
        sys.executable,
        str(SEMANTIC_DIFF),
        "compare",
        str(FIXTURES / "decoder_base.v"),
        str(FIXTURES / "decoder_rewrite.v"),
        "--gate-library",
        str(GATE_LIBRARY),
        "--correspondence",
        str(bad),
        "-o",
        str(work_dir / "bad_out"),
    ]
    for entry in hal_libs:
        command += ["--hal-lib", entry]
    completed = subprocess.run(
        command, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    require(
        completed.returncode == 1,
        "a malformed correspondence exited with {}, expected 1 (a handled error). "
        "Exit code 3 would say 'the designs differ', which is a different claim.".format(
            completed.returncode
        ),
    )
    report.ok("exit code 1 with: {}".format(completed.stderr.decode("utf-8", "replace").strip()))


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------


def run(args, report):
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    ground_truth = json.loads((FIXTURES / "ground_truth.json").read_text(encoding="utf-8"))

    hal_py = import_hal(args.hal_lib, report)
    hal_libs = list(args.hal_lib)
    if not hal_libs:
        hal_file = getattr(hal_py, "__file__", None)
        if hal_file:
            hal_libs = [str(Path(hal_file).resolve().parent)]

    check_fixture_netlists(hal_py, ground_truth, report)

    runtimes = []
    cases = {case["name"]: case for case in ground_truth["cases"]}
    for case in ground_truth["cases"]:
        runtimes.append(check_case(case, work_dir, hal_libs, report))

    check_fail_on_inconclusive(cases["renamed_register_without_mapping"], work_dir, hal_libs, report)
    check_determinism(cases["changed_decoder"], work_dir, hal_libs, report)
    check_bad_correspondence(work_dir, hal_libs, report)

    runtimes_path = work_dir / "runtimes.json"
    runtimes_path.write_text(
        json.dumps({"cases": runtimes}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report.step("measured runtimes")
    for entry in runtimes:
        report.note(
            "{:<38} {:>6.2f} s total, {:>3} solver queries, {:>6.3f} s in the solver".format(
                entry["case"],
                entry["wall_time_s"],
                entry["solver_queries"],
                entry["solver_wall_time_s"],
            )
        )
    report.ok("wrote {}".format(runtimes_path))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Headless end-to-end smoke test for tools/hal_semantic_diff: parse the "
        "hand-written fixtures with hal_py, run every ground-truth comparison and check "
        "the verdicts, the counterexample and the produced evidence.",
    )
    parser.add_argument(
        "--hal-lib", action="append", default=[], metavar="DIR",
        help="directory containing hal_py (repeatable); PYTHONPATH works too",
    )
    parser.add_argument(
        "--work-dir", metavar="DIR",
        help="where to write output (default: a temporary directory)",
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep the work directory on success"
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))

    temporary = None
    if not args.work_dir:
        temporary = tempfile.mkdtemp(prefix="hal_semantic_diff_smoke_")
        args.work_dir = temporary

    report = Report()
    try:
        run(args, report)
    except SmokeError as exc:
        print("\nFAILED: {}".format(exc), file=sys.stderr)
        print("work directory kept at {}".format(args.work_dir), file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001
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
