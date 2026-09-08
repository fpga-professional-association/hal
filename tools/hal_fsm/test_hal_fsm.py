"""Unit tests for hal_fsm.  No HAL build, no netlist, no solver.

Run from the repository root::

    python -m unittest discover -s tools/hal_fsm -t tools -p "test_*.py"

The interesting cases are the negative ones: a candidate that is not an FSM, a
solver that returns nothing, a bit order that does not match the reference, a
relation that is not deterministic, a witness search that hits its bound.  Those
are the situations in which a tool is tempted to claim something it has not
shown, so they are the ones under test.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_findings import validate as findings_validate  # noqa: E402
from hal_fsm import candidates, config, diagram, extract, findings, reference  # noqa: E402
from hal_fsm import run as run_module  # noqa: E402
from hal_fsm import solve, stubs, transitions  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GROUND_TRUTH = os.path.join(
    REPO_ROOT, "tests", "fixtures", "fsm_controller", "ground_truth.json"
)


def fixture_extraction():
    netlist, net = stubs.fixture_netlist()
    return netlist, net, extract.extract(netlist)


# ---------------------------------------------------------------------------
# graph algorithms
# ---------------------------------------------------------------------------


class SccTest(unittest.TestCase):
    def test_singletons_and_cycles(self):
        nodes = {1, 2, 3, 4}
        edges = {1: {2}, 2: {1}, 3: {3}, 4: set()}
        components = candidates.strongly_connected_components(nodes, edges)
        self.assertIn([1, 2], components)
        self.assertIn([3], components)
        self.assertIn([4], components)

    def test_deep_chain_does_not_recurse(self):
        depth = 5000
        nodes = set(range(depth))
        edges = {index: {index + 1} for index in range(depth - 1)}
        components = candidates.strongly_connected_components(nodes, edges)
        self.assertEqual(len(components), depth)


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


class ExtractionTest(unittest.TestCase):
    def setUp(self):
        self.netlist, self.net, self.extraction = fixture_extraction()
        self.graph = self.extraction.graph
        self.by_name = {gate.name: gate for gate in self.graph.gates.values()}

    def test_only_sequential_gates_become_nodes(self):
        self.assertEqual(
            sorted(self.by_name),
            ["cnt_r0", "cnt_r1", "cnt_r2", "dp_x0", "dp_x1", "nx_a1_reg", "nx_b2_reg"],
        )

    def test_controller_bits_depend_on_each_other(self):
        a = self.by_name["nx_a1_reg"].id
        b = self.by_name["nx_b2_reg"].id
        self.assertEqual(self.graph.depends[a], {a, b})
        self.assertEqual(self.graph.depends[b], {a, b})

    def test_counter_is_not_strongly_connected(self):
        ids = {name: self.by_name[name].id for name in ("cnt_r0", "cnt_r1", "cnt_r2")}
        self.assertEqual(self.graph.depends[ids["cnt_r0"]], {ids["cnt_r0"]})
        self.assertEqual(
            self.graph.depends[ids["cnt_r2"]], {ids["cnt_r0"], ids["cnt_r1"], ids["cnt_r2"]}
        )
        self.assertNotIn(ids["cnt_r2"], self.graph.depends[ids["cnt_r0"]])

    def test_pipeline_has_no_feedback(self):
        x0 = self.by_name["dp_x0"].id
        x1 = self.by_name["dp_x1"].id
        self.assertEqual(self.graph.depends[x0], set())
        self.assertEqual(self.graph.depends[x1], {x0})

    def test_cone_stops_at_flip_flops_and_includes_constants(self):
        a = self.by_name["nx_a1_reg"].id
        cone_names = sorted(
            self.extraction.gates_by_id[gate_id].get_name() for gate_id in self.graph.cones[a]
        )
        self.assertEqual(cone_names, ["u_and_a", "u_inv_b", "u_inv_f", "u_mux_a", "u_vcc"])

    def test_free_inputs_are_the_primary_inputs_of_the_machine(self):
        members = [self.by_name["nx_a1_reg"].id, self.by_name["nx_b2_reg"].id]
        names = sorted(
            self.extraction.net_name(net_id) for net_id in self.graph.free_inputs_of(members)
        )
        self.assertEqual(names, ["i_fin", "i_go"])

    def test_a_single_controller_bit_sees_the_other_as_an_input(self):
        members = [self.by_name["nx_a1_reg"].id]
        names = sorted(
            self.extraction.net_name(net_id) for net_id in self.graph.free_inputs_of(members)
        )
        self.assertEqual(names, ["i_fin", "i_go", "q_b"])

    def test_reset_pins_are_classified(self):
        controller = self.by_name["nx_a1_reg"]
        resets = [entry for entry in controller.control if entry["pin_type"] == "reset"]
        self.assertEqual(len(resets), 1)
        self.assertEqual(resets[0]["constant"], 0)

        counter = self.by_name["cnt_r0"]
        resets = [entry for entry in counter.control if entry["pin_type"] == "reset"]
        self.assertEqual(len(resets), 1)
        self.assertIsNone(resets[0]["constant"])
        self.assertEqual(resets[0]["net_name"], "i_rst")

    def test_enable_pin_is_recognised_as_tied_high(self):
        controller = self.by_name["nx_a1_reg"]
        enables = [entry for entry in controller.control if entry["pin_type"] == "enable"]
        self.assertEqual([entry["constant"] for entry in enables], [1])

    def test_init_attribute_is_read(self):
        self.assertEqual(self.by_name["nx_a1_reg"].init_value, 0)

    def test_resolve_gates_reports_unknown_names(self):
        resolved, problems = extract.resolve_gates(self.extraction, ["nx_a1_reg", "nope"])
        self.assertEqual(len(resolved), 1)
        self.assertEqual(len(problems), 1)
        self.assertIn("nope", problems[0])


class InitParsingTest(unittest.TestCase):
    def test_the_verilog_parsers_own_format(self):
        # plugins/verilog_parser converts #(.INIT(1'h0)) to ("bit_value", "0")
        self.assertEqual(extract.parse_init_value(("bit_value", "0")), 0)
        self.assertEqual(extract.parse_init_value(("bit_value", "1")), 1)

    def test_other_notations(self):
        self.assertEqual(extract.parse_init_value(("bit_vector", "1'h0")), 0)
        self.assertEqual(extract.parse_init_value(("generic", "1'b1")), 1)
        self.assertEqual(extract.parse_init_value("0x1"), 1)

    def test_unparseable_stays_unknown(self):
        # An unreadable INIT must leave the initial state *assumed*, never guessed:
        # hal_fsm falls back to zero and does not discharge the assumption.
        self.assertIsNone(extract.parse_init_value(None))
        self.assertIsNone(extract.parse_init_value(("bit_value", "")))
        self.assertIsNone(extract.parse_init_value(("bit_vector", "C8CCCCCC")))
        self.assertIsNone(extract.parse_init_value(("bit_vector", "64'hc8cccccc")))


# ---------------------------------------------------------------------------
# candidate proposal
# ---------------------------------------------------------------------------


class ProposalTest(unittest.TestCase):
    def setUp(self):
        self.netlist, self.net, self.extraction = fixture_extraction()
        self.graph = self.extraction.graph
        self.limits = config.Limits()
        self.proposed, self.notes = candidates.propose(self.graph, limits=self.limits)

    def names(self, candidate):
        return sorted(candidate.names(self.graph))

    def test_controller_is_proposed_first(self):
        self.assertEqual(self.names(self.proposed[0]), ["nx_a1_reg", "nx_b2_reg"])
        self.assertIn("scc", self.proposed[0].sources)

    def test_counter_is_proposed_but_ranked_lower(self):
        self.assertEqual(self.names(self.proposed[1]), ["cnt_r0", "cnt_r1", "cnt_r2"])
        self.assertIn("self_loop_cluster", self.proposed[1].sources)
        self.assertGreater(self.proposed[0].score, self.proposed[1].score)

    def test_the_difference_is_mutual_feedback(self):
        self.assertEqual(self.proposed[0].features["mutual_feedback"], 1.0)
        self.assertEqual(self.proposed[1].features["mutual_feedback"], 0.5)

    def test_pipeline_is_not_proposed_but_is_reported(self):
        for candidate in self.proposed:
            self.assertNotIn("dp_x0", self.names(candidate))
        self.assertTrue(any("dp_x0" in note for note in self.notes))

    def test_no_ambiguity_at_the_default_margin(self):
        self.assertEqual(candidates.ambiguous_group(self.proposed, 0.05), [])

    def test_a_wide_margin_makes_the_tie_explicit(self):
        tied = candidates.ambiguous_group(self.proposed, 0.5)
        self.assertEqual(len(tied), 2)

    def test_dataflow_agreement_raises_the_score(self):
        ids = sorted(
            gate.id for gate in self.graph.gates.values() if gate.name.startswith("nx_")
        )
        with_dataflow, _ = candidates.propose(
            self.graph, dataflow_groups={0: ids}, limits=self.limits
        )
        self.assertGreater(with_dataflow[0].score, self.proposed[0].score)
        self.assertEqual(sorted(with_dataflow[0].sources), ["dataflow", "scc"])

    def test_max_state_bits_is_reported_not_silently_applied(self):
        limits = config.Limits({"max_state_bits": 2})
        _, notes = candidates.propose(self.graph, limits=limits)
        self.assertTrue(any("max_state_bits" in note for note in notes))


# ---------------------------------------------------------------------------
# bit orders, comparison, reachability
# ---------------------------------------------------------------------------


def simple_table(bit_order=("ff0", "ff1"), initial_state=0):
    table = transitions.TransitionTable(list(bit_order), initial_state=initial_state)
    for source, target, condition, variables in (
        (0, 0, "!go", ("go",)),
        (0, 1, "go", ("go",)),
        (1, 1, "!fin", ("fin",)),
        (1, 2, "fin", ("fin",)),
        (2, 0, "1", ()),
    ):
        table.add(transitions.Transition(source, target, condition, variables))
    return table


class BitOrderTest(unittest.TestCase):
    def test_permutation_renames_states(self):
        permutation = transitions.bit_permutation(["b", "a"], ["a", "b"])
        self.assertEqual(permutation, [1, 0])
        self.assertEqual(transitions.permute_state(0b01, permutation), 0b10)

    def test_different_registers_are_an_error_not_a_difference(self):
        with self.assertRaises(ValueError):
            transitions.bit_permutation(["a", "b"], ["a", "c"])

    def test_a_renamed_register_still_compares_equal(self):
        machine = reference.load(GROUND_TRUTH).get("controller")
        # the same machine, discovered in the opposite bit order
        swapped = transitions.TransitionTable(["nx_b2_reg", "nx_a1_reg"], initial_state=0)
        for source, target in machine.edges:
            permutation = [1, 0]
            swapped.add(
                transitions.Transition(
                    transitions.permute_state(source, permutation),
                    transitions.permute_state(target, permutation),
                )
            )
        report = machine.compare(swapped)
        self.assertTrue(report["matches"], report)
        self.assertEqual(report["permutation"], [1, 0])

    def test_a_wrong_edge_is_reported(self):
        machine = reference.load(GROUND_TRUTH).get("controller")
        table = transitions.TransitionTable(["nx_a1_reg", "nx_b2_reg"])
        for source, target in machine.edges:
            if (source, target) == (1, 2):
                continue
            table.add(transitions.Transition(source, target))
        table.add(transitions.Transition(1, 3))
        report = machine.compare(table)
        self.assertFalse(report["matches"])
        self.assertEqual(report["missing"], [[1, 2]])
        self.assertEqual(report["unexpected"], [[1, 3]])

    def test_reachable_only_comparison_ignores_the_unused_state(self):
        machine = reference.load(GROUND_TRUTH).get("controller")
        table = transitions.TransitionTable(["nx_a1_reg", "nx_b2_reg"])
        for source, target in machine.edges:
            if source == 3:
                continue  # an SMT run never visits the unreachable state
            table.add(transitions.Transition(source, target))
        self.assertFalse(machine.compare(table)["matches"])
        self.assertTrue(machine.compare(table, restrict_to_reachable=True)["matches"])


class InitialStateEncodingTest(unittest.TestCase):
    def test_zero_is_unambiguous(self):
        bits, encoding, note = transitions.initial_state_argument(0, 3, "auto")
        self.assertEqual(bits, [0, 0, 0])
        self.assertIsNone(note)

    def test_asymmetric_state_is_compensated_and_explained(self):
        # state value 1 means "state_reg[0] is high".  solve_fsm shifts the first
        # flip-flop of the list into the *most* significant bit, so to make its
        # search start at state 1 the bits have to be handed over reversed.
        bits, encoding, note = transitions.initial_state_argument(0b001, 3, "auto")
        self.assertEqual(encoding, "transition_index")
        self.assertEqual(bits, [0, 0, 1])
        solver_value = 0
        for bit in bits:
            solver_value = (solver_value << 1) | bit
        self.assertEqual(solver_value, 0b001)
        self.assertIn("solve_fsm", note)

    def test_argument_order_passes_the_bits_through(self):
        bits, encoding, _ = transitions.initial_state_argument(0b001, 3, "argument_order")
        self.assertEqual(encoding, "argument_order")
        self.assertEqual(bits, [1, 0, 0])


class ReachabilityTest(unittest.TestCase):
    def test_reachable_closure(self):
        states, truncated = transitions.reachable_states(simple_table())
        self.assertEqual(states, [0, 1, 2])
        self.assertFalse(truncated)

    def test_shortest_path(self):
        path, depth = transitions.shortest_path(simple_table(), 2, max_cycles=8)
        self.assertEqual(path, [0, 1, 2])
        self.assertEqual(depth, 2)

    def test_bound_is_respected(self):
        path, depth = transitions.shortest_path(simple_table(), 2, max_cycles=1)
        self.assertIsNone(path)
        self.assertEqual(depth, 1)

    def test_explored_set_cross_check(self):
        table = simple_table()
        matches, only_explored, only_reachable = transitions.explored_set_matches(table)
        self.assertTrue(matches)

        detached = simple_table()
        detached.add(transitions.Transition(7, 7, "1", ()))
        matches, only_explored, _ = transitions.explored_set_matches(detached)
        self.assertFalse(matches)
        self.assertEqual(only_explored, [7])


class WitnessTest(unittest.TestCase):
    def evaluator(self, table):
        def evaluate(source, target, assignment):
            condition = table.successors(source)[target].condition
            if condition == "1":
                return True
            negated = condition.startswith("!")
            name = condition.lstrip("!")
            value = assignment.get(name)
            if value is None:
                return None
            return bool(value) != negated

        return evaluate

    def test_witness_is_checked_step_by_step(self):
        table = simple_table()
        steps = transitions.build_witness(table, [0, 1, 2], self.evaluator(table))
        self.assertEqual([step["target"] for step in steps], [1, 2])
        self.assertEqual(steps[0]["inputs"], {"go": 1})
        self.assertEqual(steps[1]["inputs"], {"fin": 1})

    def test_unsatisfiable_condition_is_not_silently_dropped(self):
        table = simple_table()
        table.add(transitions.Transition(2, 3, "impossible", ("x",)))

        def evaluate(source, target, assignment):
            return False

        with self.assertRaises(transitions.WitnessError) as raised:
            transitions.build_witness(table, [2, 3], evaluate)
        self.assertEqual(raised.exception.kind, "unsatisfiable")

    def test_too_many_variables_is_a_limit_not_a_verdict(self):
        table = transitions.TransitionTable(["a"])
        table.add(
            transitions.Transition(0, 1, "big", tuple("v{}".format(i) for i in range(20)))
        )
        with self.assertRaises(transitions.WitnessError) as raised:
            transitions.build_witness(table, [0, 1], lambda *_: True, max_condition_vars=8)
        self.assertEqual(raised.exception.kind, "limit")

    def test_undecided_evaluation_is_not_false(self):
        table = simple_table()
        with self.assertRaises(transitions.WitnessError) as raised:
            transitions.build_witness(table, [0, 1], lambda *_: None)
        self.assertEqual(raised.exception.kind, "unknown")


class DeterminismTest(unittest.TestCase):
    def test_a_clean_relation_passes(self):
        table = simple_table()
        report = transitions.check_determinism(table, WitnessTest().evaluator(table))
        self.assertTrue(report["ok"])
        self.assertEqual(report["checked_states"], 3)

    def test_two_enabled_successors_are_reported(self):
        table = simple_table()
        report = transitions.check_determinism(table, lambda *_: True)
        self.assertFalse(report["ok"])
        self.assertTrue(report["nondeterministic"])

    def test_no_enabled_successor_is_reported(self):
        table = simple_table()
        report = transitions.check_determinism(table, lambda *_: False)
        self.assertFalse(report["ok"])
        self.assertTrue(report["incomplete"])

    def test_states_beyond_the_variable_limit_are_skipped_not_passed(self):
        table = transitions.TransitionTable(["a"])
        table.add(
            transitions.Transition(0, 1, "big", tuple("v{}".format(i) for i in range(20)))
        )
        report = transitions.check_determinism(table, lambda *_: True, max_condition_vars=4)
        self.assertEqual(report["checked_states"], 0)
        self.assertEqual(len(report["skipped_states"]), 1)
        self.assertFalse(report["ok"])


# ---------------------------------------------------------------------------
# diagram
# ---------------------------------------------------------------------------


class DiagramTest(unittest.TestCase):
    def test_initial_state_and_bit_order_are_in_the_output(self):
        source = diagram.state_diagram(simple_table(), base=2).to_dot()
        self.assertIn("state bit order (bit 0 first): ff0, ff1", source)
        self.assertIn("doublecircle", source)
        self.assertIn('"__entry__" -> "s0" [label="reset"]', source)

    def test_witness_path_is_highlighted(self):
        source = diagram.state_diagram(simple_table(), witness_path=[0, 1, 2]).to_dot()
        self.assertIn("#b30000", source)

    def test_truncation_is_drawn_not_hidden(self):
        source = diagram.state_diagram(simple_table(), max_states=2).to_dot()
        self.assertIn("more state(s)", source)

    def test_conditions_are_shown_with_net_names(self):
        table = simple_table()
        table.transitions[1].condition = "net_7"
        table.signals = {"net_7": {"name": "i_go", "net_id": 7, "role": "input"}}
        source = diagram.state_diagram(table).to_dot()
        self.assertIn('label="i_go"', source)
        self.assertEqual(diagram.readable_condition("(! net_7)", table.signals), "(! i_go)")

    def test_an_unknown_variable_keeps_its_net_id(self):
        self.assertEqual(diagram.readable_condition("net_9 & net_7", {}), "net_9 & net_7")

    def test_incomplete_recovery_is_labelled(self):
        table = simple_table()
        table.complete = False
        source = diagram.state_diagram(table).to_dot()
        self.assertIn("INCOMPLETE", source)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


class ConfigurationTest(unittest.TestCase):
    def test_unknown_key_is_rejected(self):
        with self.assertRaises(config.ConfigError) as raised:
            config.from_dict({"config_version": "1.0.0", "max_state_bit": 4})
        self.assertIn("unknown configuration key", str(raised.exception))

    def test_unknown_limit_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.from_dict({"config_version": "1.0.0", "limits": {"max_state_bit": 4}})

    def test_limits_above_the_plugin_ceiling_are_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.from_dict({"config_version": "1.0.0", "limits": {"max_state_bits": 65}})

    def test_transition_logic_without_state_registers_is_rejected(self):
        with self.assertRaises(config.ConfigError):
            config.from_dict({"config_version": "1.0.0", "transition_logic": ["u_and_a"]})

    def test_round_trips_through_json(self):
        original = config.from_dict(
            {
                "config_version": "1.0.0",
                "state_registers": ["nx_a1_reg", "nx_b2_reg"],
                "exclude_gates": ["dp_x0"],
                "targets": [2],
                "limits": {"max_cycles": 4},
            }
        )
        again = config.from_dict(original.to_json())
        self.assertEqual(again.to_json(), original.to_json())
        self.assertEqual(again.override.state_registers, ["nx_a1_reg", "nx_b2_reg"])
        self.assertEqual(again.limits.max_cycles, 4)

    def test_shipped_example_is_valid(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "examples", "controller.json")
        configuration = config.load(path)
        self.assertTrue(os.path.isfile(configuration.reference))

    def test_shipped_fixture_configurations_are_valid(self):
        directory = os.path.join(REPO_ROOT, "tests", "fixtures", "fsm_controller")
        for name in ("counter_override.json", "wrong_candidate.json"):
            configuration = config.load(os.path.join(directory, name))
            self.assertTrue(configuration.override.active)


class ReferenceTest(unittest.TestCase):
    def test_ground_truth_loads(self):
        document = reference.load(GROUND_TRUTH)
        self.assertEqual([machine.id for machine in document.machines], ["controller", "counter"])
        controller = document.get("controller")
        self.assertEqual(controller.reachable_states, [0, 1, 2])
        self.assertEqual(controller.unreachable_states, [3])

    def test_version_mismatch_is_refused(self):
        with self.assertRaises(reference.ReferenceError):
            reference.from_dict({"reference_version": "9.9.9", "machines": []})

    def test_machine_without_state_registers_is_refused(self):
        with self.assertRaises(reference.ReferenceError):
            reference.from_dict(
                {
                    "reference_version": "1.0.0",
                    "machines": [{"id": "x", "transitions": [{"source": 0, "target": 0}]}],
                }
            )

    def test_lookup_by_register(self):
        document = reference.load(GROUND_TRUTH)
        self.assertEqual(document.for_register(["nx_b2_reg", "nx_a1_reg"]).id, "controller")
        self.assertIsNone(document.for_register(["nope"]))


# ---------------------------------------------------------------------------
# the whole run, against the stub netlist and the stub solver
# ---------------------------------------------------------------------------


class RunTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="hal_fsm_test_")
        self.netlist, self.net = stubs.fixture_netlist()
        self.hal_py = stubs.HalPy()
        controller_gates = frozenset(("nx_a1_reg", "nx_b2_reg"))
        counter_gates = frozenset(("cnt_r0", "cnt_r1", "cnt_r2"))
        self.plugin = stubs.StubSolveFsm(
            {
                controller_gates: stubs.controller_model(self.net),
                counter_gates: stubs.counter_model(self.net),
            }
        )

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def analyse(self, configuration=None, plugin=None):
        configuration = configuration or config.Configuration(reference=GROUND_TRUTH)
        return run_module.analyse(
            self.hal_py,
            plugin or self.plugin,
            self.netlist,
            configuration,
            self.directory,
            netlist_path=os.path.join(REPO_ROOT, "tests", "fixtures", "fsm_controller", "controller.v"),
        )

    def findings_by_id(self, document):
        return {finding["id"]: finding for finding in document["findings"]}

    # -- the happy path --------------------------------------------------

    def test_document_validates(self):
        document, _, _, _ = self.analyse()
        findings_validate.validate_document(document)

    def test_controller_is_the_solved_machine(self):
        document, _, metrics, _ = self.analyse()
        by_id = self.findings_by_id(document)
        self.assertEqual(metrics["solved"], 1)
        transitions_finding = by_id["fsm/machine01/transitions"]
        self.assertEqual(
            transitions_finding["data"]["bit_order"], ["nx_a1_reg", "nx_b2_reg"]
        )
        self.assertEqual(transitions_finding["data"]["states"], [0, 1, 2])

    def test_candidate_confidence_and_solver_claim_are_separate_findings(self):
        document, _, _, _ = self.analyse()
        by_id = self.findings_by_id(document)
        candidate = by_id["fsm/candidate/001"]
        transitions_finding = by_id["fsm/machine01/transitions"]
        self.assertEqual(candidate["status"], "heuristic")
        self.assertIn("confidence", candidate)
        self.assertEqual(transitions_finding["status"], "proven_under_assumptions")
        self.assertNotIn("confidence", transitions_finding)
        # the heuristic is an explicit assumption of the proven finding
        assumption_ids = [
            entry["id"] for entry in transitions_finding["assumptions"]
        ]
        self.assertIn("hal_fsm.state-register", assumption_ids)

    def test_reference_comparison_matches_the_ground_truth(self):
        document, _, _, _ = self.analyse()
        comparison = self.findings_by_id(document)["fsm/machine01/reference-comparison"]
        self.assertEqual(comparison["status"], "proven_under_assumptions")
        self.assertEqual(comparison["data"]["missing"], [])
        self.assertEqual(comparison["data"]["unexpected"], [])

    def test_reset_assumption_is_discharged_for_the_controller(self):
        document, _, _, _ = self.analyse()
        transitions_finding = self.findings_by_id(document)["fsm/machine01/transitions"]
        by_assumption = {entry["id"]: entry for entry in transitions_finding["assumptions"]}
        self.assertTrue(by_assumption["hal_fsm.asynchronous-control-inactive"]["discharged"])
        self.assertTrue(by_assumption["hal_fsm.initial-state"]["discharged"])
        self.assertTrue(by_assumption["hal_fsm.transition-cone-complete"]["discharged"])
        self.assertFalse(by_assumption["hal_fsm.state-register"]["discharged"])

    def test_determinism_check_runs_and_passes(self):
        document, _, _, _ = self.analyse()
        consistency = self.findings_by_id(document)["fsm/machine01/relation-consistency"]
        self.assertEqual(consistency["status"], "proven_under_assumptions")
        self.assertEqual(consistency["metrics"]["checked_states"], 3)

    def test_witness_is_produced_and_checked(self):
        configuration = config.Configuration(reference=GROUND_TRUTH, targets=[2])
        document, _, _, _ = self.analyse(configuration)
        witness = self.findings_by_id(document)["fsm/machine01/witness/2"]
        self.assertEqual(witness["status"], "proven_under_assumptions")
        self.assertEqual(witness["data"]["path"], [0, 1, 2])
        self.assertEqual(witness["data"]["cycles"], 2)

        # The recovered conditions are simplified, so each step names exactly the
        # input that decides it: i_go leaving IDLE, i_fin leaving RUN.
        signals = self.findings_by_id(document)["fsm/machine01/transitions"]["data"]["signals"]
        named = [
            {signals[name]["name"]: value for name, value in step["inputs"].items()}
            for step in witness["data"]["steps"]
        ]
        self.assertEqual(named, [{"i_go": 1}, {"i_fin": 1}])
        self.assertEqual(witness["data"]["external_state_inputs"], [])

    def test_witness_beyond_the_cycle_limit_is_unknown_not_unreachable(self):
        configuration = config.Configuration(
            reference=GROUND_TRUTH, targets=[2], limits=config.Limits({"max_cycles": 1})
        )
        document, _, _, _ = self.analyse(configuration)
        witness = self.findings_by_id(document)["fsm/machine01/witness/2"]
        self.assertEqual(witness["status"], "unknown")
        self.assertIn("not evidence that the state is unreachable", witness["summary"])

    def test_artifacts_are_written(self):
        _, artifacts, _, _ = self.analyse()
        paths = sorted(entry["path"] for entry in artifacts)
        self.assertIn("state-diagram-machine01.dot", paths)
        self.assertIn("transitions-machine01.json", paths)
        for entry in artifacts:
            self.assertTrue(os.path.isfile(os.path.join(self.directory, entry["path"])))

    def test_written_table_round_trips_through_the_cli_comparison(self):
        self.analyse()
        table_path = os.path.join(self.directory, "transitions-machine01.json")
        with open(table_path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document["bit_order"], ["nx_a1_reg", "nx_b2_reg"])
        self.assertEqual(document["initial_state"], 0)

    # -- brute force -----------------------------------------------------

    def test_brute_force_reports_the_unreachable_state(self):
        configuration = config.Configuration(reference=GROUND_TRUTH, solver="brute_force")
        document, _, _, _ = self.analyse(configuration)
        by_id = self.findings_by_id(document)
        reachability = by_id["fsm/machine01/reachable-states"]
        self.assertEqual(reachability["data"]["unreachable_in_encoding"], [3])
        self.assertEqual(reachability["data"]["reachable"], [0, 1, 2])
        self.assertEqual(by_id["fsm/machine01/transitions"]["data"]["states"], [0, 1, 2, 3])

    def test_brute_force_matches_the_total_reference(self):
        configuration = config.Configuration(reference=GROUND_TRUTH, solver="brute_force")
        document, _, _, _ = self.analyse(configuration)
        comparison = self.findings_by_id(document)["fsm/machine01/reference-comparison"]
        self.assertTrue(comparison["data"]["matches"])
        self.assertFalse(comparison["data"]["restricted_to_reachable"])

    # -- overrides and failures ------------------------------------------

    def test_user_override_selects_the_counter_and_reports_the_reset_gap(self):
        configuration = config.from_dict(
            {
                "config_version": "1.0.0",
                "state_registers": ["cnt_r0", "cnt_r1", "cnt_r2"],
                "targets": [7],
            }
        )
        configuration.reference = GROUND_TRUTH
        document, _, metrics, _ = self.analyse(configuration)
        by_id = self.findings_by_id(document)
        self.assertEqual(metrics["solved"], 1)
        self.assertEqual(by_id["fsm/candidate/001"]["data"]["origin"], "user_override")
        self.assertNotIn("confidence", by_id["fsm/candidate/001"])
        gap = by_id["fsm/machine01/asynchronous-control"]
        self.assertEqual(gap["status"], "unsupported")
        self.assertIn("i_rst", json.dumps(gap["data"]))
        transitions_finding = by_id["fsm/machine01/transitions"]
        by_assumption = {entry["id"]: entry for entry in transitions_finding["assumptions"]}
        self.assertFalse(by_assumption["hal_fsm.asynchronous-control-inactive"]["discharged"])
        findings_validate.validate_document(document)

    def test_counter_witness_needs_seven_cycles(self):
        configuration = config.from_dict(
            {
                "config_version": "1.0.0",
                "state_registers": ["cnt_r0", "cnt_r1", "cnt_r2"],
                "targets": [7],
            }
        )
        document, _, _, _ = self.analyse(configuration)
        witness = self.findings_by_id(document)["fsm/machine01/witness/7"]
        self.assertEqual(witness["status"], "proven_under_assumptions")
        self.assertEqual(witness["data"]["cycles"], 7)

    def test_a_wrong_candidate_fails_loudly(self):
        configuration = config.from_dict(
            {
                "config_version": "1.0.0",
                "state_registers": ["nx_a1_reg", "cnt_r2"],
            }
        )
        document, _, metrics, _ = self.analyse(configuration)
        by_id = self.findings_by_id(document)
        self.assertEqual(metrics["solved"], 0)
        failure = by_id["fsm/machine01/transitions"]
        self.assertEqual(failure["status"], "error")
        self.assertNotIn("data", failure.get("counterexample", {}))
        self.assertNotIn("states", failure.get("metrics", {}))
        findings_validate.validate_document(document)

    def test_a_closed_machine_has_no_external_state_dependence(self):
        document, _, _, _ = self.analyse()
        by_id = self.findings_by_id(document)
        self.assertNotIn("fsm/machine01/external-state-dependence", by_id)
        assumptions = {
            entry["id"]: entry
            for entry in by_id["fsm/machine01/transitions"]["assumptions"]
        }
        self.assertTrue(assumptions["hal_fsm.closed-machine"]["discharged"])

    def test_half_a_machine_is_reported_as_not_closed(self):
        """One of the two controller bits: a register that is too small.

        solve_fsm happily solves it -- the other bit simply becomes a free
        variable -- so the only thing that distinguishes this from a real
        recovery is that a condition reads a net another flip-flop drives.
        """
        q_b = "net_{}".format(self.net["q_b"].get_id())
        go = "net_{}".format(self.net["i_go"].get_id())
        fin = "net_{}".format(self.net["i_fin"].get_id())

        def next_state(state, assignment):
            a = state & 1
            n_b = 1 - assignment[q_b]
            m_a = (1 - assignment[fin]) if a else assignment[go]
            return n_b & m_a

        plugin = stubs.StubSolveFsm(
            {frozenset(("nx_a1_reg",)): ([go, fin, q_b], next_state)}
        )
        configuration = config.from_dict(
            {"config_version": "1.0.0", "state_registers": ["nx_a1_reg"]}
        )
        document, _, _, _ = self.analyse(configuration, plugin=plugin)
        by_id = self.findings_by_id(document)
        finding = by_id["fsm/machine01/external-state-dependence"]
        self.assertEqual(finding["status"], "unsupported")
        self.assertEqual(finding["data"]["external_state"][0]["net_name"], "q_b")
        self.assertEqual(
            finding["data"]["external_state"][0]["driver_gate_name"], "nx_b2_reg"
        )
        assumptions = {
            entry["id"]: entry
            for entry in by_id["fsm/machine01/transitions"]["assumptions"]
        }
        self.assertFalse(assumptions["hal_fsm.closed-machine"]["discharged"])
        findings_validate.validate_document(document)

    def test_an_unknown_gate_in_the_override_is_an_error(self):
        configuration = config.from_dict(
            {"config_version": "1.0.0", "state_registers": ["not_a_gate"]}
        )
        with self.assertRaises(run_module.RunError):
            self.analyse(configuration)

    def test_too_many_state_bits_is_unsupported_not_attempted(self):
        configuration = config.from_dict(
            {
                "config_version": "1.0.0",
                "state_registers": ["cnt_r0", "cnt_r1", "cnt_r2"],
                "limits": {"max_state_bits": 2},
            }
        )
        document, _, _, _ = self.analyse(configuration)
        finding = self.findings_by_id(document)["fsm/machine01/transitions"]
        self.assertEqual(finding["status"], "unsupported")
        self.assertEqual(finding["unsupported"]["kind"], "scale")
        self.assertEqual(self.plugin.calls, [])
        findings_validate.validate_document(document)

    def test_a_solver_that_returns_nothing_claims_nothing(self):
        plugin = stubs.StubSolveFsm({}, fail=[frozenset(("nx_a1_reg", "nx_b2_reg"))])
        document, _, metrics, _ = self.analyse(plugin=plugin)
        finding = self.findings_by_id(document)["fsm/machine01/transitions"]
        self.assertEqual(finding["status"], "error")
        self.assertEqual(metrics["states"], 0)
        self.assertNotIn("fsm/machine01/reachable-states", self.findings_by_id(document))
        findings_validate.validate_document(document)

    def test_solve_none_proposes_without_solving(self):
        configuration = config.from_dict({"config_version": "1.0.0", "solve": "none"})
        document, _, metrics, notes = self.analyse(configuration)
        self.assertEqual(metrics["solved"], 0)
        self.assertEqual(self.plugin.calls, [])
        self.assertTrue(any("not solved" in note for note in notes))
        self.assertIn("fsm/candidate/001", self.findings_by_id(document))

    def test_uncovered_sequential_gates_are_reported(self):
        document, _, _, _ = self.analyse()
        finding = self.findings_by_id(document)["fsm/coverage/uncovered-sequential-gates"]
        self.assertEqual(finding["status"], "unsupported")
        gate_types = {
            primitive["gate_type"] for primitive in finding["unsupported"]["primitives"]
        }
        self.assertEqual(gate_types, {"FF", "FFR"})


class InitialStateCrossCheckTest(unittest.TestCase):
    """The explored-set cross-check must catch a wrong initial-state encoding.

    The machine below is one-way: from state 2 the run reaches 0 and 1, from
    state 1 it reaches nothing else.  So if ``solve_fsm`` starts somewhere other
    than the state it was asked for, the set of states it explored is not the set
    reachable from the declared initial state -- and that is the only signal
    there is, because the plugin never reports where it started.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="hal_fsm_encoding_")
        self.netlist, self.net = stubs.fixture_netlist()

        def next_state(state, assignment):
            return {0: 1, 1: 1, 2: 0, 3: 0}[state]

        self.plugin = stubs.StubSolveFsm(
            {frozenset(("nx_a1_reg", "nx_b2_reg")): ([], next_state)}
        )

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def analyse(self, encoding):
        configuration = config.from_dict(
            {
                "config_version": "1.0.0",
                "state_registers": ["nx_a1_reg", "nx_b2_reg"],
                "initial_state": 2,
                "initial_state_encoding": encoding,
            }
        )
        document, _, _, _ = run_module.analyse(
            stubs.HalPy(),
            self.plugin,
            self.netlist,
            configuration,
            self.directory,
        )
        return {finding["id"]: finding for finding in document["findings"]}

    def test_compensated_encoding_reaches_the_requested_state(self):
        by_id = self.analyse("auto")
        reachability = by_id["fsm/machine01/reachable-states"]
        self.assertEqual(reachability["status"], "proven_under_assumptions")
        self.assertEqual(reachability["data"]["reachable"], [0, 1, 2])

    def test_uncompensated_encoding_is_caught_not_believed(self):
        by_id = self.analyse("argument_order")
        reachability = by_id["fsm/machine01/reachable-states"]
        self.assertEqual(reachability["status"], "unknown")
        self.assertIsNotNone(reachability["data"]["explored_mismatch"])
        self.assertEqual(
            reachability["data"]["explored_mismatch"]["only_explored_by_solver"], [1]
        )


class CliTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="hal_fsm_cli_")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def run_cli(self, argv):
        from hal_fsm import cli

        stream = open(os.devnull, "w")
        try:
            return cli.main(argv)
        finally:
            stream.close()

    def test_validate_config_accepts_the_example(self):
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "examples", "controller.json"
        )
        self.assertEqual(self.run_cli(["-q", "validate-config", path]), 0)

    def test_validate_config_rejects_a_typo(self):
        path = os.path.join(self.directory, "bad.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"config_version": "1.0.0", "state_register": ["a"]}')
        self.assertEqual(self.run_cli(["-q", "validate-config", path]), 1)

    def test_a_killed_run_still_produces_a_valid_timeout_document(self):
        """The CLI writes this one itself: the analysis never got to write any."""
        from hal_fsm import cli

        class Execution(object):
            command = ["hal", "--python-script", "run_in_hal.py"]
            duration_s = 0.9
            timeout_s = 0.5
            stderr_path = os.path.join(self.directory, "stderr.log")

        with open(Execution.stderr_path, "w", encoding="utf-8") as handle:
            handle.write("[solve_fsm] querying SMT solver for state 0\n")

        netlist = os.path.join(REPO_ROOT, "tests", "fixtures", "fsm_controller", "controller.v")
        document = cli._timeout_document(
            None, config.Configuration(), Execution(), netlist
        )
        findings_validate.validate_document(document)
        finding = document["findings"][0]
        self.assertEqual(finding["status"], "timeout")
        self.assertEqual(finding["limits"]["timeout_s"], 0.5)
        self.assertTrue(finding["limits"]["hit"])
        self.assertEqual(len(document["artifacts"][0]["sha256"]), 64)
        self.assertFalse(finding["bounds"]["unbounded"])
        self.assertEqual(len(document["findings"]), 1)

    def test_compare_and_diagram_work_offline(self):
        table = transitions.TransitionTable(["nx_a1_reg", "nx_b2_reg"], initial_state=0)
        machine = reference.load(GROUND_TRUTH).get("controller")
        for source, target in machine.edges:
            if source == 3:
                continue
            table.add(transitions.Transition(source, target, "1", ()))
        table_path = os.path.join(self.directory, "table.json")
        with open(table_path, "w", encoding="utf-8") as handle:
            json.dump(table.to_json(), handle)

        self.assertEqual(
            self.run_cli(["-q", "compare", table_path, GROUND_TRUTH, "--reachable-only"]), 0
        )
        self.assertEqual(self.run_cli(["-q", "compare", table_path, GROUND_TRUTH]), 1)

        dot_path = os.path.join(self.directory, "graph.dot")
        self.assertEqual(self.run_cli(["-q", "diagram", table_path, "-o", dot_path]), 0)
        with open(dot_path, "r", encoding="utf-8") as handle:
            self.assertIn("nx_a1_reg, nx_b2_reg", handle.read())


class SolveHelpersTest(unittest.TestCase):
    def setUp(self):
        self.netlist, self.net, self.extraction = fixture_extraction()

    def test_state_register_order_is_by_name(self):
        ids = [
            gate.id
            for gate in self.extraction.graph.gates.values()
            if gate.name in ("nx_b2_reg", "nx_a1_reg")
        ]
        order = solve.state_register_order(self.extraction, ids)
        names = [self.extraction.graph.gates[gate_id].name for gate_id in order]
        self.assertEqual(names, ["nx_a1_reg", "nx_b2_reg"])

    def test_net_variable_names_are_resolved(self):
        self.assertEqual(solve.net_id_of("net_17"), 17)
        self.assertIsNone(solve.net_id_of("i_go"))

    def test_cone_holes_are_detected(self):
        table = transitions.TransitionTable(["nx_a1_reg"])
        table.signals = {
            "net_{}".format(self.net["m_a"].get_id()): {
                "name": "m_a",
                "net_id": self.net["m_a"].get_id(),
                "role": "internal",
            }
        }
        holes = solve.cone_holes(table, self.extraction, cone_gate_ids=[])
        self.assertEqual(len(holes), 1)
        self.assertEqual(holes[0]["driver_gate_name"], "u_mux_a")

    def test_a_primary_input_is_not_a_hole(self):
        table = transitions.TransitionTable(["nx_a1_reg"])
        table.signals = {
            "net_{}".format(self.net["i_go"].get_id()): {
                "name": "i_go",
                "net_id": self.net["i_go"].get_id(),
                "role": "input",
            }
        }
        self.assertEqual(solve.cone_holes(table, self.extraction, cone_gate_ids=[]), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
