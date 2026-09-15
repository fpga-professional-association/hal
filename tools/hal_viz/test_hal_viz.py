"""Standalone unit tests for the parts of hal_viz that do not need HAL.

These cover the DOT formatting/escaping layer and the graph extraction layer,
the latter driven by stub objects that mimic the ``hal_py`` accessors.  Run
them with a plain interpreter from the repository root:

    python -m unittest discover -s tools/hal_viz -t tools -p "test_*.py"

or simply::

    python tools/hal_viz/test_hal_viz.py
"""

import os
import sys
import unittest

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_viz import cli, dot, extract, levels, render  # noqa: E402


# ---------------------------------------------------------------------------
# stub netlist objects (duck-typed against the hal_py bindings)
# ---------------------------------------------------------------------------


class StubNamed(object):
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


class StubGateType(StubNamed):
    """A gate type that reports properties, the way the real bindings do.

    ``str(hal_py.GateTypeProperty.ff)`` is ``"GateTypeProperty.ff"``, so the
    qualified spelling is what the stub hands out.
    """

    def __init__(self, name, properties=()):
        StubNamed.__init__(self, name)
        self._properties = ["GateTypeProperty." + entry for entry in properties]

    def get_properties(self):
        return list(self._properties)


class StubEndpoint(object):
    def __init__(self, gate, net, pin):
        self._gate = gate
        self._net = net
        self._pin = StubNamed(pin)

    def get_gate(self):
        return self._gate

    def get_net(self):
        return self._net

    def get_pin(self):
        return self._pin


class StubNet(object):
    def __init__(self, net_id, name):
        self.id = net_id
        self._name = name
        self.sources = []
        self.destinations = []
        self.global_input = False
        self.global_output = False

    def get_id(self):
        return self.id

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


class StubGate(object):
    def __init__(self, gate_id, name, gate_type="AND2", module=None, properties=None):
        self.id = gate_id
        self._name = name
        # ``properties=None`` mimics a binding that does not report gate-type
        # properties at all, which is what makes the name fallback testable.
        self._type = (
            StubNamed(gate_type)
            if properties is None
            else StubGateType(gate_type, properties)
        )
        self.module = module
        self.fan_out = []
        self.fan_in = []

    def get_id(self):
        return self.id

    def get_name(self):
        return self._name

    def get_type(self):
        return self._type

    def get_module(self):
        return self.module

    def get_fan_out_endpoints(self):
        return list(self.fan_out)

    def get_fan_in_endpoints(self):
        return list(self.fan_in)

    def get_unique_successors(self):
        out = []
        for endpoint in self.fan_out:
            for dest in endpoint.get_net().get_destinations():
                if dest.get_gate() is not None and dest.get_gate() not in out:
                    out.append(dest.get_gate())
        return out

    def get_unique_predecessors(self):
        out = []
        for endpoint in self.fan_in:
            for src in endpoint.get_net().get_sources():
                if src.get_gate() is not None and src.get_gate() not in out:
                    out.append(src.get_gate())
        return out

    def is_gnd_gate(self):
        return False

    def is_vcc_gate(self):
        return False


class StubModule(object):
    def __init__(self, module_id, name, module_type="", gates=(), submodules=()):
        self.id = module_id
        self._name = name
        self._type = module_type
        self.gates = list(gates)
        self.submodules = list(submodules)

    def get_id(self):
        return self.id

    def get_name(self):
        return self._name

    def get_type(self):
        return self._type

    def get_gates(self):
        return list(self.gates)

    def get_submodules(self):
        return list(self.submodules)


class StubConstantGate(StubGate):
    """A GND or VCC gate, identified the way ``hal_py.Gate`` identifies one."""

    def __init__(self, gate_id, name, value):
        StubGate.__init__(self, gate_id, name, "GND" if value == "0" else "VCC")
        self.value = value

    def is_gnd_gate(self):
        return self.value == "0"

    def is_vcc_gate(self):
        return self.value == "1"


def connect(source_gate, source_pin, net, targets):
    """Wire ``source_gate.source_pin`` to ``[(gate, pin), ...]`` through ``net``."""
    src_ep = StubEndpoint(source_gate, net, source_pin)
    net.sources.append(src_ep)
    source_gate.fan_out.append(src_ep)
    for gate, pin in targets:
        dst_ep = StubEndpoint(gate, net, pin)
        net.destinations.append(dst_ep)
        gate.fan_in.append(dst_ep)
    return net


def subgraph_block(source, name):
    """The body of the ``subgraph "<name>"`` block of a .dot source."""
    marker = 'subgraph "{}" {{'.format(name)
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if marker not in line:
            continue
        depth = 1
        body = []
        for inner in lines[index + 1:]:
            depth += inner.count("{") - inner.count("}")
            if depth == 0:
                return "\n".join(body)
            body.append(inner)
    raise AssertionError("no subgraph {!r} in the emitted .dot".format(name))


def node_lines(source, prefix=""):
    """The node declaration lines of a .dot whose node id starts with ``prefix``."""
    return [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith('"' + prefix) and "->" not in line
    ]


def declared_nodes(source, prefix=""):
    return [line.split('"')[1] for line in node_lines(source, prefix)]


def edges_from(source, prefix=""):
    """The ``(source, target)`` pairs whose source id starts with ``prefix``."""
    pairs = []
    for line in source.splitlines():
        stripped = line.strip()
        if "->" not in stripped or not stripped.startswith('"' + prefix):
            continue
        parts = stripped.split('"')
        pairs.append((parts[1], parts[3]))
    return pairs


def attach_global_input(gate, pin, net):
    """Drive ``gate.pin`` from a global input net that no gate sources."""
    net.global_input = True
    endpoint = StubEndpoint(gate, net, pin)
    net.destinations.append(endpoint)
    gate.fan_in.append(endpoint)
    return net


def attach_global_output(gate, pin, net):
    """Send ``gate.pin`` to a global output net that no gate sinks."""
    net.global_output = True
    endpoint = StubEndpoint(gate, net, pin)
    net.sources.append(endpoint)
    gate.fan_out.append(endpoint)
    return net


def build_chain(length):
    """a0 -> a1 -> ... -> a(length-1), returning the gate list."""
    gates = [StubGate(i + 1, "a{}".format(i)) for i in range(length)]
    for index in range(length - 1):
        connect(
            gates[index],
            "O",
            StubNet(100 + index, "n{}".format(index)),
            [(gates[index + 1], "I")],
        )
    return gates


# ---------------------------------------------------------------------------
# dot.py
# ---------------------------------------------------------------------------


class TestEscaping(unittest.TestCase):
    def test_quotes_and_backslashes(self):
        self.assertEqual(dot.escape('a"b'), 'a\\"b')
        self.assertEqual(dot.escape("a\\b"), "a\\\\b")
        # A trailing backslash must not escape the closing quote.
        self.assertTrue(dot.quote("path\\").endswith('\\\\"'))

    def test_newlines_become_dot_line_breaks(self):
        self.assertEqual(dot.escape("a\nb"), "a\\nb")
        self.assertEqual(dot.escape("a\r\nb"), "a\\nb")
        self.assertEqual(dot.escape("a\rb"), "a\\nb")

    def test_control_characters_are_neutralized(self):
        self.assertEqual(dot.escape("a\x00b\x07c"), "a b c")
        self.assertEqual(dot.escape("a\tb"), "a b")

    def test_quote_wraps(self):
        self.assertEqual(dot.quote("x"), '"x"')
        self.assertEqual(dot.quote(42), '"42"')

    def test_truncate(self):
        self.assertEqual(dot.truncate("abcdef", 10), "abcdef")
        self.assertEqual(dot.truncate("abcdef", 5), "ab...")
        self.assertEqual(dot.truncate("abcdef", 2), "ab")
        self.assertEqual(dot.truncate("abcdef", None), "abcdef")

    def test_sanitize_id(self):
        self.assertEqual(dot.sanitize_id("mod-1/x"), "mod_1_x")
        self.assertEqual(dot.sanitize_id("9lives", prefix="m"), "m_9lives")


class TestDotGraph(unittest.TestCase):
    def test_minimal_graph(self):
        graph = dot.DotGraph("g")
        graph.add_node("a", label="A")
        graph.add_node("b", label="B")
        graph.add_edge("a", "b", label="e")
        source = graph.to_dot()
        self.assertTrue(source.startswith('digraph "g" {'))
        self.assertTrue(source.endswith("}\n"))
        self.assertIn('"a" [label="A"];', source)
        self.assertIn('"a" -> "b" [label="e"];', source)
        self.assertEqual(graph.node_count, 2)
        self.assertEqual(graph.edge_count, 1)

    def test_undirected_uses_correct_edge_operator(self):
        graph = dot.DotGraph("g", directed=False)
        graph.add_edge("a", "b")
        self.assertIn('"a" -- "b";', graph.to_dot())

    def test_defaults_and_comment(self):
        graph = dot.DotGraph("g", comment="hello")
        graph.graph_attrs["rankdir"] = "LR"
        graph.node_defaults["shape"] = "box"
        graph.edge_defaults["color"] = "#fff"
        source = graph.to_dot()
        self.assertTrue(source.startswith("// hello\n"))
        self.assertIn('graph [rankdir="LR"];', source)
        self.assertIn('node [shape="box"];', source)
        self.assertIn('edge [color="#fff"];', source)

    def test_clusters_nest_and_count(self):
        graph = dot.DotGraph("g")
        cluster = graph.add_cluster("mod_3", label="mod")
        cluster.add_node("a")
        graph.add_node("b")
        source = graph.to_dot()
        self.assertIn('subgraph "cluster_mod_3" {', source)
        self.assertIn('label="mod"', source)
        self.assertEqual(graph.node_count, 2)

    def test_node_attributes_merge(self):
        graph = dot.DotGraph("g")
        graph.add_node("a", label="A")
        graph.add_node("a", color="red")
        source = graph.to_dot()
        self.assertEqual(source.count('"a" ['), 1)
        self.assertIn('label="A"', source)
        self.assertIn('color="red"', source)

    def test_none_attributes_are_dropped(self):
        graph = dot.DotGraph("g")
        graph.add_node("a", label=None, color=None)
        self.assertIn('"a";', graph.to_dot())

    def test_invalid_attribute_name_rejected(self):
        graph = dot.DotGraph("g")
        graph.add_node("a", **{"bad name": "x"})
        self.assertRaises(ValueError, graph.to_dot)

    def test_hostile_names_stay_parseable(self):
        graph = dot.DotGraph('design "x"')
        graph.add_node('gate\\"; evil', label='net "clk"\nfoo')
        source = graph.to_dot()
        # Every quote in the body is either a delimiter or escaped.
        self.assertNotIn('"; evil', source.replace('\\"', ""))
        self.assertIn('\\"', source)

    def test_write_roundtrip(self):
        import tempfile

        graph = dot.DotGraph("g")
        graph.add_node("a")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.dot")
            graph.write(path)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), graph.to_dot())


# ---------------------------------------------------------------------------
# extract.py
# ---------------------------------------------------------------------------


class TestNetlistGraph(unittest.TestCase):
    def test_chain_produces_expected_nodes_and_edges(self):
        gates = build_chain(3)
        graph = extract.build_netlist_graph(gates, title="chain")
        source = graph.to_dot()
        self.assertEqual(graph.node_count, 3)
        self.assertEqual(graph.edge_count, 2)
        self.assertIn('"g1" -> "g2"', source)
        self.assertIn('"g2" -> "g3"', source)
        self.assertIn("a0", source)
        self.assertIn("AND2", source)

    def test_edges_to_out_of_scope_gates_are_dropped(self):
        gates = build_chain(3)
        graph = extract.build_netlist_graph(gates[:2])
        self.assertEqual(graph.node_count, 2)
        self.assertEqual(graph.edge_count, 1)

    def test_show_boundary_adds_stubs(self):
        gates = build_chain(3)
        with_boundary = extract.build_netlist_graph(gates[1:2], show_boundary=True)
        # one real gate plus an incoming and an outgoing stub
        self.assertEqual(with_boundary.node_count, 3)
        self.assertEqual(with_boundary.edge_count, 2)
        self.assertIn('shape="point"', with_boundary.to_dot())

    def test_net_and_pin_labels(self):
        gates = build_chain(2)
        labelled = extract.build_netlist_graph(gates, net_labels=True, pin_labels=True)
        self.assertIn('"g1" -> "g2" [label="n0\\nO -> I"];', labelled.to_dot())
        bare = extract.build_netlist_graph(gates, net_labels=False, pin_labels=False)
        self.assertIn('"g1" -> "g2";', bare.to_dot())

    def test_fanout_to_multiple_destinations(self):
        driver = StubGate(1, "drv")
        sink_a = StubGate(2, "a")
        sink_b = StubGate(3, "b")
        connect(driver, "O", StubNet(10, "fan"), [(sink_a, "I"), (sink_b, "I")])
        graph = extract.build_netlist_graph([driver, sink_a, sink_b])
        self.assertEqual(graph.edge_count, 2)

    def test_cluster_modules(self):
        module = StubModule(7, "core")
        gates = build_chain(2)
        for gate in gates:
            gate.module = module
        graph = extract.build_netlist_graph(gates, cluster_modules=True)
        source = graph.to_dot()
        self.assertIn('subgraph "cluster_mod_7"', source)
        self.assertIn("core", source)
        self.assertEqual(graph.node_count, 2)

    def test_empty_scope_is_valid(self):
        graph = extract.build_netlist_graph([])
        self.assertTrue(graph.to_dot().rstrip().endswith("}"))


class TestConstantTieOffs(unittest.TestCase):
    """GND/VCC are drawn as one 0/1 stub per consuming edge, never as a hub."""

    def _fanout_design(self):
        gnd = StubConstantGate(1, "GND_inst", "0")
        vcc = StubConstantGate(2, "VCC_inst", "1")
        sinks = [StubGate(10 + i, "sink{}".format(i)) for i in range(3)]
        connect(gnd, "O", StubNet(100, "gnd_net"), [(g, "I0") for g in sinks])
        connect(vcc, "O", StubNet(101, "vcc_net"), [(sinks[0], "I1")])
        return [gnd, vcc] + sinks

    def test_one_stub_per_consuming_edge(self):
        source = extract.build_netlist_graph(self._fanout_design()).to_dot()
        # three GND sinks and one VCC sink -> four stubs and four edges
        self.assertEqual(3, len(declared_nodes(source, "tie0_")))
        self.assertEqual(1, len(declared_nodes(source, "tie1_")))
        self.assertEqual(4, len(edges_from(source, "tie")))

    def test_stubs_are_labelled_with_the_literal_value(self):
        source = extract.build_netlist_graph(self._fanout_design()).to_dot()
        for line in node_lines(source, "tie"):
            value = "0" if '"tie0_' in line else "1"
            self.assertIn('label="{}"'.format(value), line)
            # the gate name/type/id block belongs to gates, not to a tie-off;
            # naming the driver is a tooltip, not a label
            self.assertNotIn("_inst", line[: line.index("tooltip")])

    def test_the_constant_gate_itself_is_not_drawn(self):
        source = extract.build_netlist_graph(self._fanout_design()).to_dot()
        self.assertNotIn('"g1" [', source)  # the GND gate
        self.assertNotIn('"g2" [', source)  # the VCC gate
        self.assertNotIn("invtriangle", source)
        self.assertNotIn("GND_inst\\n[GND]", source)

    def test_no_shared_hub_edge_source(self):
        source = extract.build_netlist_graph(self._fanout_design()).to_dot()
        sources = [edge[0] for edge in edges_from(source, "tie")]
        self.assertEqual(len(sources), len(set(sources)), sources)

    def test_stubs_sit_on_the_source_rank(self):
        source = extract.build_netlist_graph(self._fanout_design()).to_dot()
        self.assertIn('subgraph "tieoffs" {', source)
        self.assertIn('rank="source"', source)

    def test_tie_off_count_is_noted_in_the_dot_comment(self):
        source = extract.build_netlist_graph(self._fanout_design()).to_dot()
        self.assertTrue(source.startswith("// 4 constant tie-off stub(s)"), source[:80])

    def test_const_hub_escape_hatch_restores_the_old_drawing(self):
        source = extract.build_netlist_graph(
            self._fanout_design(), const_hub=True
        ).to_dot()
        self.assertIn("invtriangle", source)
        self.assertIn("GND_inst", source)
        self.assertNotIn('"tie0_', source)
        self.assertEqual(4, source.count('"g1" -> ') + source.count('"g2" -> '))

    def test_out_of_scope_constant_driver_becomes_a_stub_with_boundary(self):
        gates = self._fanout_design()
        in_scope = gates[2:]  # only the sinks
        graph = extract.build_netlist_graph(in_scope, show_boundary=True)
        source = graph.to_dot()
        self.assertIn('label="0"', source)
        self.assertIn('label="1"', source)
        self.assertEqual(4, len(edges_from(source, "tie")))
        self.assertNotIn('"in_1"', source)  # not an anonymous boundary stub


class TestLevels(unittest.TestCase):
    """Pure graph theory: no netlist, no HAL, no Graphviz."""

    def test_chain_levels_increase_by_one(self):
        result = levels.compute_levels("abc", [("a", "b"), ("b", "c")])
        self.assertEqual({"a": 0, "b": 1, "c": 2}, result.levels)
        self.assertEqual(3, result.level_count)
        self.assertEqual(2, result.max_level)
        self.assertFalse(result.has_cycles)

    def test_a_gate_sits_behind_its_deepest_driver(self):
        # a -> b -> c and a -> c: c must be level 2, not level 1
        result = levels.compute_levels(
            "abc", [("a", "b"), ("b", "c"), ("a", "c")]
        )
        self.assertEqual(2, result.levels["c"])

    def test_isolated_and_unreferenced_nodes_are_sources(self):
        result = levels.compute_levels(["a", "b"], [])
        self.assertEqual({"a": 0, "b": 0}, result.levels)
        self.assertEqual(["a", "b"], result.by_level[0])

    def test_edges_outside_the_node_set_are_ignored(self):
        result = levels.compute_levels(["a"], [("x", "a"), ("a", "y")])
        self.assertEqual({"a": 0}, result.levels)

    def test_parallel_edges_do_not_deepen_a_node(self):
        result = levels.compute_levels("ab", [("a", "b"), ("a", "b")])
        self.assertEqual({"a": 0, "b": 1}, result.levels)

    def test_two_node_cycle_is_levelled_and_reported(self):
        result = levels.compute_levels(
            "abc", [("a", "b"), ("b", "c"), ("c", "b")]
        )
        self.assertEqual(3, len(result.levels))  # nothing was dropped
        self.assertTrue(result.has_cycles)
        self.assertEqual({"b", "c"}, result.cycle_nodes)
        self.assertEqual([["b", "c"]], result.cycle_groups)
        self.assertTrue(result.broken_edges)

    def test_self_loop_counts_as_a_cycle(self):
        result = levels.compute_levels(["a"], [("a", "a")])
        self.assertEqual({"a"}, result.cycle_nodes)
        self.assertEqual(0, result.levels["a"])

    def test_a_cycle_does_not_starve_the_nodes_behind_it(self):
        result = levels.compute_levels(
            "abcd", [("a", "b"), ("b", "c"), ("c", "b"), ("c", "d")]
        )
        self.assertEqual(sorted("abcd"), sorted(result.levels))
        self.assertEqual(0, result.levels["a"])
        self.assertGreater(result.levels["d"], result.levels["c"])

    def test_levelling_is_deterministic(self):
        edges = [("a", "b"), ("b", "c"), ("c", "b"), ("a", "c")]
        first = levels.compute_levels("abc", edges)
        second = levels.compute_levels("abc", edges)
        self.assertEqual(first.levels, second.levels)
        self.assertEqual(first.broken_edges, second.broken_edges)
        self.assertEqual(first.cycle_groups, second.cycle_groups)

    def test_several_cycles_are_reported_in_input_order(self):
        # two disjoint loops: the report must not depend on set iteration order
        nodes = ["a", "b", "c", "d"]
        edges = [("a", "b"), ("b", "a"), ("c", "d"), ("d", "c")]
        result = levels.compute_levels(nodes, edges)
        self.assertEqual([["a", "b"], ["c", "d"]], result.cycle_groups)

    def test_strongly_connected_components(self):
        components = levels.strongly_connected_components(
            "abcd", [("a", "b"), ("b", "a"), ("b", "c"), ("c", "d")]
        )
        self.assertIn(["a", "b"], components)
        self.assertIn(["c"], components)
        self.assertIn(["d"], components)

    def test_deep_chain_does_not_recurse(self):
        keys = list(range(2000))
        edges = [(index, index + 1) for index in range(1999)]
        result = levels.compute_levels(keys, edges)
        self.assertEqual(1999, result.max_level)
        self.assertFalse(result.has_cycles)


class TestGateClassification(unittest.TestCase):
    def test_properties_identify_a_register(self):
        flop = StubGate(1, "q", "FDRE", properties=["sequential", "ff"])
        self.assertTrue(extract.is_sequential_gate(flop))
        latch = StubGate(2, "l", "DLH", properties=["latch"])
        self.assertTrue(extract.is_sequential_gate(latch))

    def test_properties_win_over_the_name(self):
        # a combinational cell that merely looks like a register
        odd = StubGate(1, "x", "FFMUX", properties=["combinational"])
        self.assertFalse(extract.is_sequential_gate(odd))

    def test_name_fallback_when_no_properties_are_reported(self):
        self.assertTrue(extract.is_sequential_gate(StubGate(1, "x", "FFR")))
        self.assertTrue(extract.is_sequential_gate(StubGate(2, "x", "tennm_ff")))
        self.assertTrue(extract.is_sequential_gate(StubGate(3, "x", "DFFSR")))
        self.assertFalse(extract.is_sequential_gate(StubGate(4, "x", "BUFF")))
        self.assertFalse(extract.is_sequential_gate(StubGate(5, "x", "LUT4")))
        self.assertFalse(extract.is_sequential_gate(StubGate(6, "x", "AND2")))

    def test_constant_value(self):
        self.assertEqual("0", extract.constant_value(StubConstantGate(1, "g", "0")))
        self.assertEqual("1", extract.constant_value(StubConstantGate(2, "v", "1")))
        self.assertIsNone(extract.constant_value(StubGate(3, "a")))
        self.assertEqual(
            "0", extract.constant_value(StubGate(4, "g", "GND", properties=["ground"]))
        )

    def test_io_gate_detection(self):
        gates = build_chain(2)
        self.assertFalse(extract.is_io_gate(gates[0]))
        attach_global_input(gates[0], "I", StubNet(200, "pad_in"))
        self.assertTrue(extract.is_io_gate(gates[0]))
        self.assertFalse(extract.is_io_gate(gates[1]))
        attach_global_output(gates[1], "O", StubNet(201, "pad_out"))
        self.assertTrue(extract.is_io_gate(gates[1]))


def build_sequential_design():
    """in -> lut -> ff -> (lut feedback, out), plus a GND and a VCC tie-off.

    Exactly the shape the ``dag`` view exists for: one cut edge into the flop,
    the flop's output back at level 0, and two constants that must not become a
    hub.
    """
    inp = StubGate(1, "in_buf", "BUF")
    lut = StubGate(2, "lut", "LUT4")
    flop = StubGate(3, "state_reg", "FFR", properties=["sequential", "ff"])
    out = StubGate(4, "out_lut", "LUT2")
    gnd = StubConstantGate(5, "GND_inst", "0")
    vcc = StubConstantGate(6, "VCC_inst", "1")

    attach_global_input(inp, "I", StubNet(9, "din_pad"))
    connect(inp, "O", StubNet(10, "din"), [(lut, "I0")])
    attach_global_output(out, "O", StubNet(15, "dout_pad"))
    connect(lut, "O", StubNet(11, "d"), [(flop, "D")])
    connect(flop, "Q", StubNet(12, "q"), [(lut, "I1"), (out, "I0")])
    connect(gnd, "O", StubNet(13, "gnd_net"), [(lut, "I2")])
    connect(vcc, "O", StubNet(14, "vcc_net"), [(out, "I1")])
    return [inp, lut, flop, out, gnd, vcc]


class TestDagGraph(unittest.TestCase):
    def test_chain_levels_left_to_right(self):
        view = extract.build_dag_graph(build_chain(3))
        self.assertEqual({1: 0, 2: 1, 3: 2}, view.gate_levels)
        self.assertEqual(3, view.level_count)
        source = view.graph.to_dot()
        self.assertIn('subgraph "level_0" {', source)
        self.assertIn('subgraph "level_2" {', source)
        self.assertEqual(3, source.count('rank="same"'))
        self.assertIn('rankdir="LR"', source)

    def test_feedback_is_cut_at_the_flip_flop(self):
        view = extract.build_dag_graph(build_sequential_design())
        # the flop is a source of the cut graph, its driver is not
        self.assertEqual(0, view.gate_levels[3])
        self.assertEqual(0, view.gate_levels[1])
        self.assertEqual(1, view.gate_levels[2])  # lut, behind in_buf and the flop
        self.assertEqual(1, view.gate_levels[4])  # out_lut, behind the flop
        self.assertEqual(1, view.cut_count)
        source = view.graph.to_dot()
        self.assertIn('"g2" -> "g3" [style="dashed", color="#c0392b"', source)
        self.assertIn('"g3" -> "g2";', source)  # the flop's output is a normal edge

    def test_registers_io_and_combinational_gates_are_styled_apart(self):
        source = extract.build_dag_graph(build_sequential_design()).graph.to_dot()
        flop_line = [l for l in source.splitlines() if "state_reg" in l][0]
        self.assertIn('fillcolor="#dbe9f6"', flop_line)
        io_line = [l for l in source.splitlines() if "in_buf" in l][0]
        self.assertIn('shape="octagon"', io_line)
        lut_line = [l for l in source.splitlines() if "\\n[LUT4]" in l][0]
        self.assertNotIn("octagon", lut_line)

    def test_constants_become_one_stub_per_sink_at_level_zero(self):
        view = extract.build_dag_graph(build_sequential_design())
        self.assertEqual(2, view.tie_off_count)
        self.assertEqual(4, view.gate_count)  # the two constant gates are gone
        source = view.graph.to_dot()
        level_zero = subgraph_block(source, "level_0")
        self.assertIn('"tie0_1"', level_zero)
        self.assertIn('"tie1_2"', level_zero)
        self.assertIn('label="0"', level_zero)
        self.assertIn('label="1"', level_zero)
        self.assertNotIn("GND_inst\\n", source)

    def test_level_labels_are_ordered_with_invisible_edges(self):
        source = extract.build_dag_graph(build_chain(3)).graph.to_dot()
        self.assertIn('"lvl_0" [', source)
        self.assertIn('label="level 2"', source)
        self.assertIn('"lvl_0" -> "lvl_1" [style="invis"];', source)
        bare = extract.build_dag_graph(build_chain(3), level_labels=False).graph.to_dot()
        self.assertNotIn('"lvl_0"', bare)

    def test_combinational_loop_is_highlighted_not_dropped(self):
        a = StubGate(1, "loop_a", "LUT2")
        b = StubGate(2, "loop_b", "LUT2")
        tail = StubGate(3, "tail", "LUT2")
        connect(a, "O", StubNet(10, "n_ab"), [(b, "I0")])
        connect(b, "O", StubNet(11, "n_ba"), [(a, "I0"), (tail, "I0")])
        view = extract.build_dag_graph([a, b, tail])

        self.assertTrue(view.has_cycles)
        self.assertEqual([(1, "loop_a"), (2, "loop_b")], view.cycle_gates)
        self.assertIn("loop_a (id 1)", view.cycle_summary())
        self.assertEqual(3, view.gate_count)  # nothing was dropped
        self.assertEqual({1, 2, 3}, set(view.gate_levels))
        source = view.graph.to_dot()
        self.assertIn("loop_a", source)
        self.assertIn('penwidth="3"', source)
        self.assertIn("combinational loop(s) involving 2 gate(s)", source)

    def test_a_loop_through_a_register_is_not_a_combinational_loop(self):
        view = extract.build_dag_graph(build_sequential_design())
        self.assertFalse(view.has_cycles)
        self.assertEqual([], view.cycle_gates)

    def test_edge_and_gate_counts(self):
        view = extract.build_dag_graph(build_sequential_design())
        self.assertEqual(4, view.gate_count)
        # in_buf->lut, lut->flop (cut), flop->lut, flop->out, and the two
        # tie-offs; the legend is not part of the count
        self.assertEqual(6, view.edge_count)
        self.assertEqual(2, view.level_count)
        # the drawing adds one 'level N' marker per rank and the invisible
        # chain that orders them; the legend is not counted at all
        self.assertEqual(
            view.gate_count + view.tie_off_count + view.level_count,
            view.graph.node_count,
        )
        self.assertEqual(view.edge_count + view.level_count - 1, view.graph.edge_count)

    def test_labels_are_optional(self):
        bare = extract.build_dag_graph(build_chain(2)).graph.to_dot()
        self.assertIn('"g1" -> "g2";', bare)
        labelled = extract.build_dag_graph(
            build_chain(2), net_labels=True, pin_labels=True
        ).graph.to_dot()
        self.assertIn('"g1" -> "g2" [label="n0\\nO -> I"];', labelled)

    def test_empty_scope_is_valid(self):
        view = extract.build_dag_graph([])
        self.assertEqual(0, view.gate_count)
        self.assertEqual(0, view.level_count)
        self.assertTrue(view.graph.to_dot().rstrip().endswith("}"))

    def test_const_hub_keeps_the_constant_gates(self):
        view = extract.build_dag_graph(build_sequential_design(), const_hub=True)
        self.assertEqual(6, view.gate_count)
        self.assertEqual(0, view.tie_off_count)
        self.assertIn("GND_inst", view.graph.to_dot())


class TestLegend(unittest.TestCase):
    """Every rendered graph decodes its own visual vocabulary."""

    def _legend_of(self, source):
        return subgraph_block(source, "cluster_legend")

    def test_netlist_graph_has_a_legend(self):
        source = extract.build_netlist_graph(build_chain(2)).to_dot()
        legend = self._legend_of(source)
        self.assertIn('label="combinational\\ngate"', legend)
        self.assertIn('label="flip-flop /\\nlatch"', legend)
        self.assertIn('label="primary I/O"', legend)
        self.assertIn('label="0"', legend)
        self.assertIn('label="1"', legend)
        self.assertIn('label="net"', legend)

    def test_dag_legend_also_explains_cut_edges_and_levels(self):
        source = extract.build_dag_graph(build_sequential_design()).graph.to_dot()
        legend = self._legend_of(source)
        self.assertIn('label="cut at register"', legend)
        self.assertIn("combinational\\nloop", legend)
        self.assertIn("depth increases to the right", legend)

    def test_legend_node_ids_are_recognizable(self):
        source = extract.build_dag_graph(build_chain(2)).graph.to_dot()
        legend = self._legend_of(source)
        ids = [
            line.strip().split('"')[1]
            for line in legend.splitlines()
            if line.strip().startswith('"')
        ]
        self.assertTrue(ids)
        for node_id in ids:
            self.assertTrue(node_id.startswith("legend"), node_id)

    def test_legend_can_be_turned_off(self):
        self.assertNotIn(
            "cluster_legend",
            extract.build_netlist_graph(build_chain(2), legend=False).to_dot(),
        )
        self.assertNotIn(
            "cluster_legend",
            extract.build_dag_graph(build_chain(2), legend=False).graph.to_dot(),
        )

    def test_the_html_legend_covers_the_same_vocabulary(self):
        terms = " ".join(term + " " + meaning for term, meaning in extract.LEGEND_ROWS)
        for expected in ("combinational", "flip-flop", "tie-off", "cut", "loop", "level"):
            self.assertIn(expected, terms)


class TestScopeSelection(unittest.TestCase):
    def test_collect_module_gates_recursive(self):
        leaf = StubModule(2, "leaf", gates=[StubGate(10, "x")])
        root = StubModule(1, "root", gates=[StubGate(11, "y")], submodules=[leaf])
        self.assertEqual(len(extract.collect_module_gates(root)), 1)
        self.assertEqual(len(extract.collect_module_gates(root, recursive=True)), 2)

    def test_neighborhood_depth(self):
        gates = build_chain(5)
        self.assertEqual(len(extract.collect_neighborhood([gates[0]], 0)), 1)
        self.assertEqual(len(extract.collect_neighborhood([gates[0]], 1)), 2)
        self.assertEqual(len(extract.collect_neighborhood([gates[0]], 4)), 5)
        self.assertEqual(len(extract.collect_neighborhood([gates[0]], 99)), 5)

    def test_neighborhood_direction(self):
        gates = build_chain(3)
        middle = gates[1]
        self.assertEqual(
            len(extract.collect_neighborhood([middle], 1, "successors")), 2
        )
        self.assertEqual(
            len(extract.collect_neighborhood([middle], 1, "predecessors")), 2
        )
        self.assertEqual(len(extract.collect_neighborhood([middle], 1, "both")), 3)

    def test_neighborhood_rejects_bad_direction(self):
        self.assertRaises(
            ValueError, extract.collect_neighborhood, [], 1, "sideways"
        )

    def test_neighborhood_enforces_budget(self):
        gates = build_chain(10)
        self.assertRaises(
            extract.ScopeTooLarge,
            extract.collect_neighborhood,
            [gates[0]],
            9,
            "both",
            3,
        )


class TestModuleTree(unittest.TestCase):
    def _hierarchy(self):
        leaf_a = StubModule(3, "alu", "ALU", gates=[StubGate(1, "g1")])
        leaf_b = StubModule(4, "regs", gates=[StubGate(2, "g2")])
        mid = StubModule(2, "core", gates=[], submodules=[leaf_a, leaf_b])
        return StubModule(1, "top", gates=[StubGate(3, "g3")], submodules=[mid])

    def test_full_tree(self):
        graph = extract.build_module_tree_graph(self._hierarchy())
        source = graph.to_dot()
        self.assertEqual(graph.node_count, 4)
        self.assertEqual(graph.edge_count, 3)
        self.assertIn('"m1" -> "m2"', source)
        self.assertIn('"m2" -> "m3"', source)
        self.assertIn("<ALU>", source)

    def test_depth_limit_marks_truncation(self):
        graph = extract.build_module_tree_graph(self._hierarchy(), max_depth=1)
        source = graph.to_dot()
        self.assertIn("submodule(s) hidden", source)
        self.assertNotIn('"m3"', source)

    def test_gate_counts(self):
        source = extract.build_module_tree_graph(self._hierarchy()).to_dot()
        self.assertIn("1 gate (3 total)", source)
        bare = extract.build_module_tree_graph(
            self._hierarchy(), show_gate_counts=False
        ).to_dot()
        self.assertNotIn("gates", bare)


# ---------------------------------------------------------------------------
# render.py / cli.py helpers
# ---------------------------------------------------------------------------


class TestRenderHelpers(unittest.TestCase):
    def test_missing_binary_raises_actionable_error(self):
        try:
            render.render_dot("x.dot", "x.svg", "svg", dot_binary=None)
        except render.RenderError as exc:
            if render.find_dot_binary() is None:
                self.assertIn("Graphviz", str(exc))
            return
        # Graphviz is installed here and rendered a nonexistent file: also fine
        # as long as it raised, which the except above covers.

    def test_unknown_format_rejected(self):
        self.assertRaises(
            render.RenderError, render.render_dot, "x.dot", "x.foo", "foo"
        )

    def test_unknown_engine_rejected(self):
        self.assertRaises(
            render.RenderError, render.render_dot, "x.dot", "x.svg", "svg", None, "nope"
        )

    def test_format_none_is_a_no_op(self):
        self.assertIsNone(render.render_dot("x.dot", "x", "none"))

    def test_html_index(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "index.html")
            render.write_html_index(
                path, 'design "x"', [("graph", os.path.join(tmp, "g.svg"))]
            )
            with open(path, encoding="utf-8") as handle:
                page = handle.read()
        self.assertIn("&quot;x&quot;", page)
        self.assertIn('data="g.svg"', page)


class TestStandaloneHtmlPage(unittest.TestCase):
    """``--html`` on ``dag`` writes one file that opens offline, on its own."""

    SVG = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/svg11.dtd">\n'
        '<!-- Generated by graphviz -->\n'
        '<svg width="100pt" height="50pt"><title>dag</title>'
        '<text>state_reg</text></svg>\n'
    )

    def _page(self, **kwargs):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            svg_path = os.path.join(tmp, "dag.svg")
            with open(svg_path, "w", encoding="utf-8") as handle:
                handle.write(self.SVG)
            path = os.path.join(tmp, "dag.html")
            kwargs.setdefault("svg_path", svg_path)
            kwargs.setdefault("caption", "Whole netlist.")
            kwargs.setdefault(
                "stats", [("gates", 4), ("edges", 5), ("levels", 2)]
            )
            kwargs.setdefault("legend", extract.LEGEND_ROWS)
            render.write_html_page(path, 'design "x" dag', **kwargs)
            with open(path, encoding="utf-8") as handle:
                return handle.read()

    def test_page_structure(self):
        page = self._page()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("<title>design &quot;x&quot; dag</title>", page)
        self.assertIn("<style>", page)
        self.assertIn("</main></body></html>", page)

    def test_the_svg_is_inlined_without_its_xml_prologue(self):
        page = self._page()
        self.assertIn("<svg width=\"100pt\"", page)
        self.assertIn("state_reg", page)
        self.assertNotIn("<?xml", page)
        self.assertNotIn("<!DOCTYPE svg", page)
        self.assertNotIn("<img", page)
        self.assertNotIn("<object", page)

    def test_offline_safe(self):
        page = self._page()
        self.assertNotIn("<script", page)
        self.assertNotIn("http://", page.replace("http://www.w3.org", ""))
        self.assertNotIn("https://", page)
        self.assertNotIn("<link", page)

    def test_caption_and_counts(self):
        page = self._page()
        self.assertIn("Whole netlist.", page)
        self.assertIn("<b>4</b> gates", page)
        self.assertIn("<b>2</b> levels", page)

    def test_legend_is_on_the_page(self):
        page = self._page()
        self.assertIn("<h2>Legend</h2>", page)
        self.assertIn("combinational gate", page)
        self.assertIn("constant tie-off", page)
        self.assertIn("cut at a register", page)

    def test_warnings_are_visible(self):
        page = self._page(warnings=["2 gate(s) form a combinational loop"])
        self.assertIn('class="warning"', page)
        self.assertIn("combinational loop", page)

    def test_without_an_svg_the_dot_is_linked(self):
        page = self._page(svg_path=None, fallback="/tmp/out/dag.dot")
        self.assertIn('href="dag.dot"', page)
        self.assertNotIn("<svg", page)

    def test_hostile_text_is_escaped(self):
        page = self._page(caption="<script>alert(1)</script>")
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)


class TestOutputResolution(unittest.TestCase):
    def test_extension_selects_format(self):
        base, fmt = cli._resolve_output("out/graph.png", "stem", None)
        self.assertEqual(fmt, "png")
        self.assertEqual(os.path.basename(base), "graph")

    def test_explicit_format_wins(self):
        _base, fmt = cli._resolve_output("out/graph.png", "stem", "pdf")
        self.assertEqual(fmt, "pdf")

    def test_dot_extension_defaults_to_svg(self):
        base, fmt = cli._resolve_output("out/graph.dot", "stem", None)
        self.assertEqual(fmt, "svg")
        self.assertEqual(os.path.basename(base), "graph")

    def test_bare_name_is_used_as_base(self):
        base, fmt = cli._resolve_output("mygraph", "stem", None)
        self.assertEqual(os.path.basename(base), "mygraph")
        self.assertEqual(fmt, "svg")

    def test_trailing_separator_means_directory(self):
        base, _fmt = cli._resolve_output("out/", "stem", None)
        self.assertEqual(os.path.basename(base), "stem")

    def test_unknown_suffix_is_kept(self):
        base, _fmt = cli._resolve_output("report.v1", "stem", None)
        self.assertEqual(os.path.basename(base), "report.v1")


class TestParser(unittest.TestCase):
    def test_subcommands_exist(self):
        parser = cli.build_parser()
        for command in ("netlist_graph", "dag", "module_tree", "dataflow", "clock_tree"):
            args = parser.parse_args([command, "some/netlist.hal"])
            self.assertEqual(args.netlist, "some/netlist.hal")
            self.assertTrue(callable(args.func))

    def test_dag_reuses_the_shared_options(self):
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "dag",
                "some/netlist.hal",
                "-o", "out/d.svg",
                "-f", "svg",
                "--engine", "sfdp",
                "--html",
                "-g", "lib.hgl",
                "--hal-lib", "/build/lib",
                "-q",
                "--traceback",
                "--module", "top",
                "--max-gates", "50",
            ]
        )
        self.assertEqual(cli.cmd_dag, args.func)
        self.assertEqual("out/d.svg", args.output)
        self.assertEqual("svg", args.format)
        self.assertEqual("sfdp", args.engine)
        self.assertTrue(args.html)
        self.assertEqual("lib.hgl", args.gate_library)
        self.assertEqual(["/build/lib"], args.hal_lib)
        self.assertTrue(args.quiet)
        self.assertTrue(args.traceback)
        self.assertEqual("top", args.module)
        self.assertEqual(50, args.max_gates)
        self.assertEqual("LR", args.rankdir)
        self.assertFalse(args.no_legend)
        self.assertFalse(args.const_hub)

    def test_the_vocabulary_switches_exist_on_both_graph_commands(self):
        parser = cli.build_parser()
        for command in ("netlist_graph", "dag"):
            args = parser.parse_args(
                [command, "x.hal", "--no-legend", "--const-hub"]
            )
            self.assertTrue(args.no_legend)
            self.assertTrue(args.const_hub)

    def test_no_command_prints_help(self):
        self.assertEqual(cli.main([]), 2)

    def test_missing_hal_py_is_reported_not_raised(self):
        # There is no built HAL on this machine, so this must exit non-zero
        # with a readable message rather than blowing up with a traceback.
        for command in ("module_tree", "dag"):
            code = cli.main(
                [command, "nonexistent.hal", "--hal-lib", "/definitely/not/here"]
            )
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
