"""Integration tests that need a built HAL; skipped everywhere else.

``test_hal_agilex.py`` proves the primitive semantics against the vendor export
without HAL.  These tests prove the other half: that the ``AGILEX_TENNM`` gate
library parses, that HAL's Verilog parser reads the imported netlist the way
this package expects, and that the semantics this package attaches survive as
real ``BooleanFunction`` objects on real gates.

They skip unless ``hal_py`` is importable -- directly, or via ``HAL_PY_PATH``
as used by ``tools/hal_viz``.  Run inside the project's build container from the
repository root::

    export HAL_PY_PATH=/opt/hal/build/lib
    export HAL_BASE_PATH=/opt/hal/build
    python -m unittest discover -s tools/hal_agilex -t tools -p "test_*_hal.py"

``HAL_AGILEX_GATE_LIBRARY`` overrides the gate library path (it defaults to the
copy in the checkout, which is what a build tree installs).
"""

import os
import sys
import unittest

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_agilex import hal_adapter, library, primitives, recognize, vo_netlist  # noqa: E402
from hal_findings import model, validate  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
FIXTURES = os.path.join(HERE, "fixtures")
COUNTER = os.path.join(FIXTURES, "agilex3_counter_adder")
LUT_LOGIC = os.path.join(FIXTURES, "agilex3_lut_logic")

GATE_LIBRARY = os.environ.get(
    "HAL_AGILEX_GATE_LIBRARY", os.path.join(REPO_ROOT, library.LIBRARY_PATH)
)


def _import_hal():
    try:
        from hal_viz import halenv
    except ImportError as exc:
        return None, "tools/hal_viz is not importable: {}".format(exc)
    try:
        return halenv.import_hal_py(), None
    except Exception as exc:  # noqa: BLE001 - any failure means "no HAL here"
        return None, "hal_py unavailable: {}".format(exc)


_HAL_PY, _SKIP_REASON = _import_hal()


@unittest.skipIf(_HAL_PY is None, _SKIP_REASON or "HAL is unavailable")
class AgilexHalIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hal_py = _HAL_PY
        if not os.path.isfile(GATE_LIBRARY):
            raise unittest.SkipTest("gate library not found at " + GATE_LIBRARY)

    def _load(self, directory, name):
        return hal_adapter.load_netlist(
            self.hal_py, os.path.join(directory, name), GATE_LIBRARY
        )

    # -- the gate library -------------------------------------------------

    def test_gate_library_parses_and_declares_the_primitives(self):
        gate_library = self.hal_py.GateLibraryManager.load(GATE_LIBRARY)
        self.assertIsNotNone(
            gate_library, "HAL could not parse " + GATE_LIBRARY
        )
        names = {gate_type.get_name() for gate_type in gate_library.get_gate_types().values()}
        self.assertIn(primitives.LCELL, names)
        self.assertIn(primitives.FF, names)
        self.assertTrue(gate_library.get_gnd_gate_types())
        self.assertTrue(gate_library.get_vcc_gate_types())

        lcell = gate_library.get_gate_types()[primitives.LCELL]
        declared = {pin.get_name() for pin in lcell.get_pins()}
        self.assertLessEqual(set(primitives.LCELL_INPUT_PINS), declared)
        self.assertLessEqual(set(primitives.LCELL_OUTPUT_PINS), declared)

    # -- import -----------------------------------------------------------

    def test_imported_counter_loads_with_the_expected_structure(self):
        netlist = self._load(COUNTER, "counter_adder.hal.v")
        histogram = {}
        for gate in netlist.get_gates():
            name = gate.get_type().get_name()
            histogram[name] = histogram.get(name, 0) + 1
        # The two constant gates are created by HAL for the 1'b0/1'b1 literals.
        self.assertEqual(histogram.get(primitives.LCELL), 9)
        self.assertEqual(histogram.get(primitives.FF), 8)
        self.assertTrue(netlist.get_gnd_gates())
        self.assertTrue(netlist.get_vcc_gates())

    def test_lut_mask_survives_as_a_generic(self):
        netlist = self._load(COUNTER, "counter_adder.hal.v")
        masks = set()
        for gate in netlist.get_gates():
            if gate.get_type().get_name() != primitives.LCELL:
                continue
            value = gate.get_data("generic", "lut_mask")
            self.assertTrue(value and value[1], gate.get_name())
            masks.add(int(str(value[1]), 16))
        # Eight adder slices with the same mask, plus the carry tap.
        self.assertEqual(masks, {0x000F0FF0, 0x0})

    # -- elaboration ------------------------------------------------------

    def test_elaboration_attaches_the_arithmetic_semantics(self):
        netlist = self._load(COUNTER, "counter_adder.hal.v")
        report = hal_adapter.elaborate(self.hal_py, netlist)
        self.assertEqual(report["refused"], [])
        self.assertEqual(report["elaborated"], 9)
        self.assertEqual(report["checked_ff"], 8)

        slices = [
            gate
            for gate in netlist.get_gates()
            if gate.get_type().get_name() == primitives.LCELL
            and int(str(gate.get_data("generic", "lut_mask")[1]), 16) == 0x000F0FF0
        ]
        self.assertEqual(len(slices), 8)
        gate = slices[0]
        for pin in ("sumout", "cout"):
            function = gate.get_boolean_function(pin)
            self.assertTrue(
                function.get_variable_names(),
                "{} of {} has no variables".format(pin, gate.get_name()),
            )
        # sumout must depend on the carry input, cout on both operands.
        self.assertIn("cin", gate.get_boolean_function("sumout").get_variable_names())
        self.assertLessEqual(
            {"datac", "datad"}, set(gate.get_boolean_function("cout").get_variable_names())
        )

    def test_elaborated_sumout_is_a_full_adder_truth_table(self):
        netlist = self._load(COUNTER, "counter_adder.hal.v")
        hal_adapter.elaborate(self.hal_py, netlist)
        gate = next(
            candidate
            for candidate in netlist.get_gates()
            if candidate.get_type().get_name() == primitives.LCELL
            and int(str(candidate.get_data("generic", "lut_mask")[1]), 16) == 0x000F0FF0
        )
        # After the import rewrite the operands are non-inverted, so the slice
        # must be the plain full adder of datac, datad and cin -- with dataa and
        # datab as don't-cares, which is why they are listed too (the mask
        # addresses all four data inputs).
        variables = ["dataa", "datab", "datac", "datad", "cin"]
        one = self.hal_py.BooleanFunction.Value.ONE
        sums = gate.get_boolean_function("sumout").compute_truth_table(variables)
        carries = gate.get_boolean_function("cout").compute_truth_table(variables)
        self.assertIsNotNone(sums, "compute_truth_table failed for sumout")
        self.assertIsNotNone(carries, "compute_truth_table failed for cout")
        sums = sums[0]
        carries = carries[0]
        self.assertEqual(len(sums), 32)
        for index in range(32):
            c = (index >> 2) & 1
            d = (index >> 3) & 1
            cin = (index >> 4) & 1
            self.assertEqual(sums[index] == one, bool(c ^ d ^ cin), index)
            self.assertEqual(carries[index] == one, (c + d + cin) >= 2, index)

    def test_combinational_fixture_elaborates(self):
        netlist = self._load(LUT_LOGIC, "lut_logic.hal.v")
        report = hal_adapter.elaborate(self.hal_py, netlist)
        self.assertEqual(report["refused"], [])
        self.assertEqual(report["elaborated"], 3)
        for gate in netlist.get_gates():
            if gate.get_type().get_name() != primitives.LCELL:
                continue
            function = gate.get_boolean_function("combout")
            self.assertTrue(function.get_variable_names(), gate.get_name())

    def test_elaboration_document_validates(self):
        netlist = self._load(COUNTER, "counter_adder.hal.v")
        report = hal_adapter.elaborate(self.hal_py, netlist)
        document = hal_adapter.build_document(
            netlist,
            report,
            artifact_id="counter_adder",
            path=os.path.join(COUNTER, "counter_adder.hal.v"),
        )
        validate.validate_document(document)
        statuses = [finding["status"] for finding in document["findings"]]
        self.assertEqual(statuses, [model.STATUS_PROVEN_UNDER_ASSUMPTIONS])

    def test_refusal_is_reported_for_an_unmodelled_configuration(self):
        """A gate whose sclr is driven must be left without semantics."""
        netlist = self._load(COUNTER, "counter_adder.hal.v")
        register = next(
            gate
            for gate in netlist.get_gates()
            if gate.get_type().get_name() == primitives.FF
        )
        enable_net = register.get_fan_in_net("ena")
        gnd_net = register.get_fan_in_net("sclr")
        self.assertIsNotNone(enable_net)
        self.assertIsNotNone(gnd_net)
        gnd_net.remove_destination(register, "sclr")
        enable_net.add_destination(register, "sclr")

        report = hal_adapter.elaborate(self.hal_py, netlist)
        self.assertEqual(len(report["refused"]), 1)
        self.assertIn("sclr", report["refused"][0]["reason"])
        document = hal_adapter.build_document(netlist, report, artifact_id="counter_adder")
        validate.validate_document(document)
        self.assertIn(
            model.STATUS_UNSUPPORTED,
            [finding["status"] for finding in document["findings"]],
        )

    # -- recognition on the HAL side --------------------------------------

    def test_recognition_agrees_with_the_loaded_netlist(self):
        """The .vo-level recognition must name gates HAL also has."""
        parsed = vo_netlist.parse_file(os.path.join(COUNTER, "counter_adder.vo"))
        candidates = [
            candidate
            for candidate in recognize.recognize_adders(parsed)
            if candidate["recognized"]
        ]
        self.assertEqual(len(candidates), 1)
        expected = {entry["cell"].name for entry in candidates[0]["slices"]}

        netlist = self._load(COUNTER, "counter_adder.hal.v")
        present = {gate.get_name() for gate in netlist.get_gates()}
        self.assertLessEqual(expected, present)


if __name__ == "__main__":
    unittest.main()
