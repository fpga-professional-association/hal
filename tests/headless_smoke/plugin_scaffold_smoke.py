#!/usr/bin/env python3
"""End-to-end check of the plugin scaffold against a built HAL and a real netlist.

``tools/test_new_plugin.py`` and ``tools/hal_capabilities/test_hal_capabilities.py`` run
anywhere, and between them they prove that the generator produces a complete tree and that
the capability logic is right. Neither of them compiles a line of C++ or loads a plugin, so
both stay green while the template stops building, the Python module stops importing, or
the capability declaration compiled into the plugin drifts away from the one in the source
tree.

This closes that gap for ``plugins/example_analysis`` -- the unmodified output of
``tools/new_plugin.py``, kept in the tree precisely so that a build has something generated
to compile. It asserts results, not exit codes:

  1. the plugin is *built* (its shared object is in the build tree) and *loadable*
     (``plugin_manager`` instantiates it and ``hal_plugins.example_analysis`` imports)
  2. the capability declaration compiled into it is byte-identical to
     ``plugins/example_analysis/capabilities.json``, is schema-valid, and agrees with
     ``get_dependencies()`` and ``get_version()``
  3. running it over ``examples/uart.zip`` finds the UART's one clock domain: all 258 FFRs
     on net ``CLK_BUF_BUF``, nothing unresolved, no unsupported gate type
  4. the generated driver writes a findings document that validates against the
     ``hal_findings`` schema, with the clock domain as a single ``heuristic`` finding
  5. a netlist with no sequential gate produces an actionable error naming the missing
     property -- not an empty result that reads like "no registers found"
  6. ``python tools/hal_capabilities`` reports the four states apart: declared, built,
     loadable, and supported *for this netlist*

Nothing is skipped when something is missing: a missing build, a missing plugin or a
missing binding is a failure with an actionable message.

Run it against a build tree with::

    HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib \
        python3 tests/headless_smoke/plugin_scaffold_smoke.py --build-dir <build>

Add ``--work-dir <dir> --keep`` to leave the unpacked example and the generated findings
behind for inspection.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ARCHIVE = REPO_ROOT / "examples" / "uart.zip"
EXAMPLE_DIR_NAME = "uart"
PLUGIN_NAME = "example_analysis"
PLUGIN_DIR = REPO_ROOT / "plugins" / PLUGIN_NAME
DECLARATION = PLUGIN_DIR / "capabilities.json"
DRIVER = PLUGIN_DIR / "python" / "run_example_analysis.py"

# ---------------------------------------------------------------------------
# Properties of examples/uart.zip, read out of uart/uart.hal: net CLK_BUF_BUF drives the
# clock pin ("C", PinType::clock) of all 258 FFR gates and of nothing else. If one of these
# changes, either the example was replaced or the analysis changed -- both are worth a red
# build.
# ---------------------------------------------------------------------------
EXPECTED_SEQUENTIAL_GATES = 258
EXPECTED_DOMAIN_COUNT = 1
EXPECTED_CLOCK_NET = "CLK_BUF_BUF"
EXPECTED_UNRESOLVED = 0
EXPECTED_UNSUPPORTED_TYPES = 0


class SmokeError(RuntimeError):
    """A check failed, or a prerequisite for one is missing."""


def require(condition, message):
    if not condition:
        raise SmokeError(message)


def require_attr(obj, name, where):
    require(
        hasattr(obj, name),
        "{} has no '{}'. The API this smoke test drives is gone or was renamed; fix the "
        "test or the code, do not skip the check.".format(where, name),
    )
    return getattr(obj, name)


class Report(object):
    """Collects the checks so the whole run is visible even when one fails."""

    def __init__(self, stream):
        self.stream = stream
        self.checks = 0

    def ok(self, message):
        self.checks += 1
        self.stream.write("  ok    {}\n".format(message))
        self.stream.flush()

    def info(self, message):
        self.stream.write("        {}\n".format(message))
        self.stream.flush()

    def step(self, message):
        self.stream.write("\n== {}\n".format(message))
        self.stream.flush()


def unpack_example(work_dir, report):
    target = work_dir / EXAMPLE_DIR_NAME
    require(EXAMPLE_ARCHIVE.is_file(), "missing example archive {}".format(EXAMPLE_ARCHIVE))
    with zipfile.ZipFile(EXAMPLE_ARCHIVE) as archive:
        archive.extractall(target)
    project = target / EXAMPLE_DIR_NAME
    require(
        (project / "uart.hal").is_file(),
        "{} did not contain {}/uart.hal".format(EXAMPLE_ARCHIVE, EXAMPLE_DIR_NAME),
    )
    report.ok("unpacked {} to {}".format(EXAMPLE_ARCHIVE.name, project))
    return project


def import_hal(hal_libs, report):
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from hal_viz import halenv

    hal_py = halenv.import_hal_py(hal_libs)
    halenv.load_all_plugins(hal_py)
    report.ok("imported hal_py and loaded all plugins")
    return hal_py, halenv


def check_built_and_loadable(hal_py, halenv, build_dir, report):
    """The three states the discovery command distinguishes, asserted one by one."""
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from hal_capabilities import discover

    library_path = discover.build_library_path(str(build_dir), PLUGIN_NAME) if build_dir else None
    if build_dir:
        require(
            library_path is not None,
            "the {} plugin is not built: no {}.so/.dylib in {}/lib/hal_plugins. Configure "
            "with -DPL_EXAMPLE_ANALYSIS=ON or -DBUILD_ALL_PLUGINS=ON.".format(
                PLUGIN_NAME, PLUGIN_NAME, build_dir
            ),
        )
        report.ok("built: {}".format(library_path))
    else:
        report.info("no --build-dir given; the 'built' state was not checked")

    names = set(hal_py.plugin_manager.get_plugin_names())
    require(
        PLUGIN_NAME in names,
        "plugin_manager did not load {!r}. Loaded plugins: {}".format(
            PLUGIN_NAME, ", ".join(sorted(names))
        ),
    )
    module = halenv.import_plugin(PLUGIN_NAME)
    instance = hal_py.plugin_manager.get_plugin_instance(PLUGIN_NAME)
    require(instance is not None, "plugin_manager.get_plugin_instance({!r}) returned None".format(PLUGIN_NAME))
    report.ok("loadable: hal_plugins.{} imported and instantiated".format(PLUGIN_NAME))
    return module, instance


def check_capability_declaration(module, instance, report):
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from hal_capabilities import validate as capability_validate

    with open(DECLARATION, "r") as handle:
        on_disk = json.load(handle)
    errors = capability_validate.collect_errors(on_disk)
    require(not errors, "{} is invalid: {}".format(DECLARATION, "; ".join(errors)))
    report.ok("{} is schema-valid".format(DECLARATION.relative_to(REPO_ROOT)))

    plugin_class = require_attr(module, "ExampleAnalysisPlugin", "hal_plugins." + PLUGIN_NAME)
    compiled = json.loads(require_attr(plugin_class, "get_capabilities", "ExampleAnalysisPlugin")())
    require(
        compiled == on_disk,
        "the capability declaration compiled into the plugin differs from {}. Rebuild, or "
        "the two have drifted apart.".format(DECLARATION),
    )
    report.ok("the compiled-in declaration matches the source tree")

    declared_dependencies = set(on_disk["dependencies"]["plugins"])
    actual_dependencies = set(instance.get_dependencies())
    require(
        declared_dependencies == actual_dependencies,
        "capabilities.json declares dependencies {} but get_dependencies() returns {}".format(
            sorted(declared_dependencies), sorted(actual_dependencies)
        ),
    )
    require(
        on_disk["plugin"]["version"] == instance.get_version(),
        "capabilities.json says version {!r}, the plugin reports {!r}".format(
            on_disk["plugin"]["version"], instance.get_version()
        ),
    )
    report.ok("dependencies and version agree with the declaration")
    return on_disk


def check_analysis(hal_py, halenv, module, project, report):
    netlist = halenv.load_netlist(hal_py, str(project))
    analyze = require_attr(module, "analyze", "hal_plugins." + PLUGIN_NAME)
    result = analyze(netlist)

    require(
        result.sequential_gate_count == EXPECTED_SEQUENTIAL_GATES,
        "expected {} sequential gates in the UART, got {}".format(
            EXPECTED_SEQUENTIAL_GATES, result.sequential_gate_count
        ),
    )
    require(
        len(result.domains) == EXPECTED_DOMAIN_COUNT,
        "expected {} clock domain(s), got {} ({})".format(
            EXPECTED_DOMAIN_COUNT,
            len(result.domains),
            ", ".join(domain.clock_net.get_name() for domain in result.domains),
        ),
    )
    domain = result.domains[0]
    require(
        domain.clock_net.get_name() == EXPECTED_CLOCK_NET,
        "expected the clock domain to be driven by {!r}, got {!r}".format(
            EXPECTED_CLOCK_NET, domain.clock_net.get_name()
        ),
    )
    require(
        len(domain.gates) == EXPECTED_SEQUENTIAL_GATES,
        "expected all {} flip-flops in one domain, got {}".format(
            EXPECTED_SEQUENTIAL_GATES, len(domain.gates)
        ),
    )
    require(
        len(result.unresolved_gates) == EXPECTED_UNRESOLVED,
        "expected {} gates with an undriven clock pin, got {}".format(
            EXPECTED_UNRESOLVED, len(result.unresolved_gates)
        ),
    )
    require(
        len(result.unsupported) == EXPECTED_UNSUPPORTED_TYPES,
        "expected {} unsupported gate type(s), got {}".format(
            EXPECTED_UNSUPPORTED_TYPES, ", ".join(entry.gate_type.get_name() for entry in result.unsupported)
        ),
    )
    report.ok(
        "analysis: 1 clock domain on {!r} with all {} flip-flops".format(
            EXPECTED_CLOCK_NET, EXPECTED_SEQUENTIAL_GATES
        )
    )
    return netlist


def check_actionable_error(hal_py, module, netlist, report):
    """A netlist this analysis cannot look at must say so, loudly."""
    library = netlist.get_gate_library()
    combinational = hal_py.NetlistFactory.create_netlist(library)
    require(combinational is not None, "NetlistFactory.create_netlist() returned None")
    buffer_type = library.get_gate_type_by_name("BUF")
    require(buffer_type is not None, "the example gate library has no 'BUF' gate type")
    combinational.create_gate(buffer_type, "lonely_buffer")

    try:
        module.analyze(combinational)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise SmokeError(
            "analyze() returned a result for a netlist without a single sequential gate. An "
            "empty result there is indistinguishable from 'no registers found'."
        )

    for expected in ("sequential", "hal_capabilities"):
        require(
            expected in message,
            "the error for a combinational netlist does not mention {!r}; it is not "
            "actionable. Message: {}".format(expected, message),
        )
    report.ok("a combinational netlist raises an actionable RuntimeError")


def run(command, report, cwd=None, expect_returncode=0):
    report.info("$ " + " ".join(str(part) for part in command))
    completed = subprocess.run(
        [str(part) for part in command],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if completed.returncode != expect_returncode:
        raise SmokeError(
            "command exited {} (expected {}):\n  {}\n--- stdout ---\n{}\n--- stderr ---\n{}".format(
                completed.returncode,
                expect_returncode,
                " ".join(str(part) for part in command),
                completed.stdout,
                completed.stderr,
            )
        )
    return completed


def check_driver(project, work_dir, hal_libs, report):
    findings_path = work_dir / "example_analysis_findings.json"
    command = [sys.executable, DRIVER, project, "--output", findings_path]
    for path in hal_libs:
        command.extend(["--hal-lib", path])
    run(command, report)

    require(findings_path.is_file(), "the driver did not write {}".format(findings_path))

    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from hal_findings import model, serialize, validate as findings_validate

    document = serialize.read_document(str(findings_path))
    findings_validate.validate_document(document)
    report.ok("the driver wrote a schema-valid findings document")

    findings = document["findings"]
    require(
        len(findings) == 1,
        "expected exactly one finding for the UART (one clock domain, nothing unresolved "
        "or unsupported), got {}: {}".format(
            len(findings), ", ".join(finding["id"] for finding in findings)
        ),
    )
    finding = findings[0]
    require(
        finding["status"] == model.STATUS_HEURISTIC,
        "a structural clock-domain grouping must be reported as {!r}, not {!r}".format(
            model.STATUS_HEURISTIC, finding["status"]
        ),
    )
    require(
        not model.is_unbounded_proof(finding),
        "the clock-domain finding must never read as an unbounded proof",
    )
    require(
        finding["metrics"]["gate_count"] == EXPECTED_SEQUENTIAL_GATES,
        "the finding reports {} gates, expected {}".format(
            finding["metrics"]["gate_count"], EXPECTED_SEQUENTIAL_GATES
        ),
    )
    require(
        len(finding["scope"]["gates"]) == EXPECTED_SEQUENTIAL_GATES,
        "expected {} gate references in the finding's scope, got {}".format(
            EXPECTED_SEQUENTIAL_GATES, len(finding["scope"]["gates"])
        ),
    )
    artifact_ids = {artifact["artifact_id"] for artifact in document["artifacts"]}
    require(
        all(ref["artifact_id"] in artifact_ids for ref in finding["scope"]["gates"]),
        "a gate reference points outside the document's artifacts",
    )
    report.ok(
        "the finding is a heuristic clock domain over {} scoped gate references".format(
            EXPECTED_SEQUENTIAL_GATES
        )
    )
    return findings_path


def check_discovery_command(project, build_dir, hal_libs, report):
    base = [sys.executable, REPO_ROOT / "tools" / "hal_capabilities"]
    if build_dir:
        base.extend(["--build-dir", build_dir])
    for path in hal_libs:
        base.extend(["--hal-lib", path])

    completed = run(base + ["--json", "list", "--probe", "--netlist", project], report)
    payload = json.loads(completed.stdout)
    entry = next(
        (item for item in payload["plugins"] if item["name"] == PLUGIN_NAME), None
    )
    require(entry is not None, "the listing does not mention {!r}".format(PLUGIN_NAME))
    require(entry["declared"], "{!r} is not reported as declared".format(PLUGIN_NAME))
    if build_dir:
        require(entry["built"], "{!r} is not reported as built".format(PLUGIN_NAME))
    require(entry["loadable"], "{!r} is not reported as loadable".format(PLUGIN_NAME))
    require(
        entry["support"]["state"] == "supported",
        "{!r} is reported as {!r} for the UART, expected 'supported' ({})".format(
            PLUGIN_NAME, entry["support"]["state"], "; ".join(entry["support"]["reasons"])
        ),
    )
    require(
        "capability_drift" not in entry,
        "the listing reports declaration drift: {}".format(entry.get("capability_drift")),
    )
    report.ok("list --probe --netlist reports declared, built, loadable and supported")

    completed = run(base + ["check", PLUGIN_NAME, "--netlist", project, "--probe"], report)
    require(
        "supported" in completed.stdout,
        "'check' did not report the plugin as supported:\n{}".format(completed.stdout),
    )
    report.ok("check exits 0 and reports 'supported' for the UART")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--hal-lib",
        action="append",
        default=[],
        help="directory holding hal_py (repeatable; default: $HAL_PY_PATH / $PYTHONPATH)",
    )
    parser.add_argument(
        "--build-dir",
        default=os.environ.get("HAL_BASE_PATH"),
        help="HAL build directory; enables the 'built' check (default: $HAL_BASE_PATH)",
    )
    parser.add_argument("--work-dir", help="directory for unpacked and generated files")
    parser.add_argument("--keep", action="store_true", help="do not delete the work directory")
    args = parser.parse_args(argv)

    temporary = None
    if args.work_dir:
        work_dir = Path(args.work_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
    else:
        temporary = tempfile.mkdtemp(prefix="hal-plugin-scaffold-smoke-")
        work_dir = Path(temporary)

    report = Report(sys.stdout)
    status = 0
    try:
        report.step("example netlist")
        project = unpack_example(work_dir, report)

        report.step("plugin states: built, loadable")
        hal_py, halenv = import_hal(args.hal_lib, report)
        module, instance = check_built_and_loadable(hal_py, halenv, args.build_dir, report)

        report.step("capability declaration")
        check_capability_declaration(module, instance, report)

        report.step("analysis over examples/uart.zip")
        netlist = check_analysis(hal_py, halenv, module, project, report)

        report.step("actionable error on an out-of-scope netlist")
        check_actionable_error(hal_py, module, netlist, report)

        report.step("findings document")
        check_driver(project, work_dir, args.hal_lib, report)

        report.step("discovery command")
        check_discovery_command(project, args.build_dir, args.hal_lib, report)

        sys.stdout.write("\n{} checks passed\n".format(report.checks))
    except SmokeError as exc:
        sys.stdout.write("\nFAILED after {} checks:\n{}\n".format(report.checks, exc))
        status = 1
    except Exception as exc:  # noqa: BLE001 - the message is the product here
        sys.stdout.write(
            "\nFAILED after {} checks with an unexpected {}:\n{}\n".format(
                report.checks, type(exc).__name__, exc
            )
        )
        status = 1
    finally:
        if temporary and not args.keep:
            import shutil

            shutil.rmtree(temporary, ignore_errors=True)
        elif args.keep:
            sys.stdout.write("kept {}\n".format(work_dir))

    return status


if __name__ == "__main__":
    sys.exit(main())
