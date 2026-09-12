#!/usr/bin/env python3
"""Exercise the Python surface of five plugins that no test used to call.

``smoke_test_bindings.py`` calls every binding that takes no arguments, which covers the accessors
but not one of these: every entry point below needs a netlist, a gate library and arguments, so none
of them was ever called from Python by the test suite. A plugin can therefore be built, shipped and
imported while its only documented way in raises on the first call.

The fixtures are the Agilex walkthroughs under ``examples/agilex3_walkthroughs``. They are used
rather than a netlist built here because their properties are published in ``spec.md`` /
``GROUND_TRUTH.md`` and re-checked by each walkthrough's ``check.py``, so every number asserted below
is a fact somebody else already verifies, not one this file invented:

* ``01_blinky_counter`` -- a 24-bit ripple counter: 24 ``tennm_ff`` on one clock ``clk``, one
  asynchronous clear ``rst_n``, ``count[0]`` toggling every cycle and ``count[1]`` every second one.
* ``02_traffic_fsm`` -- 8 flip-flops of which exactly 4 (``g1``, ``g2``, ``g4``, ``g6`` in the
  anonymised netlist) are the always-enabled state register of the controller.
* ``10_crc8_checker`` -- a CRC-8/SMBUS shift register whose ``u3`` (bit 0) takes its ``d`` from an
  ALM computing ``crc_r[7] ^ din``, i.e. ``port_o0(7) ^ port_i0`` after anonymisation.

The AGILEX gate library deliberately carries no Boolean function for ``tennm_lcell_comb``: the
function of an ALM lives in its per-instance ``lut_mask`` generic. ``tools/hal_agilex`` is what turns
that generic into a Boolean function, exactly as the walkthroughs do, so the tests that need gate
semantics call it before touching a plugin.

Three findings from writing this are encoded in the tests rather than worked around silently; see
the comments at each site: the clock tree carries no edge for a directly driven clock input, the
simulator aborts the process on an output pin without a Boolean function, and ``solve_fsm`` refuses
any flip-flop whose gate type has more than one pin of type ``data``.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WALKTHROUGHS = REPO / "examples" / "agilex3_walkthroughs"
GATE_LIBRARY = REPO / "plugins" / "gate_libraries" / "definitions" / "AGILEX_TENNM.hgl"

BLINKY = WALKTHROUGHS / "01_blinky_counter" / "netlist.hal.v"
TRAFFIC_FSM = WALKTHROUGHS / "02_traffic_fsm" / "netlist_anon.hal.v"
CRC8 = WALKTHROUGHS / "10_crc8_checker" / "netlist" / "netlist.anon.hal.v"

# The engine states of NetlistSimulatorController, which the bindings expose as plain integers.
ENGINE_DONE = 0
ENGINE_FAILED = -1

hal_py = None


def require(*paths):
    """Skip rather than fail when a fixture is not in the checkout."""
    for path in paths:
        if not path.exists():
            raise unittest.SkipTest("fixture {} is missing".format(path))


def elaborate(netlist):
    """Attach the ALM Boolean functions that the gate library cannot carry.

    ``tools/hal_agilex`` reads each ``tennm_lcell_comb``'s ``lut_mask`` generic and calls
    ``Gate.add_boolean_function``; the walkthroughs do the same before any analysis. Without it every
    ALM is a black box and every plugin below sees an empty function.
    """
    tools = str(REPO / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from hal_agilex import hal_adapter

    report = hal_adapter.elaborate(hal_py, netlist)
    if report["refused"]:
        raise unittest.SkipTest(
            "hal_agilex refused {} gate(s): {}".format(
                len(report["refused"]), report["refused"][0]["reason"]
            )
        )
    return report


def load(path, gate_library=None):
    netlist = hal_py.NetlistFactory.load_netlist(str(path), str(gate_library or GATE_LIBRARY))
    if netlist is None:
        raise AssertionError("could not load {}".format(path))
    return netlist


def gate_named(netlist, name):
    gates = netlist.get_gates(lambda gate: gate.get_name() == name)
    if len(gates) != 1:
        raise AssertionError("expected exactly one gate named {!r}, found {}".format(name, len(gates)))
    return gates[0]


def net_named(netlist, name):
    nets = netlist.get_nets(lambda net: net.get_name() == name)
    if len(nets) != 1:
        raise AssertionError("expected exactly one net named {!r}, found {}".format(name, len(nets)))
    return nets[0]


class ClockTreeExtractorTest(unittest.TestCase):
    """``clock_tree_extractor`` on the blinky counter: one clock domain, 24 flip-flops."""

    def test_single_domain_rooted_at_clk_covers_every_flip_flop(self):
        require(BLINKY, GATE_LIBRARY)
        from hal_plugins import clock_tree_extractor

        netlist = load(BLINKY)
        tree = clock_tree_extractor.ClockTree.from_netlist(netlist)
        self.assertIsNotNone(tree, "ClockTree.from_netlist returned None")

        # One net in the tree is one clock domain: the extractor only inserts a net when it is the
        # root a flip-flop's clock pin is reached from.
        nets = tree.get_nets()
        self.assertEqual(len(nets), 1, "expected exactly one clock domain")
        self.assertEqual(nets[0].get_name(), "clk")
        self.assertTrue(nets[0].is_global_input_net())

        # And the domain reaches every flip-flop in the design: walkthrough 01 publishes 24
        # tennm_ff, all sharing clk.
        flip_flops = netlist.get_gates(lambda gate: gate.get_type().get_name() == "tennm_ff")
        self.assertEqual(len(flip_flops), 24)
        self.assertEqual(
            sorted(gate.get_id() for gate in tree.get_gates()),
            sorted(gate.get_id() for gate in flip_flops),
        )
        self.assertEqual(len(tree.get_all()), 25, "24 flip-flops plus the clock net")
        self.assertIs(tree.get_netlist(), netlist)

    def test_direct_clock_input_reaches_every_flip_flop_through_edges(self):
        """The traversal accessors see the same domain the accessors above report.

        ``clk`` arrives straight from a port, which is the case ``ClockTree::from_netlist`` used to
        insert as a vertex and then ``continue`` past without adding a single net -> flip-flop edge:
        every vertex was there, none of them was connected, and ``get_childs``/``get_parents``
        reported nothing for anything. ``get_subtree`` compounded it by reading igraph's forward
        vertex map as if 0 meant "not in the subgraph", which since igraph 1.0 marks absent vertices
        with -1, so it dropped the root and returned exactly the *excluded* vertices.
        """
        require(BLINKY, GATE_LIBRARY)
        from hal_plugins import clock_tree_extractor

        netlist = load(BLINKY)
        tree = clock_tree_extractor.ClockTree.from_netlist(netlist)
        clock = tree.get_nets()[0]
        flip_flop_ids = sorted(gate.get_id() for gate in tree.get_gates())

        # The pointer round trip works, so the neighbour lists below are real answers and not
        # rejected arguments.
        vertex = tree.get_vertex_from_ptr(clock)
        self.assertIsNotNone(vertex)
        self.assertEqual(tree.get_ptr_from_vertex(vertex).get_id(), clock.get_id())

        # The clock net is the root of the domain and every flip-flop hangs directly off it.
        self.assertEqual(sorted(gate.get_id() for gate in tree.get_childs(clock)), flip_flop_ids)
        self.assertEqual(tree.get_parents(clock), [])
        for gate in tree.get_gates():
            self.assertEqual(tree.get_childs(gate), [])
            self.assertEqual([net.get_id() for net in tree.get_parents(gate)], [clock.get_id()])

        # And the subtree rooted at the clock is the whole domain: the root plus its 24 children.
        subtree = tree.get_subtree(clock)
        self.assertIsNotNone(subtree, "get_subtree returned None")
        self.assertEqual([net.get_id() for net in subtree.get_nets()], [clock.get_id()])
        self.assertEqual(sorted(gate.get_id() for gate in subtree.get_gates()), flip_flop_ids)

        # A leaf reaches nothing, so its subtree is itself alone.
        leaf = tree.get_gates()[0]
        leaf_subtree = tree.get_subtree(leaf)
        self.assertIsNotNone(leaf_subtree)
        self.assertEqual([gate.get_id() for gate in leaf_subtree.get_gates()], [leaf.get_id()])
        self.assertEqual(leaf_subtree.get_nets(), [])


class BooleanInfluenceTest(unittest.TestCase):
    """``boolean_influence`` on the CRC-8 checker: the influence set is the function's support."""

    def setUp(self):
        require(CRC8, GATE_LIBRARY)
        self.netlist = load(CRC8)
        elaborate(self.netlist)
        # u3 is crc_r[0]; its d comes from the ALM that computes crc_r[7] ^ din. Both names are
        # from netlist/GROUND_TRUTH.md.
        self.register = gate_named(self.netlist, "u3")
        self.data_net = self.register.get_fan_in_net("d")
        self.driver = self.data_net.get_sources()[0].get_gate()

    def test_subcircuit_influence_is_the_support_of_the_next_state_function(self):
        from hal_plugins import boolean_influence

        influences = boolean_influence.get_boolean_influences_of_subcircuit_deterministic(
            [self.driver], self.data_net
        )
        self.assertIsNotNone(influences, "get_boolean_influences_of_subcircuit_deterministic failed")
        by_name = {net.get_name(): value for net, value in influences.items()}

        # crc_r[0]' = crc_r[7] ^ din, so the fan-in cone depends on exactly those two nets and on
        # nothing else -- every other ALM input is tied to ground.
        self.assertEqual(sorted(by_name), ["port_i0", "port_o0(7)"])
        # Every variable of an XOR flips the output for every assignment of the others.
        for name, value in by_name.items():
            self.assertAlmostEqual(value, 1.0, msg="influence of {} is {}".format(name, value))

    def test_function_level_influence_matches_the_variables_of_the_function(self):
        """The same claim one level down, on a BooleanFunction rather than on nets.

        This path never leaves z3, whereas the subcircuit variant above compiles and runs generated
        C, so the two together tell a compiler problem apart from a wrong answer.
        """
        from hal_plugins import boolean_influence

        function = self.driver.get_boolean_function("combout")
        support = set(function.get_variable_names())
        self.assertEqual(support, {"dataa", "datab"}, "the ALM is not the expected 2-input XOR")

        influences = boolean_influence.get_boolean_influence_deterministic(function)
        self.assertIsNotNone(influences, "get_boolean_influence_deterministic failed")
        self.assertEqual(set(influences), support)
        for name, value in influences.items():
            self.assertAlmostEqual(value, 1.0, msg="influence of {} is {}".format(name, value))


class SolveFsmTest(unittest.TestCase):
    """``solve_fsm`` on the traffic-light controller's 4-flip-flop state register."""

    STATE_REGISTER = ("g1", "g2", "g4", "g6")

    def _state_and_logic(self, netlist):
        state = [gate_named(netlist, name) for name in self.STATE_REGISTER]
        # Walkthrough 02 publishes that exactly these 4 of the 8 flip-flops are always enabled and
        # are the ones reaching a primary output; re-check it here so a regenerated netlist that
        # renames them fails loudly instead of solving the wrong register.
        for gate in state:
            self.assertEqual(gate.get_type().get_name(), "tennm_ff")
            self.assertEqual(gate.get_fan_in_net("ena").get_name(), "'1'")
        logic = netlist.get_gates(
            lambda gate: gate.get_type().has_property(hal_py.GateTypeProperty.combinational)
            and not gate.is_gnd_gate()
            and not gate.is_vcc_gate()
        )
        self.assertEqual(len(logic), 14, "walkthrough 02 publishes 14 tennm_lcell_comb")
        return state, logic

    def test_refuses_a_flip_flop_type_with_two_data_pins(self):
        """The vendor flip-flop cannot be solved as shipped, and that is worth pinning down.

        ``generate_state_bfs`` (``solve_fsm.cpp``) selects the data pin by asking the *gate type* for
        pins of type ``data`` and refuses unless there is exactly one. ``tennm_ff`` has two, ``d`` and
        the never-used ``asdata``, so every Agilex netlist is rejected with

            failed to create input - output mapping: currently not supporting flip-flops with
            multiple or no data inputs, but found 2 for gate type tennm_ff.

        The binding turns that into ``None`` after logging it. If the plugin learns to pick the pin
        by more than its type, this test fails and the next one stops needing its workaround.
        """
        require(TRAFFIC_FSM, GATE_LIBRARY)
        from hal_plugins import solve_fsm

        netlist = load(TRAFFIC_FSM)
        elaborate(netlist)
        state, logic = self._state_and_logic(netlist)

        self.assertIsNone(
            solve_fsm.solve_fsm_brute_force(netlist, state, logic),
            "solve_fsm now accepts tennm_ff; drop the retyped library from the next test",
        )

    def test_brute_force_returns_a_transition_graph(self):
        require(TRAFFIC_FSM, GATE_LIBRARY)
        from hal_plugins import solve_fsm

        netlist = load(TRAFFIC_FSM, self._library_with_asdata_as_control())
        elaborate(netlist)
        state, logic = self._state_and_logic(netlist)

        transitions = solve_fsm.solve_fsm_brute_force(netlist, state, logic)
        self.assertIsNotNone(transitions, "solve_fsm_brute_force returned None")

        # Shape only: brute force enumerates every assignment of the state register, so there is one
        # entry per state, each mapping successor states to the condition that takes them.
        self.assertIsInstance(transitions, dict)
        self.assertEqual(sorted(transitions), list(range(1 << len(state))))
        for origin, successors in transitions.items():
            self.assertIsInstance(successors, dict)
            self.assertTrue(successors, "state {} has no successor at all".format(origin))
            for successor, condition in successors.items():
                self.assertIn(successor, range(1 << len(state)))
                self.assertIsInstance(condition, hal_py.BooleanFunction)

        dot = solve_fsm.generate_dot_graph(state, transitions)
        self.assertIsNotNone(dot)
        self.assertIn("digraph", dot)

    def _library_with_asdata_as_control(self):
        """A copy of AGILEX_TENNM whose ``asdata`` pin is typed ``control`` instead of ``data``.

        The only thing standing between ``solve_fsm`` and this netlist is the pin *type* of a pin the
        design ties to a constant and never uses, so retyping it is enough to let the plugin run
        while leaving every function, every pin name and every connection alone. It is written to a
        temporary file under a different library name so that it cannot collide with the AGILEX
        library the other tests load.
        """
        import json

        document = json.loads(GATE_LIBRARY.read_text(encoding="utf-8"))
        retyped = 0
        for cell in document["cells"]:
            if cell["name"] != "tennm_ff":
                continue
            for group in cell["pin_groups"]:
                if group["name"] != "asdata":
                    continue
                group["type"] = "control"
                for pin in group["pins"]:
                    pin["type"] = "control"
                retyped += 1
        self.assertEqual(retyped, 1, "tennm_ff has no asdata pin group any more")

        name = "AGILEX_TENNM_ASDATA_AS_CONTROL"
        document["library"] = name
        # Not deleted afterwards: the netlist keeps a pointer into the gate library for as long as
        # it lives, and the temporary directory goes away with the process anyway.
        directory = tempfile.mkdtemp(prefix="hal_solve_fsm_")
        path = Path(directory) / (name + ".hgl")
        path.write_text(json.dumps(document), encoding="utf-8")
        return path


class NetlistSimulatorTest(unittest.TestCase):
    """``netlist_simulator``/``netlist_simulator_controller``: 32 cycles of the blinky counter."""

    #: Divisible by four, so that the edge (P/2) and the sample point (3P/4) are whole picoseconds.
    PERIOD_PS = 1000
    CYCLES = 32

    def test_counter_bits_toggle_at_half_the_rate_of_their_predecessor(self):
        require(BLINKY, GATE_LIBRARY)
        from hal_plugins import netlist_simulator_controller  # noqa: F401  (registers the types)

        netlist = load(BLINKY)
        elaborate(netlist)
        self._give_every_output_pin_a_function(netlist)

        plugin = hal_py.plugin_manager.get_plugin_instance("netlist_simulator_controller")
        self.assertIsNotNone(plugin)
        work_dir = tempfile.mkdtemp(prefix="hal_sim_")
        controller = plugin.create_simulator_controller("blinky", work_dir)
        self.assertIsNotNone(
            controller, "create_simulator_controller refused the working directory"
        )

        controller.add_gates(netlist.get_gates())
        self.assertIn("hal_simulator", controller.get_engine_names())
        engine = controller.create_simulation_engine("hal_simulator")
        self.assertIsNotNone(engine)

        period = self.PERIOD_PS
        total = (self.CYCLES + 2) * period
        # The fourth argument is not optional in practice: add_clock_period falls back to a 2000 ps
        # clock waveform ("duration ? duration : 2000"), so without it the clock simply stops after
        # two periods and every later sample repeats the last value instead of failing.
        controller.add_clock_period(net_named(netlist, "clk"), period, True, total)

        value = hal_py.BooleanFunction.Value
        reset = net_named(netlist, "rst_n")
        # Cycle 0 is spent in asynchronous clear, so the counter is 0 when cycle 1 starts.
        controller.set_input(reset, value.ZERO)
        controller.simulate(period)
        controller.set_input(reset, value.ONE)
        controller.simulate((self.CYCLES + 1) * period)

        self.assertTrue(controller.run_simulation(), "run_simulation refused to start")
        self.assertEqual(self._wait_for(engine), ENGINE_DONE, "the hal_simulator engine failed")
        self.assertTrue(controller.get_results(), "get_results could not read the run back")

        def samples(net_name):
            waveform = controller.get_waveform_by_net(net_named(netlist, net_name))
            self.assertIsNotNone(waveform, "no waveform recorded for {}".format(net_name))
            return [
                int(waveform.get_value_at(cycle * period + (3 * period) // 4))
                for cycle in range(self.CYCLES + 1)
            ]

        # The counter holds the cycle index, so bit i is (cycle >> i) & 1: bit 0 toggles every
        # cycle, bit 1 every second one. Walkthrough 01 publishes the design as a 24-bit ripple
        # counter clocked by clk with an asynchronous clear on rst_n.
        bit0, bit1 = samples("count(0)"), samples("count(1)")
        self.assertEqual(bit0, [cycle & 1 for cycle in range(self.CYCLES + 1)])
        self.assertEqual(bit1, [(cycle >> 1) & 1 for cycle in range(self.CYCLES + 1)])
        self.assertEqual(sum(a != b for a, b in zip(bit0, bit0[1:])), self.CYCLES)
        self.assertEqual(sum(a != b for a, b in zip(bit1, bit1[1:])), self.CYCLES // 2)

    def _wait_for(self, engine, timeout_s=300.0):
        """``run_simulation`` starts a thread and returns; polling is all the bindings offer."""
        deadline = time.time() + timeout_s
        while True:
            state = engine.get_state()
            if state in (ENGINE_DONE, ENGINE_FAILED):
                # The thread sets Done inside finalize() and only then reports back to the
                # controller, so reading the results immediately races that hand-off.
                time.sleep(0.05)
                return state
            if time.time() > deadline:
                self.fail("engine still in state {} after {:.0f}s".format(state, timeout_s))
            time.sleep(0.02)

    def _give_every_output_pin_a_function(self, netlist):
        """Work around an abort, not a wrong answer.

        ``SimulationGateCombinational``'s constructor does ``functions.at(pin->get_name())`` for
        *every* output pin of the gate type. ``tennm_lcell_comb`` has four (``combout``, ``sumout``,
        ``cout``, ``shareout``) and any one ALM uses at most two, so the lookup throws
        ``std::out_of_range`` out of the engine thread and terminates the process -- there is no
        exception for a test to catch and no way to skip afterwards.

        The unused pins are given a constant and a net of their own. A net is needed as well as a
        function because the same constructor stores ``gate->get_fan_out_net(pin)`` as the key of the
        result map, and a null key is dereferenced when the results are read back, which segfaults.
        Neither addition can change the answer: the stub nets have no destinations.
        """
        constant = hal_py.BooleanFunction.Const(0, 1)
        for gate in netlist.get_gates():
            if not gate.get_type().has_property(hal_py.GateTypeProperty.combinational):
                continue
            defined = set(gate.get_boolean_functions())
            for pin in gate.get_type().get_output_pins():
                name = pin.get_name()
                if name in defined:
                    continue
                self.assertIsNone(
                    gate.get_fan_out_net(pin),
                    "{}.{} drives a net but has no Boolean function".format(gate.get_name(), name),
                )
                stub = netlist.create_net("__unused_{}_{}".format(gate.get_id(), name))
                stub.add_source(gate, name)
                gate.add_boolean_function(name, constant)


class SequentialSymbolicExecutionTest(unittest.TestCase):
    """``sequential_symbolic_execution``: one bounded step over the CRC-8 register."""

    def test_one_bounded_step_yields_a_word_level_expression(self):
        require(CRC8, GATE_LIBRARY)
        from hal_plugins import sequential_symbolic_execution as sse

        plugin = hal_py.plugin_manager.get_plugin_instance("sequential_symbolic_execution")
        self.assertIsNotNone(plugin)
        self.assertEqual(plugin.get_name(), "sequential_symbolic_execution")

        netlist = load(CRC8)
        elaborate(netlist)
        top = netlist.get_top_module()
        word = [group for group in top.get_pin_groups() if group.get_name() == "port_o0"]
        self.assertEqual(len(word), 1)
        self.assertEqual(word[0].size(), 8, "port_o0 is the 8-bit CRC register")

        # The map is indexed by gate ID, so it has to be long enough for the largest one; every gate
        # is in the subgraph. Time index 1 is the bounded step: at index 0 a sequential source is a
        # free variable, at index 1 the solver unrolls one clock edge through it.
        time_index = 1
        in_subgraph = [True] * (max(gate.get_id() for gate in netlist.get_gates()) + 1)
        known_inputs = [{} for _ in range(time_index + 1)]

        # The binding returns None by design: it prints the expressions it computed to stdout rather
        # than handing back the z3 terms. Capturing the file descriptor is therefore the only way to
        # see whether it produced a result or logged an error and gave up.
        printed = self._capture_stdout(
            lambda: sse.get_pg_word_values_at_z3(
                [(top, word[0])], [time_index], in_subgraph, known_inputs, {}, False
            )
        )

        self.assertIn("T[{}]".format(time_index), printed, "no word value was printed")
        self.assertIn("port_o0", printed)
        expression = printed.split("port_o0")[-1]
        self.assertGreater(len(expression), 100, "the printed expression is suspiciously short")
        # One step back from the register, the next state is a function of the inputs at time 0.
        self.assertIn("_0", expression, "the expression names nothing from the previous time step")

    def _capture_stdout(self, call):
        """Redirect file descriptor 1, because the output comes from C++ and not from sys.stdout."""
        sys.stdout.flush()
        saved = os.dup(1)
        handle, path = tempfile.mkstemp(prefix="hal_sse_", suffix=".txt")
        try:
            os.dup2(handle, 1)
            try:
                self.assertIsNone(call(), "the binding started returning something")
            finally:
                sys.stdout.flush()
                os.dup2(saved, 1)
            with open(path, "r", errors="replace") as captured:
                return captured.read()
        finally:
            os.close(handle)
            os.close(saved)
            os.unlink(path)


def main():
    global hal_py

    import hal_py as module

    hal_py = module
    # Nothing below can parse a netlist or read a gate library until the plugins that provide the
    # parsers are registered, and every "from hal_plugins import ..." needs its library loaded.
    hal_py.plugin_manager.load_all_plugins()

    unittest.main(argv=[sys.argv[0]] + sys.argv[1:], verbosity=2)


if __name__ == "__main__":
    main()
