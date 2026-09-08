"""Unit tests for hal_apb_check. No HAL build, no third-party packages.

Run from the repository root::

    python -m unittest discover -s tools/hal_apb_check -t tools -p "test_hal_apb_check.py"

These cover the parts that decide whether a verdict is trustworthy:

* the SAT engine, cross-checked against brute force on random formulas;
* the mapping validator, on every way a mapping can lie;
* the property engine on all five fixtures, against the ground truth stated in
  ``fixtures/README.md``;
* vacuity and overconstraint, on designs built to trigger them;
* counterexample replay, including the case where a replayed stimulus breaks an
  environment assumption and must therefore be rejected;
* the findings documents, validated against the shipped ``hal_findings`` schema.

The container test ``test_hal_apb_check_hal.py`` adds the one thing that cannot
be checked here: that the Verilog fixtures really do describe these systems.
"""

import contextlib
import io
import itertools
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_apb_check import (  # noqa: E402
    cli,
    engine,
    expr,
    findings as findings_module,
    mapping as mapping_module,
    reference_models,
    sat,
    spec,
    system as system_module,
    witness,
)

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))

#: The ground truth of ``fixtures/README.md``, in executable form.
EXPECTED = {
    "completer_ok": {
        "mapping": "apb_completer_ok.map.json",
        "outcomes": {
            "apb/completer/ready_within_bound": engine.CheckOutcome.HOLDS_BOUNDED,
            "apb/completer/slverr_requires_ready": engine.CheckOutcome.HOLDS_BOUNDED,
        },
    },
    "completer_broken_slverr": {
        "mapping": "apb_completer_broken_slverr.map.json",
        "outcomes": {
            "apb/completer/ready_within_bound": engine.CheckOutcome.HOLDS_BOUNDED,
            "apb/completer/slverr_requires_ready": engine.CheckOutcome.VIOLATED,
        },
    },
    "completer_broken_stall": {
        "mapping": "apb_completer_broken_stall.map.json",
        "outcomes": {
            "apb/completer/ready_within_bound": engine.CheckOutcome.VIOLATED,
            "apb/completer/slverr_requires_ready": engine.CheckOutcome.HOLDS_BOUNDED,
        },
    },
    "requester_ok": {
        "mapping": "apb_requester_ok.map.json",
        "outcomes": {
            "apb/requester/reset_inactive": engine.CheckOutcome.HOLDS_BOUNDED,
            "apb/requester/enable_requires_select": engine.CheckOutcome.HOLDS_BOUNDED,
            "apb/requester/setup_to_access": engine.CheckOutcome.HOLDS_BOUNDED,
            "apb/requester/control_stable_setup_to_access": engine.CheckOutcome.HOLDS_BOUNDED,
            "apb/requester/access_exit": engine.CheckOutcome.HOLDS_BOUNDED,
            "apb/requester/wait_hold": engine.CheckOutcome.HOLDS_BOUNDED,
            "apb/requester/control_stable_wait": engine.CheckOutcome.HOLDS_BOUNDED,
        },
    },
    "requester_broken_stability": {
        "mapping": "apb_requester_broken_stability.map.json",
        "outcomes": {
            "apb/requester/reset_inactive": engine.CheckOutcome.HOLDS_BOUNDED,
            "apb/requester/enable_requires_select": engine.CheckOutcome.HOLDS_BOUNDED,
            "apb/requester/setup_to_access": engine.CheckOutcome.HOLDS_BOUNDED,
            "apb/requester/control_stable_setup_to_access": engine.CheckOutcome.VIOLATED,
            "apb/requester/access_exit": engine.CheckOutcome.HOLDS_BOUNDED,
            "apb/requester/wait_hold": engine.CheckOutcome.HOLDS_BOUNDED,
            "apb/requester/control_stable_wait": engine.CheckOutcome.VIOLATED,
        },
    },
}


def load_fixture_mapping(name):
    return mapping_module.load(os.path.join(FIXTURES, EXPECTED[name]["mapping"]))


def run_fixture(name, **overrides):
    bus_mapping = load_fixture_mapping(name)
    bus_mapping.options.update(overrides)
    return engine.check(bus_mapping, reference_models.build(name)), bus_mapping


# ---------------------------------------------------------------------------
# expr
# ---------------------------------------------------------------------------


class ExprTest(unittest.TestCase):
    def test_constant_folding(self):
        a = expr.var("a")
        self.assertEqual(expr.and_(a, expr.FALSE), expr.FALSE)
        self.assertEqual(expr.or_(a, expr.TRUE), expr.TRUE)
        self.assertEqual(expr.and_(a, expr.not_(a)), expr.FALSE)
        self.assertEqual(expr.or_(a, expr.not_(a)), expr.TRUE)
        self.assertEqual(expr.not_(expr.not_(a)), a)
        self.assertEqual(expr.and_(a, a), a)
        self.assertEqual(expr.xor_(a, a), expr.FALSE)
        self.assertEqual(expr.iff(a, a), expr.TRUE)

    def test_sequence_and_variadic_forms_agree(self):
        terms = [expr.var("a"), expr.var("b"), expr.var("c")]
        self.assertEqual(expr.and_(*terms), expr.and_(terms))
        self.assertEqual(expr.or_(*terms), expr.or_(terms))
        self.assertEqual(expr.and_([]), expr.TRUE)
        self.assertEqual(expr.or_([]), expr.FALSE)

    def test_evaluate_refuses_unknown_variable(self):
        with self.assertRaises(KeyError):
            expr.evaluate(expr.var("missing"), {})

    def test_evaluate_matches_truth_table(self):
        a, b, c = expr.var("a"), expr.var("b"), expr.var("c")
        term = expr.ite(a, expr.xor_(b, c), expr.and_(b, expr.not_(c)))
        for values in itertools.product([False, True], repeat=3):
            env = dict(zip("abc", values))
            expected = (values[1] != values[2]) if values[0] else (values[1] and not values[2])
            self.assertEqual(expr.evaluate(term, env), expected, env)

    def test_smt2_declares_every_variable(self):
        text = expr.to_smt2([expr.and_(expr.var("x@1"), expr.not_(expr.var("y@2")))])
        self.assertIn("(declare-fun |x@1| () Bool)", text)
        self.assertIn("(declare-fun |y@2| () Bool)", text)
        self.assertIn("(check-sat)", text)


# ---------------------------------------------------------------------------
# sat
# ---------------------------------------------------------------------------


class SatTest(unittest.TestCase):
    def test_trivial(self):
        status, model = sat.solve_term([expr.var("a"), expr.not_(expr.var("b"))])
        self.assertEqual(status, sat.SAT)
        self.assertTrue(model["a"])
        self.assertFalse(model["b"])
        self.assertEqual(sat.solve_term([expr.var("a"), expr.not_(expr.var("a"))])[0], sat.UNSAT)

    def test_against_brute_force(self):
        """Random formulas, decided both ways. A wrong 'unsat' is a wrong proof."""
        rng = random.Random(20240917)
        names = ["v{}".format(index) for index in range(8)]
        for _ in range(300):
            clauses = []
            for _ in range(rng.randint(4, 34)):
                chosen = rng.sample(names, 3)
                clauses.append(
                    expr.or_(
                        *[
                            expr.var(name) if rng.random() < 0.5 else expr.not_(expr.var(name))
                            for name in chosen
                        ]
                    )
                )
            status, model = sat.solve_term(clauses)
            satisfiable = any(
                all(expr.evaluate(clause, dict(zip(names, bits))) for clause in clauses)
                for bits in itertools.product([False, True], repeat=len(names))
            )
            self.assertEqual(status == sat.SAT, satisfiable)
            if status == sat.SAT:
                assignment = {name: model.get(name, False) for name in names}
                self.assertTrue(all(expr.evaluate(clause, assignment) for clause in clauses))

    def test_budget_is_raised_not_swallowed(self):
        # A pigeonhole instance is exponential for resolution; a tiny budget must
        # produce a Budget exception rather than a bogus verdict.
        holes, pigeons = 6, 7
        variables = {
            (pigeon, hole): expr.var("p{}h{}".format(pigeon, hole))
            for pigeon in range(pigeons)
            for hole in range(holes)
        }
        clauses = [
            expr.or_(*[variables[(pigeon, hole)] for hole in range(holes)])
            for pigeon in range(pigeons)
        ]
        for hole in range(holes):
            for left in range(pigeons):
                for right in range(left + 1, pigeons):
                    clauses.append(
                        expr.or_(
                            expr.not_(variables[(left, hole)]),
                            expr.not_(variables[(right, hole)]),
                        )
                    )
        with self.assertRaises(sat.Budget):
            sat.solve_term(clauses, conflict_limit=5)


# ---------------------------------------------------------------------------
# spec
# ---------------------------------------------------------------------------


class SpecTest(unittest.TestCase):
    def test_property_ids_are_unique_and_schema_shaped(self):
        import re

        pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
        seen = set()
        for prop in spec.PROPERTIES:
            self.assertNotIn(prop.id, seen)
            seen.add(prop.id)
            self.assertRegex(prop.id, pattern)
            self.assertTrue(prop.description)
            self.assertIn(prop.obligation_of, spec.ROLES)
        for exclusion in spec.EXCLUSIONS:
            self.assertTrue(pattern.match(exclusion["id"]))

    def test_revision_gating(self):
        apb2 = {prop.id for prop in spec.properties_for("APB2")}
        apb4 = {prop.id for prop in spec.properties_for("APB4")}
        self.assertIn("apb/requester/access_exit_single_cycle", apb2)
        self.assertNotIn("apb/requester/access_exit_single_cycle", apb4)
        self.assertNotIn("apb/completer/slverr_requires_ready", apb2)
        self.assertIn("apb/completer/slverr_requires_ready", apb4)
        self.assertNotIn("PREADY", spec.signals_for_revision("APB2"))
        self.assertIn("PSTRB", spec.signals_for_revision("APB4"))

    def test_obligations_and_assumptions_are_complementary(self):
        for role in spec.ROLES:
            obligations = {prop.id for prop in spec.obligations_for(role, "APB4")}
            assumptions = {prop.id for prop in spec.assumptions_for(role, "APB4")}
            self.assertFalse(obligations & assumptions)

    def test_bounded_liveness_is_never_assumed_of_the_environment(self):
        """Assuming 'the completer is ready within N cycles' would hide wait-state bugs."""
        assumed = {prop.id for prop in spec.assumptions_for("requester", "APB4")}
        self.assertNotIn("apb/completer/ready_within_bound", assumed)
        self.assertNotIn("apb/completer/ready_low_when_deselected", assumed)


# ---------------------------------------------------------------------------
# mapping
# ---------------------------------------------------------------------------


class MappingTest(unittest.TestCase):
    def base(self, **changes):
        document = {
            "mapping_version": "1.0",
            "name": "t",
            "protocol": {"family": "APB", "revision": "APB4", "dut_role": "completer"},
            "design": {"reference_model": "completer_ok"},
            "reset": {"signal": "PRESETn", "active_low": True, "cycles": 2},
            "signals": {
                "PRESETn": "PRESETn",
                "PSEL": "PSEL",
                "PENABLE": "PENABLE",
                "PREADY": "PREADY",
            },
            "options": {"bound": 12},
        }
        document.update(changes)
        return document

    def assert_rejected(self, document, needle):
        with self.assertRaises(mapping_module.MappingError) as caught:
            mapping_module.loads(json.dumps(document))
        self.assertIn(needle, str(caught.exception))

    def test_valid(self):
        bus_mapping = mapping_module.loads(json.dumps(self.base()))
        self.assertEqual(bus_mapping.revision, "APB4")
        self.assertEqual(bus_mapping.bits("PSEL"), ["PSEL"])
        self.assertEqual(bus_mapping.expected_direction("PSEL"), "input")
        self.assertEqual(bus_mapping.expected_direction("PREADY"), "output")

    def test_apb5_is_refused_rather_than_approximated(self):
        document = self.base()
        document["protocol"]["revision"] = "APB5"
        self.assert_rejected(document, "APB5")

    def test_axi_lite_is_refused(self):
        document = self.base()
        document["protocol"]["family"] = "AXI4-Lite"
        self.assert_rejected(document, "only covers 'APB'")

    def test_signal_absent_from_revision(self):
        document = self.base()
        document["protocol"]["revision"] = "APB2"
        self.assert_rejected(document, "PREADY does not exist in APB2")

    def test_unknown_signal_and_unknown_option(self):
        document = self.base()
        document["signals"]["PWAKEUP"] = "wake"
        self.assert_rejected(document, "not an APB signal")
        document = self.base()
        document["options"]["max_waits"] = 4
        self.assert_rejected(document, "unknown option")

    def test_double_mapped_design_signal(self):
        document = self.base()
        document["signals"]["PWRITE"] = "PSEL"
        self.assert_rejected(document, "mapped to both")

    def test_reset_must_be_held_long_enough(self):
        document = self.base()
        document["reset"]["cycles"] = 1
        self.assert_rejected(document, "reset.cycles must be an integer >= 2")

    def test_reset_signal_and_presetn_must_agree(self):
        document = self.base()
        document["signals"]["PRESETn"] = "rst_n"
        self.assert_rejected(document, "disagree")

    def test_bound_must_leave_room_after_reset(self):
        document = self.base()
        document["options"]["bound"] = 3
        self.assert_rejected(document, "leaves no room")

    def test_design_must_name_exactly_one_source(self):
        document = self.base(design={"reference_model": "completer_ok", "netlist": "x.v"})
        self.assert_rejected(document, "exactly one")
        document = self.base(design={"netlist": "x.v"})
        self.assert_rejected(document, "needs a 'gate_library'")

    def test_scalar_signal_cannot_be_a_bus(self):
        document = self.base()
        document["signals"]["PSEL"] = ["a", "b"]
        self.assert_rejected(document, "single-bit signal")

    def test_shipped_fixture_mappings_are_valid(self):
        for name, entry in sorted(EXPECTED.items()):
            bus_mapping = load_fixture_mapping(name)
            self.assertEqual(bus_mapping.name, os.path.splitext(entry["mapping"])[0][: -4])
            netlist = bus_mapping.resolve_design_path("netlist")
            library = bus_mapping.resolve_design_path("gate_library")
            self.assertTrue(os.path.isfile(netlist), netlist)
            self.assertTrue(os.path.isfile(library), library)


# ---------------------------------------------------------------------------
# transition systems
# ---------------------------------------------------------------------------


class TransitionSystemTest(unittest.TestCase):
    def test_reference_models_are_sound(self):
        for name in reference_models.names():
            self.assertEqual(reference_models.build(name).check(), [], name)

    def test_combinational_loop_is_reported(self):
        system = system_module.TransitionSystem("loop")
        system.add_input("i")
        system.define("a", expr.and_(expr.var("i"), expr.var("b")))
        system.define("b", expr.not_(expr.var("a")))
        problems = system.check()
        self.assertTrue(any("combinational loop" in problem for problem in problems), problems)

    def test_missing_next_state_is_reported(self):
        system = system_module.TransitionSystem("bare")
        system.add_state("q")
        self.assertTrue(any("no next-state term" in problem for problem in system.check()))

    def test_unrolling_agrees_with_simulation(self):
        """A model of the unrolling must be an executable run of the same system."""
        system = reference_models.build("completer_ok")
        unrolling = system.unroll(6)
        status, model = sat.solve_term(
            unrolling.constraints + [unrolling.signal("PREADY", 4)]
        )
        self.assertEqual(status, sat.SAT)
        trace, _, _ = witness.trace_from_model(system, unrolling, model, 6)
        self.assertTrue(trace[4]["PREADY"])
        for cycle, values in enumerate(trace):
            for name in system.signals:
                key = unrolling.variable(name, cycle)
                if key in model:
                    self.assertEqual(values[name], model[key], (name, cycle))

    def test_ff_next_clear_dominates(self):
        clear = expr.var("r")
        data = expr.var("d")
        term = system_module.ff_next(data, clear=clear)
        self.assertFalse(expr.evaluate(term, {"r": True, "d": True}))
        self.assertTrue(expr.evaluate(term, {"r": False, "d": True}))
        with_preset = system_module.ff_next(data, clear=clear, preset=expr.var("s"))
        self.assertFalse(expr.evaluate(with_preset, {"r": True, "s": True, "d": False}))
        self.assertTrue(expr.evaluate(with_preset, {"r": False, "s": True, "d": False}))


# ---------------------------------------------------------------------------
# the engine, on the shipped fixtures
# ---------------------------------------------------------------------------


class EngineFixtureTest(unittest.TestCase):
    def test_every_fixture_matches_its_documented_ground_truth(self):
        for name, entry in sorted(EXPECTED.items()):
            report, _ = run_fixture(name)
            outcomes = {result.property.id: result.outcome for result in report.results}
            self.assertEqual(outcomes, entry["outcomes"], name)
            self.assertFalse(report.diagnostics["overconstrained"], name)
            self.assertTrue(report.diagnostics["transfer_reachable"], name)

    def test_no_fixture_check_is_vacuous(self):
        """A fixture whose properties never fire would prove nothing at all."""
        for name in sorted(EXPECTED):
            report, _ = run_fixture(name)
            vacuous = [
                result.property.id
                for result in report.results
                if result.outcome == engine.CheckOutcome.VACUOUS
            ]
            self.assertEqual(vacuous, [], name)

    def test_violations_are_inside_the_bound_and_reproduce(self):
        for name in ("completer_broken_slverr", "completer_broken_stall",
                     "requester_broken_stability"):
            report, bus_mapping = run_fixture(name)
            self.assertTrue(report.violations, name)
            for result in report.violations:
                cycle = result.detail["violation_cycle"]
                self.assertGreaterEqual(cycle, engine.CHECK_FROM)
                self.assertLessEqual(cycle + result.detail["horizon"], report.bound)
                context = witness.TraceContext(bus_mapping, result.detail["trace"])
                self.assertFalse(context.holds(result.property, cycle))
                self.assertTrue(context.activated(result.property, cycle))

    def test_stall_fixture_needs_a_generous_wait_budget_to_pass(self):
        """The bounded liveness budget is a real dial, not decoration."""
        report, _ = run_fixture("completer_broken_stall", max_wait_states=1)
        outcomes = {result.property.id: result.outcome for result in report.results}
        self.assertEqual(
            outcomes["apb/completer/ready_within_bound"], engine.CheckOutcome.VIOLATED
        )

    def test_bound_too_small_reports_not_instantiable_not_success(self):
        bus_mapping = load_fixture_mapping("completer_ok")
        bus_mapping.options["max_wait_states"] = 20
        report = engine.check(bus_mapping, reference_models.build("completer_ok"), bound=12)
        outcomes = {result.property.id: result.outcome for result in report.results}
        self.assertEqual(
            outcomes["apb/completer/ready_within_bound"],
            engine.CheckOutcome.NOT_INSTANTIABLE,
        )

    def test_optional_property_is_off_unless_requested(self):
        report, _ = run_fixture("completer_ok")
        self.assertNotIn(
            "apb/completer/ready_low_when_deselected",
            {result.property.id for result in report.results},
        )
        report, _ = run_fixture("completer_ok", check_ready_low_when_deselected=True)
        outcomes = {result.property.id: result.outcome for result in report.results}
        self.assertIn("apb/completer/ready_low_when_deselected", outcomes)

    def test_reset_signal_missing_from_the_design_is_a_hard_stop(self):
        bus_mapping = load_fixture_mapping("completer_ok")
        bus_mapping.reset_signal = "not_a_net"
        with self.assertRaises(engine.EngineError) as caught:
            engine.check(bus_mapping, reference_models.build("completer_ok"))
        self.assertIn("reset sequence", str(caught.exception))

    def test_unmapped_signal_becomes_unsupported_not_a_pass(self):
        bus_mapping = load_fixture_mapping("completer_ok")
        del bus_mapping._signals["PSLVERR"]
        report = engine.check(bus_mapping, reference_models.build("completer_ok"))
        result = [
            entry
            for entry in report.results
            if entry.property.id == "apb/completer/slverr_requires_ready"
        ][0]
        self.assertEqual(result.outcome, engine.CheckOutcome.UNSUPPORTED)
        self.assertEqual(result.detail["missing_signals"], ["PSLVERR"])


# ---------------------------------------------------------------------------
# vacuity and overconstraint
# ---------------------------------------------------------------------------


def tied_off_completer():
    """A completer whose PSLVERR is tied to ground: the property can never fire."""
    system = system_module.TransitionSystem("apb_completer_tied_off")
    for name in ("PRESETn", "PSEL", "PENABLE", "PWRITE", "PADDR_0", "PWDATA_0"):
        system.add_input(name)
    system.add_state("w_q", initial=None)
    system.define("rst", expr.not_(expr.var("PRESETn")))
    system.define("access", expr.and_(expr.var("PSEL"), expr.var("PENABLE")))
    system.define("w_d", expr.and_(expr.var("access"), expr.not_(expr.var("w_q"))))
    system.define("PREADY", expr.and_(expr.var("access"), expr.var("w_q")))
    system.define("PSLVERR", expr.FALSE)
    system.define("PRDATA_0", expr.var("w_q"))
    system.set_next("w_q", system_module.ff_next(expr.var("w_d"), clear=expr.var("rst")))
    return system


def never_selected_completer():
    """PSEL tied off, so the environment can never start a transfer."""
    system = tied_off_completer()
    system.inputs.remove("PSEL")
    system.defines["PSEL"] = expr.FALSE
    system._order.insert(0, "PSEL")
    return system


class VacuityTest(unittest.TestCase):
    def test_tied_off_signal_is_vacuous_not_proven(self):
        bus_mapping = load_fixture_mapping("completer_ok")
        report = engine.check(bus_mapping, tied_off_completer())
        result = [
            entry
            for entry in report.results
            if entry.property.id == "apb/completer/slverr_requires_ready"
        ][0]
        self.assertEqual(result.outcome, engine.CheckOutcome.VACUOUS)
        self.assertIn("activates the antecedent", result.detail["reason"])
        # ...and the non-vacuous one next to it still passes, so vacuity is
        # detected per property rather than for the whole run.
        other = [
            entry
            for entry in report.results
            if entry.property.id == "apb/completer/ready_within_bound"
        ][0]
        self.assertEqual(other.outcome, engine.CheckOutcome.HOLDS_BOUNDED)

    def test_no_transfer_possible_is_reported_as_overconstrained(self):
        bus_mapping = load_fixture_mapping("completer_ok")
        report = engine.check(bus_mapping, never_selected_completer())
        self.assertTrue(report.diagnostics["overconstrained"])
        self.assertFalse(report.diagnostics["transfer_reachable"])
        for result in report.results:
            self.assertEqual(result.outcome, engine.CheckOutcome.VACUOUS, result.property.id)
            self.assertTrue(result.detail["overconstrained"])


# ---------------------------------------------------------------------------
# counterexample replay
# ---------------------------------------------------------------------------


class ReplayTest(unittest.TestCase):
    def make_bundle(self, name="requester_broken_stability"):
        report, bus_mapping = run_fixture(name)
        result = report.violations[0]
        bundle = witness.build_bundle(
            bus_mapping,
            report.system,
            result.property,
            result.detail["trace"],
            result.detail["initial_state"],
            result.detail["violation_cycle"],
            report.bound,
            [entry["assumption"] for entry in report.assumptions],
            unconstrained=result.detail.get("unconstrained", ()),
        )
        return bundle, report, bus_mapping

    def test_round_trip_and_reproduction(self):
        bundle, report, bus_mapping = self.make_bundle()
        directory = tempfile.mkdtemp(prefix="apb_replay_")
        try:
            path = witness.write_bundle(bundle, os.path.join(directory, "ce.json"))
            reloaded = witness.read_bundle(path)
            self.assertEqual(reloaded, bundle)
            outcome = witness.replay(
                reloaded,
                report.system,
                bus_mapping,
                {prop.id: prop for prop in spec.PROPERTIES},
                report.assumption_properties,
                check_from=engine.CHECK_FROM,
            )
            self.assertEqual(outcome["violation_cycle"], bundle["violation_cycle"])
            self.assertIn(bundle["violation_cycle"], outcome["failing_cycles"])
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_replay_rejects_a_bundle_that_does_not_reproduce(self):
        bundle, report, bus_mapping = self.make_bundle()
        bundle["violation_cycle"] = engine.CHECK_FROM  # a cycle where it holds
        with self.assertRaises(witness.ReplayError) as caught:
            witness.replay(
                bundle,
                report.system,
                bus_mapping,
                {prop.id: prop for prop in spec.PROPERTIES},
                report.assumption_properties,
                check_from=engine.CHECK_FROM,
            )
        self.assertIn("did not reproduce", str(caught.exception))

    def test_replay_rejects_a_stimulus_that_breaks_an_environment_assumption(self):
        """A 'bug' found by an illegal testbench is a testbench bug."""
        report, bus_mapping = run_fixture("completer_broken_slverr")
        result = report.violations[0]
        bundle = witness.build_bundle(
            bus_mapping,
            report.system,
            result.property,
            result.detail["trace"],
            result.detail["initial_state"],
            result.detail["violation_cycle"],
            report.bound,
            [entry["assumption"] for entry in report.assumptions],
        )
        # Drive PENABLE high in every cycle: PENABLE without a preceding SETUP
        # violates apb/requester/setup_to_access, which the run assumed.
        bundle["inputs"]["PENABLE"] = [1] * (bundle["bound"] + 1)
        bundle["trace"] = []
        cycles = bundle["bound"] + 1
        inputs = {
            (name, cycle): bool(bundle["inputs"][name][cycle])
            for name in bundle["inputs"]
            for cycle in range(cycles)
        }
        trace = report.system.simulate(inputs, initial_values=bundle["initial_state"])
        bundle["trace"] = [
            {name: (1 if values[name] else 0) for name in sorted(values)} for values in trace
        ]
        with self.assertRaises(witness.ReplayError) as caught:
            witness.replay(
                bundle,
                report.system,
                bus_mapping,
                {prop.id: prop for prop in spec.PROPERTIES},
                report.assumption_properties,
                check_from=engine.CHECK_FROM,
            )
        self.assertIn("environment assumption", str(caught.exception))

    def test_replay_rejects_an_unknown_schema_version(self):
        directory = tempfile.mkdtemp(prefix="apb_replay_")
        try:
            path = os.path.join(directory, "ce.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"replay_version": "9.9"}, handle)
            with self.assertRaises(witness.ReplayError):
                witness.read_bundle(path)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_vcd_is_well_formed(self):
        bundle, _, _ = self.make_bundle()
        trace = [{name: bool(value) for name, value in cycle.items()} for cycle in bundle["trace"]]
        text = witness.to_vcd(trace)
        self.assertIn("$enddefinitions $end", text)
        self.assertIn("$dumpvars", text)
        self.assertEqual(text.count("$var wire 1 "), len(trace[0]))
        self.assertIn("#{}".format(len(trace)), text)


# ---------------------------------------------------------------------------
# findings documents
# ---------------------------------------------------------------------------


class FindingsTest(unittest.TestCase):
    def build(self, name):
        report, bus_mapping = run_fixture(name)
        model = findings_module.findings_model()
        design = model.artifact(
            "design",
            kind="other",
            path=os.path.abspath(reference_models.__file__),
            sha256="0" * 64,
            description="reference model {}".format(name),
        )
        bus = model.artifact(
            "bus-mapping",
            kind="other",
            path=os.path.join(FIXTURES, EXPECTED[name]["mapping"]),
            sha256="1" * 64,
        )
        return findings_module.build_document(report, design, bus), report

    def test_every_fixture_produces_a_valid_document(self):
        package = findings_module.findings_package()
        for name in sorted(EXPECTED):
            document, _ = self.build(name)
            package.validate_document(document)
            self.assertEqual(document["schema_version"], package.SCHEMA_VERSION)

    def test_status_mapping_is_exactly_as_documented(self):
        expected_status = {
            engine.CheckOutcome.HOLDS_BOUNDED: "proven_bounded",
            engine.CheckOutcome.VIOLATED: "bounded_counterexample",
        }
        for name, entry in sorted(EXPECTED.items()):
            document, _ = self.build(name)
            statuses = {finding["id"]: finding["status"] for finding in document["findings"]}
            for property_id, outcome in entry["outcomes"].items():
                self.assertEqual(statuses[property_id], expected_status[outcome], property_id)

    def test_nothing_ever_claims_an_unbounded_proof(self):
        model = findings_module.findings_model()
        for name in sorted(EXPECTED):
            document, _ = self.build(name)
            for finding in document["findings"]:
                self.assertNotEqual(finding["status"], "proven_under_assumptions", finding["id"])
                self.assertFalse(model.is_unbounded_proof(finding), finding["id"])
                if finding["status"] in ("proven_bounded", "bounded_counterexample"):
                    self.assertTrue(model.is_bounded_claim(finding))
                    self.assertEqual(finding["bounds"]["cycle_bound"], 12)

    def test_bounded_coverage_is_reported_per_finding(self):
        document, report = self.build("completer_ok")
        finding = [
            entry
            for entry in document["findings"]
            if entry["id"] == "apb/completer/ready_within_bound"
        ][0]
        metrics = finding["metrics"]
        self.assertEqual(metrics["cycle_bound"], report.bound)
        self.assertEqual(metrics["horizon_cycles"], 4)
        self.assertEqual(metrics["instantiated_from_cycle"], engine.CHECK_FROM)
        self.assertEqual(metrics["instantiated_to_cycle"], report.bound - 4)
        self.assertEqual(metrics["uncovered_tail_cycles"], 4)

    def test_exclusions_are_reported_as_unsupported_findings(self):
        document, _ = self.build("completer_ok")
        unsupported = {
            finding["id"]: finding
            for finding in document["findings"]
            if finding["status"] == "unsupported"
        }
        for exclusion in spec.EXCLUSIONS:
            if exclusion["applies"] in ("always", "APB4"):
                self.assertIn(exclusion["id"], unsupported)
                self.assertEqual(
                    unsupported[exclusion["id"]]["unsupported"]["kind"], exclusion["kind"]
                )

    def test_assumptions_name_the_environment_properties_in_force(self):
        document, report = self.build("completer_ok")
        finding = [
            entry
            for entry in document["findings"]
            if entry["id"] == "apb/completer/ready_within_bound"
        ][0]
        assumption_ids = {entry["id"] for entry in finding["assumptions"]}
        for entry in report.assumptions:
            self.assertIn(entry["assumption"], assumption_ids)
        self.assertIn("apb/env/reset-sequence", assumption_ids)
        self.assertIn("apb/tool/cycle-abstraction", assumption_ids)

    def test_documents_are_deterministic(self):
        package = findings_module.findings_package()
        first, _ = self.build("completer_broken_slverr")
        second, _ = self.build("completer_broken_slverr")
        self.assertEqual(package.document_digest(first), package.document_digest(second))

    def test_overconstrained_run_gets_its_own_finding(self):
        bus_mapping = load_fixture_mapping("completer_ok")
        report = engine.check(bus_mapping, never_selected_completer())
        model = findings_module.findings_model()
        document = findings_module.build_document(
            report,
            model.artifact("design", kind="other", sha256="0" * 64),
            model.artifact("bus-mapping", kind="other", sha256="1" * 64),
        )
        findings_module.findings_package().validate_document(document)
        ids = {finding["id"] for finding in document["findings"]}
        self.assertIn("apb/diagnostics/overconstrained", ids)


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------


class CliTest(unittest.TestCase):
    def run_cli(self, *argv):
        """Run a command, capturing its output so the test log stays readable."""
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = cli.main(list(argv))
        self.last_output = buffer.getvalue()
        return code

    def test_properties_and_exclusions_run(self):
        self.assertEqual(self.run_cli("properties", "--revision", "APB4"), cli.EXIT_OK)
        self.assertEqual(self.run_cli("exclusions"), cli.EXIT_OK)

    def test_validate_mapping(self):
        for entry in EXPECTED.values():
            self.assertEqual(
                self.run_cli("validate-mapping", os.path.join(FIXTURES, entry["mapping"])),
                cli.EXIT_OK,
            )

    def test_check_exit_codes(self):
        directory = tempfile.mkdtemp(prefix="apb_cli_")
        try:
            for name, entry in sorted(EXPECTED.items()):
                output = os.path.join(directory, name + ".json")
                code = self.run_cli(
                    "check",
                    os.path.join(FIXTURES, entry["mapping"]),
                    "--reference-model",
                    name,
                    "-o",
                    output,
                )
                violated = any(
                    outcome == engine.CheckOutcome.VIOLATED
                    for outcome in entry["outcomes"].values()
                )
                self.assertEqual(code, cli.EXIT_VIOLATION if violated else cli.EXIT_OK, name)
                self.assertTrue(os.path.isfile(output))
                with open(output, encoding="utf-8") as handle:
                    findings_module.findings_package().validate_document(json.load(handle))
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_check_then_replay_the_exported_bundle(self):
        directory = tempfile.mkdtemp(prefix="apb_cli_")
        try:
            output = os.path.join(directory, "report.json")
            code = self.run_cli(
                "check",
                os.path.join(FIXTURES, EXPECTED["requester_broken_stability"]["mapping"]),
                "--reference-model",
                "requester_broken_stability",
                "-o",
                output,
            )
            self.assertEqual(code, cli.EXIT_VIOLATION)
            bundles = [
                os.path.join(directory, "evidence", entry)
                for entry in sorted(os.listdir(os.path.join(directory, "evidence")))
                if entry.endswith(".replay.json")
            ]
            self.assertTrue(bundles)
            for bundle in bundles:
                self.assertEqual(
                    self.run_cli(
                        "replay",
                        bundle,
                        "--reference-model",
                        "requester_broken_stability",
                    ),
                    cli.EXIT_OK,
                )
            # Evidence paths are stored relative to the document, so the whole
            # results directory can be moved or uploaded as one unit.
            with open(output, encoding="utf-8") as handle:
                document = json.load(handle)
            for finding in document["findings"]:
                for evidence in finding.get("evidence", []):
                    if "path" in evidence:
                        self.assertFalse(os.path.isabs(evidence["path"]), evidence["path"])
                        self.assertTrue(
                            os.path.isfile(os.path.join(directory, evidence["path"])),
                            evidence["path"],
                        )
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_bad_mapping_exits_unusable(self):
        directory = tempfile.mkdtemp(prefix="apb_cli_")
        try:
            path = os.path.join(directory, "bad.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"mapping_version": "1.0"}')
            self.assertEqual(self.run_cli("validate-mapping", path), cli.EXIT_UNUSABLE)
            self.assertEqual(self.run_cli("check", path), cli.EXIT_UNUSABLE)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_apb2_completer_reports_that_it_checked_nothing(self):
        """An empty result set is a coverage gap, not a clean run."""
        directory = tempfile.mkdtemp(prefix="apb_cli_")
        try:
            source = os.path.join(FIXTURES, EXPECTED["completer_ok"]["mapping"])
            with open(source, encoding="utf-8") as handle:
                document = json.load(handle)
            document["protocol"]["revision"] = "APB2"
            for signal in ("PREADY", "PSLVERR"):
                document["signals"].pop(signal)
            path = os.path.join(directory, "apb2.map.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(document, handle)
            output = os.path.join(directory, "apb2.json")
            code = self.run_cli(
                "check", path, "--reference-model", "completer_ok", "-o", output
            )
            self.assertEqual(code, cli.EXIT_UNUSABLE)
            self.assertIn("nothing was checked", self.last_output)
            with open(output, encoding="utf-8") as handle:
                written = json.load(handle)
            findings_module.findings_package().validate_document(written)
            statuses = {entry["id"]: entry["status"] for entry in written["findings"]}
            self.assertEqual(statuses["apb/diagnostics/no-obligations"], "unsupported")
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_strict_flags_inconclusive_results(self):
        directory = tempfile.mkdtemp(prefix="apb_cli_")
        try:
            source = os.path.join(FIXTURES, EXPECTED["completer_ok"]["mapping"])
            with open(source, encoding="utf-8") as handle:
                document = json.load(handle)
            # A wait budget larger than the bound makes the liveness obligation
            # uninstantiable: not a pass, and --strict must say so.
            document["options"]["max_wait_states"] = 20
            path = os.path.join(directory, "strict.map.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(document, handle)
            arguments = ["check", path, "--reference-model", "completer_ok", "--no-evidence"]
            self.assertEqual(self.run_cli(*arguments), cli.EXIT_OK)
            self.assertEqual(self.run_cli(*(arguments + ["--strict"])), cli.EXIT_UNUSABLE)
            self.assertIn("inconclusive", self.last_output)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_unknown_reference_model_exits_unusable(self):
        self.assertEqual(
            self.run_cli(
                "check",
                os.path.join(FIXTURES, EXPECTED["completer_ok"]["mapping"]),
                "--reference-model",
                "does_not_exist",
            ),
            cli.EXIT_UNUSABLE,
        )

    def test_runs_as_a_subprocess_from_the_repository_root(self):
        completed = subprocess.run(
            [sys.executable, os.path.join("tools", "hal_apb_check"), "properties"],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        self.assertIn(b"apb/completer/ready_within_bound", completed.stdout)


if __name__ == "__main__":
    unittest.main()
