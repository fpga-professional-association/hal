"""Offline tests for ``tools/hal_secprop`` -- no HAL build, standard library only.

Run from the repository root::

    python -m unittest discover -s tools/hal_secprop -t tools -p "test_hal_secprop.py"

What these cover, and why each one is here:

* the policy parser refuses every way a policy can lie (unknown keys, two roles
  on one signal, an access that constrains nothing, a locked_write obligation
  with no declared write path, a second clock);
* the offline front end reproduces the fixture's semantics, and refuses a latch
  and a gated clock rather than modelling them;
* **the cones of the correct and the faulty fixture are identical** -- the claim
  that justifies reporting structural reachability as ``heuristic``;
* the four result classes the issue asks for: correct (``proven_bounded``),
  violating (``bounded_counterexample`` whose witness replays), unsupported (a
  latch), and timeout (a zero conflict budget), plus the vacuous case that a
  green report would otherwise hide;
* the witness: the transaction sequence really contains the two accesses the
  attack needs, replay reproduces it, and replay *refuses* a bundle whose
  stimulus breaks the declared environment or whose trace has been edited;
* every findings document validates against the shipped schema and none of them
  claims an unbounded proof.
"""

import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
REPO = os.path.dirname(TOOLS)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from hal_apb_check import expr  # noqa: E402
from hal_findings import serialize as findings_serialize  # noqa: E402
from hal_findings import validate as findings_validate  # noqa: E402
from hal_findings.model import is_unbounded_proof  # noqa: E402

from hal_secprop import cli, cones, engine, findings, policy as policy_module  # noqa: E402
from hal_secprop import properties as properties_module  # noqa: E402
from hal_secprop import transitions, witness  # noqa: E402
from hal_secprop.errors import DesignError, PolicyError, UnsupportedPrimitives  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
LIBRARY = os.path.join(
    REPO, "plugins", "gate_libraries", "definitions", "NangateOpenCellLibrary.hgl"
)
with open(os.path.join(FIXTURES, "ground_truth.json"), "r", encoding="utf-8") as _handle:
    GROUND_TRUTH = json.load(_handle)


def policy_path(variant):
    return os.path.join(FIXTURES, "secreg_{}.policy.json".format(variant))


def netlist_path(variant):
    return os.path.join(FIXTURES, "secreg_{}.v".format(variant))


def load_policy(variant):
    return policy_module.load(policy_path(variant))


def load_system(variant):
    system, artifact = transitions.load(netlist_path(variant), LIBRARY)
    return system, artifact


# ---------------------------------------------------------------------------
# the policy
# ---------------------------------------------------------------------------


class PolicyTest(unittest.TestCase):
    def test_the_shipped_policies_parse(self):
        for variant in ("ok", "faulty", "blackbox"):
            parsed = load_policy(variant)
            self.assertEqual(parsed.name, "secreg_{}".format(variant))
            self.assertEqual(len(parsed.registers), 1)
            self.assertEqual(len(parsed.registers[0].bits), 4)
            self.assertEqual(parsed.reset_cycles, 2)
            self.assertTrue(parsed.reset_active_low)
            self.assertEqual(parsed.lock_locked_value, 1)
            self.assertEqual(
                sorted(access.id for access in parsed.accesses),
                ["apb_write", "debug_write", "lock_write"],
            )

    def test_access_conditions_expand_vectors_lsb_first(self):
        parsed = load_policy("ok")
        debug = parsed.access_by_id["debug_write"]
        # slot 2 -> paddr = 0,1,0 least significant bit first
        self.assertEqual(debug.condition["io_06"], 0)
        self.assertEqual(debug.condition["io_07"], 1)
        self.assertEqual(debug.condition["io_08"], 0)
        self.assertEqual(debug.condition["io_05"], 1)

    def _mutate(self, variant, mutate):
        document = copy.deepcopy(load_policy(variant).document)
        mutate(document)
        return document

    def test_unknown_top_level_key_is_refused(self):
        document = self._mutate("ok", lambda d: d.update({"observation": []}))
        with self.assertRaises(PolicyError) as raised:
            policy_module.Policy(document)
        self.assertIn("unknown key", str(raised.exception))

    def test_unknown_option_is_refused(self):
        document = self._mutate("ok", lambda d: d["options"].update({"bounds": 4}))
        with self.assertRaises(PolicyError) as raised:
            policy_module.Policy(document)
        self.assertIn("bounds", str(raised.exception))

    def test_a_signal_cannot_have_two_roles(self):
        def mutate(document):
            document["lock"]["signal"] = document["external_write_controls"]["signals"]["psel"]

        with self.assertRaises(PolicyError) as raised:
            policy_module.Policy(self._mutate("ok", mutate))
        self.assertIn("both", str(raised.exception))

    def test_a_second_clock_is_refused(self):
        def mutate(document):
            document["clock"]["signal"] = ["io_00", "io_01"]

        with self.assertRaises(PolicyError) as raised:
            policy_module.Policy(self._mutate("ok", mutate))
        self.assertIn("single clock", str(raised.exception))

    def test_an_empty_access_condition_is_refused(self):
        def mutate(document):
            document["external_write_controls"]["accesses"][0]["condition"] = {}

        with self.assertRaises(PolicyError) as raised:
            policy_module.Policy(self._mutate("ok", mutate))
        self.assertIn("empty condition", str(raised.exception))

    def test_locked_write_without_a_declared_write_path_is_refused(self):
        def mutate(document):
            document["sensitive_registers"][0]["write_accesses"] = []

        with self.assertRaises(PolicyError) as raised:
            policy_module.Policy(self._mutate("ok", mutate))
        self.assertIn("write_accesses", str(raised.exception))

    def test_reset_must_be_held_for_at_least_two_cycles(self):
        def mutate(document):
            document["reset"]["cycles"] = 1

        with self.assertRaises(PolicyError):
            policy_module.Policy(self._mutate("ok", mutate))

    def test_a_wrong_address_width_in_an_access_is_refused(self):
        def mutate(document):
            document["external_write_controls"]["accesses"][0]["condition"]["paddr"] = [1, 0]

        with self.assertRaises(PolicyError) as raised:
            policy_module.Policy(self._mutate("ok", mutate))
        self.assertIn("bit(s)", str(raised.exception))


# ---------------------------------------------------------------------------
# the offline front end
# ---------------------------------------------------------------------------


class FrontEndTest(unittest.TestCase):
    def test_the_ok_fixture_becomes_a_sound_transition_system(self):
        system, artifact = load_system("ok")
        self.assertEqual(system.check(), [])
        self.assertEqual(artifact["front_end"], "offline")
        # 4 secret bits + the lock + the registered PREADY
        self.assertEqual(len(system.states), 6)
        # the clock is abstracted away and never an input
        self.assertNotIn("io_00", system.inputs)
        self.assertIn("io_01", system.inputs)
        self.assertEqual(artifact["statistics"]["clock_nets"], ["io_00"])

    def test_every_policy_signal_exists_in_the_design(self):
        for variant in ("ok", "faulty"):
            system, _ = load_system(variant)
            parsed = load_policy(variant)
            missing = [
                name for name in parsed.design_signals() if not system.has_signal(name)
            ]
            self.assertEqual(missing, [], "{}: {}".format(variant, missing))

    def test_a_latch_is_refused_by_name_not_modelled(self):
        with self.assertRaises(UnsupportedPrimitives) as raised:
            load_system("blackbox")
        self.assertIn("DLH_X1", str(raised.exception))
        self.assertEqual(
            [entry["gate_type"] for entry in raised.exception.primitives], ["DLH_X1"]
        )

    def test_a_gated_clock_is_refused(self):
        with open(netlist_path("ok"), "r", encoding="utf-8") as handle:
            source = handle.read()
        # feed the clock through an inverter pair so it is combinationally driven
        gated = source.replace(
            "endmodule",
            "  wire n900;\n"
            "  INV_X1 g900 (\n    .A(io_02),\n    .ZN(n900)\n  );\n"
            "  INV_X1 g901 (\n    .A(n900),\n    .ZN(io_00)\n  );\n"
            "endmodule",
        )
        directory = tempfile.mkdtemp(prefix="secprop_gated_")
        try:
            path = os.path.join(directory, "gated.v")
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(gated)
            with self.assertRaises(DesignError) as raised:
                transitions.load(path, LIBRARY)
            self.assertIn("clock", str(raised.exception))
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_the_model_reproduces_the_fixture_semantics(self):
        """A hand-driven simulation of the documented attack, offline."""
        system, _ = load_system("faulty")
        parsed = load_policy("faulty")
        bound = 6
        stimulus = {}

        def drive(signal, cycle, value):
            stimulus[(signal, cycle)] = bool(value)

        for cycle in range(bound + 1):
            for name in system.inputs:
                drive(name, cycle, 0)
            drive(parsed.reset_signal, cycle, cycle >= parsed.reset_cycles)
        # cycle 2: write 1 to slot 0 -> the lock engages at cycle 3
        for signal, value in parsed.access_by_id["lock_write"].condition.items():
            drive(signal, 2, value)
        drive(parsed.interface_signals["pwdata"][0], 2, 1)
        # cycle 3: the debug write that must not get through
        for signal, value in parsed.access_by_id["debug_write"].condition.items():
            drive(signal, 3, value)
        for index in range(4):
            drive(parsed.interface_signals["pwdata"][index], 3, 1)

        trace = system.simulate(
            stimulus, initial_values={name: False for name in system.states}
        )
        self.assertFalse(trace[2][parsed.lock_signal], "the lock must start clear")
        self.assertTrue(trace[3][parsed.lock_signal], "the lock must engage at cycle 3")
        secret = [bit.signal for bit in parsed.registers[0].bits]
        self.assertTrue(
            any(trace[4][name] for name in secret),
            "the faulty debug path must write SECRET while the lock is engaged",
        )


# ---------------------------------------------------------------------------
# structural cones
# ---------------------------------------------------------------------------


class ConeTest(unittest.TestCase):
    def _cone(self, variant):
        system, _ = load_system(variant)
        parsed = load_policy(variant)
        return cones.analyse(system, parsed), parsed

    def test_the_cone_matches_the_ground_truth(self):
        report, parsed = self._cone("ok")
        controls = sorted(
            {signal for signals in parsed.interface_signals.values() for signal in signals}
        )
        data = report.cones["SECRET"].to_dict(
            external_controls=controls, lock_signal=parsed.lock_signal
        )
        expected = GROUND_TRUTH["structural_claim"]["expected_cone"]
        self.assertEqual(data["input_count"], expected["input_count"])
        self.assertEqual(data["state_count"], expected["state_count"])
        self.assertEqual(
            len(data["external_controls_in_cone"]), expected["external_controls_in_cone"]
        )
        self.assertEqual(data["lock_in_cone"], expected["lock_in_cone"])
        self.assertEqual(
            max(data["external_control_depths"].values()),
            expected["max_external_control_depth"],
        )

    def test_correct_and_faulty_have_the_same_cone(self):
        """The claim that makes 'candidate reachability' the honest label.

        The faulty design differs by one AND gate. The lock still reaches SECRET
        through the other write path, so the fan-in cone is identical -- the
        structural view literally cannot see the bug.
        """
        summaries = {}
        for variant in ("ok", "faulty"):
            report, parsed = self._cone(variant)
            controls = sorted(
                {
                    signal
                    for signals in parsed.interface_signals.values()
                    for signal in signals
                }
            )
            data = report.cones["SECRET"].to_dict(
                external_controls=controls, lock_signal=parsed.lock_signal
            )
            summaries[variant] = {
                "inputs": sorted(data["inputs"]),
                "depths": data["external_control_depths"],
                "controls": data["external_controls_in_cone"],
                "lock_in_cone": data["lock_in_cone"],
                "states": data["state_count"],
            }
        self.assertEqual(summaries["ok"], summaries["faulty"])

    def test_cone_selection_excludes_unrelated_state(self):
        report, _ = self._cone("ok")
        summary = report.summary()
        self.assertEqual(summary["design_states"], 6)
        # PREADY is not in the fan-in of any policy target
        self.assertEqual(summary["states_in_selected_cones"], 5)
        self.assertGreater(summary["state_reduction_fraction"], 0.0)


# ---------------------------------------------------------------------------
# the bounded checks
# ---------------------------------------------------------------------------


def run_engine(variant, bound=None, **options):
    system, artifact = load_system(variant)
    parsed = load_policy(variant)
    parsed.options.update(options)
    report = engine.check(parsed, system, bound=bound if bound is not None else parsed.bound)
    return report, system, parsed, artifact


class EngineTest(unittest.TestCase):
    def test_the_correct_fixture_is_proven_bounded_and_exercised(self):
        report, _, _, _ = run_engine("ok")
        expected = GROUND_TRUTH["designs"]["secreg_ok"]["expected"]["obligations"]
        outcomes = {result.property.id: result for result in report.results}
        self.assertEqual(sorted(outcomes), sorted(expected))
        for identifier, truth in expected.items():
            result = outcomes[identifier]
            self.assertEqual(result.outcome, truth["outcome"], identifier)
            self.assertTrue(result.detail.get("exercised") is not False, identifier)
        self.assertFalse(report.violations)
        self.assertFalse(report.overconstrained)

    def test_the_faulty_fixture_yields_both_violations(self):
        report, _, _, _ = run_engine("faulty")
        expected = GROUND_TRUTH["designs"]["secreg_faulty"]["expected"]["obligations"]
        outcomes = {result.property.id: result for result in report.results}
        for identifier, truth in expected.items():
            self.assertEqual(outcomes[identifier].outcome, truth["outcome"], identifier)

        locked = outcomes["secprop/locked-write-blocked/SECRET"]
        truth = expected["secprop/locked-write-blocked/SECRET"]["witness"]
        self.assertGreaterEqual(
            locked.detail["violation_cycle"], truth["min_violation_cycle"]
        )
        self.assertTrue(locked.detail["failing_bits"])
        self.assertTrue(
            locked.detail["exercised"],
            "a locked-write violation must coincide with a declared external write",
        )

        reset = outcomes["secprop/reset-clears/SECRET"]
        self.assertEqual(
            reset.detail["failing_bits"],
            expected["secprop/reset-clears/SECRET"]["witness"]["failing_bits"],
        )
        self.assertGreaterEqual(
            reset.detail["violation_cycle"],
            expected["secprop/reset-clears/SECRET"]["witness"]["min_violation_cycle"],
        )

    def test_a_zero_conflict_budget_is_a_timeout_not_a_pass(self):
        report, _, _, _ = run_engine("ok", conflict_limit=0)
        self.assertTrue(report.results)
        for result in report.results:
            self.assertEqual(result.outcome, engine.CheckOutcome.TIMEOUT)
            self.assertEqual(result.detail["budget"]["kind"], "conflict")

    def test_a_bound_below_the_lookahead_is_unknown_not_a_pass(self):
        report, _, _, _ = run_engine("ok", bound=1)
        for result in report.results:
            self.assertEqual(result.outcome, engine.CheckOutcome.NOT_INSTANTIABLE)

    def test_an_unreachable_write_makes_the_pass_vacuous(self):
        """Pinning PSEL low means no write is ever attempted.

        The obligation then cannot be violated -- and the tool must say so
        instead of reporting a proof it never earned.
        """
        system, _ = load_system("ok")
        parsed = load_policy("ok")
        parsed.quiescent_inputs[parsed.interface_signals["psel"][0]] = 0
        report = engine.check(parsed, system)
        locked = next(
            result
            for result in report.results
            if result.property.id == "secprop/locked-write-blocked/SECRET"
        )
        self.assertEqual(locked.outcome, engine.CheckOutcome.VACUOUS)
        self.assertIn("exercise", locked.detail["reason"])

    def test_contradictory_assumptions_are_reported_not_hidden(self):
        system, _ = load_system("ok")
        parsed = load_policy("ok")
        # pin the reset inactive while the schedule holds it active: no run exists
        parsed.quiescent_inputs[parsed.reset_signal] = 1
        report = engine.check(parsed, system)
        self.assertTrue(report.overconstrained)
        self.assertTrue(report.results)
        for result in report.results:
            self.assertEqual(result.outcome, engine.CheckOutcome.VACUOUS)
            self.assertTrue(result.detail["overconstrained"])

    def test_a_policy_naming_a_missing_signal_is_unsupported(self):
        system, _ = load_system("ok")
        parsed = load_policy("ok")
        parsed.registers[0].bits[0].signal = "nowhere"
        report = engine.check(parsed, system)
        locked = next(
            result
            for result in report.results
            if result.property.id == "secprop/locked-write-blocked/SECRET"
        )
        self.assertEqual(locked.outcome, engine.CheckOutcome.UNSUPPORTED)
        self.assertIn("nowhere", locked.detail["missing_signals"])


# ---------------------------------------------------------------------------
# witnesses
# ---------------------------------------------------------------------------


class WitnessTest(unittest.TestCase):
    def _violation(self, identifier="secprop/locked-write-blocked/SECRET"):
        report, system, parsed, _ = run_engine("faulty")
        result = next(
            item for item in report.violations if item.property.id == identifier
        )
        bundle = witness.build_bundle(
            parsed, system, result.property, result, report.bound,
            design_source={"netlist": netlist_path("faulty")},
        )
        return bundle, system, parsed, result

    def test_the_witness_replays(self):
        bundle, system, parsed, _ = self._violation()
        obligations = {prop.id: prop for prop in properties_module.build(parsed)}
        result = witness.replay(bundle, system, parsed, obligations)
        self.assertTrue(result["environment_assumptions_hold"])
        self.assertTrue(result["exercised"])
        self.assertEqual(result["violation_cycle"], bundle["violation_cycle"])

    def test_the_witness_is_a_transaction_sequence(self):
        bundle, _, _, result = self._violation()
        records = bundle["transactions"]
        self.assertEqual(len(records), bundle["bound"] + 1)
        engaged = [entry for entry in records if "lock_write" in entry["accesses"]]
        attack = [
            entry
            for entry in records
            if "debug_write" in entry["accesses"] and entry.get("locked")
        ]
        self.assertTrue(engaged, "the witness must show the lock being engaged")
        self.assertTrue(
            attack, "the witness must show a debug write while the lock is engaged"
        )
        self.assertEqual(attack[0]["cycle"], result.detail["violation_cycle"])
        rendered = witness.format_transactions(records, highlight=attack[0]["cycle"])
        self.assertIn("LOCKED", rendered)
        self.assertIn("debug_write", rendered)

    def test_observation_points_are_recorded_in_the_witness(self):
        bundle, _, parsed, _ = self._violation()
        for entry in bundle["transactions"]:
            self.assertEqual(
                sorted(entry["observations"]),
                sorted(point["name"] for point in parsed.observation_points),
            )

    def test_an_edited_trace_is_refused(self):
        bundle, system, parsed, _ = self._violation()
        obligations = {prop.id: prop for prop in properties_module.build(parsed)}
        tampered = copy.deepcopy(bundle)
        name = sorted(tampered["trace"][0])[0]
        tampered["trace"][0][name] = 1 - tampered["trace"][0][name]
        with self.assertRaises(witness.ReplayError) as raised:
            witness.replay(tampered, system, parsed, obligations)
        self.assertIn("diverges", str(raised.exception))

    def test_a_stimulus_that_breaks_the_reset_schedule_is_refused(self):
        bundle, system, parsed, _ = self._violation()
        obligations = {prop.id: prop for prop in properties_module.build(parsed)}
        tampered = copy.deepcopy(bundle)
        # release reset a cycle early and re-derive the trace so it is consistent
        tampered["inputs"][parsed.reset_signal][0] = 1
        inputs = {
            (name, cycle): bool(value)
            for name, values in tampered["inputs"].items()
            for cycle, value in enumerate(values)
        }
        trace = system.simulate(
            inputs, initial_values={k: bool(v) for k, v in tampered["initial_state"].items()}
        )
        tampered["trace"] = [
            {name: (1 if values[name] else 0) for name in sorted(values)}
            for values in trace
        ]
        with self.assertRaises(witness.ReplayError) as raised:
            witness.replay(tampered, system, parsed, obligations)
        self.assertIn("environment assumption", str(raised.exception))

    def test_a_bundle_from_another_tool_is_refused(self):
        directory = tempfile.mkdtemp(prefix="secprop_bundle_")
        try:
            path = os.path.join(directory, "foreign.replay.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"replay_version": "1.0", "bundle_kind": "hal_apb_check"}, handle)
            with self.assertRaises(witness.ReplayError) as raised:
                witness.read_bundle(path)
            self.assertIn("not a hal_secprop witness", str(raised.exception))
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_the_vcd_covers_every_signal_of_the_trace(self):
        bundle, _, _, result = self._violation()
        text = witness.to_vcd(result.detail["trace"])
        self.assertIn("$enddefinitions", text)
        for name in bundle["signals"]:
            self.assertIn(name, text)


# ---------------------------------------------------------------------------
# findings documents
# ---------------------------------------------------------------------------


class FindingsTest(unittest.TestCase):
    def _document(self, variant, **options):
        report, system, parsed, artifact = run_engine(variant, **options)
        model = findings.findings_model()
        document = findings.build_document(
            report,
            model.artifact(
                "design",
                kind="netlist",
                path=netlist_path(variant),
                sha256=findings_serialize.sha256_file(netlist_path(variant)),
            ),
            model.artifact(
                "policy",
                kind="other",
                path=policy_path(variant),
                sha256=findings_serialize.sha256_file(policy_path(variant)),
            ),
            generated_at="2026-09-08T00:00:00Z",
        )
        return document, report

    def test_documents_validate(self):
        for variant in ("ok", "faulty"):
            document, _ = self._document(variant)
            errors = findings_validate.collect_errors(document)
            self.assertEqual(errors, [], "{}: {}".format(variant, errors))

    def test_no_finding_claims_an_unbounded_proof(self):
        for variant in ("ok", "faulty"):
            document, _ = self._document(variant)
            for finding in document["findings"]:
                self.assertFalse(
                    is_unbounded_proof(finding),
                    "{} claims an unbounded proof".format(finding["id"]),
                )

    def test_the_statuses_match_the_ground_truth(self):
        for variant in ("ok", "faulty"):
            document, _ = self._document(variant)
            by_id = {finding["id"]: finding for finding in document["findings"]}
            expected = GROUND_TRUTH["designs"]["secreg_" + variant]["expected"]["obligations"]
            for identifier, truth in expected.items():
                self.assertEqual(by_id[identifier]["status"], truth["status"], identifier)

    def test_cone_findings_are_heuristic_and_say_candidate(self):
        document, _ = self._document("faulty")
        cone_findings = [
            finding
            for finding in document["findings"]
            if finding["id"].startswith("secprop/candidate-reachability/")
        ]
        self.assertTrue(cone_findings)
        for finding in cone_findings:
            self.assertEqual(finding["status"], "heuristic")
            self.assertIn("candidate", finding["title"])
            self.assertEqual(finding["method"]["kind"], "structural")
            self.assertNotIn("bounds", finding)

    def test_a_violation_carries_a_witness_and_its_bound(self):
        document, report = self._document("faulty")
        refutations = [
            finding
            for finding in document["findings"]
            if finding["status"] == "bounded_counterexample"
        ]
        self.assertEqual(len(refutations), 2)
        for finding in refutations:
            self.assertTrue(finding["counterexample"]["witness"])
            self.assertTrue(finding["counterexample"]["witness_available"])
            self.assertLessEqual(
                finding["counterexample"]["cycle_bound"], finding["bounds"]["cycle_bound"]
            )
            self.assertFalse(finding["bounds"]["unbounded"])

    def test_the_document_exports_everything_a_reader_needs_to_re_derive_it(self):
        """policy + solver settings + cycle bound + net references, per the issue."""
        document, report = self._document("faulty")
        artifacts = {entry["artifact_id"]: entry for entry in document["artifacts"]}
        self.assertEqual(sorted(artifacts), ["design", "policy"])
        for artifact in artifacts.values():
            self.assertTrue(artifact["sha256"], artifact["artifact_id"])
        configuration = document["analysis"]["configuration"]
        self.assertEqual(configuration["bound"], report.bound)
        self.assertEqual(configuration["policy"], "secreg_faulty")
        self.assertEqual(configuration["options"], dict(report.policy.options))

        checked = [
            finding
            for finding in document["findings"]
            if finding["id"].startswith("secprop/") and finding.get("solver")
        ]
        self.assertTrue(checked)
        for finding in checked:
            options = finding["solver"]["options"]
            for key in ("decision_limit", "conflict_limit", "timeout_s"):
                self.assertIsNotNone(options[key], key)
            self.assertEqual(finding["bounds"]["cycle_bound"], report.bound)

    def test_net_references_are_exported_when_the_front_end_supplies_ids(self):
        report, system, parsed, _ = run_engine("faulty")
        model = findings.findings_model()
        # the offline reader has no HAL ids; simulate the hal_py front end's map
        net_refs = {
            name: model.net_ref("design", index + 1, name)
            for index, name in enumerate(sorted(system.signals))
        }
        document = findings.build_document(
            report,
            model.artifact("design", kind="netlist", path=netlist_path("faulty"),
                           sha256=findings_serialize.sha256_file(netlist_path("faulty"))),
            model.artifact("policy", kind="other", path=policy_path("faulty"),
                           sha256=findings_serialize.sha256_file(policy_path("faulty"))),
            net_refs=net_refs,
            generated_at="2026-09-08T00:00:00Z",
        )
        self.assertEqual(findings_validate.collect_errors(document), [])
        locked = next(
            finding
            for finding in document["findings"]
            if finding["id"] == "secprop/locked-write-blocked/SECRET"
        )
        scoped = {entry["name"] for entry in locked["scope"]["nets"]}
        for bit in parsed.registers[0].bits:
            self.assertIn(bit.signal, scoped)
        self.assertIn(parsed.lock_signal, scoped)
        witnessed = {entry["signal"] for entry in locked["counterexample"]["witness"]}
        self.assertIn("lock", witnessed)
        self.assertIn("PRDATA[0]", witnessed)

    def test_the_exclusions_are_always_reported(self):
        document, _ = self._document("ok")
        identifiers = {finding["id"] for finding in document["findings"]}
        for exclusion in properties_module.EXCLUSIONS:
            self.assertIn(exclusion["id"], identifiers)

    def test_assumptions_name_the_policy_and_the_reset(self):
        document, _ = self._document("ok")
        proven = [
            finding
            for finding in document["findings"]
            if finding["status"] == "proven_bounded"
        ]
        self.assertTrue(proven)
        for finding in proven:
            identifiers = {entry["id"] for entry in finding["assumptions"]}
            self.assertIn("secprop/tool/policy-is-trusted", identifiers)
            self.assertIn("secprop/env/reset-sequence", identifiers)
            self.assertIn("secprop/env/single-clock", identifiers)

    def test_the_unsupported_document_checks_nothing_and_says_so(self):
        parsed = load_policy("blackbox")
        try:
            load_system("blackbox")
            self.fail("the latch fixture must be refused")
        except UnsupportedPrimitives as error:
            model = findings.findings_model()
            document = findings.build_unsupported_document(
                parsed,
                error.primitives,
                str(error),
                model.artifact(
                    "design",
                    kind="netlist",
                    path=netlist_path("blackbox"),
                    sha256=findings_serialize.sha256_file(netlist_path("blackbox")),
                ),
                model.artifact(
                    "policy",
                    kind="other",
                    path=policy_path("blackbox"),
                    sha256=findings_serialize.sha256_file(policy_path("blackbox")),
                ),
                generated_at="2026-09-08T00:00:00Z",
            )
        self.assertEqual(findings_validate.collect_errors(document), [])
        statuses = {finding["status"] for finding in document["findings"]}
        self.assertEqual(statuses, {"unsupported"})
        identifiers = {finding["id"] for finding in document["findings"]}
        for prop in properties_module.build(parsed):
            self.assertIn(prop.id, identifiers)


# ---------------------------------------------------------------------------
# the CLI, end to end and offline
# ---------------------------------------------------------------------------


class CliTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="secprop_cli_")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def run_cli(self, arguments):
        """Run the CLI with its output captured; returns ``(code, stdout)``."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(arguments))
        self.output = out.getvalue()
        self.errors = err.getvalue()
        return code, self.output

    def _check(self, variant, *extra):
        output = os.path.join(self.directory, variant, "findings.json")
        code, _ = self.run_cli(
            ["check", policy_path(variant), "-o", output, "--source", "offline"]
            + list(extra)
        )
        return code, output

    def test_correct_fixture_exits_zero(self):
        code, output = self._check("ok")
        self.assertEqual(code, cli.EXIT_OK)
        document = findings_serialize.read_document(output)
        self.assertEqual(findings_validate.collect_errors(document), [])

    def test_faulty_fixture_exits_one_and_the_witness_replays(self):
        code, output = self._check("faulty")
        self.assertEqual(code, cli.EXIT_VIOLATION)
        evidence = os.path.join(os.path.dirname(output), "evidence")
        bundles = [
            name for name in os.listdir(evidence) if name.endswith(".replay.json")
        ]
        self.assertEqual(len(bundles), 2)
        for name in bundles:
            code, printed = self.run_cli(
                ["replay", os.path.join(evidence, name), "--source", "offline"]
            )
            self.assertEqual(code, cli.EXIT_OK)
            self.assertIn("reproduced", printed)
            self.assertIn("transaction sequence", printed)
        self.assertTrue(
            any(name.endswith(".transactions.txt") for name in os.listdir(evidence))
        )
        self.assertTrue(any(name.endswith(".vcd") for name in os.listdir(evidence)))
        self.assertTrue(any(name.endswith(".smt2") for name in os.listdir(evidence)))

    def test_replay_refuses_a_changed_netlist(self):
        _, output = self._check("faulty")
        evidence = os.path.join(os.path.dirname(output), "evidence")
        bundle_path = os.path.join(
            evidence,
            [name for name in os.listdir(evidence) if name.endswith(".replay.json")][0],
        )
        modified = os.path.join(self.directory, "modified.v")
        with open(netlist_path("faulty"), "r", encoding="utf-8") as handle:
            text = handle.read()
        with open(modified, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n// a comment is enough to change the hash\n")
        with open(bundle_path, "r", encoding="utf-8") as handle:
            bundle = json.load(handle)
        bundle["design_source"]["netlist"] = modified
        with open(bundle_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(bundle, handle, indent=2, sort_keys=True)
        code, _ = self.run_cli(["replay", bundle_path, "--source", "offline"])
        self.assertEqual(code, cli.EXIT_UNUSABLE)
        self.assertIn("REPLAY REFUSED", self.errors)

    def test_blackbox_fixture_reports_unsupported(self):
        code, output = self._check("blackbox")
        self.assertEqual(code, cli.EXIT_OK)
        document = findings_serialize.read_document(output)
        self.assertEqual(
            {finding["status"] for finding in document["findings"]}, {"unsupported"}
        )

    def test_strict_escalates_an_inconclusive_run(self):
        code, _ = self._check("ok", "--conflict-limit", "0", "--strict")
        self.assertEqual(code, cli.EXIT_UNUSABLE)

    def test_validate_policy_checks_the_design(self):
        code, printed = self.run_cli(
            ["validate-policy", policy_path("ok"), "--check-design", "--source", "offline"]
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("every policy signal exists", printed)

    def test_cones_prints_the_report_on_stdout(self):
        code, printed = self.run_cli(["cones", policy_path("ok"), "--source", "offline"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("CANDIDATE", printed)

    def test_cones_json_is_exclusive(self):
        """Issue #66: with --json, stdout is the document and nothing else.

        The prose moves to stderr in full -- it is not dropped -- so a reader
        still gets the "these are CANDIDATE paths" caveat, and a consumer can
        do ``json.loads`` of the whole stream.
        """
        code, printed = self.run_cli(
            ["cones", policy_path("ok"), "--source", "offline", "--json"]
        )
        self.assertEqual(code, cli.EXIT_OK)
        document = json.loads(printed)
        self.assertEqual(sorted(document), ["cones", "summary"])
        self.assertIn("SECRET", document["cones"])
        self.assertNotIn("CANDIDATE", printed)
        self.assertIn("CANDIDATE", self.errors)
        self.assertIn("register bit(s) are in the fan-in", self.errors)

    def test_properties_and_exclusions_run(self):
        self.assertEqual(self.run_cli(["properties", policy_path("ok")])[0], cli.EXIT_OK)
        self.assertEqual(self.run_cli(["exclusions"])[0], cli.EXIT_OK)


# ---------------------------------------------------------------------------
# the properties themselves
# ---------------------------------------------------------------------------


class PropertyTest(unittest.TestCase):
    def test_the_catalogue_matches_the_policy(self):
        parsed = load_policy("ok")
        identifiers = [prop.id for prop in properties_module.build(parsed)]
        self.assertEqual(
            sorted(identifiers),
            [
                "secprop/lock-integrity",
                "secprop/locked-write-blocked/SECRET",
                "secprop/reset-clears/SECRET",
            ],
        )

    def test_reset_clears_skips_bits_without_a_declared_reset_value(self):
        parsed = load_policy("ok")
        parsed.registers[0].bits[0].reset_value = None
        reset = next(
            prop
            for prop in properties_module.build(parsed)
            if prop.id.startswith("secprop/reset-clears/")
        )
        self.assertEqual(len(reset.bit_terms), 3)
        self.assertNotIn(parsed.registers[0].bits[0].signal, reset.signals)

    def test_locked_write_ignores_reset_cycles(self):
        """A register being forced to its reset value is not a break-in."""
        parsed = load_policy("ok")
        prop = next(
            item
            for item in properties_module.build(parsed)
            if item.id.startswith("secprop/locked-write-blocked/")
        )
        trace = [
            {parsed.reset_signal: False, parsed.lock_signal: True},
            {parsed.reset_signal: False, parsed.lock_signal: True},
        ]
        for bit in parsed.registers[0].bits:
            trace[0][bit.signal] = True
            trace[1][bit.signal] = False
        context = witness.TraceContext(parsed, trace)
        # reset asserted (active low, so the signal is 0) -> the antecedent is false
        self.assertTrue(context.holds(prop, 0))
        self.assertFalse(expr.const_value(prop.antecedent(context, 0)))


if __name__ == "__main__":
    unittest.main()
