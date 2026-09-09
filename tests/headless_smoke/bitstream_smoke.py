#!/usr/bin/env python3
"""End-to-end check of ``tools/hal_bitstream``: a real bitstream, the real converters, real HAL.

``tools/hal_bitstream/test_hal_bitstream.py`` drives the whole tool with *stub* converters, so it
proves the registry, the detection, the chain planning, the error messages and the manifest -- and
nothing about IceStorm and nothing about ``hal_py``. This script closes that gap the way the other
smoke tests in this directory do: it needs the real toolchain and a built HAL, and it asserts
results rather than exit codes.

  1. the shipped fixture bitstream (``tools/hal_bitstream/fixtures/ice40_blinky.bin``, produced by
     yosys -> nextpnr-ice40 -> icepack; see fixtures/README.md) is detected as iCE40 from its sync
     word, not from its extension
  2. ``hal_bitstream load`` runs the family's whole chain over it -- IceStorm's ``iceunpack`` and
     ``icebox_vlog``, then ``yosys -p synth_ice40`` to turn icebox_vlog's behavioural Verilog into
     the cell netlist HAL's parser reads -- and loads the result into HAL with the gate library the
     registry names (``ice40ultra.hgl``); the netlist has gates and every gate type is an iCE40
     primitive
  3. the design's flip-flops and LUTs are there: a bitstream that converted into an empty or
     LUT-less netlist is a failure, not a small design
  4. the run is reproducible from the manifest: bitstream sha256, converter paths and the literal
     command lines are recorded
  5. the netlist round-trips through a HAL project directory, which is what hands it to the rest
     of the tool family
  6. with the converter hidden, the same command fails with exit code 2 and an error naming
     ``iceunpack`` and IceStorm -- the 'never silent' half of the feature

Prerequisites: IceStorm and yosys (Debian/Ubuntu: ``apt-get install fpga-icestorm yosys``) and a
built HAL. All are required, not optional: a smoke test that turns itself off proves nothing. Use
``--rebuild-fixture`` (additionally needs nextpnr-ice40) to regenerate the bitstream from
``fixtures/blinky.v`` before running, which is how the checked-in fixture was made.

Run it against a build tree with::

    HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib \\
        python3 tests/headless_smoke/bitstream_smoke.py --work-dir <build>/bitstream_smoke --keep
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
FIXTURES = TOOLS / "hal_bitstream" / "fixtures"
BITSTREAM = FIXTURES / "ice40_blinky.bin"
SOURCE = FIXTURES / "blinky.v"
GATE_LIBRARY = REPO_ROOT / "plugins" / "gate_libraries" / "definitions" / "ice40ultra.hgl"

sys.path.insert(0, str(TOOLS))


class SmokeError(RuntimeError):
    """A check failed, or a prerequisite for one is missing."""


class Report(object):
    def __init__(self):
        self.checks = 0

    def ok(self, message):
        self.checks += 1
        print("  ok  {}".format(message), flush=True)

    def note(self, message):
        print("      {}".format(message), flush=True)

    def step(self, message):
        print("\n== {} ==".format(message), flush=True)


def require(condition, message):
    if not condition:
        raise SmokeError(message)


def require_tool(name, hint):
    path = shutil.which(name)
    require(
        path,
        "{} is not on PATH. {}\nThis test is about the real converters; it does not stub "
        "them.".format(name, hint),
    )
    return path


def run(command, cwd, env=None, timeout=1800):
    environment = dict(os.environ)
    environment.update(env or {})
    return subprocess.run(
        [str(part) for part in command],
        cwd=str(cwd),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        timeout=timeout,
    )


def tool_env(hal_lib):
    return {
        "PYTHONPATH": os.pathsep.join(
            [str(TOOLS), str(hal_lib), os.environ.get("PYTHONPATH", "")]
        ).strip(os.pathsep),
        "HAL_PY_PATH": str(hal_lib),
    }


# ---------------------------------------------------------------------------
# fixture generation (how the checked-in bitstream was produced)
# ---------------------------------------------------------------------------


def rebuild_fixture(work_dir, report):
    report.step("rebuilding the fixture bitstream with the open toolchain")
    yosys = require_tool("yosys", "Debian/Ubuntu: 'apt-get install yosys'.")
    nextpnr = require_tool("nextpnr-ice40", "Debian/Ubuntu: 'apt-get install nextpnr-ice40'.")
    icepack = require_tool("icepack", "Debian/Ubuntu: 'apt-get install fpga-icestorm'.")

    json_path = work_dir / "blinky.json"
    asc_path = work_dir / "blinky.asc"
    result = run([yosys, "-p", "synth_ice40 -top blinky -json {}".format(json_path), str(SOURCE)], work_dir)
    require(result.returncode == 0, "yosys failed:\n{}".format(result.stdout))
    result = run(
        [nextpnr, "--up5k", "--package", "sg48", "--json", json_path, "--asc", asc_path,
         "--pcf-allow-unconstrained", "--seed", "1"],
        work_dir,
    )
    require(result.returncode == 0, "nextpnr-ice40 failed:\n{}".format(result.stdout))
    result = run([icepack, asc_path, str(BITSTREAM)], work_dir)
    require(result.returncode == 0, "icepack failed:\n{}".format(result.stdout))
    report.ok("wrote {} ({} bytes)".format(BITSTREAM, BITSTREAM.stat().st_size))


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def check_detection(work_dir, hal_lib, report):
    report.step("detection")
    result = run(
        [sys.executable, "-m", "hal_bitstream", "detect", str(BITSTREAM), "--json"],
        work_dir,
        tool_env(hal_lib),
    )
    require(result.returncode == 0, "hal_bitstream detect failed:\n{}".format(result.stdout))
    detection = json.loads(result.stdout)
    require(
        detection["family"] == "ice40",
        "the fixture was detected as {}, not ice40".format(detection["family"]),
    )
    require(
        detection["method"] == "magic",
        "the family came from the extension ({}), not from the file's magic".format(
            detection["method"]
        ),
    )
    report.ok("{} -> {} ({})".format(BITSTREAM.name, detection["name"], detection["evidence"]))
    return detection


def check_load(work_dir, hal_lib, report):
    report.step("convert with IceStorm and load into HAL")
    summary_path = work_dir / "summary.json"
    project_dir = work_dir / "blinky_project"
    result = run(
        [
            sys.executable, "-m", "hal_bitstream", "load", str(BITSTREAM),
            "-o", str(work_dir / "blinky.v"),
            "--output-summary", str(summary_path),
            "--project-dir", str(project_dir),
        ],
        work_dir,
        tool_env(hal_lib),
    )
    require(result.returncode == 0, "hal_bitstream load failed:\n{}".format(result.stdout))
    with open(str(summary_path), encoding="utf-8") as handle:
        summary = json.load(handle)

    require(summary["family"] == "ice40", "wrong family in the summary")
    require(
        str(summary["gate_library"]).endswith("ice40ultra.hgl"),
        "the netlist was loaded with {}, not the iCE40 library".format(summary["gate_library"]),
    )
    require(
        summary["gate_count"] > 0,
        "HAL loaded the converted netlist but it has no gate; the conversion produced nothing "
        "usable",
    )
    report.ok(
        "{} gates, {} nets, design {!r}".format(
            summary["gate_count"], summary["net_count"], summary["design_name"]
        )
    )

    types = summary["gate_types"]
    # SB_* are the iCE40 primitives; GND/VCC are the gate library's constant drivers, which yosys
    # instantiates for the tied inputs of a LUT.
    foreign = sorted(
        name for name in types if not name.startswith("SB_") and name not in ("GND", "VCC", "VDD")
    )
    require(not foreign, "non-iCE40 gate types in the netlist: {}".format(foreign))
    report.note("gate types: {}".format(", ".join("{}x{}".format(v, k) for k, v in sorted(types.items()))))
    require(
        any(name.startswith("SB_LUT") for name in types),
        "no LUT in the recovered netlist: {}".format(sorted(types)),
    )
    require(
        any(name.startswith("SB_DFF") for name in types),
        "no flip-flop in the recovered netlist, but blinky.v is a counter: {}".format(
            sorted(types)
        ),
    )
    report.ok("the netlist contains the LUTs and flip-flops the fixture design needs")

    manifest = summary["conversion"]
    programs = [step["program"] for step in manifest["steps"]]
    require(
        programs == ["iceunpack", "icebox_vlog", "yosys"],
        "the chain that ran was {}, not the registered iCE40 one".format(programs),
    )
    for step in manifest["steps"]:
        require(os.path.isabs(step["resolved"]), "converter path not recorded: {}".format(step))
        require(step["command"], "no command line recorded for {}".format(step["program"]))
    require(len(manifest["bitstream"]["sha256"]) == 64, "no bitstream digest in the manifest")
    report.ok(
        "manifest records sha256 {}... and both converter invocations".format(
            manifest["bitstream"]["sha256"][:16]
        )
    )

    require(project_dir.is_dir(), "no HAL project directory was written")
    require(
        (project_dir / ".project.json").is_file(),
        "the project directory has no .project.json: {}".format(sorted(os.listdir(str(project_dir)))),
    )
    report.ok("HAL project written to {}".format(project_dir))
    return summary, project_dir


def check_project_reload(project_dir, hal_lib, summary, report):
    report.step("reload the saved project")
    script = (
        "import json, sys\n"
        "sys.path.insert(0, {tools!r})\n"
        "from hal_viz.halenv import import_hal_py, load_netlist\n"
        "hal_py = import_hal_py([{lib!r}])\n"
        "netlist = load_netlist(hal_py, {project!r})\n"
        "print(json.dumps({{'gates': len(netlist.get_gates()), 'nets': len(netlist.get_nets())}}))\n"
    ).format(tools=str(TOOLS), lib=str(hal_lib), project=str(project_dir))
    result = run([sys.executable, "-c", script], project_dir.parent, tool_env(hal_lib))
    require(result.returncode == 0, "reloading the project failed:\n{}".format(result.stdout))
    reloaded = json.loads(result.stdout.strip().splitlines()[-1])
    require(
        reloaded["gates"] == summary["gate_count"],
        "the reloaded project has {} gates, the converted netlist had {}".format(
            reloaded["gates"], summary["gate_count"]
        ),
    )
    report.ok("round-trip preserved {} gates".format(reloaded["gates"]))


def check_missing_converter(work_dir, hal_lib, report):
    report.step("missing converter")
    empty_bin = work_dir / "empty_bin"
    empty_bin.mkdir(exist_ok=True)
    environment = tool_env(hal_lib)
    environment["PATH"] = str(empty_bin)
    result = run(
        [
            sys.executable, "-m", "hal_bitstream", "convert", str(BITSTREAM),
            "-o", str(work_dir / "unreachable.v"), "--quiet",
        ],
        work_dir,
        environment,
    )
    require(
        result.returncode == 2,
        "hiding the converters gave exit code {}, expected 2:\n{}".format(
            result.returncode, result.stdout
        ),
    )
    for expected in ("iceunpack", "IceStorm", "--converter"):
        require(
            expected in result.stdout,
            "the missing-converter error does not mention {!r}:\n{}".format(expected, result.stdout),
        )
    require(
        not (work_dir / "unreachable.v").exists(),
        "a netlist was written even though no converter ran",
    )
    report.ok("a missing converter is an actionable error, not a silent failure")


def check_unconvertible_family(work_dir, hal_lib, report):
    report.step("a family with no converter")
    fake = work_dir / "design.bit"
    fake.write_bytes(b"\x00\x09\x0f\xf0" + b"\xff" * 32 + b"\xaa\x99\x55\x66" + b"\x00" * 64)
    result = run(
        [sys.executable, "-m", "hal_bitstream", "convert", str(fake), "-o", str(work_dir / "x.v"), "--quiet"],
        work_dir,
        tool_env(hal_lib),
    )
    require(result.returncode == 2, "expected exit 2, got {}".format(result.returncode))
    require(
        "no open bitstream-to-netlist converter" in result.stdout,
        "the Xilinx path does not explain itself:\n{}".format(result.stdout),
    )
    report.ok("a documented-but-unconvertible family says so")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hal-lib", help="directory containing hal_py (default: $HAL_PY_PATH)")
    parser.add_argument("--work-dir", help="where generated files go (default: a temp dir)")
    parser.add_argument("--keep", action="store_true", help="keep the work directory")
    parser.add_argument(
        "--rebuild-fixture",
        action="store_true",
        help="regenerate fixtures/ice40_blinky.bin with yosys + nextpnr-ice40 + icepack first",
    )
    args = parser.parse_args(argv)

    hal_lib = args.hal_lib or os.environ.get("HAL_PY_PATH", "").split(os.pathsep)[0]
    require(hal_lib, "no HAL library directory: pass --hal-lib <build>/lib or export HAL_PY_PATH")
    require(os.path.isdir(hal_lib), "--hal-lib {} is not a directory".format(hal_lib))
    require(GATE_LIBRARY.is_file(), "missing gate library {}".format(GATE_LIBRARY))

    if args.work_dir:
        work_dir = Path(args.work_dir).resolve()
        if work_dir.exists():
            shutil.rmtree(str(work_dir))
        work_dir.mkdir(parents=True)
        keep = True
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="hal_bitstream_smoke_"))
        keep = args.keep

    report = Report()
    print("work directory: {}".format(work_dir))
    print("HAL library:    {}".format(hal_lib))
    try:
        if args.rebuild_fixture:
            rebuild_fixture(work_dir, report)
        require(
            BITSTREAM.is_file(),
            "the fixture bitstream {} is missing; regenerate it with --rebuild-fixture "
            "(needs yosys, nextpnr-ice40 and icepack)".format(BITSTREAM),
        )
        require_tool("iceunpack", "Debian/Ubuntu: 'apt-get install fpga-icestorm'.")
        require_tool("icebox_vlog", "Debian/Ubuntu: 'apt-get install fpga-icestorm'.")
        require_tool("yosys", "Debian/Ubuntu: 'apt-get install yosys'.")

        check_detection(work_dir, hal_lib, report)
        summary, project_dir = check_load(work_dir, hal_lib, report)
        check_project_reload(project_dir, hal_lib, summary, report)
        check_missing_converter(work_dir, hal_lib, report)
        check_unconvertible_family(work_dir, hal_lib, report)
    finally:
        if not keep and work_dir.exists():
            shutil.rmtree(str(work_dir), ignore_errors=True)

    print("\n{} checks passed".format(report.checks))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SmokeError as error:
        print("\nFAILED: {}".format(error), file=sys.stderr)
        sys.exit(1)
