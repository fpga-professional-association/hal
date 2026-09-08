"""Integration tests that need a built HAL: the ``hal_py`` path must agree.

The offline tests prove the analysis is right about the fixture.  They cannot
prove that ``hal_source`` builds the same circuit from the same netlist, and
that is exactly where a binding rename, a changed gate-type property or a
different ``FFComponent`` shape would break the tool.  So these tests load the
*same* fixture through ``hal_py``, run the *same* recovery, and require the
result to be behaviourally identical to the offline one -- and to the hand
written ground truth.

Gate and net ids differ between the two paths (instance names versus HAL object
ids), so the comparison is on
:func:`hal_apb_recover.regmap.behavioural_summary`, which contains no
netlist-local identifiers.

Run against a build tree with::

    export HAL_PY_PATH=<build>/lib
    export HAL_BASE_PATH=<build>
    python -m unittest discover -s tools/hal_apb_recover -t tools -p "test_*_hal.py"

Without ``HAL_PY_PATH`` (and no importable ``hal_py``) the tests skip; with it
set they never skip -- a missing binding is a failure, because a test that
turns itself off is worse than no test.
"""

import json
import os
import sys
import unittest

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from hal_apb_recover import regmap, replay  # noqa: E402
from hal_apb_recover.hgl_library import GateLibrary  # noqa: E402
from hal_apb_recover.mapping import load_mapping  # noqa: E402
from hal_apb_recover.recover import recover  # noqa: E402
from hal_apb_recover.verilog_source import read_netlist  # noqa: E402
from hal_findings import validate as findings_validate  # noqa: E402
from hal_apb_recover import findings as findings_module  # noqa: E402

REPO_ROOT = os.path.dirname(_TOOLS)
FIXTURE_DIR = os.path.join(_TOOLS, "hal_apb_recover", "fixtures", "apb_regs")
NETLIST = os.path.join(FIXTURE_DIR, "apb_regs.v")
MAPPING = os.path.join(FIXTURE_DIR, "mapping.json")
GROUND_TRUTH = os.path.join(FIXTURE_DIR, "ground_truth.json")
GATE_LIBRARY = os.path.join(
    REPO_ROOT, "plugins", "gate_libraries", "definitions", "NangateOpenCellLibrary.hgl"
)

HAL_PY_PATH = os.environ.get("HAL_PY_PATH")


def _import_hal_py():
    if HAL_PY_PATH:
        path = os.path.abspath(os.path.expanduser(HAL_PY_PATH))
        if not os.path.isdir(path):
            raise AssertionError(
                "HAL_PY_PATH is set to {!r}, which is not a directory".format(HAL_PY_PATH)
            )
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        import hal_py
    except ImportError as exc:
        if HAL_PY_PATH:
            raise AssertionError(
                "HAL_PY_PATH is set but hal_py could not be imported: {}".format(exc)
            )
        return None
    return hal_py


HAL_PY = _import_hal_py()


@unittest.skipIf(
    HAL_PY is None,
    "hal_py is not importable; set HAL_PY_PATH=<build>/lib to run the integration tests",
)
class HalPathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from hal_apb_recover.hal_source import build_circuit, load_netlist

        HAL_PY.plugin_manager.load_all_plugins()
        netlist = load_netlist(HAL_PY, NETLIST, GATE_LIBRARY)
        cls.hal_circuit = build_circuit(HAL_PY, netlist)
        cls.hal_mapping = load_mapping(MAPPING, cls.hal_circuit)
        cls.hal_document = recover(cls.hal_circuit, cls.hal_mapping)

        library = GateLibrary.from_file(GATE_LIBRARY)
        cls.offline_circuit = read_netlist(NETLIST, library)
        cls.offline_mapping = load_mapping(MAPPING, cls.offline_circuit)
        cls.offline_document = recover(cls.offline_circuit, cls.offline_mapping)

        with open(GROUND_TRUTH, "r", encoding="utf-8") as handle:
            cls.truth = json.load(handle)

    # -- the circuit models must agree -------------------------------------

    def test_hal_builds_the_same_circuit_shape(self):
        hal = self.hal_circuit.statistics()
        offline = self.offline_circuit.statistics()
        for key in ("gates", "inputs", "outputs", "flip_flops", "unsupported_gates"):
            self.assertEqual(hal[key], offline[key], "{} differs".format(key))
        self.assertEqual(self.hal_circuit.combinational_loop, [])

    def test_hal_reports_the_same_unmodelled_primitive(self):
        self.assertEqual(
            sorted(gate.type_name for gate in self.hal_circuit.unsupported_gates),
            sorted(gate.type_name for gate in self.offline_circuit.unsupported_gates),
        )

    def test_reset_values_come_out_of_the_ff_components(self):
        # SCRATCH resets to 0xBEEF, which only works if HAL's FFComponent async
        # set/reset functions are read correctly (DFFS for the 1 bits).
        scratch = [
            register
            for register in self.hal_document["registers"]
            if register["address"] == 0x10
        ]
        self.assertEqual(len(scratch), 1)
        self.assertEqual(scratch[0]["reset_value"]["value"], "0xbeef")
        self.assertTrue(scratch[0]["reset_value"]["complete"])

    # -- the recovery must agree -------------------------------------------

    def test_the_recovered_map_is_identical_to_the_offline_one(self):
        self.assertEqual(
            regmap.behavioural_summary(self.hal_document),
            regmap.behavioural_summary(self.offline_document),
        )

    def test_the_recovered_map_matches_the_ground_truth(self):
        by_address = {
            register["address"]: register for register in self.hal_document["registers"]
        }
        for truth in self.truth["registers"]:
            register = by_address[truth["address"]]
            self.assertEqual(register["access"], truth["access"])
            self.assertEqual(register["reset_value"]["value"], truth["reset_value"])
            fields = {field["bit"]: field for field in register["fields"]}
            for expected in truth["fields"]:
                field = fields[expected["bit"]]
                where = "0x{:02x}[{}]".format(truth["address"], expected["bit"])
                self.assertEqual(field["access"], expected["access"], where)
                self.assertEqual(
                    field["confidence"], expected["expected_confidence"], where
                )
        self.assertEqual(
            sorted(entry["address"] for entry in self.hal_document["unmapped_addresses"]),
            sorted(self.truth["unmapped_addresses"]),
        )
        self.assertEqual(
            [entry["addresses"] for entry in self.hal_document["alias_classes"]],
            [[0x00, 0x08]],
        )

    def test_the_document_and_findings_validate(self):
        regmap.validate_document(self.hal_document)
        document = findings_module.build_document(
            self.hal_document, netlist_path=NETLIST
        )
        findings_validate.validate_document(document)

    def test_the_generated_replay_passes_on_the_hal_netlist(self):
        document = replay.build_replay(self.hal_document)
        results = replay.run_replay(self.hal_circuit, self.hal_mapping, document)
        ok, lines = replay.format_results(results)
        self.assertTrue(ok, "\n".join(lines))

    def test_a_wrong_map_still_fails_on_the_hal_netlist(self):
        import copy

        broken = copy.deepcopy(self.hal_document)
        for register in broken["registers"]:
            if register["address"] != 0x04:
                continue
            for field in register["fields"]:
                if field["kind"] == "storage":
                    field["template"] = "write"
        results = replay.run_replay(
            self.hal_circuit, self.hal_mapping, replay.build_replay(broken)
        )
        ok, _ = replay.format_results(results)
        self.assertFalse(ok)


@unittest.skipIf(HAL_PY is None, "hal_py is not importable")
class HalBindingSurfaceTest(unittest.TestCase):
    """The exact bindings ``hal_source`` drives; a rename fails here loudly."""

    def test_the_bindings_the_recovery_depends_on_exist(self):
        for name in ("NetlistFactory", "BooleanFunction", "FFComponent", "plugin_manager"):
            self.assertTrue(hasattr(HAL_PY, name), "hal_py has no {}".format(name))
        for name in ("ZERO", "ONE", "X"):
            self.assertTrue(
                hasattr(HAL_PY.BooleanFunction.Value, name),
                "hal_py.BooleanFunction.Value has no {}".format(name),
            )
        for name in (
            "get_next_state_function",
            "get_clock_function",
            "get_async_reset_function",
            "get_async_set_function",
            "is_class_of",
        ):
            self.assertTrue(
                hasattr(HAL_PY.FFComponent, name),
                "hal_py.FFComponent has no {}".format(name),
            )

    def test_boolean_function_evaluation_keeps_controlling_values(self):
        # The whole "proven under assumptions" tier rests on this: an AND with
        # one input at 0 must evaluate to 0 even when the other is unknown.
        function = HAL_PY.BooleanFunction.from_string("A & B")
        self.assertFalse(function.is_empty(), "hal_py could not parse 'A & B'")
        zero = HAL_PY.BooleanFunction.Value.ZERO
        one = HAL_PY.BooleanFunction.Value.ONE
        self.assertEqual(function.evaluate({"A": zero}), zero)
        self.assertNotEqual(function.evaluate({"A": one}), one)


if __name__ == "__main__":
    unittest.main()
