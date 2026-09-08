"""Integration tests that need a built HAL; skipped everywhere else.

These are the tests that prove the adapters speak to the *real* bindings rather
than to the stubs in ``test_hal_findings.py``.  They cannot run on a machine
without a HAL build (this fork builds on Linux/macOS only), so they skip unless
both of the following hold:

* ``hal_py`` is importable -- directly, or via ``HAL_PY_PATH`` /
  ``--hal-lib``-style paths as used by ``tools/hal_viz``;
* ``HAL_FINDINGS_NETLIST`` points at a netlist file or an unpacked HAL project
  directory (``HAL_FINDINGS_GATE_LIBRARY`` in addition, for netlist formats
  that do not embed their library).

Run inside the project's build/verification container, from the repo root::

    unzip -o examples/toy_cipher.zip -d /tmp/toy_cipher
    export HAL_PY_PATH=/path/to/hal/build/lib
    export HAL_FINDINGS_NETLIST=/tmp/toy_cipher/<project-dir>
    python -m unittest discover -s tools/hal_findings -t tools -p "test_*_hal.py"

Set ``HAL_FINDINGS_OUTPUT_DIR`` to keep the produced findings documents (for
example as a CI artifact) instead of writing them to a temporary directory.
"""

import os
import sys
import tempfile
import unittest

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_findings import model, serialize, validate  # noqa: E402
from hal_findings.adapters import dataflow as dataflow_adapter  # noqa: E402
from hal_findings.adapters import netlist_comparison as comparison_adapter  # noqa: E402

NETLIST_ENV = "HAL_FINDINGS_NETLIST"
LIBRARY_ENV = "HAL_FINDINGS_GATE_LIBRARY"
OUTPUT_ENV = "HAL_FINDINGS_OUTPUT_DIR"


def _requirements():
    """Return ``(hal_py, halenv, netlist_path)`` or ``None`` with a skip reason."""
    netlist_path = os.environ.get(NETLIST_ENV)
    if not netlist_path:
        return None, "{} is not set".format(NETLIST_ENV)
    if not os.path.exists(netlist_path):
        return None, "{}={} does not exist".format(NETLIST_ENV, netlist_path)
    try:
        from hal_viz import halenv  # the repo's existing hal_py bootstrap
    except ImportError as exc:
        return None, "tools/hal_viz is not importable: {}".format(exc)
    try:
        hal_py = halenv.import_hal_py()
    except Exception as exc:
        return None, "hal_py unavailable: {}".format(exc)
    return (hal_py, halenv, netlist_path), None


_REQUIREMENTS, _SKIP_REASON = _requirements()


@unittest.skipIf(_REQUIREMENTS is None, _SKIP_REASON or "HAL is unavailable")
class HalAdapterIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hal_py, cls.halenv, cls.netlist_path = _REQUIREMENTS
        cls.hal_py.plugin_manager.load_all_plugins()
        cls.library = os.environ.get(LIBRARY_ENV)
        cls.output_dir = os.environ.get(OUTPUT_ENV)
        if cls.output_dir:
            os.makedirs(cls.output_dir, exist_ok=True)
        cls._temp = None if cls.output_dir else tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        if cls._temp is not None:
            cls._temp.cleanup()
        try:
            cls.hal_py.plugin_manager.unload_all_plugins()
        except Exception:
            pass

    def _load(self):
        return self.halenv.load_netlist(self.hal_py, self.netlist_path, self.library)

    def _write(self, document, name):
        directory = self.output_dir or self._temp.name
        return serialize.write_document(document, os.path.join(directory, name))

    def test_dataflow_result_maps_to_valid_findings(self):
        from hal_plugins import dataflow

        netlist = self._load()
        config = dataflow.Configuration(netlist).with_flip_flops()
        result = dataflow.analyze(config)
        self.assertIsNotNone(result, "dataflow.analyze returned None")

        pin_types = [
            ("clock", self.hal_py.PinType.clock),
            ("enable", self.hal_py.PinType.enable),
            ("reset", self.hal_py.PinType.reset),
            ("set", self.hal_py.PinType.set),
        ]
        document = dataflow_adapter.build_document(
            result,
            artifact_id="netlist",
            configuration=config,
            control_pin_types=pin_types,
            netlist_path=self.netlist_path,
        )
        self.assertEqual([], validate.collect_errors(document))

        path = self._write(document, "dataflow-findings.json")
        reread = serialize.read_document(path)
        self.assertEqual(
            serialize.document_digest(document), serialize.document_digest(reread)
        )

        for finding in document["findings"]:
            self.assertIn(
                finding["status"], (model.STATUS_HEURISTIC, model.STATUS_UNSUPPORTED)
            )
            self.assertFalse(model.is_unbounded_proof(finding))
            for gate in finding["scope"].get("gates", []):
                self.assertEqual("netlist", gate["artifact_id"])
                self.assertIsNotNone(netlist.get_gate_by_id(gate["id"]))

    def test_compare_netlists_of_two_loads_of_the_same_design(self):
        from hal_plugins import z3_utils

        netlist_a = self._load()
        netlist_b = self._load()
        document = comparison_adapter.run_compare_netlists(
            z3_utils,
            netlist_a,
            netlist_b,
            fail_on_unknown=True,
            solver_timeout=30,
            netlist_path_a=self.netlist_path,
            netlist_path_b=self.netlist_path,
        )
        self.assertEqual([], validate.collect_errors(document))
        self._write(document, "compare-netlists-findings.json")

        verdict = [
            finding
            for finding in document["findings"]
            if finding["id"].endswith("/equivalence")
        ][0]
        # Two loads of the same file are equivalent, so with fail_on_unknown the
        # honest outcomes are a proof or -- if the solver could not decide every
        # query -- unknown/error. Never a counterexample.
        self.assertIn(
            verdict["status"],
            (
                model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
                model.STATUS_UNKNOWN,
                model.STATUS_ERROR,
            ),
            verdict.get("summary"),
        )
        if verdict["status"] == model.STATUS_PROVEN_UNDER_ASSUMPTIONS:
            self.assertTrue(model.is_unbounded_proof(verdict))
            self.assertTrue(verdict["assumptions"])

    def test_artifacts_are_pinned_by_content_hash_when_possible(self):
        from hal_plugins import dataflow

        netlist = self._load()
        result = dataflow.analyze(dataflow.Configuration(netlist).with_flip_flops())
        document = dataflow_adapter.build_document(
            result, netlist_path=self.netlist_path
        )
        artifact = document["artifacts"][0]
        if os.path.isfile(self.netlist_path):
            self.assertEqual(64, len(artifact["sha256"]))
        else:
            self.assertIn("unhashed_reason", artifact)


if __name__ == "__main__":
    unittest.main(verbosity=2)
