#!/usr/bin/env python3
"""Unit tests for hal_runner. No HAL build, no netlist, no network.

Everything except the in-HAL step implementations is exercised here, including
the orchestrator itself: :class:`Runner` talks to the outside world through one
object with one method (:meth:`hal_runner.execute.ProcessExecutor.execute`), so
a stub executor that writes the files a real step would write lets the whole
run -- manifest, checkpointing, failure propagation, diagnostics -- be tested
against a plain interpreter.

The cases that matter most are the negative ones: a cache that must *not* hit, a
step that exits 0 without producing anything, a findings document that does not
validate.  A runner that only works when everything works is not worth having.

Run from the repository root::

    python -m unittest discover -s tools/hal_runner -t tools -p "test_*.py"
"""

import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_findings import jsonschema_mini, model, validate as findings_validate  # noqa: E402
from hal_findings.adapters.common import utc_now  # noqa: E402

from hal_runner import analyses, checkpoint, cli, config as config_module  # noqa: E402
from hal_runner import execute, hashing, manifest as manifest_module, protocol  # noqa: E402
from hal_runner.runner import Runner, RunnerError  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLE_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "examples", "uart_components.json"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def minimal_config(netlist, steps=None, **overrides):
    document = {
        "config_version": "1.0.0",
        "name": "unit-run",
        "netlist": netlist,
        "steps": steps
        or [
            {
                "id": "components",
                "analysis": "graph_algorithm.connected_components",
                "config": {"strong": True, "min_size": 2},
            }
        ],
    }
    document.update(overrides)
    return document


def fake_findings_document(finding_id="fake/0001"):
    return model.document(
        {"name": "test_hal_runner", "version": "1.0.0"},
        [
            model.artifact(
                "netlist",
                kind="netlist",
                path="fake",
                unhashed_reason="synthetic input built by the unit tests",
            )
        ],
        {"plugin": {"name": "graph_algorithm", "version": "0"}, "entry_point": "test"},
        [
            model.finding(
                finding_id,
                "A finding produced by the stub step",
                model.STATUS_HEURISTIC,
                model.method("stub", "structural", False),
                model.scope(["netlist"]),
            )
        ],
        generated_at=utc_now(),
    )


class StubExecutor(object):
    """Stands in for a ``hal --python-script`` process.

    ``behaviour`` is called with the request the runner wrote and returns
    ``(exit_code, timed_out)``; it is where a test decides to succeed, fail,
    hang or write garbage.
    """

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or self.succeed
        self.calls = []

    # -- behaviours ---------------------------------------------------------

    @staticmethod
    def succeed(request, step_dir):
        findings_path = os.path.join(step_dir, request["findings_file"])
        from hal_findings import serialize

        serialize.write_document(fake_findings_document(), findings_path)
        protocol.write_json(
            protocol.result(
                "ok",
                request["analysis"],
                artifacts=[{"path": request["findings_file"], "role": "findings"}],
                metrics={"components": 3},
            ),
            os.path.join(step_dir, request["result_file"]),
        )
        return 0, False

    @staticmethod
    def crash(request, step_dir):
        return 3, False

    @staticmethod
    def hang(request, step_dir):
        return -9, True

    @staticmethod
    def silent_success(request, step_dir):
        return 0, False

    @staticmethod
    def invalid_findings(request, step_dir):
        path = os.path.join(step_dir, request["findings_file"])
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"schema_version": "1.0.0", "findings": []}, handle)
        protocol.write_json(
            protocol.result(
                "ok",
                request["analysis"],
                artifacts=[{"path": request["findings_file"], "role": "findings"}],
            ),
            os.path.join(step_dir, request["result_file"]),
        )
        return 0, False

    @staticmethod
    def missing_artifact(request, step_dir):
        protocol.write_json(
            protocol.result(
                "ok",
                request["analysis"],
                artifacts=[{"path": "nothing.json", "role": "findings"}],
            ),
            os.path.join(step_dir, request["result_file"]),
        )
        return 0, False

    # -- the executor interface --------------------------------------------

    def execute(
        self,
        command,
        cwd=None,
        env=None,
        timeout_s=None,
        memory_mb=None,
        stdout_path=None,
        stderr_path=None,
    ):
        request_path = (env or {}).get(protocol.REQUEST_ENV)
        request = protocol.read_request(request_path)
        step_dir = request["output_dir"]
        for path in (stdout_path, stderr_path):
            if path:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("stub log for step {}\n".format(request["step_id"]))
        exit_code, timed_out = self.behaviour(request, step_dir)
        self.calls.append(request["step_id"])
        return execute.ExecutionResult(
            command,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_s=0.01,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            killed=timed_out,
            timeout_s=timeout_s,
            memory_mb=memory_mb,
        )


def silent_reporter():
    """A reporter that swallows progress output; the tests assert on files, not logs."""
    return type(
        "Silent",
        (),
        {"info": lambda *a: None, "warn": lambda *a: None, "error": lambda *a: None},
    )()


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hal_runner_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def make_project_dir(self, name="project", content="netlist content"):
        directory = self.path(name)
        os.makedirs(os.path.join(directory, "sub"), exist_ok=True)
        with open(os.path.join(directory, "design.hal"), "w", encoding="utf-8") as handle:
            handle.write(content)
        with open(os.path.join(directory, "sub", "library.hgl"), "w", encoding="utf-8") as handle:
            handle.write("library")
        return directory

    def write_config(self, document, name="run.json"):
        path = self.path(name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        return path


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------


class HashingTests(TempCase):
    def test_tree_digest_is_stable_and_content_sensitive(self):
        directory = self.make_project_dir()
        first, entries, count, size = hashing.tree_digest(directory)
        second, _, _, _ = hashing.tree_digest(directory)
        self.assertEqual(first, second)
        self.assertEqual(count, 2)
        self.assertEqual(sorted(entry["path"] for entry in entries), ["design.hal", "sub/library.hgl"])
        self.assertGreater(size, 0)

        with open(os.path.join(directory, "design.hal"), "a", encoding="utf-8") as handle:
            handle.write("!")
        self.assertNotEqual(first, hashing.tree_digest(directory)[0])

    def test_tree_digest_separates_name_from_content(self):
        """('ab', 'c') and ('a', 'bc') must not hash the same."""
        one = self.path("one")
        two = self.path("two")
        for directory, (name, content) in ((one, ("ab", "c")), (two, ("a", "bc"))):
            os.makedirs(directory)
            with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write(content)
        self.assertNotEqual(hashing.tree_digest(one)[0], hashing.tree_digest(two)[0])

    def test_describe_input_distinguishes_files_from_directories(self):
        directory = self.make_project_dir()
        described = hashing.describe_input(directory)
        self.assertEqual(described["kind"], "directory")
        self.assertEqual(described["digest_algorithm"], hashing.ALGORITHM_TREE)
        self.assertNotIn("sha256", described)

        archive = self.path("project.zip")
        with zipfile.ZipFile(archive, "w") as handle:
            handle.write(os.path.join(directory, "design.hal"), "project/design.hal")
        described = hashing.describe_input(archive)
        self.assertEqual(described["kind"], "file")
        self.assertEqual(described["digest_algorithm"], hashing.ALGORITHM_FILE)
        self.assertEqual(described["digest"], described["sha256"])

    def test_describe_input_reports_a_missing_input(self):
        with self.assertRaises(FileNotFoundError):
            hashing.describe_input(self.path("nope"))

    def test_json_digest_ignores_key_order(self):
        self.assertEqual(
            hashing.json_digest({"a": 1, "b": [1, 2]}), hashing.json_digest({"b": [1, 2], "a": 1})
        )
        self.assertNotEqual(hashing.json_digest({"a": 1}), hashing.json_digest({"a": 2}))


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


class ConfigTests(TempCase):
    def test_schema_uses_only_keywords_the_validator_enforces(self):
        """Tripwire: the schema must never drift past jsonschema_mini."""
        self.assertEqual(jsonschema_mini.check_schema_support(config_module.load_schema()), [])

    def test_shipped_example_is_valid(self):
        run_config = config_module.load(EXAMPLE_CONFIG)
        self.assertEqual(run_config.name, "uart-baseline")
        self.assertEqual([step.id for step in run_config.steps], ["feedback-loops", "connectivity"])
        self.assertEqual(
            os.path.normpath(run_config.netlist),
            os.path.normpath(os.path.join(REPO_ROOT, "examples", "uart.zip")),
        )
        self.assertTrue(os.path.isfile(run_config.netlist), "examples/uart.zip is missing")

    def test_paths_resolve_relative_to_the_configuration_file(self):
        run_config = config_module.parse(
            minimal_config("netlists/design.hal"), path=self.path("run.json")
        )
        self.assertEqual(run_config.netlist, os.path.join(self.tmp, "netlists", "design.hal"))
        self.assertEqual(run_config.output_dir, os.path.join(self.tmp, "unit-run"))

    def test_limits_default_and_override_per_step(self):
        document = minimal_config(
            "design.hal",
            steps=[
                {"id": "a", "analysis": "graph_algorithm.connected_components"},
                {
                    "id": "b",
                    "analysis": "graph_algorithm.connected_components",
                    "timeout_s": 5,
                },
            ],
            defaults={"timeout_s": 60, "memory_mb": 2048},
        )
        run_config = config_module.parse(document, path=self.path("run.json"))
        self.assertEqual(run_config.steps[0].timeout_s, 60)
        self.assertEqual(run_config.steps[1].timeout_s, 5)
        self.assertEqual(run_config.steps[1].memory_mb, 2048)

    def test_defaults_are_part_of_the_resolved_step_configuration(self):
        run_config = config_module.parse(minimal_config("design.hal"), path=self.path("run.json"))
        resolved = run_config.steps[0].config
        self.assertEqual(resolved["strong"], True)
        self.assertEqual(resolved["max_components"], 64)
        self.assertIn("create_dummy_vertices", resolved)

    def _errors(self, document):
        with self.assertRaises(config_module.ConfigError) as caught:
            config_module.parse(document, path=self.path("run.json"))
        return "\n".join(caught.exception.errors)

    def test_unknown_analysis_is_rejected(self):
        document = minimal_config("design.hal", steps=[{"id": "a", "analysis": "nope"}])
        self.assertIn("unknown analysis", self._errors(document))

    def test_unknown_step_option_is_rejected_rather_than_ignored(self):
        document = minimal_config(
            "design.hal",
            steps=[
                {
                    "id": "a",
                    "analysis": "graph_algorithm.connected_components",
                    "config": {"stong": True},
                }
            ],
        )
        self.assertIn("unknown option 'stong'", self._errors(document))

    def test_option_type_is_checked_and_bool_is_not_an_int(self):
        document = minimal_config(
            "design.hal",
            steps=[
                {
                    "id": "a",
                    "analysis": "graph_algorithm.connected_components",
                    "config": {"min_size": True},
                }
            ],
        )
        self.assertIn("is a boolean", self._errors(document))

    def test_duplicate_step_ids_are_rejected(self):
        step = {"id": "a", "analysis": "graph_algorithm.connected_components"}
        self.assertIn("duplicate step id", self._errors(minimal_config("d.hal", steps=[step, dict(step)])))

    def test_unknown_config_version_is_rejected_outright(self):
        document = minimal_config("design.hal")
        document["config_version"] = "9.9.9"
        self.assertIn("unsupported config_version", self._errors(document))

    def test_missing_required_fields_are_reported(self):
        self.assertIn("missing required property 'netlist'", self._errors({"config_version": "1.0.0", "name": "x", "steps": []}))

    def test_step_ids_must_be_safe_directory_names(self):
        document = minimal_config(
            "design.hal", steps=[{"id": "../escape", "analysis": "graph_algorithm.connected_components"}]
        )
        self.assertIn("step", self._errors(document))


class AnalysesTests(unittest.TestCase):
    def test_every_analysis_is_complete(self):
        for name in analyses.names():
            analysis = analyses.get(name)
            self.assertTrue(analysis.module.startswith("hal_runner.steps."))
            self.assertIn(analysis.method_kind, model.METHOD_KINDS)
            self.assertIn("findings", analysis.artifact_roles)
            self.assertTrue(analysis.description)

    def test_every_plugin_name_is_a_real_python_module(self):
        """Tripwire against the directory-name trap.

        ``plugins/dataflow_analysis`` publishes its bindings as
        ``PYBIND11_MODULE(dataflow, ...)``, so a step that imports
        ``hal_plugins.dataflow_analysis`` fails only once a built HAL runs it.
        The C++ source says which name is real, and it is in this checkout.
        """
        import glob
        import re

        declared = set()
        for path in glob.glob(os.path.join(REPO_ROOT, "plugins", "*", "python", "*.cpp")):
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                declared.update(re.findall(r"PYBIND11_MODULE\(\s*(\w+)", handle.read()))
        self.assertTrue(declared, "no PYBIND11_MODULE declarations found; check the glob")
        for name in analyses.names():
            plugin = analyses.get(name).plugin
            self.assertIn(
                plugin,
                declared,
                "analysis {!r} names the plugin module {!r}, which no plugin in this "
                "checkout declares".format(name, plugin),
            )

    def test_unknown_analysis_lists_the_alternatives(self):
        with self.assertRaises(analyses.UnknownAnalysis) as caught:
            analyses.get("dataflow.grops")
        self.assertIn("dataflow.groups", str(caught.exception))

    def test_resolve_config_rejects_unknown_options(self):
        with self.assertRaises(analyses.AnalysisConfigError):
            analyses.resolve_config(analyses.get("dataflow.groups"), {"min_group": 4})

    def test_resolve_config_allows_a_nullable_option(self):
        resolved = analyses.resolve_config(analyses.get("dataflow.groups"), {"min_group_size": None})
        self.assertIsNone(resolved["min_group_size"])


# ---------------------------------------------------------------------------
# process execution
# ---------------------------------------------------------------------------


class ExecuteTests(TempCase):
    def executor(self):
        return execute.ProcessExecutor(kill_grace_s=1.0)

    def run_python(self, code, timeout_s=None):
        return self.executor().execute(
            [sys.executable, "-c", code],
            cwd=self.tmp,
            env=os.environ.copy(),
            timeout_s=timeout_s,
            stdout_path=self.path("out.log"),
            stderr_path=self.path("err.log"),
        )

    def test_exit_code_and_logs_are_captured(self):
        result = self.run_python("import sys; print('hello'); sys.stderr.write('problem\\n'); sys.exit(7)")
        self.assertEqual(result.exit_code, 7)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.succeeded)
        with open(self.path("out.log"), encoding="utf-8") as handle:
            self.assertIn("hello", handle.read())
        self.assertIn("problem", execute.tail(self.path("err.log")))

    def test_success_is_reported_as_success(self):
        self.assertTrue(self.run_python("print('fine')").succeeded)

    def test_a_step_that_overruns_is_killed_and_its_logs_survive(self):
        result = self.run_python(
            "import sys, time; print('starting', flush=True); time.sleep(60)", timeout_s=1
        )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.succeeded)
        self.assertLess(result.duration_s, 40, "the timeout did not stop the child")
        with open(self.path("out.log"), encoding="utf-8") as handle:
            self.assertIn("starting", handle.read())
        self.assertIn("exceeded its 1s limit", execute.tail(self.path("err.log")))

    @unittest.skipUnless(os.name == "posix", "signal handling is POSIX specific")
    def test_a_child_that_ignores_sigterm_is_killed_anyway(self):
        result = self.run_python(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(60)\n",
            timeout_s=1,
        )
        self.assertTrue(result.timed_out)
        self.assertTrue(result.killed)

    def test_a_missing_binary_is_a_result_not_an_exception(self):
        result = self.executor().execute(
            [self.path("there-is-no-such-binary")],
            cwd=self.tmp,
            stderr_path=self.path("err.log"),
        )
        self.assertEqual(result.exit_code, 127)
        self.assertIn("could not start", execute.tail(self.path("err.log")))


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


class ManifestTests(unittest.TestCase):
    def manifest(self, **overrides):
        document = {
            "manifest_version": "1.0.0",
            "generated_at": "2026-01-01T00:00:00Z",
            "producer": {"name": "hal_runner", "version": "1.0.0", "command": ["a"]},
            "run": {
                "id": "x-1",
                "name": "r",
                "status": "success",
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
                "duration_s": 1.0,
            },
            "tool": {"hal": {"version": "4.5.0", "source": "hal --version", "binary": "/a/hal"}},
            "environment": {"python": "3.12.0"},
            "output_dir": "/tmp/a",
            "inputs": [
                {"role": "netlist", "digest": "abc", "digest_algorithm": "sha256", "resolved_path": "/a"}
            ],
            "steps": [
                {
                    "id": "s",
                    "status": "success",
                    "analysis": "graph_algorithm.connected_components",
                    "duration_s": 0.5,
                    "output_dir": "steps/s",
                    "command": ["hal"],
                    "logs": {"stdout": "a"},
                    "artifacts": [{"path": "findings.json", "sha256": "d"}],
                }
            ],
            "outcome": {"status": "success"},
        }
        document.update(overrides)
        return document

    def test_digest_ignores_timing_paths_and_machine_details(self):
        one = self.manifest()
        two = copy.deepcopy(one)
        two["run"]["id"] = "x-2"
        two["run"]["duration_s"] = 99.0
        two["run"]["started_at"] = "2027-05-05T05:05:05Z"
        two["generated_at"] = "2027-05-05T05:05:05Z"
        two["environment"] = {"python": "3.13.0"}
        two["output_dir"] = "/somewhere/else"
        two["tool"]["hal"]["binary"] = "/elsewhere/hal"
        two["inputs"][0]["resolved_path"] = "/elsewhere"
        two["steps"][0]["duration_s"] = 42.0
        two["steps"][0]["command"] = ["hal", "--other"]
        two["producer"]["command"] = ["different"]
        self.assertEqual(manifest_module.digest(one), manifest_module.digest(two))

    def test_digest_reacts_to_results_inputs_and_versions(self):
        base = manifest_module.digest(self.manifest())
        for mutate in (
            lambda d: d["inputs"][0].__setitem__("digest", "def"),
            lambda d: d["steps"][0].__setitem__("status", "failed"),
            lambda d: d["steps"][0]["artifacts"][0].__setitem__("sha256", "other"),
            lambda d: d["tool"]["hal"].__setitem__("version", "4.6.0"),
        ):
            document = self.manifest()
            mutate(document)
            self.assertNotEqual(base, manifest_module.digest(document))

    def test_serialization_is_deterministic(self):
        document = self.manifest()
        text = manifest_module.dumps(document)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text, manifest_module.dumps(json.loads(text)))

    def test_hal_version_falls_back_to_the_checkout(self):
        info = manifest_module.hal_version_info(None, REPO_ROOT)
        self.assertNotEqual(info["version"], "unknown")
        self.assertIn(info["source"], ("CURRENT_VERSION", "git describe"))


# ---------------------------------------------------------------------------
# checkpoints
# ---------------------------------------------------------------------------


class CheckpointTests(TempCase):
    def setUp(self):
        TempCase.setUp(self)
        self.step = {
            "id": "s",
            "analysis": "graph_algorithm.connected_components",
            "config": {"strong": True, "min_size": 2},
        }
        self.inputs = [
            {"role": "netlist", "digest_algorithm": "sha256", "digest": "a" * 64},
        ]
        self.tool = {
            "hal": {"version": "4.5.0", "source": "hal --version"},
            "findings_schema_version": "1.0.0",
        }
        self.store = checkpoint.CheckpointStore(self.path("cache"))
        self.step_dir = self.path("steps", "s")
        os.makedirs(self.step_dir)
        with open(os.path.join(self.step_dir, "findings.json"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        self.artifacts = [
            {
                "path": "findings.json",
                "role": "findings",
                "sha256": hashing.sha256_file(os.path.join(self.step_dir, "findings.json")),
            }
        ]

    def components(self, **overrides):
        step = dict(self.step)
        inputs = copy.deepcopy(self.inputs)
        tool = copy.deepcopy(self.tool)
        if "config" in overrides:
            step["config"] = overrides["config"]
        if "digest" in overrides:
            inputs[0]["digest"] = overrides["digest"]
        if "hal_version" in overrides:
            tool["hal"]["version"] = overrides["hal_version"]
        if "hal_source" in overrides:
            tool["hal"]["source"] = overrides["hal_source"]
        return checkpoint.key_components(step, inputs, tool)

    def store_entry(self):
        components = self.components()
        key = checkpoint.cache_key(components)
        self.store.store(key, components, self.step_dir, self.artifacts, {"status": "ok"})
        return key, components

    def test_a_stored_entry_is_found_and_restored(self):
        key, components = self.store_entry()
        entry, reason = self.store.lookup(key, components)
        self.assertIsNotNone(entry, reason)
        target = self.path("restored")
        restored = self.store.restore(entry, target)
        self.assertEqual(restored, ["findings.json"])
        self.assertTrue(os.path.isfile(os.path.join(target, "findings.json")))

    def test_a_changed_input_config_or_hal_version_is_a_miss(self):
        self.store_entry()
        for label, components in (
            ("input", self.components(digest="b" * 64)),
            ("config", self.components(config={"strong": False, "min_size": 2})),
            ("hal version", self.components(hal_version="4.6.0")),
            ("version source", self.components(hal_source="git describe")),
        ):
            key = checkpoint.cache_key(components)
            entry, reason = self.store.lookup(key, components)
            self.assertIsNone(entry, "a changed {} must not hit the cache".format(label))
            self.assertEqual(reason, "no entry for this key")

    def test_a_tampered_artifact_is_rejected(self):
        key, components = self.store_entry()
        cached = os.path.join(self.store.entry_dir(key), checkpoint.ARTIFACT_DIR, "findings.json")
        with open(cached, "a", encoding="utf-8") as handle:
            handle.write("tampered")
        entry, reason = self.store.lookup(key, components)
        self.assertIsNone(entry)
        self.assertIn("no longer matches its recorded sha256", reason)

    def test_a_deleted_artifact_is_rejected(self):
        key, components = self.store_entry()
        os.remove(os.path.join(self.store.entry_dir(key), checkpoint.ARTIFACT_DIR, "findings.json"))
        entry, reason = self.store.lookup(key, components)
        self.assertIsNone(entry)
        self.assertIn("missing", reason)

    def test_an_entry_whose_components_were_edited_is_rejected(self):
        key, components = self.store_entry()
        record_path = os.path.join(self.store.entry_dir(key), checkpoint.ENTRY_FILE)
        record = manifest_module.read(record_path)
        record["key_components"]["config"]["min_size"] = 99
        manifest_module.write(record, record_path)
        entry, reason = self.store.lookup(key, components)
        self.assertIsNone(entry)
        self.assertIn("key components do not match", reason)

    def test_an_entry_from_another_cache_format_is_rejected(self):
        key, components = self.store_entry()
        record_path = os.path.join(self.store.entry_dir(key), checkpoint.ENTRY_FILE)
        record = manifest_module.read(record_path)
        record["cache_format"] = "0.9.0"
        manifest_module.write(record, record_path)
        entry, reason = self.store.lookup(key, components)
        self.assertIsNone(entry)
        self.assertIn("cache format", reason)

    def test_a_corrupt_entry_is_rejected(self):
        key, components = self.store_entry()
        with open(os.path.join(self.store.entry_dir(key), checkpoint.ENTRY_FILE), "w") as handle:
            handle.write("{not json")
        entry, reason = self.store.lookup(key, components)
        self.assertIsNone(entry)
        self.assertIn("unreadable", reason)

    def test_disabled_and_refreshed_stores_never_hit(self):
        key, components = self.store_entry()
        self.assertIsNone(
            checkpoint.CheckpointStore(self.path("cache"), enabled=False).lookup(key, components)[0]
        )
        entry, reason = checkpoint.CheckpointStore(self.path("cache"), refresh=True).lookup(
            key, components
        )
        self.assertIsNone(entry)
        self.assertEqual(reason, "cache refresh requested")


# ---------------------------------------------------------------------------
# the orchestrator
# ---------------------------------------------------------------------------


class RunnerTests(TempCase):
    def build(self, behaviour=None, steps=None, netlist=None, use_cache=True, **overrides):
        netlist = netlist or self.make_project_dir()
        document = minimal_config(netlist, steps=steps, **overrides)
        run_config = config_module.parse(document, path=self.path("run.json"))
        executor = StubExecutor(behaviour)
        runner = Runner(
            run_config,
            output_dir=self.path("out"),
            hal_binary=sys.executable,
            executor=executor,
            use_cache=use_cache,
            reporter=silent_reporter(),
        )
        runner.prepare()
        return runner, executor, netlist

    def test_a_successful_run_records_everything(self):
        runner, _, netlist = self.build()
        exit_code, document = runner.run()

        self.assertEqual(exit_code, 0)
        self.assertEqual(document["run"]["status"], "success")
        self.assertTrue(os.path.isfile(self.path("out", "manifest.json")))

        netlist_input = document["inputs"][0]
        self.assertEqual(netlist_input["digest_algorithm"], hashing.ALGORITHM_TREE)
        self.assertEqual(netlist_input["digest"], hashing.tree_digest(netlist)[0])

        step = document["steps"][0]
        self.assertEqual(step["status"], "success")
        self.assertEqual(step["artifacts"][0]["path"], "findings.json")
        self.assertEqual(step["findings"]["counts"], {"heuristic": 1})
        self.assertEqual(step["cache"]["status"], "miss")
        self.assertTrue(document["tool"]["hal"]["version"])
        self.assertEqual(
            document["configuration"]["steps"][0]["config"]["max_components"], 64
        )

    def test_the_request_tells_the_step_everything_it_needs(self):
        runner, _, netlist = self.build()
        runner.run()
        request = protocol.read_request(self.path("out", "steps", "components", "request.json"))
        self.assertEqual(request["analysis"], "graph_algorithm.connected_components")
        self.assertEqual(request["netlist"], netlist)
        self.assertEqual(request["tools_path"], os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(request["config"]["strong"], True)
        self.assertIn("unhashed_reason", request["pin"])

    def test_a_second_run_reuses_the_checkpoint_and_a_changed_netlist_does_not(self):
        runner, executor, netlist = self.build()
        self.assertEqual(runner.run()[0], 0)

        again, executor2, _ = self.build()
        exit_code, document = again.run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["steps"][0]["status"], "reused")
        self.assertEqual(executor2.calls, [], "a reused step must not run the analysis")
        self.assertTrue(os.path.isfile(self.path("out", "steps", "components", "findings.json")))

        with open(os.path.join(netlist, "design.hal"), "a", encoding="utf-8") as handle:
            handle.write(" modified")
        third, executor3, _ = self.build(netlist=netlist)
        exit_code, document = third.run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["steps"][0]["status"], "success")
        self.assertEqual(executor3.calls, ["components"], "a changed netlist must re-run the step")

    def test_a_cached_result_that_no_longer_validates_is_not_reused(self):
        runner, _, _ = self.build()
        runner.run()
        key = runner.plan()[0]["cache_key"]
        cached = os.path.join(
            runner.cache.entry_dir(key), checkpoint.ARTIFACT_DIR, "findings.json"
        )
        with open(cached, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        # The recorded hash no longer matches, so the entry is rejected outright.
        again, executor, _ = self.build()
        _, document = again.run()
        self.assertEqual(document["steps"][0]["status"], "success")
        self.assertEqual(executor.calls, ["components"])

    def test_a_failed_step_fails_the_run_and_leaves_a_diagnostic(self):
        runner, _, _ = self.build(
            behaviour=StubExecutor.crash,
            steps=[
                {"id": "first", "analysis": "graph_algorithm.connected_components"},
                {"id": "second", "analysis": "graph_algorithm.connected_components"},
            ],
        )
        exit_code, document = runner.run()

        self.assertEqual(exit_code, 1)
        self.assertEqual(document["run"]["status"], "failure")
        first, second = document["steps"]
        self.assertEqual(first["status"], "failed")
        self.assertIn("hal exited with 3", first["diagnostic"]["message"])
        self.assertEqual(second["status"], "skipped")

        diagnostic_path = self.path("out", "steps", "first", "diagnostic.json")
        self.assertTrue(os.path.isfile(diagnostic_path))
        with open(diagnostic_path, encoding="utf-8") as handle:
            diagnostic = json.load(handle)
        findings_validate.validate_document(diagnostic)
        self.assertEqual(diagnostic["findings"][0]["status"], "error")
        self.assertTrue(os.path.isfile(self.path("out", "steps", "first", "logs", "stderr.log")))

    def test_continue_on_failure_runs_the_remaining_steps_but_still_fails(self):
        runner, executor, _ = self.build(
            behaviour=StubExecutor.crash,
            steps=[
                {
                    "id": "first",
                    "analysis": "graph_algorithm.connected_components",
                    "continue_on_failure": True,
                },
                {"id": "second", "analysis": "graph_algorithm.connected_components"},
            ],
        )
        exit_code, document = runner.run()
        self.assertEqual(exit_code, 1)
        self.assertEqual([step["status"] for step in document["steps"]], ["failed", "failed"])
        self.assertEqual(executor.calls, ["first", "second"])

    def test_a_timed_out_step_is_recorded_as_a_timeout(self):
        runner, _, _ = self.build(
            behaviour=StubExecutor.hang,
            steps=[
                {
                    "id": "slow",
                    "analysis": "graph_algorithm.connected_components",
                    "timeout_s": 30,
                }
            ],
        )
        exit_code, document = runner.run()
        self.assertEqual(exit_code, 1)
        self.assertEqual(document["steps"][0]["status"], "timeout")

        with open(self.path("out", "steps", "slow", "diagnostic.json"), encoding="utf-8") as handle:
            diagnostic = json.load(handle)
        findings_validate.validate_document(diagnostic)
        finding = diagnostic["findings"][0]
        self.assertEqual(finding["status"], "timeout")
        self.assertEqual(finding["limits"]["timeout_s"], 30)
        self.assertTrue(finding["limits"]["hit"])

    def test_a_step_that_exits_cleanly_without_a_result_has_still_failed(self):
        runner, _, _ = self.build(behaviour=StubExecutor.silent_success)
        exit_code, document = runner.run()
        self.assertEqual(exit_code, 1)
        self.assertIn("wrote no result record", document["steps"][0]["diagnostic"]["message"])

    def test_an_invalid_findings_document_fails_the_step(self):
        runner, _, _ = self.build(behaviour=StubExecutor.invalid_findings)
        exit_code, document = runner.run()
        self.assertEqual(exit_code, 1)
        self.assertIn("does not validate", document["steps"][0]["diagnostic"]["message"])

    def test_a_declared_but_unwritten_artifact_fails_the_step(self):
        runner, _, _ = self.build(behaviour=StubExecutor.missing_artifact)
        exit_code, document = runner.run()
        self.assertEqual(exit_code, 1)
        self.assertIn("did not write it", document["steps"][0]["diagnostic"]["message"])

    def test_a_project_archive_is_pinned_by_its_own_sha256_and_unpacked(self):
        netlist = self.make_project_dir()
        archive = self.path("uart.zip")
        with zipfile.ZipFile(archive, "w") as handle:
            handle.write(os.path.join(netlist, "design.hal"), "uart/design.hal")
        run_config = config_module.parse(minimal_config(archive), path=self.path("run.json"))
        runner = Runner(
            run_config,
            output_dir=self.path("out"),
            hal_binary=sys.executable,
            executor=StubExecutor(),
            reporter=silent_reporter(),
        )
        runner.prepare()
        _, document = runner.run()

        netlist_input = document["inputs"][0]
        self.assertEqual(netlist_input["sha256"], hashing.sha256_file(archive))
        self.assertEqual(runner.netlist_for_steps, self.path("out", "inputs", "uart", "uart"))
        self.assertTrue(os.path.isfile(os.path.join(runner.netlist_for_steps, "design.hal")))
        self.assertEqual(runner.pin["sha256"], hashing.sha256_file(archive))
        self.assertEqual(runner.pin["kind"], "hal_project")

    def test_a_missing_netlist_stops_the_run_before_it_starts(self):
        run_config = config_module.parse(minimal_config(self.path("nope")), path=self.path("run.json"))
        runner = Runner(run_config, output_dir=self.path("out"), hal_binary=sys.executable)
        with self.assertRaises(RunnerError):
            runner.prepare()

    def test_selecting_an_unknown_step_is_an_error(self):
        runner, _, _ = self.build()
        runner.only = ["nope"]
        with self.assertRaises(RunnerError):
            runner.steps

    def test_the_manifest_digest_is_stable_across_reruns(self):
        """Two runs of the same configuration on the same inputs must be indistinguishable.

        The cache is off on purpose: with it on the second run would legitimately
        report ``reused`` instead of ``success``, which is a different outcome and
        *should* change the digest.
        """
        runner, _, netlist = self.build(use_cache=False)
        _, first = runner.run()
        again, _, _ = self.build(netlist=netlist, use_cache=False)
        _, second = again.run()
        self.assertEqual(manifest_module.digest(first), manifest_module.digest(second))
        self.assertNotEqual(first["run"]["id"], second["run"]["id"])


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------


class CliTests(TempCase):
    class Stream(object):
        def __init__(self):
            self.text = ""

        def write(self, chunk):
            self.text += chunk

    def call(self, argv):
        """Run the CLI, capturing stdout separately from the stderr diagnostics."""
        stream = self.Stream()
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = cli.main(argv, stream=stream)
        self.stderr = errors.getvalue()
        return code, stream.text

    def test_validate_accepts_the_shipped_example(self):
        code, text = self.call(["validate", EXAMPLE_CONFIG])
        self.assertEqual(code, 0)
        self.assertIn("uart-baseline", text)

    def test_validate_reports_a_broken_configuration(self):
        path = self.write_config(minimal_config("d.hal", steps=[{"id": "a", "analysis": "nope"}]))
        code, text = self.call(["validate", path])
        self.assertEqual(code, 1)
        self.assertIn("FAIL", text)

    def test_analyses_lists_every_registered_analysis(self):
        code, text = self.call(["analyses"])
        self.assertEqual(code, 0)
        for name in analyses.names():
            self.assertIn(name, text)

    def test_schema_path_points_at_the_shipped_schema(self):
        code, text = self.call(["schema", "--path"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(text.strip()))

    def test_dry_run_plans_without_a_hal_binary_and_writes_nothing(self):
        netlist = self.make_project_dir()
        path = self.write_config(minimal_config(netlist, output_dir=self.path("out")))
        code, text = self.call(["run", path, "--dry-run", "-q"])
        self.assertEqual(code, 0)
        plan = json.loads(text)
        self.assertEqual(len(plan["steps"]), 1)
        self.assertEqual(len(plan["steps"][0]["cache_key"]), 64)
        self.assertIn("--python-script", plan["steps"][0]["command"])
        self.assertFalse(os.path.exists(self.path("out")), "a dry run must not write anything")

    def test_dry_run_locates_a_project_archive_without_unpacking_it(self):
        netlist = self.make_project_dir()
        archive = self.path("uart.zip")
        with zipfile.ZipFile(archive, "w") as handle:
            handle.write(os.path.join(netlist, "design.hal"), "uart/design.hal")
        path = self.write_config(minimal_config(archive, output_dir=self.path("out")))
        code, text = self.call(["run", path, "--dry-run", "-q"])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(text)["inputs"][0]["extracted_to"],
            os.path.join(self.path("out"), "inputs", "uart", "uart"),
        )
        self.assertFalse(os.path.exists(self.path("out")))

    def test_a_broken_configuration_exits_two(self):
        path = self.write_config({"config_version": "1.0.0"})
        self.assertEqual(self.call(["run", path])[0], 2)
        self.assertIn("missing required property 'netlist'", self.stderr)

    def test_manifest_summary_and_digest(self):
        netlist = self.make_project_dir()
        run_config = config_module.parse(minimal_config(netlist), path=self.path("run.json"))
        runner = Runner(
            run_config,
            output_dir=self.path("out"),
            hal_binary=sys.executable,
            executor=StubExecutor(),
            reporter=silent_reporter(),
        )
        runner.prepare()
        runner.run()
        path = self.path("out", "manifest.json")

        code, text = self.call(["manifest", path])
        self.assertEqual(code, 0)
        self.assertIn("success", text)

        code, text = self.call(["manifest", path, "--digest"])
        self.assertEqual(code, 0)
        self.assertEqual(len(text.strip()), 64)

    def test_no_command_prints_help(self):
        self.assertEqual(self.call([])[0], 2)


# ---------------------------------------------------------------------------
# the step entry point, as a file
# ---------------------------------------------------------------------------


class StepRunnerTests(TempCase):
    """The bootstrap script is checked as a *script*, because that is how HAL runs it."""

    SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steps", "step_runner.py")

    def run_script(self, env):
        environment = os.environ.copy()
        environment.pop(protocol.REQUEST_ENV, None)
        environment.update(env)
        return subprocess.run(
            [sys.executable, self.SCRIPT],
            cwd=self.tmp,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_without_a_request_it_refuses_to_run(self):
        completed = self.run_script({})
        self.assertEqual(completed.returncode, 2)
        self.assertIn(b"is not set", completed.stderr)

    def test_an_unreadable_request_is_reported(self):
        completed = self.run_script({protocol.REQUEST_ENV: self.path("nope.json")})
        self.assertEqual(completed.returncode, 2)
        self.assertIn(b"could not read the step request", completed.stderr)

    def test_a_bad_tools_path_leaves_an_error_result_behind(self):
        step_dir = self.path("step")
        os.makedirs(step_dir)
        request_path = self.path("request.json")
        protocol.write_json(
            {
                "request_version": "1.0.0",
                "analysis": "graph_algorithm.connected_components",
                "tools_path": self.path("not-the-tools-directory"),
                "output_dir": step_dir,
                "result_file": "result.json",
            },
            request_path,
        )
        completed = self.run_script({protocol.REQUEST_ENV: request_path})
        self.assertEqual(completed.returncode, 2)
        result = protocol.read_result(os.path.join(step_dir, "result.json"))
        self.assertEqual(result["status"], "error")
        self.assertIn("could not import hal_runner", result["error"]["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
