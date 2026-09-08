"""``hal_py`` tests for ``tools/hal_secprop`` -- these need a built HAL.

The offline suite proves the analysis is right *about its own model*. What it
cannot prove is that the model is the one HAL builds from the same Verilog, and
that is the thing that breaks silently when a binding changes. So this suite
loads every fixture through ``hal_py`` and requires the two front ends to agree
on everything that a verdict depends on:

1. the same signals, the same registers, the same inputs;
2. the same transition relation -- checked by an **equivalence miter**, not by a
   handful of traces: for every register, the two next-state functions are
   asserted to differ and the SAT solver must report ``unsat``;
3. the same verdicts, cycle for cycle, on all three fixtures;
4. the latch fixture refused identically by both, as
   :class:`~hal_secprop.errors.UnsupportedPrimitives` naming ``DLH_X1``;
5. a witness produced on the ``hal_py`` model replays on it.

Run against a build tree::

    HAL_BASE_PATH=<build> PYTHONPATH=<build>/lib \\
        python -m unittest discover -s tools/hal_secprop -t tools -p "test_*_hal.py"

They skip only when ``hal_py`` is not importable *and* ``HAL_PY_PATH`` is unset;
with ``HAL_PY_PATH`` set, a missing binding is a failure, not a skip, because a
suite that turns itself off in the one environment it was written for is worse
than no suite.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
REPO = os.path.dirname(TOOLS)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from hal_apb_check import expr, sat  # noqa: E402

from hal_secprop import engine, halsource, policy as policy_module  # noqa: E402
from hal_secprop import properties as properties_module, transitions, witness  # noqa: E402
from hal_secprop.errors import UnsupportedPrimitives  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
LIBRARY = os.path.join(
    REPO, "plugins", "gate_libraries", "definitions", "NangateOpenCellLibrary.hgl"
)
HAL_PY_PATH = os.environ.get("HAL_PY_PATH")
VARIANTS = ("ok", "faulty")


def _hal_available():
    if HAL_PY_PATH and HAL_PY_PATH not in sys.path:
        sys.path.insert(0, HAL_PY_PATH)
    try:
        import hal_py  # noqa: F401
    except ImportError:
        return False
    return True


HAVE_HAL = _hal_available()
SKIP_REASON = (
    "hal_py is not importable and HAL_PY_PATH is unset; point HAL_PY_PATH at <build>/lib "
    "and HAL_BASE_PATH at <build> to run the hal_py suite"
)


def policy_path(variant):
    return os.path.join(FIXTURES, "secreg_{}.policy.json".format(variant))


def netlist_path(variant):
    return os.path.join(FIXTURES, "secreg_{}.v".format(variant))


@unittest.skipUnless(HAVE_HAL or HAL_PY_PATH, SKIP_REASON)
class HalFrontEndTest(unittest.TestCase):
    """The two front ends must produce the same model, not merely similar ones."""

    def _both(self, variant):
        offline, _ = transitions.load(netlist_path(variant), LIBRARY)
        hal, _ = halsource.load(
            netlist_path=netlist_path(variant),
            gate_library=LIBRARY,
            hal_libs=[HAL_PY_PATH] if HAL_PY_PATH else [],
        )
        return offline, hal

    def test_the_same_signals(self):
        for variant in VARIANTS:
            offline, hal = self._both(variant)
            self.assertEqual(sorted(offline.states), sorted(hal.states), variant)
            self.assertEqual(sorted(offline.inputs), sorted(hal.inputs), variant)

    def test_the_transition_relations_are_equivalent(self):
        """A miter over every register: the two next-state functions cannot differ."""
        for variant in VARIANTS:
            offline, hal = self._both(variant)
            for state in sorted(offline.states):
                left = offline.flatten(offline.next_terms[state])
                right = hal.flatten(hal.next_terms[state])
                status, model = sat.solve_term([expr.xor_(left, right)])
                self.assertEqual(
                    status,
                    sat.UNSAT,
                    "{}: the offline and hal_py next-state functions of {} differ "
                    "(witness {})".format(variant, state, model),
                )

    def test_the_policy_signals_resolve_through_hal(self):
        for variant in VARIANTS:
            _, hal = self._both(variant)
            parsed = policy_module.load(policy_path(variant))
            missing = [
                name for name in parsed.design_signals() if not hal.has_signal(name)
            ]
            self.assertEqual(missing, [], "{}: {}".format(variant, missing))

    def test_a_latch_is_refused_on_the_hal_path_too(self):
        with self.assertRaises(UnsupportedPrimitives) as raised:
            halsource.load(
                netlist_path=netlist_path("blackbox"),
                gate_library=LIBRARY,
                hal_libs=[HAL_PY_PATH] if HAL_PY_PATH else [],
            )
        self.assertIn("DLH_X1", str(raised.exception))
        self.assertTrue(raised.exception.primitives)


@unittest.skipUnless(HAVE_HAL or HAL_PY_PATH, SKIP_REASON)
class HalVerdictTest(unittest.TestCase):
    def _report(self, variant, loader):
        parsed = policy_module.load(policy_path(variant))
        system, _ = loader(variant)
        return parsed, system, engine.check(parsed, system)

    def _hal(self, variant):
        return halsource.load(
            netlist_path=netlist_path(variant),
            gate_library=LIBRARY,
            hal_libs=[HAL_PY_PATH] if HAL_PY_PATH else [],
        )

    def _offline(self, variant):
        return transitions.load(netlist_path(variant), LIBRARY)

    def test_the_verdicts_agree_with_the_offline_run(self):
        for variant in VARIANTS:
            _, _, offline = self._report(variant, self._offline)
            _, _, hal = self._report(variant, self._hal)
            self.assertEqual(
                {result.property.id: result.outcome for result in offline.results},
                {result.property.id: result.outcome for result in hal.results},
                variant,
            )

    def test_the_faulty_fixture_is_refuted_through_hal_and_the_witness_replays(self):
        parsed, system, report = self._report("faulty", self._hal)
        self.assertTrue(report.violations)
        obligations = {prop.id: prop for prop in properties_module.build(parsed)}
        for result in report.violations:
            bundle = witness.build_bundle(
                parsed,
                system,
                result.property,
                result,
                report.bound,
                design_source={"netlist": netlist_path("faulty")},
            )
            replayed = witness.replay(bundle, system, parsed, obligations)
            self.assertTrue(replayed["environment_assumptions_hold"])
            self.assertTrue(replayed["exercised"])

    def test_the_correct_fixture_holds_through_hal(self):
        _, _, report = self._report("ok", self._hal)
        self.assertFalse(report.violations)
        for result in report.results:
            self.assertEqual(result.outcome, engine.CheckOutcome.HOLDS_BOUNDED)


if __name__ == "__main__":
    unittest.main()
