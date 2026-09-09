#!/usr/bin/env python3
"""Every tool CLI that loads a netlist in-process must load HAL's plugins itself.

HAL's gate-library and netlist parsers are *plugins*. A process that calls
``hal_py.NetlistFactory.load_netlist`` without ``plugin_manager.load_all_plugins()`` gets ``None``
back and 'no gate library parser registered for file extension .hgl' in the log. That bug was found
twice (tools/hal_migration, tools/hal_agilex) before it was fixed centrally: ``hal_viz.halenv``'s
``load_netlist``/``load_hal_project`` now load the plugins themselves, and the three loaders that
talk to ``hal_py.NetlistFactory`` outside halenv (hal_agilex.hal_adapter, hal_apb_check.netlist,
hal_apb_recover.hal_source) do the same.

A unit test can only prove that against a stub (tools/hal_viz/test_halenv.py does). This script
proves it against a real build, the way the bug actually appeared: it runs **one real CLI command
per tool** in a *fresh* interpreter -- no plugin was loaded for it, nothing is imported first --
and checks that the command parsed the netlist, by asserting on what it wrote rather than on its
exit code alone.

Tools whose CLI never loads a netlist in-process (hal_analysis_api, hal_fault_campaign, hal_fsm,
hal_runner, hal_explain's 'collect', hal_findings) run their analyses inside ``hal --python-script``
subprocesses; those in-HAL scripts load the plugins themselves and are covered end to end by the
other smoke tests in this directory. They are listed in TOOLS_WITHOUT_IN_PROCESS_LOAD, and
hal_bitstream -- whose load needs a bitstream and the IceStorm toolchain -- in
TOOLS_COVERED_ELSEWHERE, so that this file stays an audit of *all* tools and not just of the ones
it can run: :func:`audit_tool_coverage` reads ``tools/*/cli.py`` from the tree and fails when a
tool appears in none of the three lists.

Run it against a build tree with::

    HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib \\
        python3 tests/headless_smoke/tool_cli_plugin_load_smoke.py --work-dir <build>/tool_cli_smoke

Add ``--keep`` to leave every generated file behind, and ``--only <tool>`` (repeatable) to run a
single tool while debugging.
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
# The checks below read the produced documents with the tools' own readers (hal_findings), so this
# process needs tools/ on its path too, not only the subprocesses it starts.
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
LIBRARIES = REPO_ROOT / "plugins" / "gate_libraries" / "definitions"

EXAMPLE_LIBRARY = LIBRARIES / "example_library.hgl"
NANGATE_LIBRARY = LIBRARIES / "NangateOpenCellLibrary.hgl"
AGILEX_LIBRARY = LIBRARIES / "AGILEX_TENNM.hgl"

ACCUMULATOR = TOOLS / "hal_explain" / "fixtures" / "accumulator.v"
APB_COMPLETER = TOOLS / "hal_apb_check" / "fixtures" / "apb_completer_ok.v"
APB_COMPLETER_MAP = TOOLS / "hal_apb_check" / "fixtures" / "apb_completer_ok.map.json"
APB_REGS = TOOLS / "hal_apb_recover" / "fixtures" / "apb_regs" / "apb_regs.v"
APB_REGS_MAPPING = TOOLS / "hal_apb_recover" / "fixtures" / "apb_regs" / "mapping.json"
SECREG = TOOLS / "hal_secprop" / "fixtures" / "secreg_ok.v"
SECREG_POLICY = TOOLS / "hal_secprop" / "fixtures" / "secreg_ok.policy.json"
AGILEX_NETLIST = TOOLS / "hal_agilex" / "fixtures" / "agilex3_lut_logic" / "lut_logic.hal.v"

#: Tools that never call ``hal_py.NetlistFactory`` in the CLI process; see the module docstring.
TOOLS_WITHOUT_IN_PROCESS_LOAD = (
    "hal_analysis_api",
    "hal_fault_campaign",
    "hal_findings",
    "hal_fsm",
    "hal_runner",
)

#: Tools whose direct-CLI load is exercised end to end by another script in this directory, and
#: what exercises it. Listing them here (rather than leaving them out) is what makes the coverage
#: check below meaningful.
TOOLS_COVERED_ELSEWHERE = {
    "hal_bitstream": "tests/headless_smoke/bitstream_smoke.py",
}


class SmokeError(RuntimeError):
    """A check failed, or a prerequisite for one is missing."""


def log(message):
    print(message, flush=True)


# ---------------------------------------------------------------------------
# checks on what each command produced
# ---------------------------------------------------------------------------


def read_json(path):
    with open(str(path), "r", encoding="utf-8") as handle:
        return json.load(handle)


def check_dot(path, minimum_nodes=4):
    text = Path(path).read_text(encoding="utf-8")
    if "digraph" not in text:
        raise SmokeError("{} is not a Graphviz graph".format(path))
    nodes = [line for line in text.splitlines() if "label=" in line]
    if len(nodes) < minimum_nodes:
        raise SmokeError(
            "{} has {} labelled elements, expected at least {}; the netlist was not "
            "parsed".format(path, len(nodes), minimum_nodes)
        )


def check_gate_list(path, minimum_gates=10):
    """hal_explain's inventory: a list of gates, one entry per gate in the netlist."""
    gates = read_json(path).get("gates")
    if not isinstance(gates, list) or len(gates) < minimum_gates:
        raise SmokeError(
            "{} lists {} gates, expected a list of at least {}; the netlist was not "
            "parsed".format(path, len(gates) if isinstance(gates, list) else gates, minimum_gates)
        )


def check_totals_gates(path, minimum_gates=10):
    """hal_migration's inventory: counts under 'totals'."""
    totals = read_json(path).get("totals") or {}
    if totals.get("gates", 0) < minimum_gates:
        raise SmokeError(
            "{} counted {} gates, expected at least {}; the netlist was not parsed".format(
                path, totals.get("gates"), minimum_gates
            )
        )


def check_findings(path):
    """A schema-valid findings document that names the design it was derived from."""
    from hal_findings import serialize as findings_serialize
    from hal_findings import validate as findings_validate

    if not os.path.isfile(str(path)):
        raise SmokeError("no findings document was written to {}".format(path))
    document = findings_serialize.read_document(str(path))
    findings_validate.validate_document(document)
    if not document.get("artifacts"):
        raise SmokeError("{} names no artifact, so no design was read".format(path))


def check_stdout_mentions_gates(result):
    if "gate" not in result.stdout.lower():
        raise SmokeError(
            "the command said nothing about gates, so it did not profile the netlist:\n"
            + result.stdout
        )


# ---------------------------------------------------------------------------
# the cases -- one direct-CLI netlist load per tool
# ---------------------------------------------------------------------------


def build_cases(work_dir):
    """Return ``[(tool, argv, verify)]``; ``verify`` gets the finished CompletedProcess."""

    def out(name):
        return str(work_dir / name)

    cases = [
        (
            "hal_viz",
            [
                "-m", "hal_viz", "netlist_graph", str(ACCUMULATOR),
                "--gate-library", str(EXAMPLE_LIBRARY),
                "--output", out("viz_graph"), "--format", "none",
            ],
            lambda result: check_dot(out("viz_graph.dot")),
        ),
        (
            "hal_explain",
            [
                "-m", "hal_explain", "inventory", str(ACCUMULATOR),
                "--gate-library", str(EXAMPLE_LIBRARY),
                "-o", out("explain_inventory.json"),
            ],
            lambda result: check_gate_list(out("explain_inventory.json")),
        ),
        (
            "hal_migration",
            [
                "-m", "hal_migration", "inventory", str(ACCUMULATOR),
                "--gate-library", str(EXAMPLE_LIBRARY),
                "-o", out("migration_inventory.json"),
            ],
            lambda result: check_totals_gates(out("migration_inventory.json")),
        ),
        (
            "hal_cdc",
            [
                "-m", "hal_cdc", "discover", str(ACCUMULATOR),
                "--gate-library", str(EXAMPLE_LIBRARY),
                "-o", out("cdc_declarations.json"),
            ],
            lambda result: check_cdc_skeleton(out("cdc_declarations.json")),
        ),
        (
            "hal_capabilities",
            [
                "-m", "hal_capabilities", "list",
                "--netlist", str(ACCUMULATOR),
                "--gate-library", str(EXAMPLE_LIBRARY),
            ],
            check_stdout_mentions_gates,
        ),
        (
            "hal_agilex",
            [
                "-m", "hal_agilex", "elaborate", str(AGILEX_NETLIST),
                "--gate-library", str(AGILEX_LIBRARY),
                "-o", out("agilex_findings.json"),
            ],
            lambda result: check_findings(out("agilex_findings.json")),
        ),
        (
            "hal_apb_check",
            [
                "-m", "hal_apb_check", "check", str(APB_COMPLETER_MAP),
                "--netlist", str(APB_COMPLETER),
                "--gate-library", str(EXAMPLE_LIBRARY),
                "--bound", "2", "--no-evidence",
                "-o", out("apb_check_findings.json"),
            ],
            lambda result: check_findings(out("apb_check_findings.json")),
        ),
        (
            "hal_apb_recover",
            [
                "-m", "hal_apb_recover", "recover", str(APB_REGS), str(APB_REGS_MAPPING),
                "--source", "hal", "--gate-library", str(NANGATE_LIBRARY),
                "-o", out("apb_register_map"),
            ],
            lambda result: check_register_map(out("apb_register_map.json")),
        ),
        (
            "hal_secprop",
            [
                "-m", "hal_secprop", "check", str(SECREG_POLICY),
                "--source", "hal", "--netlist", str(SECREG),
                "--gate-library", str(NANGATE_LIBRARY),
                "--bound", "2", "--no-evidence",
                "-o", out("secprop_findings.json"),
            ],
            lambda result: check_findings(out("secprop_findings.json")),
        ),
        (
            "hal_semantic_diff",
            [
                "-m", "hal_semantic_diff", "compare", str(ACCUMULATOR), str(ACCUMULATOR),
                "--auto-correspondence", "--gate-library", str(EXAMPLE_LIBRARY),
                "-o", out("semantic_diff"), "--no-html", "--diagrams", "none",
                "--no-witness",
            ],
            lambda result: check_findings(str(Path(out("semantic_diff")) / "findings.json")),
        ),
    ]
    return cases


def check_cdc_skeleton(path):
    document = read_json(path)
    clocks = document.get("clocks") or document.get("clock_domains") or []
    if not clocks:
        raise SmokeError(
            "{} declares no clock, so the netlist was not parsed: {}".format(path, document)
        )


def check_register_map(path):
    document = read_json(path)
    registers = document.get("registers") or []
    if not registers:
        raise SmokeError("{} recovered no register, so the netlist was not read".format(path))


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def run_case(tool, argv, work_dir, hal_lib, timeout):
    environment = dict(os.environ)
    python_path = [str(TOOLS)]
    if hal_lib:
        python_path.append(str(hal_lib))
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    if hal_lib:
        environment["HAL_PY_PATH"] = str(hal_lib)
    # Deliberately nothing else: the point of this test is that the tool loads HAL's plugins by
    # itself, in a process where nothing has loaded them for it.
    command = [sys.executable] + argv
    log("  $ " + " ".join(command))
    return subprocess.run(
        command,
        cwd=str(work_dir),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        timeout=timeout,
    )


def audit_tool_coverage(cases):
    """Every tool CLI in the checkout must be accounted for, or this is not an audit.

    A new ``tools/<name>/cli.py`` that loads a netlist and forgets the plugins is exactly the bug
    this file exists for, and it would be invisible if the file only ran the cases somebody
    remembered to add. So the set of tool packages is read from the tree and compared against the
    three lists above.
    """
    packages = {path.parent.name for path in TOOLS.glob("*/cli.py")}
    exercised = {tool for tool, _, _ in cases}
    accounted = exercised | set(TOOLS_WITHOUT_IN_PROCESS_LOAD) | set(TOOLS_COVERED_ELSEWHERE)

    unaccounted = sorted(packages - accounted)
    if unaccounted:
        raise SmokeError(
            "these tool CLIs are in the checkout but in none of this file's lists: {}. Add a case "
            "that runs one real netlist load through each of them, or -- if the CLI never calls "
            "hal_py.NetlistFactory in its own process -- add it to "
            "TOOLS_WITHOUT_IN_PROCESS_LOAD with a reason.".format(", ".join(unaccounted))
        )

    stale = sorted(accounted - packages)
    if stale:
        raise SmokeError(
            "these tools are listed here but no longer exist: {}".format(", ".join(stale))
        )
    return sorted(packages)


def missing_inputs(argv, work_dir):
    """Fixture files this case needs that are not in the checkout.

    Only *inputs* count: a path under the work directory is something the command is about to
    write, and demanding that it already exists would fail every case.
    """
    missing = []
    work_prefix = str(work_dir) + os.sep
    for entry in argv:
        if not (entry.endswith((".v", ".hgl", ".json")) and os.path.isabs(entry)):
            continue
        if entry == str(work_dir) or entry.startswith(work_prefix):
            continue
        if entry.startswith(str(REPO_ROOT)) and not os.path.exists(entry):
            missing.append(entry)
    return missing


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hal-lib", help="directory containing hal_py (default: $HAL_PY_PATH)")
    parser.add_argument("--work-dir", help="where generated files go (default: a temp dir)")
    parser.add_argument("--keep", action="store_true", help="keep the work directory")
    parser.add_argument(
        "--only", action="append", default=[], metavar="TOOL", help="run only this tool"
    )
    parser.add_argument(
        "--timeout", type=int, default=900, help="per-command timeout in seconds (default: 900)"
    )
    args = parser.parse_args(argv)

    hal_lib = args.hal_lib or os.environ.get("HAL_PY_PATH", "").split(os.pathsep)[0]
    if not hal_lib:
        raise SmokeError(
            "no HAL library directory: pass --hal-lib <build>/lib or export HAL_PY_PATH. This "
            "test needs a built HAL -- it is about HAL's plugins."
        )
    if not os.path.isdir(hal_lib):
        raise SmokeError("--hal-lib {} is not a directory".format(hal_lib))

    if args.work_dir:
        work_dir = Path(args.work_dir).resolve()
        if work_dir.exists():
            shutil.rmtree(str(work_dir))
        work_dir.mkdir(parents=True)
        keep = True
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="hal_tool_cli_smoke_"))
        keep = args.keep

    log("work directory: {}".format(work_dir))
    log("HAL library:    {}".format(hal_lib))

    cases = build_cases(work_dir)
    packages = audit_tool_coverage(cases)
    log("tool CLIs in the checkout:  {}".format(", ".join(packages)))

    failures = []
    ran = []
    try:
        for tool, case_argv, verify in cases:
            if args.only and tool not in args.only:
                continue
            log("")
            log("== {} ==".format(tool))
            absent = missing_inputs(case_argv, work_dir)
            if absent:
                failures.append((tool, "missing fixture(s): {}".format(", ".join(absent))))
                continue
            try:
                result = run_case(tool, case_argv, work_dir, hal_lib, args.timeout)
            except subprocess.TimeoutExpired:
                failures.append((tool, "timed out after {}s".format(args.timeout)))
                continue
            ran.append(tool)
            if result.returncode != 0:
                failures.append(
                    (tool, "exited {}:\n{}".format(result.returncode, result.stdout.strip()))
                )
                continue
            if "no gate library parser registered" in result.stdout:
                failures.append(
                    (
                        tool,
                        "HAL reported a missing parser plugin, so the CLI loaded a netlist "
                        "without loading HAL's plugins:\n" + result.stdout.strip(),
                    )
                )
                continue
            try:
                verify(result)
            except Exception as error:  # a broken check is a failure of this tool, not of the run
                failures.append(
                    (
                        tool,
                        "{}: {}\n{}".format(
                            type(error).__name__, error, result.stdout.strip()
                        ),
                    )
                )
                continue
            log("  ok")
    finally:
        if not keep and work_dir.exists():
            shutil.rmtree(str(work_dir), ignore_errors=True)

    log("")
    log("tools exercised:            {}".format(", ".join(ran) or "none"))
    log("tools without such a path:  {}".format(", ".join(TOOLS_WITHOUT_IN_PROCESS_LOAD)))
    log(
        "tools covered elsewhere:    {}".format(
            ", ".join("{} ({})".format(k, v) for k, v in sorted(TOOLS_COVERED_ELSEWHERE.items()))
        )
    )
    if failures:
        log("")
        for tool, message in failures:
            log("FAILED {}: {}".format(tool, message))
        return 1
    log("")
    log("all direct-CLI netlist loads worked without an externally loaded plugin set")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeError as error:
        print("error: {}".format(error), file=sys.stderr)
        sys.exit(2)
