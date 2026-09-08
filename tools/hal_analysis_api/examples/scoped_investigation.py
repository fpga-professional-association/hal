#!/usr/bin/env python3
"""One scoped investigation, exactly as an agent would run it.

This is the worked example behind ``tools/hal_analysis_api``: eleven tool calls
that go from "I have a netlist" to "here is a finding, with the evidence it
rests on", without ever dumping the netlist.

    export HAL_BASE_PATH=/path/to/hal/build
    python3 tools/hal_analysis_api/examples/scoped_investigation.py \\
        --hal-binary /path/to/hal/build/bin/hal

It drives :class:`hal_analysis_api.api.AnalysisApi` in process.  Over MCP the
call sequence is identical -- the adapter forwards the same names, the same
JSON and the same envelopes -- which is the point of defining the local API
first.

What the sequence demonstrates, in order:

1.  ``hal.capabilities``  -- what this build can do, and whether HAL was found.
2.  ``project.open``      -- an explicit handle, pinned to the archive's sha256.
3.  ``netlist.summary``   -- 407 gates, and a gate-type histogram that says
                             this is a flip-flop-heavy design.
4.  ``netlist.gates``     -- one *page* of the 258 flip-flops, not all of them.
5.  ``netlist.gate``      -- one gate: what drives each pin, what each pin
                             drives.
6.  ``netlist.cone``      -- the bounded fan-in cone around that flip-flop:
                             the scope of the investigation.
7.  ``netlist.cone``      -- the same question with a small budget, to show
                             what a truncated answer looks like.
8.  ``analysis.submit``   -- run the deterministic component analysis over the
                             whole design through hal_runner.
9.  ``analysis.status``   -- wait, bounded, and see the state.
10. ``findings.get``      -- the findings, schema-validated, with the artifact
                             they are scoped to.
11. ``artifact.list``     -- everything the job wrote, including the manifest.

Exit code 0 when every step answered, 1 when any of them returned an error
envelope: the sequence is also a usable end-to-end check.
"""

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

from hal_analysis_api.api import AnalysisApi  # noqa: E402


class Session(object):
    """A tiny wrapper that prints every call the way an agent would see it."""

    def __init__(self, api, verbose=True):
        self.api = api
        self.verbose = verbose
        self.failures = 0

    def call(self, tool, request, expect_ok=True):
        envelope = self.api.call(tool, request)
        if self.verbose:
            sys.stdout.write(
                "\n$ {} {}\n".format(tool, json.dumps(request, sort_keys=True))
            )
        if envelope["ok"]:
            return envelope["result"]
        if expect_ok:
            self.failures += 1
            sys.stdout.write(
                "  ERROR {}: {}\n".format(
                    envelope["error"]["code"], envelope["error"]["message"]
                )
            )
            if envelope["error"].get("detail"):
                sys.stdout.write("  {}\n".format(envelope["error"]["detail"][:500]))
        return None

    def show(self, text):
        if self.verbose:
            sys.stdout.write("  {}\n".format(text))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project",
        default=os.path.join(REPO_ROOT, "examples", "uart.zip"),
        help="the netlist or project to investigate (default: examples/uart.zip)",
    )
    parser.add_argument("--gate-library", help="required for a plain HDL netlist")
    parser.add_argument("--hal-binary", help="path to the hal executable")
    parser.add_argument("--workspace", help="where handles and jobs are kept")
    parser.add_argument(
        "--analysis-timeout", type=int, default=600, help="limit for the analysis step"
    )
    parser.add_argument(
        "--wait", type=float, default=300.0, help="how long to wait for the analysis"
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    api = AnalysisApi(workspace=args.workspace, hal_binary=args.hal_binary)
    session = Session(api, verbose=not args.quiet)

    # 1 -- what can this build do?
    capabilities = session.call("hal.capabilities", {})
    if capabilities is None:
        return 1
    session.show(
        "{} tools, {} analyses, hal {}".format(
            len(capabilities["tools"]),
            len(capabilities["analyses"]),
            capabilities["hal"]["binary"] if capabilities["hal"]["available"] else "NOT FOUND",
        )
    )
    if not capabilities["hal"]["available"]:
        sys.stdout.write(
            "\nNo hal binary: the netlist tools cannot run.\n  {}\n".format(
                capabilities["hal"]["reason"]
            )
        )
        return 1

    # 2 -- an explicit, content-pinned handle
    request = {"path": args.project}
    if args.gate_library:
        request["gate_library"] = args.gate_library
    opened = session.call("project.open", request)
    if opened is None:
        return 1
    project = opened["project"]
    session.show(
        "{} -> {} ({} {})".format(
            os.path.basename(args.project),
            project,
            opened["info"]["digest"]["algorithm"],
            opened["info"]["digest"]["value"][:16],
        )
    )

    # 3 -- what kind of design is this?
    summary = session.call("netlist.summary", {"project": project})
    if summary is None:
        return 1
    session.show(
        "{}: {} gates, {} nets, {} modules".format(
            summary["design_name"],
            summary["counts"]["gates"],
            summary["counts"]["nets"],
            summary["counts"]["modules"],
        )
    )
    session.show(
        "top gate types: "
        + ", ".join(
            "{}x{}".format(entry["count"], entry["type"]) for entry in summary["gate_types"][:5]
        )
    )

    # 4 -- one page of the sequential elements, never all of them
    sequential_type = None
    for entry in summary["gate_types"]:
        if entry["type"].startswith("FF"):
            sequential_type = entry["type"]
            break
    listing = session.call(
        "netlist.gates",
        {"project": project, "gate_type": sequential_type, "limit": 5},
    )
    if listing is None:
        return 1
    session.show(
        "{} gates of type {}; showing {} (has_more={})".format(
            listing["page"]["total"],
            sequential_type,
            listing["page"]["returned"],
            listing["page"]["has_more"],
        )
    )
    if not listing["gates"]:
        sys.stdout.write("no sequential gates found; nothing to scope around\n")
        return 1
    seed = listing["gates"][0]
    session.show("seed gate: id {} name {!r}".format(seed["id"], seed["name"]))

    # 5 -- one gate in detail
    detail = session.call("netlist.gate", {"project": project, "gate_id": seed["id"]})
    if detail is None:
        return 1
    session.show(
        "pins: "
        + ", ".join(
            "{}<-{}".format(entry["pin"], entry["gate"]["name"] if entry["gate"] else "(open)")
            for entry in detail["gate"]["fan_in"][:4]
        )
    )

    # 6 -- the scope of the investigation
    cone = session.call(
        "netlist.cone",
        {
            "project": project,
            "seed_gate_ids": [seed["id"]],
            "direction": "fan_in",
            "depth": 2,
            "max_gates": 60,
        },
    )
    if cone is None:
        return 1
    session.show(
        "fan-in cone depth 2: {} gates, {} edges, truncated={}".format(
            cone["counts"]["gates"], cone["counts"]["edges"], cone["truncation"]["truncated"]
        )
    )
    if cone["truncation"]["truncated"]:
        session.show("why: " + cone["truncation"]["reason"])

    # 7 -- what a budget-limited answer looks like
    cut = session.call(
        "netlist.cone",
        {
            "project": project,
            "seed_gate_ids": [seed["id"]],
            "direction": "both",
            "depth": 8,
            "max_gates": 5,
            "include_edges": False,
        },
    )
    if cut is None:
        return 1
    session.show(
        "budget 5: {} gates, truncated={} ({})".format(
            cut["counts"]["gates"], cut["truncation"]["truncated"], cut["truncation"]["kind"]
        )
    )

    # 8 -- an analysis, submitted rather than blocked on
    submitted = session.call(
        "analysis.submit",
        {
            "project": project,
            "analysis": "graph_algorithm.connected_components",
            "config": {"strong": True, "min_size": 2},
            "timeout_s": args.analysis_timeout,
            "label": "feedback loops",
        },
    )
    if submitted is None:
        return 1
    job = submitted["job"]
    session.show("job {} is {}".format(job, submitted["status"]["state"]))

    # 9 -- bounded waiting; an expired wait is an answer, not a failure
    status = None
    waited = 0.0
    while waited < args.wait:
        step = min(30.0, args.wait - waited)
        result = session.call("analysis.status", {"job": job, "wait_s": step})
        if result is None:
            return 1
        status = result["status"]
        waited += step
        if status["terminal"]:
            break
        session.show("still {} after {:.0f}s".format(status["state"], waited))
    if status is None or not status["terminal"]:
        sys.stdout.write("the analysis did not finish within --wait\n")
        return 1
    session.show(
        "job {} in {}s (step: {})".format(
            status["state"], status["duration_s"], status["step_status"]
        )
    )

    # 10 -- the findings, and what they are evidence about
    findings = session.call("findings.get", {"job": job, "limit": 3})
    if findings is None:
        return 1
    session.show(
        "{} findings ({}), schema {}".format(
            findings["page"]["total"],
            ", ".join(
                "{} {}".format(count, status_name)
                for status_name, count in sorted(findings["counts"].items())
            ),
            findings["schema_version"],
        )
    )
    for finding in findings["findings"]:
        scope = finding.get("scope", {})
        session.show(
            "- [{}] {} (evidence: {} gate refs in {})".format(
                finding["status"],
                finding["title"],
                len(scope.get("gates", [])),
                ", ".join(scope.get("artifact_ids", [])),
            )
        )
    for artifact in findings["artifacts"]:
        session.show(
            "  artifact {} pinned as sha256 {}".format(
                artifact["artifact_id"], (artifact.get("sha256") or "(none)")[:16]
            )
        )

    # 11 -- and everything the job left behind
    artifacts = session.call("artifact.list", {"job": job})
    if artifacts is None:
        return 1
    session.show(
        "{} artifacts, e.g. {}".format(
            len(artifacts["artifacts"]),
            ", ".join(entry["path"] for entry in artifacts["artifacts"][:4]),
        )
    )

    sys.stdout.write(
        "\n{} tool calls, {} failed. Job directory: {}\n".format(
            11, session.failures, artifacts["job_dir"]
        )
    )
    return 1 if session.failures else 0


if __name__ == "__main__":
    sys.exit(main())
