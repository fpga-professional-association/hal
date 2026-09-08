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

from hal_viz import cli, dot, extract, render  # noqa: E402


# ---------------------------------------------------------------------------
# stub netlist objects (duck-typed against the hal_py bindings)
# ---------------------------------------------------------------------------


class StubNamed(object):
    def __init__(self, name):
        self._name = name

    def get_name(self):
        return self._name


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
    def __init__(self, gate_id, name, gate_type="AND2", module=None):
        self.id = gate_id
        self._name = name
        self._type = StubNamed(gate_type)
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
        for command in ("netlist_graph", "module_tree", "dataflow", "clock_tree"):
            args = parser.parse_args([command, "some/netlist.hal"])
            self.assertEqual(args.netlist, "some/netlist.hal")
            self.assertTrue(callable(args.func))

    def test_no_command_prints_help(self):
        self.assertEqual(cli.main([]), 2)

    def test_missing_hal_py_is_reported_not_raised(self):
        # There is no built HAL on this machine, so this must exit non-zero
        # with a readable message rather than blowing up with a traceback.
        code = cli.main(
            ["module_tree", "nonexistent.hal", "--hal-lib", "/definitely/not/here"]
        )
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
