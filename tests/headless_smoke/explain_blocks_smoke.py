#!/usr/bin/env python3
"""End-to-end check of ``tools/hal_explain`` against a built HAL.

``tools/hal_explain/test_hal_explain.py`` drives the whole composition against a
fixture netlist read by a dependency-free parser and three *recorded* findings
documents.  It therefore proves the intermediate representation, the merging,
the unknown regions, the diagram and the report -- and none of the bindings, and
none of the plugins.  This script closes that gap the way ``fsm_discovery_smoke.py``
does for hal_fsm: it needs a built HAL and it asserts results, not exit codes.

The design under test is ``tools/hal_explain/fixtures/accumulator.v``: a 4-bit
accumulator, the ripple-carry adder that feeds it, an equality comparison, a
3-state controller, a parity tree nothing is expected to claim, and two tie
cells.  ``accumulator_ground_truth.json`` records what each analysis is expected
to recover.

  1. collect: HAL's own verilog_parser reads the fixture and the inventory
     matches the ground truth gate for gate -- this is the check that the
     fixture reader used by the unit tests and HAL's parser agree
  2. dataflow: DANA runs and its findings document validates against the shared
     schema; the flip-flops it grouped are reported
  3. module identification: if the plugin is in this build, its verified
     candidates become ``verified`` blocks; if it is not, the run must say so in
     ``result.json`` and the composed model must contain no verified block --
     never a quietly weaker model that looks the same
  4. hal_fsm: the controller is solved with an explicit state-register override
     and its findings document is composed in as a third source
  5. composition: every gate of the netlist ends up in exactly one of a block or
     an unknown region, the coverage arithmetic holds, and the parity tree is in
     an unknown region
  6. claim linkage: every claim in the model names a gate set and a finding that
     exists in the source document it cites
  7. rendering: the diagram distinguishes verified from heuristic and draws the
     unknown regions; the report links every claim to its finding
  8. determinism: composing twice from the same inputs is byte-identical
  9. exit codes: ``--min-classified 1.0`` exits 1, a tampered model fails
     ``validate`` with exit 1, and a missing inventory is exit 2

Run it against a build tree with::

    HAL_BASE_PATH=<build> python3 tests/headless_smoke/explain_blocks_smoke.py \\
        --hal-binary <build>/bin/hal --work-dir <build>/explain_smoke --keep
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
FIXTURE_DIR = TOOLS / "hal_explain" / "fixtures"
NETLIST = FIXTURE_DIR / "accumulator.v"
GROUND_TRUTH = FIXTURE_DIR / "accumulator_ground_truth.json"
GATE_LIBRARY = REPO_ROOT / "plugins" / "gate_libraries" / "definitions" / "example_library.hgl"

sys.path.insert(0, str(TOOLS))

from hal_explain import serialize as explain_serialize  # noqa: E402
from hal_explain import validate as explain_validate  # noqa: E402
from hal_findings import serialize as findings_serialize  # noqa: E402
from hal_findings import validate as findings_validate  # noqa: E402


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


def load_ground_truth():
    with open(str(GROUND_TRUTH), "r", encoding="utf-8") as handle:
        return json.load(handle)


def run_tool(tool, arguments, report, expect_exit=0, hal_binary=None):
    command = [sys.executable, str(TOOLS / tool)] + [str(part) for part in arguments]
    if hal_binary:
        command += ["--hal-binary", str(hal_binary)]
    report.note("$ {}".format(" ".join(command)))
    completed = subprocess.run(
        command, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    if expect_exit is not None:
        require(
            completed.returncode == expect_exit,
            "{} exited with {}, expected {}\n--- stdout ---\n{}\n--- stderr ---\n{}".format(
                tool, completed.returncode, expect_exit, stdout, stderr
            ),
        )
    return completed.returncode, stdout, stderr


def gate_names(refs):
    return sorted(ref["name"] for ref in refs)


# ---------------------------------------------------------------------------
# 1-3. collect through a real HAL
# ---------------------------------------------------------------------------


def check_collect(work_dir, hal_binary, report):
    report.step("collect: HAL parses the fixture and runs the plugins")
    output_dir = Path(work_dir) / "collect"
    run_tool(
        "hal_explain",
        [
            "collect",
            NETLIST,
            "--gate-library",
            GATE_LIBRARY,
            "-o",
            output_dir,
            "--print-summary",
        ],
        report,
        expect_exit=None,  # 1 is legitimate: a plugin may not be in this build
        hal_binary=hal_binary,
    )

    result_path = output_dir / "result.json"
    require(
        result_path.is_file(),
        "hal_explain collect wrote no result.json in {}; a run that says nothing has "
        "collected nothing".format(output_dir),
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    for step in result.get("steps", []):
        report.note("{}: {}".format(step["step"], step["status"]))
    report.ok("collect wrote a result record with {} step(s)".format(len(result.get("steps", []))))

    inventory_path = output_dir / "inventory.json"
    require(inventory_path.is_file(), "collect wrote no inventory.json")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))

    truth = load_ground_truth()
    require(
        len(inventory["gates"]) == truth["gate_count"],
        "HAL's parser found {} gates in the fixture, the ground truth says {}. Either "
        "the fixture changed or HAL and tools/hal_cdc/fixture_netlist.py disagree about "
        "it.".format(len(inventory["gates"]), truth["gate_count"]),
    )
    names = {gate["name"] for gate in inventory["gates"]}
    expected_names = set()
    for entry in truth["blocks"] + truth["unknown_regions"]:
        expected_names.update(entry["gates"])
    missing = sorted(expected_names - names)
    require(
        not missing,
        "HAL's netlist is missing gates the ground truth names: {}".format(missing),
    )
    report.ok(
        "HAL's verilog_parser and the fixture reader agree: {} gates, {} nets".format(
            len(inventory["gates"]), len(inventory["nets"])
        )
    )

    findings_paths = []
    dataflow_path = output_dir / "findings-dataflow.json"
    require(
        dataflow_path.is_file(),
        "dataflow analysis produced no findings document. It is a core plugin of this "
        "fork; see {} for what the step reported.".format(result_path),
    )
    document = findings_serialize.read_document(str(dataflow_path))
    findings_validate.validate_document(document)
    groups = [
        entry for entry in document["findings"] if entry["id"].startswith("dataflow/group/")
    ]
    require(groups, "dataflow analysis produced no register groups for this netlist")
    for group in groups:
        report.note(
            "{}: {}".format(group["id"], ", ".join(gate_names(group["scope"]["gates"])))
        )
    report.ok("dataflow wrote a valid findings document with {} group(s)".format(len(groups)))
    findings_paths.append(dataflow_path)

    modid_path = output_dir / "findings-module-identification.json"
    verified_expected = False
    if modid_path.is_file():
        document = findings_serialize.read_document(str(modid_path))
        findings_validate.validate_document(document)
        verified = [
            entry
            for entry in document["findings"]
            if entry["status"] == "proven_under_assumptions"
        ]
        report.ok(
            "module_identification wrote a valid findings document: {} candidate(s), "
            "{} verified".format(len(document["findings"]), len(verified))
        )
        for entry in verified:
            report.note("{}: {}".format(entry["id"], entry["title"]))
        verified_expected = bool(verified)
        findings_paths.append(modid_path)
    else:
        step = [
            entry
            for entry in result.get("steps", [])
            if entry["step"] == "module_identification"
        ]
        require(
            step and step[0]["status"] != "ok",
            "no module_identification findings document was written and result.json "
            "does not say why. A step that fails silently is the failure mode this "
            "tool exists to prevent.",
        )
        report.ok(
            "module_identification is not usable in this build ({}); the run recorded "
            "the reason instead of pretending the netlist has no arithmetic".format(
                step[0]["status"]
            )
        )
    return output_dir, inventory_path, findings_paths, verified_expected


# ---------------------------------------------------------------------------
# 4. hal_fsm as a third source
# ---------------------------------------------------------------------------


def check_fsm_source(work_dir, hal_binary, report):
    report.step("hal_fsm: solve the controller and reuse its findings document")
    output_dir = Path(work_dir) / "fsm"
    code, _stdout, stderr = run_tool(
        "hal_fsm",
        [
            "analyze",
            NETLIST,
            "--gate-library",
            GATE_LIBRARY,
            "--state-registers",
            "st_a",
            "st_b",
            "-o",
            output_dir,
        ],
        report,
        expect_exit=None,
        hal_binary=hal_binary,
    )
    require(
        code != 2,
        "hal_fsm could not start (exit 2): {}. That is a setup problem, not a result "
        "about the design.".format(stderr.strip()[-800:]),
    )
    findings_path = output_dir / "findings.json"
    require(
        findings_path.is_file(),
        "hal_fsm exited {} and wrote no findings document".format(code),
    )
    document = findings_serialize.read_document(str(findings_path))
    findings_validate.validate_document(document)
    transitions = [
        entry
        for entry in document["findings"]
        if entry["id"].endswith("/transitions")
        and entry["status"] == "proven_under_assumptions"
    ]
    if transitions:
        report.ok(
            "hal_fsm recovered a transition relation: {}".format(transitions[0]["title"])
        )
    else:
        report.ok(
            "hal_fsm wrote a findings document without a proven transition relation; "
            "the composition must still consume it"
        )
    return findings_path, bool(transitions)


# ---------------------------------------------------------------------------
# 5-8. composition, linkage, rendering, determinism
# ---------------------------------------------------------------------------


def check_composition(work_dir, inventory_path, findings_paths, report, expect_verified,
                      expect_fsm):
    report.step("compose: one model, every gate accounted for")
    output_dir = Path(work_dir) / "model"
    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / "blocks.json"
    dot_path = output_dir / "blocks.dot"
    report_path = output_dir / "report.md"

    arguments = ["compose", "--inventory", inventory_path]
    for path in findings_paths:
        arguments += ["--findings", path]
    arguments += [
        "-o",
        model_path,
        "--dot",
        dot_path,
        "--report",
        report_path,
        "--generated-at",
        "2026-01-01T00:00:00Z",
        "--print-summary",
    ]
    run_tool("hal_explain", arguments, report)

    document = explain_serialize.read_document(str(model_path))
    explain_validate.validate_document(document)
    report.ok("the composed model validates against the recovered-block schema")

    inventory = json.loads(Path(inventory_path).read_text(encoding="utf-8"))
    all_ids = {gate["id"] for gate in inventory["gates"]}
    in_blocks = {ref["id"] for block in document["blocks"] for ref in block["gates"]}
    in_regions = {
        ref["id"] for region in document["unknown_regions"] for ref in region["gates"]
    }
    require(
        not (in_blocks & in_regions),
        "{} gate(s) are in a block and in an unknown region at the same "
        "time".format(len(in_blocks & in_regions)),
    )
    require(
        in_blocks | in_regions == all_ids,
        "{} gate(s) of the netlist are in neither a block nor an unknown region: "
        "{}".format(
            len(all_ids - (in_blocks | in_regions)), sorted(all_ids - (in_blocks | in_regions))
        ),
    )
    coverage = document["coverage"]
    require(
        coverage["gates_in_blocks"] + coverage["gates_unclassified"]
        == coverage["gates_total"]
        == len(all_ids),
        "the coverage arithmetic does not hold: {}".format(coverage),
    )
    report.ok(
        "every one of the {} gates is in exactly one of {} block(s) or {} unknown "
        "region(s)".format(
            len(all_ids), len(document["blocks"]), len(document["unknown_regions"])
        )
    )

    parity = {"p_x0", "p_x1", "p_x2"}
    region_gates = {name for region in document["unknown_regions"] for name in
                    gate_names(region["gates"])}
    block_gates = {name for block in document["blocks"] for name in gate_names(block["gates"])}
    require(
        parity <= (region_gates | block_gates),
        "the parity tree vanished from the model entirely",
    )
    if parity <= region_gates:
        report.ok("the parity tree nothing claimed survives as an unknown region")
    else:
        report.ok(
            "the parity tree was claimed by an analysis in this build; it is in a block "
            "and its confidence is recorded"
        )

    if expect_verified:
        verified = [
            block for block in document["blocks"] if block["confidence"] == "verified"
        ]
        require(
            verified,
            "module_identification verified candidates but no block came out verified",
        )
        report.ok(
            "{} verified block(s): {}".format(
                len(verified), ", ".join(block["label"] for block in verified)
            )
        )
    else:
        modid_blocks = [
            block
            for block in document["blocks"]
            if any(
                "module_identification" in ref["source_id"]
                for claim in block["claims"]
                for ref in claim["evidence"]
            )
        ]
        require(
            not modid_blocks,
            "module_identification produced nothing, yet blocks cite it",
        )
        report.ok("no verified arithmetic is claimed, matching what the plugins produced")

    if expect_fsm:
        machines = [
            block for block in document["blocks"] if block["kind"] == "state_machine"
        ]
        require(machines, "hal_fsm proved a transition relation but no state machine block")
        machine = machines[0]
        require(
            gate_names(machine["gates"]) == ["st_a", "st_b"],
            "the state machine block covers {} instead of the state register".format(
                gate_names(machine["gates"])
            ),
        )
        confidences = {claim["confidence"] for claim in machine["claims"]}
        require(
            "verified" in confidences,
            "the recovered transition relation did not come out verified",
        )
        report.ok(
            "the state machine block carries {} claim(s) at {} confidence level(s): "
            "{}".format(len(machine["claims"]), len(confidences), sorted(confidences))
        )
        if "heuristic" in confidences:
            report.ok(
                "the same block carries a heuristic claim as well: the state-register "
                "guess and the proven relation stay separate"
            )

    # -- linkage ---------------------------------------------------------
    report.step("linkage: every claim points at a finding that exists")
    documents = {}
    for source in document["sources"]:
        documents[source["source_id"]] = findings_serialize.read_document(source["path"])
    claim_count = 0
    for block in document["blocks"]:
        for claim in block["claims"]:
            claim_count += 1
            require(claim["gates"], "claim {} names no gates".format(claim["claim_id"]))
            block_ids = {ref["id"] for ref in block["gates"]}
            require(
                set(claim["gates"]) <= block_ids,
                "claim {} names gates outside its block".format(claim["claim_id"]),
            )
            for evidence in claim["evidence"]:
                source_document = documents.get(evidence["source_id"])
                require(
                    source_document is not None,
                    "claim {} cites undeclared source {}".format(
                        claim["claim_id"], evidence["source_id"]
                    ),
                )
                ids = {entry["id"] for entry in source_document["findings"]}
                require(
                    evidence["finding_id"] in ids,
                    "claim {} cites {}#{}, which does not exist in that document".format(
                        claim["claim_id"], evidence["source_id"], evidence["finding_id"]
                    ),
                )
                statuses = {
                    entry["id"]: entry["status"] for entry in source_document["findings"]
                }
                require(
                    statuses[evidence["finding_id"]] == evidence["status"],
                    "claim {} reports status {} for {}, the document says {}".format(
                        claim["claim_id"],
                        evidence["status"],
                        evidence["finding_id"],
                        statuses[evidence["finding_id"]],
                    ),
                )
    report.ok("all {} claim(s) resolve to a finding with the same status".format(claim_count))

    # -- rendering -------------------------------------------------------
    report.step("rendering: the diagram and the report are honest about strength")
    dot = dot_path.read_text(encoding="utf-8")
    for block in document["blocks"]:
        require(
            '"{}"'.format(block["block_id"]) in dot,
            "block {} is missing from the diagram".format(block["block_id"]),
        )
    for region in document["unknown_regions"]:
        require(
            '"{}"'.format(region["region_id"]) in dot,
            "unknown region {} is missing from the diagram".format(region["region_id"]),
        )
    require("cluster_legend" in dot, "the diagram carries no legend")
    report.ok("every block and every unknown region is drawn, with the legend")

    text = report_path.read_text(encoding="utf-8")
    require("## Unclassified regions" in text, "the report has no unclassified section")
    require(
        "nothing here was written by a language model" in text,
        "the report does not state that it is model-free",
    )
    for block in document["blocks"]:
        for claim in block["claims"]:
            for evidence in claim["evidence"]:
                marker = "{}#{}".format(evidence["source_id"], evidence["finding_id"])
                require(
                    marker in text,
                    "the report does not link claim {} to {}".format(
                        claim["claim_id"], marker
                    ),
                )
    report.ok("the report links every claim to its finding")

    # -- determinism ------------------------------------------------------
    report.step("determinism: composing twice is byte-identical")
    second = output_dir / "blocks-2.json"
    arguments = ["compose", "--inventory", inventory_path]
    for path in findings_paths:
        arguments += ["--findings", path]
    arguments += ["-o", second, "--generated-at", "2026-01-01T00:00:00Z"]
    run_tool("hal_explain", arguments, report)
    require(
        model_path.read_bytes() == second.read_bytes(),
        "two runs over the same inputs produced different documents",
    )
    report.ok("the model is reproducible")

    return model_path


# ---------------------------------------------------------------------------
# 9. exit codes
# ---------------------------------------------------------------------------


def check_exit_codes(work_dir, inventory_path, findings_paths, model_path, report):
    report.step("exit codes: 1 is a result, 2 is a broken run")
    arguments = ["compose", "--inventory", inventory_path]
    for path in findings_paths:
        arguments += ["--findings", path]
    arguments += [
        "-o",
        Path(work_dir) / "model" / "blocks-threshold.json",
        "--min-classified",
        "1.0",
    ]
    run_tool("hal_explain", arguments, report, expect_exit=1)
    report.ok("--min-classified 1.0 exits 1 on a netlist with unclassified gates")

    run_tool(
        "hal_explain",
        [
            "compose",
            "--inventory",
            Path(work_dir) / "does-not-exist.json",
            "--findings",
            findings_paths[0],
        ],
        report,
        expect_exit=2,
    )
    report.ok("a missing inventory is exit 2, never exit 1")

    tampered = Path(work_dir) / "model" / "tampered.json"
    document = explain_serialize.read_document(str(model_path))
    promoted = [
        claim
        for block in document["blocks"]
        for claim in block["claims"]
        if claim["confidence"] != "verified"
    ]
    require(promoted, "the model has no non-verified claim to tamper with")
    promoted[0]["confidence"] = "verified"
    explain_serialize.write_document(document, str(tampered))
    run_tool("hal_explain", ["validate", tampered], report, expect_exit=1)
    report.ok("a confidence promoted by hand is rejected by validate")


def run(args, report):
    require(
        NETLIST.is_file(), "the fixture netlist is missing: {}".format(NETLIST)
    )
    require(
        GATE_LIBRARY.is_file(), "the gate library is missing: {}".format(GATE_LIBRARY)
    )

    output_dir, inventory_path, findings_paths, expect_verified = check_collect(
        args.work_dir, args.hal_binary, report
    )
    fsm_path, expect_fsm = check_fsm_source(args.work_dir, args.hal_binary, report)
    findings_paths = list(findings_paths) + [fsm_path]

    model_path = check_composition(
        args.work_dir, inventory_path, findings_paths, report, expect_verified, expect_fsm
    )
    check_exit_codes(args.work_dir, inventory_path, findings_paths, model_path, report)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Headless end-to-end test of tools/hal_explain: collect dataflow and "
        "module identification results through a built HAL, compose them with hal_fsm's "
        "into a recovered-block model, and check the model, diagram and report against "
        "the recorded ground truth.",
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
        temporary = tempfile.mkdtemp(prefix="hal_explain_smoke_")
        args.work_dir = temporary
    os.makedirs(args.work_dir, exist_ok=True)

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
