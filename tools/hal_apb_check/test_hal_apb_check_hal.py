"""Integration tests that need a built HAL. Skipped without one.

    export HAL_BASE_PATH=/opt/hal-build
    export PYTHONPATH=/opt/hal-build/lib
    python -m unittest discover -s tools/hal_apb_check -t tools -p "test_*_hal.py"

or point at the libraries explicitly with ``HAL_PY_PATH=/opt/hal-build/lib``.

What these add over ``test_hal_apb_check.py`` -- which proves the property
engine correct against hand-written models -- is the one link that cannot be
checked without HAL: that the shipped **Verilog** fixtures really describe those
models, and that the netlist front end reads them faithfully.

The central test is an equivalence miter. For every fixture it extracts a
transition system from the netlist through ``hal_py``, pairs it with the
reference model, and asks the SAT engine whether any assignment makes a
next-state term or an output definition differ. An unsatisfiable miter is a
proof of equality over all inputs and states -- much stronger than comparing a
few simulated traces, and it names the disagreeing signal when it fails.
Everything after that (properties, counterexamples, findings) is then known to
be about the netlist, not about a stand-in.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_apb_check import (  # noqa: E402
    engine,
    expr,
    findings as findings_module,
    mapping as mapping_module,
    reference_models,
    sat,
    spec,
    witness,
)
from hal_apb_check.test_hal_apb_check import EXPECTED, FIXTURES  # noqa: E402

GATE_LIBRARY_ENV = "HAL_APB_GATE_LIBRARY"


def _hal_libs():
    libs = []
    for variable in ("HAL_PY_PATH", "HAL_BASE_PATH"):
        value = os.environ.get(variable)
        if not value:
            continue
        candidate = value if variable == "HAL_PY_PATH" else os.path.join(value, "lib")
        if os.path.isdir(candidate):
            libs.append(candidate)
    return libs


def _hal_available():
    from hal_apb_check import netlist as frontend

    try:
        frontend.import_hal(_hal_libs())
    except frontend.NetlistFrontendError:
        return False
    return True


HAVE_HAL = _hal_available()
SKIP_REASON = (
    "hal_py is not importable. Set PYTHONPATH=<build>/lib and HAL_BASE_PATH=<build> (the "
    "parsers are plugins) to run the netlist-backed tests."
)

_CACHE = {}


def extracted(name):
    """Load the fixture netlist through hal_py once per process."""
    if name not in _CACHE:
        from hal_apb_check import netlist as frontend

        bus_mapping = mapping_module.load(os.path.join(FIXTURES, EXPECTED[name]["mapping"]))
        library = os.environ.get(GATE_LIBRARY_ENV) or bus_mapping.resolve_design_path(
            "gate_library"
        )
        system, artifact = frontend.load(
            netlist_path=bus_mapping.resolve_design_path("netlist"),
            gate_library=library,
            hal_libs=_hal_libs(),
        )
        _CACHE[name] = (bus_mapping, system, artifact)
    return _CACHE[name]


def miter_differences(reference, extracted_system):
    """Signals whose behaviour differs between the two systems, as SAT witnesses.

    Both systems are flattened to terms over inputs and registers, so the
    comparison is over *all* inputs and *all* reachable and unreachable states.
    """
    differences = []
    missing_inputs = sorted(set(reference.inputs) - set(extracted_system.inputs))
    missing_states = sorted(set(reference.states) - set(extracted_system.states))
    if missing_inputs or missing_states:
        differences.append(
            {
                "signal": "<structure>",
                "detail": "inputs missing from the netlist: {}; registers missing: {}".format(
                    missing_inputs, missing_states
                ),
            }
        )
        return differences

    for name in sorted(reference.states):
        left = reference.flatten(reference.next_terms[name])
        right = extracted_system.flatten(extracted_system.next_terms[name])
        status, model = sat.solve_term([expr.xor_(left, right)])
        if status == sat.SAT:
            differences.append(
                {"signal": name, "kind": "next_state", "witness": model}
            )
    for name in sorted(reference.defines):
        if name not in extracted_system.defines:
            differences.append({"signal": name, "kind": "missing_definition"})
            continue
        left = reference.flatten(expr.var(name))
        right = extracted_system.flatten(expr.var(name))
        status, model = sat.solve_term([expr.xor_(left, right)])
        if status == sat.SAT:
            differences.append({"signal": name, "kind": "definition", "witness": model})
    return differences


@unittest.skipUnless(HAVE_HAL, SKIP_REASON)
class NetlistFrontendTest(unittest.TestCase):
    def test_every_fixture_parses(self):
        for name in sorted(EXPECTED):
            _, system, artifact = extracted(name)
            self.assertTrue(system.inputs, name)
            self.assertTrue(system.states, name)
            self.assertEqual(system.check(), [], name)
            self.assertGreater(artifact["gate_count"], 0, name)
            self.assertGreater(artifact["statistics"]["registers"], 0, name)

    def test_clock_is_abstracted_away(self):
        for name in sorted(EXPECTED):
            _, system, artifact = extracted(name)
            self.assertIn("PCLK", artifact["statistics"]["clock_nets"], name)
            self.assertNotIn("PCLK", system.inputs, name)
            self.assertFalse(system.has_signal("PCLK"), name)

    def test_every_mapped_signal_exists_in_the_netlist(self):
        for name in sorted(EXPECTED):
            bus_mapping, system, _ = extracted(name)
            missing = [
                signal
                for signal in bus_mapping.design_signals()
                if not system.has_signal(signal)
            ]
            self.assertEqual(missing, [], name)

    def test_netlist_is_equivalent_to_its_reference_model(self):
        """The hand-written models are only useful if they match the Verilog."""
        for name in sorted(EXPECTED):
            _, system, _ = extracted(name)
            reference = reference_models.build(name)
            differences = miter_differences(reference, system)
            self.assertEqual(differences, [], "{}: {}".format(name, differences))

    def test_broken_fixtures_really_do_differ_from_the_correct_ones(self):
        """Guards against two fixtures accidentally being the same circuit."""
        pairs = (
            ("completer_ok", "completer_broken_slverr"),
            ("completer_ok", "completer_broken_stall"),
            ("requester_ok", "requester_broken_stability"),
        )
        for good, bad in pairs:
            _, good_system, _ = extracted(good)
            differences = miter_differences(reference_models.build(good), good_system)
            self.assertEqual(differences, [])
            _, bad_system, _ = extracted(bad)
            cross = miter_differences(reference_models.build(good), bad_system)
            self.assertTrue(cross, "{} and {} extract to the same system".format(good, bad))


@unittest.skipUnless(HAVE_HAL, SKIP_REASON)
class NetlistCheckTest(unittest.TestCase):
    def test_ground_truth_holds_on_the_real_netlists(self):
        for name, entry in sorted(EXPECTED.items()):
            bus_mapping, system, _ = extracted(name)
            report = engine.check(bus_mapping, system)
            outcomes = {result.property.id: result.outcome for result in report.results}
            self.assertEqual(outcomes, entry["outcomes"], name)
            self.assertFalse(report.diagnostics["overconstrained"], name)

    def test_counterexamples_from_netlists_replay(self):
        for name in ("completer_broken_slverr", "completer_broken_stall",
                     "requester_broken_stability"):
            bus_mapping, system, _ = extracted(name)
            report = engine.check(bus_mapping, system)
            self.assertTrue(report.violations, name)
            for result in report.violations:
                bundle = witness.build_bundle(
                    bus_mapping,
                    system,
                    result.property,
                    result.detail["trace"],
                    result.detail["initial_state"],
                    result.detail["violation_cycle"],
                    report.bound,
                    [entry["assumption"] for entry in report.assumptions],
                )
                outcome = witness.replay(
                    bundle,
                    system,
                    bus_mapping,
                    {prop.id: prop for prop in spec.PROPERTIES},
                    report.assumption_properties,
                    check_from=engine.CHECK_FROM,
                )
                self.assertIn(result.detail["violation_cycle"], outcome["failing_cycles"])

    def test_findings_from_a_netlist_validate_and_pin_the_artifact(self):
        package = findings_module.findings_package()
        model = findings_module.findings_model()
        for name in sorted(EXPECTED):
            bus_mapping, system, artifact = extracted(name)
            report = engine.check(bus_mapping, system)
            design = model.artifact(
                "design",
                kind="netlist",
                path=artifact["path"],
                sha256=package.sha256_file(artifact["path"]),
                design_name=artifact["design_name"],
                gate_count=artifact["gate_count"],
                net_count=artifact["net_count"],
            )
            bus = model.artifact(
                "bus-mapping",
                kind="other",
                path=bus_mapping.path,
                sha256=package.sha256_file(bus_mapping.path),
            )
            net_refs = {
                signal: model.net_ref("design", net_id, signal)
                for signal, net_id in artifact["net_refs"].items()
            }
            document = findings_module.build_document(
                report, design, bus, net_refs=net_refs
            )
            package.validate_document(document)
            for finding in document["findings"]:
                self.assertNotEqual(finding["status"], "proven_under_assumptions")


#: A shipped library with level-sensitive latches, used to exercise the refusal
#: path. ``example_library.hgl`` has none, which is why the fixtures cannot.
LATCH_LIBRARY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "plugins",
    "gate_libraries",
    "definitions",
    "NangateOpenCellLibrary.hgl",
)


@unittest.skipUnless(HAVE_HAL, SKIP_REASON)
class UnsupportedPrimitiveTest(unittest.TestCase):
    def test_a_latch_is_refused_by_name(self):
        """A primitive the cycle model cannot express must be named, not approximated."""
        from hal_apb_check import netlist as frontend

        hal_py = frontend.import_hal(_hal_libs())
        hal_py.plugin_manager.load_all_plugins()
        if not os.path.isfile(LATCH_LIBRARY):
            self.skipTest("{} is not in this checkout".format(LATCH_LIBRARY))
        library = hal_py.GateLibraryManager.load(LATCH_LIBRARY)
        self.assertIsNotNone(library, "could not load {}".format(LATCH_LIBRARY))

        latch_types = [
            gate_type
            for gate_type in library.get_gate_types().values()
            if "latch" in {
                str(entry).rsplit(".", 1)[-1] for entry in gate_type.get_properties()
            }
        ]
        if not latch_types:
            self.skipTest("{} declares no latch primitives".format(LATCH_LIBRARY))

        netlist = hal_py.NetlistFactory.create_netlist(library)
        netlist.create_gate(latch_types[0], "a_latch")
        with self.assertRaises(frontend.UnsupportedPrimitives) as caught:
            frontend.build_transition_system(hal_py, netlist)
        self.assertTrue(caught.exception.primitives)
        self.assertIn(latch_types[0].get_name(), str(caught.exception))
        self.assertIn("only edge-triggered flip-flops", caught.exception.primitives[0]["reason"])


if __name__ == "__main__":
    unittest.main()
