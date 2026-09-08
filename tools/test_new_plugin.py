#!/usr/bin/env python3
"""Tests for the plugin scaffold generator.

These run on a plain interpreter: no HAL build, no compiler, no netlist. What
they can prove is that the generator produces a complete, consistent, valid tree
-- and, crucially, that ``plugins/example_analysis`` is still *exactly* what the
templates produce. That last check is what makes the checked-in plugin a
regression test for the templates: CI compiles it and runs its gtest suite, so a
template that stops compiling stops the build instead of stopping the next
contributor.

What they cannot prove is that the generated C++ compiles. Only a build can, and
that is why the example plugin is checked in rather than generated into a
temporary directory and thrown away.

    python3 tools/test_new_plugin.py
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import new_plugin  # noqa: E402
from hal_capabilities import collect_errors  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_PLUGIN = "example_analysis"
EXAMPLE_DESCRIPTION = (
    "Group sequential gates by the net that drives their clock pin "
    "(reference output of tools/new_plugin.py)."
)


class NameValidationTest(unittest.TestCase):
    def test_accepts_a_plain_identifier(self):
        self.assertEqual(new_plugin.check_name("my_analysis"), "my_analysis")

    def test_rejects_names_that_are_not_c_identifiers(self):
        for name in ("", "1abc", "_abc", "my-analysis", "my analysis", "my.analysis", "änalyse"):
            with self.assertRaises(new_plugin.PluginNameError):
                new_plugin.check_name(name)

    def test_rejects_upper_case(self):
        # The name is the namespace, the library file name and the Python module name.
        with self.assertRaises(new_plugin.PluginNameError):
            new_plugin.check_name("MyAnalysis")

    def test_rejects_keywords_and_namespace_collisions(self):
        for name in ("class", "template", "hal", "test"):
            with self.assertRaises(new_plugin.PluginNameError):
                new_plugin.check_name(name)

    def test_class_name(self):
        self.assertEqual(new_plugin.class_name("my_analysis"), "MyAnalysis")
        self.assertEqual(new_plugin.class_name("uart"), "Uart")
        self.assertEqual(new_plugin.class_name("a_b_c"), "ABC")


class GeneratedTreeTest(unittest.TestCase):
    name = "my_analysis"

    def setUp(self):
        self.files = new_plugin.generated_files(self.name)

    def test_every_declared_file_is_generated(self):
        expected = {
            new_plugin.render(target, self.name, new_plugin.DEFAULT_DESCRIPTION).replace(
                os.sep, "/"
            )
            for _template, target in new_plugin.FILES
        }
        self.assertEqual(set(self.files), expected)
        self.assertIn("include/my_analysis/my_analysis.h", self.files)
        self.assertIn("python/run_my_analysis.py", self.files)
        self.assertIn("test/my_analysis.cpp", self.files)

    def test_no_placeholder_survives(self):
        placeholder = re.compile(r"##[A-Z]+##")
        for path, contents in self.files.items():
            self.assertIsNone(
                placeholder.search(contents), "placeholder left in {}".format(path)
            )
            self.assertIsNone(placeholder.search(path))

    def test_generated_python_is_syntactically_valid(self):
        source = self.files["python/run_my_analysis.py"]
        compile(source, "run_my_analysis.py", "exec")

    def test_generated_json_is_a_valid_capability_declaration(self):
        document = json.loads(self.files["capabilities.json"])
        self.assertEqual(collect_errors(document), [])
        self.assertEqual(document["plugin"]["name"], self.name)
        self.assertEqual(document["plugin"]["build_option"], "PL_MY_ANALYSIS")
        self.assertEqual(
            document["findings"]["driver"], "plugins/my_analysis/python/run_my_analysis.py"
        )

    def test_cmake_registers_the_plugin_and_its_tests(self):
        cmake = self.files["CMakeLists.txt"]
        self.assertIn('option(PL_MY_ANALYSIS "PL_MY_ANALYSIS" OFF)', cmake)
        self.assertIn("hal_add_plugin(my_analysis", cmake)
        self.assertIn("add_subdirectory(test)", cmake)
        # capabilities.json is embedded and installed, which is what keeps the compiled-in
        # declaration and the source-tree one from drifting.
        self.assertIn("capabilities_generated.h", cmake)
        self.assertIn("share/hal/plugin_capabilities", cmake)

        test_cmake = self.files["test/CMakeLists.txt"]
        self.assertIn("add_executable(runTest-my_analysis my_analysis.cpp)", test_cmake)
        self.assertIn("add_test(runTest-my_analysis", test_cmake)

    def test_plugin_interface_is_implemented(self):
        header = self.files["include/my_analysis/plugin_my_analysis.h"]
        self.assertIn("class PLUGIN_API MyAnalysisPlugin : public BasePluginInterface", header)
        for method in ("get_name", "get_version", "get_description", "get_dependencies",
                       "get_capabilities"):
            self.assertIn(method, header)

        source = self.files["src/plugin_my_analysis.cpp"]
        self.assertIn("create_plugin_instance", source)
        self.assertIn('return std::string("my_analysis")', source)

    def test_python_module_name_matches_the_library_name(self):
        # A mismatch here is the classic "dynamic module does not define module export
        # function" import error.
        self.assertIn("PYBIND11_MODULE(my_analysis, m)", self.files["python/python_bindings.cpp"])

    def test_description_reaches_every_place_it_is_shown(self):
        files = new_plugin.generated_files(self.name, "Find the thing.")
        for path in (
            "capabilities.json",
            "README.md",
            "src/plugin_my_analysis.cpp",
            "python/python_bindings.cpp",
        ):
            self.assertIn("Find the thing.", files[path], path)

    def test_generation_is_deterministic(self):
        self.assertEqual(self.files, new_plugin.generated_files(self.name))


class WriteToDiskTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="hal-new-plugin-")
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_creates_the_tree(self):
        written = new_plugin.create_plugin("scratch_analysis", self.work)
        self.assertEqual(len(written), len(new_plugin.FILES))
        for path in written:
            self.assertTrue(os.path.isfile(path), path)
        self.assertTrue(
            os.path.isfile(os.path.join(self.work, "scratch_analysis", "test", "CMakeLists.txt"))
        )

    def test_refuses_to_overwrite_without_force(self):
        new_plugin.create_plugin("scratch_analysis", self.work)
        with self.assertRaises(IOError):
            new_plugin.create_plugin("scratch_analysis", self.work)
        # ... and does overwrite with it.
        new_plugin.create_plugin("scratch_analysis", self.work, force=True)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_driver_script_is_executable(self):
        new_plugin.create_plugin("scratch_analysis", self.work)
        path = os.path.join(
            self.work, "scratch_analysis", "python", "run_scratch_analysis.py"
        )
        self.assertTrue(os.access(path, os.X_OK))

    def test_allow_lists_the_plugin_in_an_allow_list_gitignore(self):
        # plugins/.gitignore ignores '*' and re-includes each plugin by name, so a
        # generated plugin is invisible to git until it is listed. Closing that trap is
        # the generator's job.
        gitignore = os.path.join(self.work, ".gitignore")
        with open(gitignore, "w") as handle:
            handle.write("*\n!alpha*\n!alpha/**/*\n!zulu*\n!zulu/**/*\n!CMakeLists.txt\n")

        new_plugin.main(["mike_analysis", "--out-dir", self.work], out=io.StringIO(), err=io.StringIO())
        with open(gitignore, "r") as handle:
            lines = handle.read().splitlines()
        self.assertIn("!mike_analysis*", lines)
        self.assertIn("!mike_analysis/**/*", lines)
        # inserted in order, before zulu and before the trailing non-plugin entries
        self.assertLess(lines.index("!alpha*"), lines.index("!mike_analysis*"))
        self.assertLess(lines.index("!mike_analysis/**/*"), lines.index("!zulu*"))
        self.assertEqual(lines[-1], "!CMakeLists.txt")

        # and it is idempotent
        before = list(lines)
        new_plugin.unignore(self.work, "mike_analysis")
        with open(gitignore, "r") as handle:
            self.assertEqual(handle.read().splitlines(), before)

    def test_leaves_an_ordinary_gitignore_alone(self):
        gitignore = os.path.join(self.work, ".gitignore")
        with open(gitignore, "w") as handle:
            handle.write("build/\n*.pyc\n")
        self.assertIsNone(new_plugin.unignore(self.work, "some_analysis"))
        with open(gitignore, "r") as handle:
            self.assertEqual(handle.read(), "build/\n*.pyc\n")

    def test_no_gitignore_flag(self):
        gitignore = os.path.join(self.work, ".gitignore")
        with open(gitignore, "w") as handle:
            handle.write("*\n!CMakeLists.txt\n")
        new_plugin.main(
            ["quiet_analysis", "--out-dir", self.work, "--no-gitignore"],
            out=io.StringIO(),
            err=io.StringIO(),
        )
        with open(gitignore, "r") as handle:
            self.assertNotIn("quiet_analysis", handle.read())

    def test_cli_reports_a_bad_name_instead_of_a_traceback(self):
        err = io.StringIO()
        status = new_plugin.main(["1nope", "--out-dir", self.work], out=io.StringIO(), err=err)
        self.assertEqual(status, 1)
        self.assertIn("1nope", err.getvalue())

    def test_cli_rejects_a_description_that_would_break_the_generated_files(self):
        err = io.StringIO()
        status = new_plugin.main(
            ["ok_analysis", "--out-dir", self.work, "--description", 'a "quoted" thing'],
            out=io.StringIO(),
            err=err,
        )
        self.assertEqual(status, 1)
        self.assertIn("double quote", err.getvalue())

    def test_cli_generates_and_prints_next_steps(self):
        out = io.StringIO()
        status = new_plugin.main(["ok_analysis", "--out-dir", self.work], out=out, err=io.StringIO())
        self.assertEqual(status, 0)
        self.assertIn("cmake", out.getvalue())
        self.assertIn("hal_capabilities", out.getvalue())

    def test_generated_driver_runs_and_explains_itself_without_hal(self):
        # The driver must fail with an actionable message rather than an ImportError
        # traceback when it cannot find the repository's tools/ directory.
        new_plugin.create_plugin("scratch_analysis", self.work)
        script = os.path.join(self.work, "scratch_analysis", "python", "run_scratch_analysis.py")
        completed = subprocess.run(
            [sys.executable, script, "--tools-dir", os.path.join(self.work, "nowhere"), "design.v"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("tools/", completed.stderr)
        self.assertIn("HAL_TOOLS_PATH", completed.stderr)

    def test_generated_driver_prints_usage(self):
        new_plugin.create_plugin("scratch_analysis", self.work)
        script = os.path.join(self.work, "scratch_analysis", "python", "run_scratch_analysis.py")
        completed = subprocess.run(
            [sys.executable, script, "--help"], capture_output=True, text=True
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--output", completed.stdout)


class _Enum(object):
    def __init__(self, name):
        self.name = name


class _StubGateType(object):
    def __init__(self, name, properties):
        self._name = name
        self._properties = [_Enum(entry) for entry in properties]

    def get_name(self):
        return self._name

    def get_property_list(self):
        return list(self._properties)


class _StubNamed(object):
    """A stand-in for a Gate or a Net: an ID, a name, and (for gates) a type."""

    def __init__(self, object_id, name, gate_type=None):
        self._id = object_id
        self._name = name
        self._type = gate_type

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_type(self):
        return self._type


class _StubDomain(object):
    def __init__(self, clock_net, gates):
        self.clock_net = clock_net
        self.gates = gates


class _StubUnsupported(object):
    def __init__(self, gate_type, count, reason):
        self.gate_type = gate_type
        self.count = count
        self.reason = reason


class _StubReport(object):
    def __init__(self, domains, unresolved_gates, unsupported, sequential_gate_count):
        self.domains = domains
        self.unresolved_gates = unresolved_gates
        self.unsupported = unsupported
        self.sequential_gate_count = sequential_gate_count


class _StubNetlist(object):
    def __init__(self, gates):
        self._gates = gates

    def get_gates(self, filter=None):
        return list(self._gates)

    def get_design_name(self):
        return "stub_design"

    def get_id(self):
        return 1

    def get_gate_library(self):
        return None

    def get_input_filename(self):
        return ""

    def get_nets(self):
        return []


class GeneratedDriverTest(unittest.TestCase):
    """The generated driver must emit a document that passes the findings schema.

    Driving it with stubs shaped like the bindings is what makes this checkable
    without a HAL build -- and it catches the mistakes that are otherwise only
    found in CI: a severity outside the schema's enum, an extra key in the
    ``analysis`` block, a status/bounds combination the model refuses.
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util

        cls.work = tempfile.mkdtemp(prefix="hal-driver-")
        new_plugin.create_plugin("driver_probe", cls.work)
        path = os.path.join(cls.work, "driver_probe", "python", "run_driver_probe.py")
        spec = importlib.util.spec_from_file_location("run_driver_probe", path)
        cls.driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.driver)

        sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
        from hal_findings import model, serialize, validate, __version__

        cls.model, cls.serialize, cls.validate, cls.version = model, serialize, validate, __version__

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def build(self, report):
        from hal_findings.adapters import common

        netlist = _StubNetlist(
            [gate for domain in report.domains for gate in domain.gates]
            + list(report.unresolved_gates)
        )
        return self.driver.build_document(
            self.model, common, self.version, report, netlist, "netlist",
            netlist_path="stub.v", plugin_version="0.1", duration_s=0.5,
            producer_command=["run_driver_probe.py", "stub.v"],
        )

    def test_document_with_one_clock_domain_validates(self):
        dff = _StubGateType("DFF", ["sequential", "ff"])
        gates = [_StubNamed(i, "ff_{}".format(i), dff) for i in range(1, 5)]
        clock = _StubNamed(9, "clk")
        report = _StubReport([_StubDomain(clock, gates)], [], [], 4)

        document = self.build(report)
        self.validate.validate_document(document)
        self.assertEqual(len(document["findings"]), 1)
        finding = document["findings"][0]
        self.assertEqual(finding["status"], self.model.STATUS_HEURISTIC)
        self.assertFalse(self.model.is_unbounded_proof(finding))
        self.assertEqual(finding["metrics"]["gate_count"], 4)
        self.assertEqual(len(finding["scope"]["gates"]), 4)
        # Deterministic output is what makes two runs comparable.
        self.assertEqual(
            self.serialize.document_digest(document),
            self.serialize.document_digest(self.build(report)),
        )

    def test_unresolved_and_unsupported_become_their_own_findings(self):
        dff = _StubGateType("DFF", ["sequential", "ff"])
        latch = _StubGateType("LATCH", ["sequential", "latch"])
        domain = _StubDomain(
            _StubNamed(9, "clk"), [_StubNamed(1, "ff_1", dff), _StubNamed(2, "ff_2", dff)]
        )
        report = _StubReport(
            [domain],
            [_StubNamed(3, "orphan_ff", dff)],
            [_StubUnsupported(latch, 7, "no clock pin on gate type 'LATCH'")],
            10,
        )

        document = self.build(report)
        self.validate.validate_document(document)
        statuses = {finding["id"]: finding["status"] for finding in document["findings"]}
        self.assertEqual(statuses["driver_probe/clock-domain/0000"], self.model.STATUS_HEURISTIC)
        self.assertEqual(statuses["driver_probe/clock-domain/unresolved"], self.model.STATUS_UNKNOWN)
        self.assertEqual(
            statuses["driver_probe/coverage/unsupported-primitives"],
            self.model.STATUS_UNSUPPORTED,
        )
        unsupported = next(
            finding
            for finding in document["findings"]
            if finding["status"] == self.model.STATUS_UNSUPPORTED
        )
        primitive = unsupported["unsupported"]["primitives"][0]
        self.assertEqual(primitive["gate_type"], "LATCH")
        self.assertEqual(primitive["count"], 7)

    def test_gate_references_are_capped_but_the_count_stays_exact(self):
        dff = _StubGateType("DFF", ["sequential", "ff"])
        gates = [_StubNamed(i, "ff_{}".format(i), dff) for i in range(1, 51)]
        report = _StubReport([_StubDomain(_StubNamed(9, "clk"), gates)], [], [], 50)

        from hal_findings.adapters import common

        netlist = _StubNetlist(gates)
        document = self.driver.build_document(
            self.model, common, self.version, report, netlist, "netlist",
            max_gates_per_finding=10,
        )
        self.validate.validate_document(document)
        finding = document["findings"][0]
        self.assertEqual(finding["metrics"]["gate_count"], 50)
        self.assertEqual(len(finding["scope"]["gates"]), 10)
        self.assertIn("truncated", finding["data"])

    def test_error_document_validates_and_blames_the_analysis(self):
        from hal_findings.adapters import common

        document = self.driver.build_error_document(
            self.model, common, self.version, "RuntimeError: no sequential gates", "netlist",
            netlist_path="stub.v",
        )
        self.validate.validate_document(document)
        self.assertEqual(len(document["findings"]), 1)
        finding = document["findings"][0]
        self.assertEqual(finding["status"], self.model.STATUS_ERROR)
        self.assertEqual(finding["error"]["kind"], "plugin_error")
        self.assertIn("no sequential gates", finding["error"]["message"])


class CheckedInExampleTest(unittest.TestCase):
    """``plugins/example_analysis`` must stay byte-identical to the templates.

    It is the generator's only compiled test: CI builds it with
    ``BUILD_ALL_PLUGINS=ON`` and runs ``runTest-example_analysis``. If someone
    edits the plugin instead of the template, that guarantee quietly evaporates
    -- so this fails and says which file drifted.
    """

    def test_matches_the_generator_output(self):
        expected = new_plugin.generated_files(EXAMPLE_PLUGIN, EXAMPLE_DESCRIPTION)
        directory = os.path.join(REPO_ROOT, "plugins", EXAMPLE_PLUGIN)
        self.assertTrue(os.path.isdir(directory), "{} is missing".format(directory))

        on_disk = {}
        for root, _dirs, names in os.walk(directory):
            for name in names:
                path = os.path.join(root, name)
                relative = os.path.relpath(path, directory).replace(os.sep, "/")
                with open(path, "r", newline="") as handle:
                    on_disk[relative] = handle.read()

        self.assertEqual(
            sorted(on_disk),
            sorted(expected),
            "plugins/{} has files the generator does not produce (or is missing some). "
            "Regenerate it: python3 tools/new_plugin.py {} --force --description "
            '"{}"'.format(EXAMPLE_PLUGIN, EXAMPLE_PLUGIN, EXAMPLE_DESCRIPTION),
        )
        for relative in sorted(expected):
            self.assertEqual(
                on_disk[relative],
                expected[relative],
                "plugins/{}/{} differs from the template it was generated from. Change "
                "tools/plugin_template/ and regenerate, do not edit the plugin.".format(
                    EXAMPLE_PLUGIN, relative
                ),
            )

    def test_declares_valid_capabilities(self):
        path = os.path.join(REPO_ROOT, "plugins", EXAMPLE_PLUGIN, "capabilities.json")
        with open(path, "r") as handle:
            document = json.load(handle)
        self.assertEqual(collect_errors(document), [])
        self.assertEqual(document["plugin"]["name"], EXAMPLE_PLUGIN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
