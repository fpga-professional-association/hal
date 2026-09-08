"""Unit tests for hal_semantic_diff. No built HAL required.

The point of these tests is not coverage for its own sake: it is that the
*classification* -- which outcome becomes a proof, which becomes a
counterexample, which must stay inconclusive -- is exercised end to end on a
machine where HAL cannot be built.  So the netlists are stub objects shaped
like the ``hal_py`` bindings, and the solver is a brute-force engine over the
same interface :mod:`hal_semantic_diff.halbridge` implements.

Two of the stub designs are transcriptions of the shipped fixtures
(``fixtures/decoder_base.v`` and ``fixtures/decoder_changed.v``), so the
expectations here and the ground truth in ``fixtures/ground_truth.json`` are
the same expectations.

Run::

    python -m unittest discover -s tools/hal_semantic_diff -t tools -p "test_*.py"
"""

import itertools
import json
import os
import shutil
import tempfile
import unittest

from hal_findings import model as findings_model
from hal_findings import serialize as findings_serialize
from hal_findings import validate as findings_validate

from hal_semantic_diff import cli, compare, cones, correspondence, diagrams, findings, report


# ---------------------------------------------------------------------------
# stub netlist objects, shaped like the hal_py bindings
# ---------------------------------------------------------------------------


GATE_FUNCTIONS = {
    "INV": lambda p: 1 - p["I"],
    "BUF": lambda p: p["I"],
    "AND2": lambda p: p["I0"] & p["I1"],
    "AND3": lambda p: p["I0"] & p["I1"] & p["I2"],
    "OR2": lambda p: p["I0"] | p["I1"],
    "OR4": lambda p: p["I0"] | p["I1"] | p["I2"] | p["I3"],
    "XOR": lambda p: p["I0"] ^ p["I1"],
    "MUX": lambda p: (p["I1"] if p["S"] else p["I0"]),
    "VCC": lambda p: 1,
    "GND": lambda p: 0,
}

GATE_PINS = {
    "INV": (["I"], ["O"]),
    "BUF": (["I"], ["O"]),
    "AND2": (["I0", "I1"], ["O"]),
    "AND3": (["I0", "I1", "I2"], ["O"]),
    "OR2": (["I0", "I1"], ["O"]),
    "OR4": (["I0", "I1", "I2", "I3"], ["O"]),
    "XOR": (["I0", "I1"], ["O"]),
    "MUX": (["I0", "I1", "S"], ["O"]),
    "VCC": ([], ["O"]),
    "GND": ([], ["O"]),
    "FFR": (["C", "CE", "D", "R"], ["Q"]),
    "FFS": (["C", "CE", "D", "S"], ["Q"]),
    "RAM": (["A", "D"], ["Q"]),
}

SEQUENTIAL_TYPES = {"FFR", "FFS"}
#: Neither combinational nor sequential -- the coverage gap the model reports.
OPAQUE_TYPES = {"RAM"}


class StubPin(object):
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class StubGateType(object):
    def __init__(self, name):
        self._name = name
        self.input_pins, self.output_pins = GATE_PINS[name]

    def get_name(self):
        return self._name

    def get_property_list(self):
        if self._name in SEQUENTIAL_TYPES:
            return ["ff", "sequential"]
        if self._name in OPAQUE_TYPES:
            return ["ram"]
        return ["combinational"]

    def get_input_pin_names(self):
        return list(self.input_pins)

    def get_input_pins(self):
        return [StubPin(name) for name in self.input_pins]


class StubEndpoint(object):
    def __init__(self, gate, net, pin_name):
        self._gate = gate
        self._net = net
        self._pin = StubPin(pin_name)

    def get_gate(self):
        return self._gate

    def get_net(self):
        return self._net

    def get_pin(self):
        return self._pin


class StubNet(object):
    def __init__(self, net_id, name):
        self._id = net_id
        self._name = name
        self.sources = []
        self.destinations = []
        self.global_input = False
        self.global_output = False

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_sources(self):
        return list(self.sources)

    def get_destinations(self):
        return list(self.destinations)

    def is_global_input_net(self):
        return self.global_input

    def is_global_output_net(self):
        return self.global_output


class StubModule(object):
    def __init__(self, module_id, name):
        self._id = module_id
        self._name = name
        self.input_pins = {}
        self.output_pins = {}

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_output_pin_names(self):
        return sorted(self.output_pins)

    def get_input_pin_names(self):
        return sorted(self.input_pins)

    def get_pin_by_name(self, name):
        net = self.output_pins.get(name) or self.input_pins.get(name)
        return _ModulePin(name, net) if net is not None else None

    def get_pin_by_net(self, net):
        for name, candidate in list(self.input_pins.items()) + list(self.output_pins.items()):
            if candidate is net:
                return _ModulePin(name, candidate)
        return None


class _ModulePin(object):
    def __init__(self, name, net):
        self._name = name
        self._net = net

    def get_name(self):
        return self._name

    def get_net(self):
        return self._net


class StubGate(object):
    def __init__(self, gate_id, name, type_name, module):
        self._id = gate_id
        self._name = name
        self._type = StubGateType(type_name)
        self._module = module
        self.fan_in = {}
        self.fan_out = {}

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_type(self):
        return self._type

    def get_module(self):
        return self._module

    def get_fan_in_endpoints(self):
        return [StubEndpoint(self, net, pin) for pin, net in sorted(self.fan_in.items())]

    def get_fan_out_endpoints(self):
        return [StubEndpoint(self, net, pin) for pin, net in sorted(self.fan_out.items())]

    def get_fan_in_net(self, pin):
        return self.fan_in.get(pin)


class StubNetlist(object):
    def __init__(self, name):
        self._name = name
        self._gates = []
        self._nets = {}
        self._top = StubModule(1, "top_module")
        self._next_id = 1

    # -- construction ---------------------------------------------------------

    def _next(self):
        value = self._next_id
        self._next_id += 1
        return value

    def net(self, name):
        if name not in self._nets:
            self._nets[name] = StubNet(self._next(), name)
        return self._nets[name]

    def add_input(self, name):
        net = self.net(name)
        net.global_input = True
        self._top.input_pins[name] = net
        return net

    def add_output(self, pin_name, net_name):
        net = self.net(net_name)
        net.global_output = True
        self._top.output_pins[pin_name] = net
        return net

    def add_gate(self, type_name, instance_name, **connections):
        gate = StubGate(self._next(), instance_name, type_name, self._top)
        inputs, outputs = GATE_PINS[type_name]
        for pin, net_name in connections.items():
            net = self.net(net_name)
            if pin in inputs:
                gate.fan_in[pin] = net
                net.destinations.append(StubEndpoint(gate, net, pin))
            elif pin in outputs:
                gate.fan_out[pin] = net
                net.sources.append(StubEndpoint(gate, net, pin))
            else:
                raise AssertionError("{} has no pin {}".format(type_name, pin))
        self._gates.append(gate)
        return gate

    # -- bindings-shaped accessors -------------------------------------------

    def get_id(self):
        return 1

    def get_design_name(self):
        return self._name

    def get_device_name(self):
        return "stub"

    def get_gates(self):
        return list(self._gates)

    def get_nets(self):
        return list(self._nets.values())

    def get_top_module(self):
        return self._top

    def get_input_filename(self):
        return ""

    def get_gate_library(self):
        return None


# ---------------------------------------------------------------------------
# the fixture designs, as stubs
# ---------------------------------------------------------------------------


def _boundary_scaffold(netlist):
    for name in ("A0", "A1", "EN", "CLK", "RST"):
        netlist.add_input(name)
    netlist.add_gate("VCC", "vcc_inst", O="one")
    netlist.add_gate("INV", "inv_a0", I="A0", O="n_a0")
    netlist.add_gate("INV", "inv_a1", I="A1", O="n_a1")


def _registers(netlist, data_nets, register_names=None):
    names = register_names or ["sel_reg_0", "sel_reg_1", "sel_reg_2", "sel_reg_3"]
    for index, (name, data) in enumerate(zip(names, data_nets)):
        netlist.add_gate("FFR", name, C="CLK", CE="one", D=data, R="RST", Q="SEL{}".format(index))
        netlist.add_output("SEL{}".format(index), "SEL{}".format(index))


def build_base():
    """``fixtures/decoder_base.v`` as stub objects."""
    netlist = StubNetlist("base")
    _boundary_scaffold(netlist)
    netlist.add_gate("AND2", "dec0", I0="n_a1", I1="n_a0", O="s0")
    netlist.add_gate("AND2", "dec1", I0="n_a1", I1="A0", O="s1")
    netlist.add_gate("AND2", "dec2", I0="A1", I1="n_a0", O="s2")
    netlist.add_gate("AND2", "dec3", I0="A1", I1="A0", O="s3")
    for index in range(4):
        netlist.add_gate(
            "AND2", "en{}".format(index), I0="s{}".format(index), I1="EN", O="g{}".format(index)
        )
    netlist.add_gate("OR4", "hit_or", I0="g0", I1="g1", I2="g2", I3="g3", O="HIT")
    netlist.add_output("HIT", "HIT")
    _registers(netlist, ["g0", "g1", "g2", "g3"])
    return netlist


def _rewrite_body(netlist, decode):
    """The De Morgan'd decode of the rewrite/changed fixtures."""
    netlist.add_gate("GND", "gnd_inst", O="zero")
    for index, (left, right) in enumerate(decode):
        netlist.add_gate("OR2", "nor{}_or".format(index), I0=left, I1=right, O="t{}".format(index))
        netlist.add_gate("INV", "nor{}_inv".format(index), I="t{}".format(index), O="s{}".format(index))
    for index in range(4):
        netlist.add_gate(
            "MUX", "en{}".format(index), I0="zero", I1="s{}".format(index), S="EN",
            O="g{}".format(index),
        )
    netlist.add_gate("OR2", "hit_01", I0="g0", I1="g1", O="h01")
    netlist.add_gate("OR2", "hit_23", I0="g2", I1="g3", O="h23")
    netlist.add_gate("OR2", "hit_or", I0="h01", I1="h23", O="HIT")
    netlist.add_output("HIT", "HIT")


def build_rewrite(register_names=None):
    """``fixtures/decoder_rewrite.v`` (or ``decoder_renamed.v``) as stub objects."""
    netlist = StubNetlist("rewrite")
    _boundary_scaffold(netlist)
    _rewrite_body(
        netlist,
        [("A1", "A0"), ("A1", "n_a0"), ("n_a1", "A0"), ("n_a1", "n_a0")],
    )
    _registers(netlist, ["g0", "g1", "g2", "g3"], register_names)
    return netlist


def build_changed():
    """``fixtures/decoder_changed.v``: the SEL2/SEL3 decode terms are swapped."""
    netlist = StubNetlist("changed")
    _boundary_scaffold(netlist)
    _rewrite_body(
        netlist,
        [("A1", "A0"), ("A1", "n_a0"), ("n_a1", "n_a0"), ("n_a1", "A0")],
    )
    _registers(netlist, ["g0", "g1", "g2", "g3"])
    return netlist


def build_retimed():
    """``fixtures/decoder_retimed.v``: the enable gating moved past the registers."""
    netlist = StubNetlist("retimed")
    _boundary_scaffold(netlist)
    netlist.add_gate("AND2", "dec0", I0="n_a1", I1="n_a0", O="s0")
    netlist.add_gate("AND2", "dec1", I0="n_a1", I1="A0", O="s1")
    netlist.add_gate("AND2", "dec2", I0="A1", I1="n_a0", O="s2")
    netlist.add_gate("AND2", "dec3", I0="A1", I1="A0", O="s3")
    for index in range(4):
        netlist.add_gate(
            "FFR", "sel_reg_{}".format(index), C="CLK", CE="one", D="s{}".format(index),
            R="RST", Q="q{}".format(index),
        )
        netlist.add_gate(
            "AND2", "en{}".format(index), I0="q{}".format(index), I1="EN",
            O="SEL{}".format(index),
        )
        netlist.add_output("SEL{}".format(index), "SEL{}".format(index))
    netlist.add_gate("OR4", "hit_or", I0="SEL0", I1="SEL1", I2="SEL2", I3="SEL3", O="HIT")
    netlist.add_output("HIT", "HIT")
    return netlist


# ---------------------------------------------------------------------------
# a brute-force engine with the interface halbridge.SmtEngine implements
# ---------------------------------------------------------------------------


class PyFunction(object):
    def __init__(self, cone, variables, text):
        self.cone = cone
        self.variables = set(variables)
        self.text = text


class BruteForceEngine(object):
    """Evaluates cones exhaustively. Same interface as ``halbridge.SmtEngine``."""

    def __init__(self, force=None, drop_model=False, wrong_model=False):
        #: ``"unknown"``/``"error"`` to make every query answer that way.
        self.force = force
        self.drop_model = drop_model
        self.wrong_model = wrong_model
        self.queries = 0

    def cone_function(self, netlist, cone):
        boundaries = {boundary.net_id: boundary for boundary in cone.boundaries}
        return PyFunction(
            cone,
            [boundary.name for boundary in cone.boundaries],
            self._text(cone.output_net, boundaries),
        )

    def function_variables(self, function):
        return set(function.variables)

    def function_text(self, function):
        return function.text

    def _text(self, net, boundaries):
        boundary = boundaries.get(net.get_id())
        if boundary is not None:
            return boundary.name
        source = net.get_sources()[0]
        gate = source.get_gate()
        arguments = ", ".join(
            "{}={}".format(endpoint.get_pin().get_name(), self._text(endpoint.get_net(), boundaries))
            for endpoint in gate.get_fan_in_endpoints()
        )
        return "{}({})".format(gate.get_type().get_name(), arguments)

    def _value(self, net, boundaries, assignment, memo):
        net_id = net.get_id()
        if net_id in memo:
            return memo[net_id]
        boundary = boundaries.get(net_id)
        if boundary is not None:
            value = int(assignment[boundary.name])
        else:
            source = net.get_sources()[0]
            gate = source.get_gate()
            inputs = {
                endpoint.get_pin().get_name(): self._value(
                    endpoint.get_net(), boundaries, assignment, memo
                )
                for endpoint in gate.get_fan_in_endpoints()
            }
            value = GATE_FUNCTIONS[gate.get_type().get_name()](inputs)
        memo[net_id] = value
        return value

    def evaluate(self, function, assignment):
        boundaries = {b.net_id: b for b in function.cone.boundaries}
        try:
            return str(self._value(function.cone.output_net, boundaries, assignment, {}))
        except KeyError:
            return None

    def solve_difference(self, function_a, function_b, timeout_s):
        self.queries += 1
        if self.force == "unknown":
            return compare.SolveResult("unknown", timed_out=False, wall_time_s=0.0)
        if self.force == "timeout":
            return compare.SolveResult("unknown", timed_out=True, wall_time_s=float(timeout_s))
        if self.force == "error":
            return compare.SolveResult("error", message="stub failure", wall_time_s=0.0)

        variables = sorted(function_a.variables | function_b.variables)
        for combination in itertools.product("01", repeat=len(variables)):
            assignment = dict(zip(variables, combination))
            if self.evaluate(function_a, assignment) != self.evaluate(function_b, assignment):
                if self.drop_model:
                    return compare.SolveResult("sat", model=None, wall_time_s=0.0)
                if self.wrong_model:
                    return compare.SolveResult(
                        "sat", model={name: "0" for name in variables}, wall_time_s=0.0
                    )
                return compare.SolveResult("sat", model=assignment, wall_time_s=0.0)
        return compare.SolveResult("unsat", wall_time_s=0.0)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def run_comparison(netlist_a, netlist_b, mapping=None, engine=None, options=None):
    mapping = mapping or correspondence.identity(labels=("a", "b"))
    engine = engine or BruteForceEngine()
    options = options or compare.Options(solver_timeout_s=5)
    points, problems, coverage = correspondence.build_observation_points(
        netlist_a, netlist_b, mapping
    )
    unmatched = (
        frozenset(coverage["unmatched_sequential_a"]),
        frozenset(coverage["unmatched_sequential_b"]),
    )
    outcomes = compare.compare_points(
        engine, netlist_a, netlist_b, points, mapping, options, unmatched=unmatched
    )
    return outcomes, problems, coverage, mapping, options


def statuses(outcomes):
    return {outcome.point.key: outcome.status for outcome in outcomes}


# ---------------------------------------------------------------------------


class ConeTest(unittest.TestCase):
    def setUp(self):
        self.base = build_base()

    def _net(self, netlist, name):
        return netlist.net(name)

    def test_cone_stops_at_sequential_and_global_inputs(self):
        cone = cones.extract_cone(self.base, self._net(self.base, "g2"))
        self.assertEqual(
            sorted(gate.get_name() for gate in cone.gates), ["dec2", "en2", "inv_a0"]
        )
        self.assertEqual(
            cone.boundary_names, ["GLOBAL_IN_A0", "GLOBAL_IN_A1", "GLOBAL_IN_EN"]
        )
        self.assertEqual(
            cone.boundary_kinds()[cones.BOUNDARY_GLOBAL_INPUT],
            ["GLOBAL_IN_A0", "GLOBAL_IN_A1", "GLOBAL_IN_EN"],
        )

    def test_registered_output_cone_is_a_single_state_variable(self):
        cone = cones.extract_cone(self.base, self._net(self.base, "SEL1"))
        self.assertEqual(cone.gates, [])
        self.assertEqual(cone.boundary_names, ["sel_reg_1_Q"])
        self.assertEqual(cone.boundaries[0].kind, cones.BOUNDARY_SEQUENTIAL)

    def test_signature_is_structural_not_nominal(self):
        other = build_base()
        left = cones.extract_cone(self.base, self._net(self.base, "g0"))
        right = cones.extract_cone(other, other.net("g0"))
        self.assertEqual(left.signature, right.signature)

        rewritten = build_rewrite()
        different = cones.extract_cone(rewritten, rewritten.net("g0"))
        self.assertNotEqual(left.signature, different.signature)

    def test_multi_driven_net_is_refused(self):
        netlist = StubNetlist("multi")
        netlist.add_input("A0")
        netlist.add_gate("BUF", "b0", I="A0", O="n")
        netlist.add_gate("BUF", "b1", I="A0", O="n")
        netlist.add_gate("INV", "i0", I="n", O="out")
        with self.assertRaises(cones.ConeError):
            cones.extract_cone(netlist, netlist.net("out"))

    def test_combinational_loop_is_refused_not_hung(self):
        netlist = StubNetlist("loop")
        netlist.add_gate("INV", "i0", I="b", O="a")
        netlist.add_gate("INV", "i1", I="a", O="b")
        with self.assertRaises(cones.ConeError):
            cones.extract_cone(netlist, netlist.net("a"))

    def test_gate_budget_is_enforced(self):
        with self.assertRaises(cones.ConeError):
            cones.extract_cone(self.base, self._net(self.base, "HIT"), max_gates=3)

    def test_renaming_rewrites_the_boundary_into_the_other_namespace(self):
        cone = cones.extract_cone(
            self.base,
            self._net(self.base, "SEL3"),
            rename=lambda name: "status_reg_3_Q" if name == "sel_reg_3_Q" else name,
        )
        self.assertEqual(cone.boundary_names, ["status_reg_3_Q"])
        self.assertEqual(cone.boundaries[0].raw_name, "sel_reg_3_Q")
        self.assertEqual(cone.boundaries[0].as_dict()["renamed_from"], "sel_reg_3_Q")


class CorrespondenceFileTest(unittest.TestCase):
    def _write(self, payload):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(payload, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_shipped_fixture_files_load(self):
        for name in sorted(os.listdir(FIXTURES)):
            if not name.startswith("correspondence_"):
                continue
            mapping = correspondence.load(os.path.join(FIXTURES, name))
            self.assertTrue(mapping.description)
            self.assertEqual(len(mapping.sequential_gates), 4)

    def test_renaming_fixture_is_not_the_identity(self):
        mapping = correspondence.load(
            os.path.join(FIXTURES, "correspondence_renamed_mapped.json")
        )
        self.assertFalse(mapping.is_identity)
        self.assertEqual(
            mapping.renamings(),
            [{"field": "sequential_gates", "a": "sel_reg_3", "b": "status_reg_3"}],
        )
        self.assertEqual(mapping.rename_boundary("sel_reg_3_Q"), "status_reg_3_Q")
        self.assertEqual(mapping.rename_boundary("sel_reg_0_Q"), "sel_reg_0_Q")

    def test_unknown_key_is_rejected(self):
        path = self._write({"version": "1.0", "registers": {}})
        with self.assertRaises(correspondence.CorrespondenceError):
            correspondence.load(path)

    def test_unsupported_version_is_rejected(self):
        path = self._write({"version": "9.9"})
        with self.assertRaises(correspondence.CorrespondenceError):
            correspondence.load(path)

    def test_non_injective_mapping_is_rejected(self):
        path = self._write({"sequential_gates": {"a": "x", "b": "x"}})
        with self.assertRaises(correspondence.CorrespondenceError):
            correspondence.load(path)

    def test_top_input_rename_rewrites_the_global_variable(self):
        mapping = correspondence.Correspondence(top_inputs={"A0": "ADDR0", "A1": "A1"})
        self.assertEqual(mapping.rename_boundary("GLOBAL_IN_A0"), "GLOBAL_IN_ADDR0")
        self.assertEqual(mapping.rename_boundary("GLOBAL_IN_A1"), "GLOBAL_IN_A1")


class ObservationPointTest(unittest.TestCase):
    def test_point_count_matches_the_documented_model(self):
        points, problems, coverage = correspondence.build_observation_points(
            build_base(), build_rewrite(), correspondence.identity()
        )
        self.assertEqual(problems, [])
        self.assertEqual(len(points), 21)
        self.assertEqual(coverage["observation_points_by_kind"]["top_output"], 5)
        self.assertEqual(coverage["observation_points_by_kind"]["sequential_input"], 16)
        self.assertIn("sequential:sel_reg_2.D", {point.key for point in points})

    def test_a_renamed_register_is_a_reported_gap_not_a_silent_drop(self):
        points, problems, coverage = correspondence.build_observation_points(
            build_base(),
            build_rewrite(["sel_reg_0", "sel_reg_1", "sel_reg_2", "status_reg_3"]),
            correspondence.identity(),
        )
        self.assertEqual(len(points), 17)
        kinds = sorted(problem.kind for problem in problems)
        self.assertEqual(kinds, ["sequential_correspondence", "sequential_correspondence"])
        self.assertEqual(coverage["unmatched_sequential_a"], ["sel_reg_3"])
        self.assertEqual(coverage["unmatched_sequential_b"], ["status_reg_3"])

    def test_a_gate_type_mismatch_is_a_gap(self):
        netlist_b = build_rewrite()
        netlist_b.get_gates()[-1]._type = StubGateType("FFS")
        _, problems, coverage = correspondence.build_observation_points(
            build_base(), netlist_b, correspondence.identity()
        )
        self.assertEqual(
            [problem.kind for problem in problems], ["sequential_type_mismatch"]
        )
        self.assertEqual(coverage["unmatched_sequential_a"], ["sel_reg_3"])

    def test_an_input_only_in_one_build_is_reported(self):
        netlist_b = build_rewrite()
        netlist_b.add_input("SCAN_EN")
        _, problems, _ = correspondence.build_observation_points(
            build_base(), netlist_b, correspondence.identity()
        )
        self.assertEqual([problem.kind for problem in problems], ["input_correspondence"])
        self.assertIn("SCAN_EN", problems[0].message)

    def test_a_mapping_naming_a_missing_input_is_reported(self):
        mapping = correspondence.Correspondence(
            top_inputs={"A0": "A0", "A1": "A1", "EN": "EN", "CLK": "CLK", "RST": "RESET"}
        )
        _, problems, _ = correspondence.build_observation_points(
            build_base(), build_rewrite(), mapping
        )
        kinds = [problem.kind for problem in problems]
        self.assertEqual(kinds, ["input_correspondence", "input_correspondence"])
        self.assertIn("RESET", problems[0].message)

    def test_an_output_only_in_one_build_is_reported(self):
        netlist_b = build_rewrite()
        netlist_b.add_output("SPARE", "HIT")
        _, problems, _ = correspondence.build_observation_points(
            build_base(), netlist_b, correspondence.identity()
        )
        self.assertEqual([problem.kind for problem in problems], ["output_correspondence"])
        self.assertIn("SPARE", problems[0].message)


class ComparisonTest(unittest.TestCase):
    def test_equivalent_rewrite_is_proven_everywhere(self):
        outcomes, problems, _, _, _ = run_comparison(build_base(), build_rewrite())
        self.assertEqual(problems, [])
        self.assertEqual(len(outcomes), 21)
        self.assertEqual(
            {outcome.status for outcome in outcomes}, {compare.EQUIVALENT}
        )

    def test_changed_decoder_is_localized_to_two_points(self):
        outcomes, _, _, _, _ = run_comparison(build_base(), build_changed())
        by_key = statuses(outcomes)
        differing = sorted(k for k, v in by_key.items() if v == compare.DIFFERENT)
        self.assertEqual(
            differing, ["sequential:sel_reg_2.D", "sequential:sel_reg_3.D"]
        )
        # The OR of all four decode terms is unchanged, so HIT must be proven.
        self.assertEqual(by_key["output:HIT"], compare.EQUIVALENT)
        self.assertEqual(by_key["sequential:sel_reg_0.D"], compare.EQUIVALENT)

    def test_the_counterexample_is_kept_and_replayed(self):
        outcomes, _, _, _, _ = run_comparison(build_base(), build_changed())
        outcome = next(o for o in outcomes if o.point.key == "sequential:sel_reg_2.D")
        self.assertTrue(outcome.witness_available)
        witness = {entry["signal"]: entry["value"] for entry in outcome.witness}
        # The difference function is exactly EN & A1, so every witness sets both.
        self.assertEqual(witness["GLOBAL_IN_EN"], "1")
        self.assertEqual(witness["GLOBAL_IN_A1"], "1")
        self.assertTrue(outcome.replay["confirmed"])
        self.assertNotEqual(outcome.replay["value_a"], outcome.replay["value_b"])

    def test_changed_cones_are_localized_to_the_swapped_gates(self):
        outcomes, _, _, _, _ = run_comparison(build_base(), build_changed())
        outcome = next(o for o in outcomes if o.point.key == "sequential:sel_reg_2.D")
        only_a, only_b = outcome.changed_gates()
        self.assertIn("dec2", [gate.get_name() for gate in only_a])
        self.assertIn("nor2_or", [gate.get_name() for gate in only_b])

    def test_retiming_is_reported_as_a_difference_not_accepted(self):
        outcomes, _, _, _, _ = run_comparison(build_base(), build_retimed())
        by_key = statuses(outcomes)
        differing = sorted(k for k, v in by_key.items() if v == compare.DIFFERENT)
        self.assertEqual(
            differing,
            [
                "output:HIT",
                "output:SEL0",
                "output:SEL1",
                "output:SEL2",
                "output:SEL3",
                "sequential:sel_reg_0.D",
                "sequential:sel_reg_1.D",
                "sequential:sel_reg_2.D",
                "sequential:sel_reg_3.D",
            ],
        )
        self.assertEqual(by_key["sequential:sel_reg_0.C"], compare.EQUIVALENT)

    def test_unmapped_state_is_unsupported_not_different(self):
        outcomes, problems, _, _, _ = run_comparison(
            build_base(),
            build_rewrite(["sel_reg_0", "sel_reg_1", "sel_reg_2", "status_reg_3"]),
        )
        by_key = statuses(outcomes)
        self.assertEqual(by_key["output:SEL3"], compare.UNSUPPORTED)
        self.assertNotIn(compare.DIFFERENT, set(by_key.values()))
        self.assertEqual(len(problems), 2)

    def test_declaring_the_rename_makes_the_pair_provable(self):
        mapping = correspondence.Correspondence(
            sequential_gates={
                "sel_reg_0": "sel_reg_0",
                "sel_reg_1": "sel_reg_1",
                "sel_reg_2": "sel_reg_2",
                "sel_reg_3": "status_reg_3",
            }
        )
        outcomes, problems, _, _, _ = run_comparison(
            build_base(),
            build_rewrite(["sel_reg_0", "sel_reg_1", "sel_reg_2", "status_reg_3"]),
            mapping=mapping,
        )
        self.assertEqual(problems, [])
        self.assertEqual(len(outcomes), 21)
        self.assertEqual({outcome.status for outcome in outcomes}, {compare.EQUIVALENT})

    def test_solver_unknown_never_becomes_equivalence(self):
        outcomes, _, _, _, _ = run_comparison(
            build_base(), build_rewrite(), engine=BruteForceEngine(force="unknown")
        )
        self.assertEqual({outcome.status for outcome in outcomes}, {compare.UNKNOWN})

    def test_solver_timeout_is_a_timeout(self):
        outcomes, _, _, _, _ = run_comparison(
            build_base(), build_rewrite(), engine=BruteForceEngine(force="timeout")
        )
        self.assertEqual({outcome.status for outcome in outcomes}, {compare.TIMEOUT})
        self.assertTrue(all(outcome.timed_out for outcome in outcomes))

    def test_solver_error_is_an_error(self):
        outcomes, _, _, _, _ = run_comparison(
            build_base(), build_rewrite(), engine=BruteForceEngine(force="error")
        )
        self.assertEqual({outcome.status for outcome in outcomes}, {compare.ERROR})

    def test_sat_without_a_model_is_a_difference_without_a_witness(self):
        outcomes, _, _, _, _ = run_comparison(
            build_base(), build_changed(), engine=BruteForceEngine(drop_model=True)
        )
        outcome = next(o for o in outcomes if o.point.key == "sequential:sel_reg_2.D")
        self.assertEqual(outcome.status, compare.DIFFERENT)
        self.assertFalse(outcome.witness_available)
        self.assertEqual(outcome.witness, [])

    def test_a_model_that_does_not_replay_is_downgraded_to_unknown(self):
        outcomes, _, _, _, _ = run_comparison(
            build_base(), build_changed(), engine=BruteForceEngine(wrong_model=True)
        )
        outcome = next(o for o in outcomes if o.point.key == "sequential:sel_reg_2.D")
        self.assertEqual(outcome.status, compare.UNKNOWN)
        self.assertFalse(outcome.witness_available)
        self.assertFalse(outcome.replay["confirmed"])

    def test_structural_fast_path_skips_the_solver_and_says_so(self):
        engine = BruteForceEngine()
        options = compare.Options(structural_fast_path=True)
        outcomes, _, _, _, _ = run_comparison(
            build_base(), build_base(), engine=engine, options=options
        )
        self.assertEqual({outcome.status for outcome in outcomes}, {compare.EQUIVALENT})
        self.assertEqual({outcome.method for outcome in outcomes}, {"structural"})
        self.assertEqual(engine.queries, 0)

    def test_without_the_fast_path_every_point_is_solved(self):
        engine = BruteForceEngine()
        outcomes, _, _, _, _ = run_comparison(build_base(), build_base(), engine=engine)
        self.assertEqual(engine.queries, len(outcomes))
        self.assertEqual({outcome.method for outcome in outcomes}, {"formal"})


class FindingsTest(unittest.TestCase):
    def _document(
        self, netlist_a, netlist_b, mapping=None, engine=None, options=None, timings=True
    ):
        outcomes, problems, coverage, mapping, options = run_comparison(
            netlist_a, netlist_b, mapping=mapping, engine=engine, options=options
        )
        document = findings.build_document(
            netlist_a,
            netlist_b,
            outcomes,
            problems,
            coverage,
            mapping,
            options,
            generated_at="2026-01-01T00:00:00Z",
            record_timings=timings,
        )
        findings_validate.validate_document(document)
        return document

    def _by_id(self, document):
        return {finding["id"]: finding for finding in document["findings"]}

    def test_equivalent_run_validates_and_proves(self):
        document = self._document(build_base(), build_rewrite())
        summary = self._by_id(document)["semantic_diff/summary"]
        self.assertEqual(summary["status"], findings_model.STATUS_PROVEN_UNDER_ASSUMPTIONS)
        self.assertTrue(findings_model.is_unbounded_proof(summary))
        self.assertTrue(summary["assumptions"])
        self.assertIn(
            "matched-state-correspondence",
            {assumption["id"] for assumption in summary["assumptions"]},
        )

    def test_changed_run_is_a_counterexample_with_a_witness(self):
        document = self._document(build_base(), build_changed())
        by_id = self._by_id(document)
        summary = by_id["semantic_diff/summary"]
        self.assertEqual(summary["status"], findings_model.STATUS_COUNTEREXAMPLE)
        self.assertTrue(summary["counterexample"]["witness_available"])
        self.assertTrue(summary["counterexample"]["witness"])
        # No bounded status is used: nothing was unrolled, so there is no bound.
        self.assertTrue(summary["bounds"]["unbounded"])
        self.assertNotIn("cycle_bound", summary["counterexample"])

        point = by_id["semantic_diff/point/sequential:sel_reg_2.D"]
        self.assertEqual(point["status"], findings_model.STATUS_COUNTEREXAMPLE)
        # inv_a0 occurs in both cones and is *not* reported as changed; only the
        # decode gate and the enable gate are.
        self.assertEqual(point["data"]["changed_gates"]["only_in_a_count"], 2)
        self.assertEqual(
            sorted(entry["name"] for entry in point["data"]["changed_gates"]["only_in_a"]),
            ["dec2", "en2"],
        )

    def test_no_run_with_an_unknown_ever_claims_a_proof(self):
        for force in ("unknown", "timeout", "error"):
            document = self._document(
                build_base(), build_rewrite(), engine=BruteForceEngine(force=force)
            )
            statuses_seen = {finding["status"] for finding in document["findings"]}
            self.assertNotIn(findings_model.STATUS_PROVEN_UNDER_ASSUMPTIONS, statuses_seen)
            self.assertEqual(
                self._by_id(document)["semantic_diff/summary"]["status"],
                findings_model.STATUS_UNKNOWN,
            )

    def test_a_correspondence_gap_blocks_the_proof(self):
        document = self._document(
            build_base(),
            build_rewrite(["sel_reg_0", "sel_reg_1", "sel_reg_2", "status_reg_3"]),
        )
        by_id = self._by_id(document)
        self.assertEqual(
            by_id["semantic_diff/summary"]["status"], findings_model.STATUS_UNKNOWN
        )
        gaps = [
            finding
            for finding in document["findings"]
            if finding["id"].startswith("semantic_diff/correspondence/")
        ]
        self.assertEqual(len(gaps), 2)
        self.assertTrue(all(gap["status"] == findings_model.STATUS_UNSUPPORTED for gap in gaps))

    def test_a_gate_outside_the_model_is_reported_as_a_coverage_gap(self):
        netlist_a = build_base()
        netlist_b = build_rewrite()
        netlist_b.add_gate("RAM", "scratch", A="A0", D="A1", Q="ram_q")
        document = self._document(netlist_a, netlist_b)
        finding = self._by_id(document)["semantic_diff/coverage/unmodelled-primitives"]
        self.assertEqual(finding["status"], findings_model.STATUS_UNSUPPORTED)
        self.assertEqual(finding["unsupported"]["primitives"][0]["gate_type"], "RAM")

    def test_timeout_findings_carry_the_limit_they_hit(self):
        document = self._document(
            build_base(), build_rewrite(), engine=BruteForceEngine(force="timeout")
        )
        point = self._by_id(document)["semantic_diff/point/output:HIT"]
        self.assertEqual(point["status"], findings_model.STATUS_TIMEOUT)
        self.assertEqual(point["limits"]["timeout_s"], 5.0)
        self.assertTrue(point["limits"]["hit"])

    def test_the_document_is_deterministic_without_timings(self):
        first = self._document(build_base(), build_changed(), timings=False)
        second = self._document(build_base(), build_changed(), timings=False)
        self.assertEqual(
            findings_serialize.document_digest(first),
            findings_serialize.document_digest(second),
        )
        self.assertEqual(findings_serialize.dumps(first), findings_serialize.dumps(second))
        self.assertNotIn("wall_time_s", findings_serialize.dumps(first))

    def test_timings_are_recorded_by_default(self):
        document = self._document(build_base(), build_changed())
        self.assertIn("wall_time_s", findings_serialize.dumps(document))

    def _run_document(self, out_dir, timings):
        """A document as the CLI builds one: real stamp, real command line."""
        outcomes, problems, coverage, mapping, options = run_comparison(
            build_base(), build_changed()
        )
        document = findings.build_document(
            build_base(),
            build_changed(),
            outcomes,
            problems,
            coverage,
            mapping,
            options,
            producer_command=["hal_semantic_diff", "compare", "a.v", "b.v", "-o", out_dir],
            record_timings=timings,
        )
        findings_validate.validate_document(document)
        return document

    def test_without_timings_two_real_runs_are_byte_identical(self):
        # The smoke test runs the same comparison twice into two output
        # directories.  A wall-clock stamp or the recorded command line would
        # make the two documents differ, which is exactly what --no-timings
        # promises not to do; hal_findings calls both fields volatile.
        first = self._run_document("out/repro1", timings=False)
        second = self._run_document("out/repro2", timings=False)
        self.assertEqual(findings_serialize.dumps(first), findings_serialize.dumps(second))
        self.assertNotIn("generated_at", first)
        self.assertNotIn("command", first["producer"])

    def test_with_timings_the_stamp_and_the_command_are_recorded(self):
        document = self._run_document("out/run", timings=True)
        self.assertTrue(document["generated_at"])
        self.assertIn("-o", document["producer"]["command"])

    def test_an_explicit_stamp_survives_no_timings(self):
        outcomes, problems, coverage, mapping, options = run_comparison(
            build_base(), build_changed()
        )
        document = findings.build_document(
            build_base(),
            build_changed(),
            outcomes,
            problems,
            coverage,
            mapping,
            options,
            generated_at="2026-01-01T00:00:00Z",
            record_timings=False,
        )
        self.assertEqual(document["generated_at"], "2026-01-01T00:00:00Z")

    def test_the_correspondence_file_is_pinned_as_an_artifact(self):
        path = os.path.join(FIXTURES, "correspondence_changed.json")
        outcomes, problems, coverage, mapping, options = run_comparison(
            build_base(), build_changed(), mapping=correspondence.load(path)
        )
        document = findings.build_document(
            build_base(),
            build_changed(),
            outcomes,
            problems,
            coverage,
            mapping,
            options,
            correspondence_artifact=findings_model.artifact(
                "correspondence",
                kind="other",
                path=path,
                sha256=findings_serialize.sha256_file(path),
            ),
        )
        findings_validate.validate_document(document)
        artifacts = {entry["artifact_id"]: entry for entry in document["artifacts"]}
        self.assertIn("correspondence", artifacts)
        self.assertEqual(len(artifacts["correspondence"]["sha256"]), 64)
        self.assertEqual(
            document["analysis"]["configuration"]["correspondence"]["identity"], True
        )

    def test_coverage_finding_names_the_registered_outputs(self):
        document = self._document(build_base(), build_rewrite())
        finding = self._by_id(document)["semantic_diff/coverage/observation-points"]
        self.assertEqual(finding["status"], findings_model.STATUS_UNSUPPORTED)
        for pin in ("SEL0", "SEL1", "SEL2", "SEL3"):
            self.assertIn(pin, finding["summary"])
        self.assertNotIn("HIT", finding["summary"])


class DiagramTest(unittest.TestCase):
    def test_cone_pair_graph_marks_the_changed_gates(self):
        outcomes, _, _, _, _ = run_comparison(build_base(), build_changed())
        outcome = next(o for o in outcomes if o.point.key == "sequential:sel_reg_2.D")
        text = diagrams.cone_pair_graph(outcome).to_dot()
        self.assertTrue(text.lstrip().startswith("//") or text.startswith("digraph"))
        self.assertEqual(text.count("{"), text.count("}"))
        self.assertIn("cluster_a", text)
        self.assertIn("cluster_b", text)
        # the shared boundary variables are drawn once, outside both clusters
        self.assertEqual(text.count('"b_GLOBAL_IN_A1" ['), 1)
        self.assertIn("#f7c7c7", text)  # the highlight of a changed gate

    def test_write_cone_pair_produces_a_file(self):
        outcomes, _, _, _, _ = run_comparison(build_base(), build_changed())
        outcome = next(o for o in outcomes if o.point.key == "sequential:sel_reg_2.D")
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = diagrams.write_cone_pair(outcome, directory)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("digraph", handle.read())


class ReportTest(unittest.TestCase):
    def _document(self, netlist_a, netlist_b):
        outcomes, problems, coverage, mapping, options = run_comparison(netlist_a, netlist_b)
        return findings.build_document(
            netlist_a, netlist_b, outcomes, problems, coverage, mapping, options
        )

    def test_report_states_the_status_and_the_witness(self):
        html = report.render(self._document(build_base(), build_changed()))
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("refuted -- counterexample", html)
        self.assertIn("GLOBAL_IN_EN", html)
        self.assertIn("assumption(s) this rests on", html)

    def test_report_labels_a_proof_as_conditional(self):
        html = report.render(self._document(build_base(), build_rewrite()))
        self.assertIn("proven (under assumptions)", html)
        self.assertNotIn("refuted -- counterexample", html)

    def test_report_escapes_content(self):
        document = self._document(build_base(), build_rewrite())
        document["findings"][0]["title"] = "<script>alert(1)</script>"
        html = report.render(document)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_write_report_round_trips(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "report.html")
        report.write_report(self._document(build_base(), build_changed()), path)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("Findings", handle.read())


class ParserTest(unittest.TestCase):
    """``-q`` must work where the sibling tools take it: after the subcommand."""

    def setUp(self):
        self.parser = cli.build_parser()

    def _compare(self, extra):
        return self.parser.parse_args(["compare", "a.v", "b.v"] + list(extra))

    def test_quiet_after_the_subcommand(self):
        self.assertTrue(self._compare(["-q"]).quiet)
        self.assertTrue(self._compare(["--quiet"]).quiet)

    def test_quiet_before_the_subcommand_still_works(self):
        args = self.parser.parse_args(["-q", "compare", "a.v", "b.v"])
        self.assertTrue(args.quiet)

    def test_quiet_defaults_to_false(self):
        self.assertFalse(self._compare([]).quiet)

    def test_report_takes_quiet_too(self):
        self.assertTrue(self.parser.parse_args(["report", "f.json", "-q"]).quiet)


class GroundTruthTest(unittest.TestCase):
    """The stub designs and ``fixtures/ground_truth.json`` must not drift apart."""

    def setUp(self):
        with open(os.path.join(FIXTURES, "ground_truth.json"), encoding="utf-8") as handle:
            self.ground_truth = json.load(handle)
        self.cases = {case["name"]: case for case in self.ground_truth["cases"]}

    def test_every_case_names_files_that_exist(self):
        for case in self.ground_truth["cases"]:
            for key in ("netlist_a", "netlist_b", "correspondence"):
                self.assertTrue(
                    os.path.isfile(os.path.join(FIXTURES, case[key])),
                    "{} references a missing {}".format(case["name"], key),
                )

    def _counts(self, outcomes):
        return {
            status: len([o for o in outcomes if o.status == status])
            for status in compare.STATUSES
        }

    def test_equivalent_rewrite_matches_the_recorded_expectation(self):
        expected = self.cases["equivalent_rewrite"]["expect"]
        outcomes, problems, _, _, _ = run_comparison(build_base(), build_rewrite())
        self.assertEqual(len(outcomes), expected["observation_points"])
        self.assertEqual(len(problems), expected["correspondence_gaps"])
        self.assertEqual(self._counts(outcomes), expected["counts"])

    def test_changed_decoder_matches_the_recorded_expectation(self):
        expected = self.cases["changed_decoder"]["expect"]
        outcomes, problems, _, _, _ = run_comparison(build_base(), build_changed())
        self.assertEqual(len(outcomes), expected["observation_points"])
        self.assertEqual(len(problems), expected["correspondence_gaps"])
        self.assertEqual(self._counts(outcomes), expected["counts"])
        by_key = statuses(outcomes)
        self.assertEqual(
            sorted(k for k, v in by_key.items() if v == compare.DIFFERENT),
            expected["different_points"],
        )
        for key in expected["equivalent_points_include"]:
            self.assertEqual(by_key[key], compare.EQUIVALENT)
        outcome = next(o for o in outcomes if o.point.key == expected["different_points"][0])
        witness = {entry["signal"]: entry["value"] for entry in outcome.witness}
        for name, value in expected["witness_constraints"].items():
            self.assertEqual(witness[name], value)

    def test_retiming_case_matches_the_recorded_expectation(self):
        expected = self.cases["retiming_is_rejected"]["expect"]
        outcomes, problems, _, _, _ = run_comparison(build_base(), build_retimed())
        self.assertEqual(len(outcomes), expected["observation_points"])
        self.assertEqual(len(problems), expected["correspondence_gaps"])
        self.assertEqual(self._counts(outcomes), expected["counts"])

    def test_renamed_gap_case_matches_the_recorded_expectation(self):
        expected = self.cases["renamed_register_without_mapping"]["expect"]
        outcomes, problems, _, _, _ = run_comparison(
            build_base(),
            build_rewrite(["sel_reg_0", "sel_reg_1", "sel_reg_2", "status_reg_3"]),
        )
        self.assertEqual(len(outcomes), expected["observation_points"])
        self.assertEqual(len(problems), expected["correspondence_gaps"])
        self.assertEqual(self._counts(outcomes), expected["counts"])
        self.assertEqual(
            sorted(
                key for key, value in statuses(outcomes).items()
                if value == compare.UNSUPPORTED
            ),
            expected["unsupported_points"],
        )


if __name__ == "__main__":
    unittest.main()
