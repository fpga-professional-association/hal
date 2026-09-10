#!/usr/bin/env python3
"""Generation <-> recovery round-trip corpus suite (issue #49, plus #54's acceptance test).

What this guards
----------------
HAL's walkthrough corpus is the closest thing this fork has to ground truth: eight
Agilex 3 designs whose gate-level facts are published and independently checked by
each walkthrough's ``check.py``.  This suite pins the *plumbing* around them:

  1. writer/parser round trip  -- import a netlist, write it back out with the
     Verilog writer, re-import the result, and require the two netlists to be
     structurally the same circuit.
  2. ``.hal`` project round trip -- import, serialize to the ``.hal`` project
     netlist format, deserialize, same structural equality.
  3. recovery invariants -- the published, check.py-verified facts of each
     corpus entry still hold when the netlist is loaded through ``hal_py``.
  4. issue #54 acceptance -- two netlists that HAL can import but cannot yet save
     and reload.  These are marked ``expectedFailure``; when #54 lands they turn
     into *unexpected successes*, which unittest reports as a failing run.  That
     is deliberate: the suite is green today and goes loudly red the moment the
     bug is fixed and these guards need to be promoted to real assertions.

On names
--------
Round-trip equality here is **structural, not nominal**.  A writer is entitled to
rename internal nets, so nothing below asserts that an internal net or gate kept
its name.  Ports are the exception -- primary input and output names are part of
the design's interface and are asserted by name where the walkthrough publishes
them.  See ``fan_in_signature`` for how the structural comparison avoids names.

Running it
----------
    export HAL_BASE_PATH=<build dir>
    export PYTHONPATH=<build dir>/lib
    python3 tests/roundtrip/test_roundtrip.py
"""

import collections
import json
import os
import subprocess
import sys
import tempfile
import unittest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(THIS_DIR))
MANIFEST = os.path.join(THIS_DIR, "corpus.json")

try:
    import hal_py
except ImportError as exc:  # pragma: no cover - environment problem, not a test failure
    raise SystemExit(
        "cannot import hal_py (%s).\n"
        "Put HAL's library directory on PYTHONPATH and point HAL_BASE_PATH at the\n"
        "build directory, e.g.:\n"
        "    export HAL_BASE_PATH=/work/build PYTHONPATH=/work/build/lib" % exc
    )

hal_py.plugin_manager.load_all_plugins()

# Plugin bindings live in their own package, not in hal_py.
from hal_plugins import graph_algorithm  # noqa: E402

with open(MANIFEST) as _fh:
    _CORPUS = json.load(_fh)

GATE_LIBRARY_PATH = os.path.join(REPO_ROOT, _CORPUS["gate_library"])
ENTRIES = _CORPUS["entries"]

# A second, unrelated gate library, used only by the #54 acceptance tests to build
# a netlist whose cells do not all come from AGILEX_TENNM.
EXAMPLE_LIBRARY_PATH = os.path.join(
    REPO_ROOT, "plugins/gate_libraries/definitions/example_library.hgl"
)


# ---------------------------------------------------------------------------
# structural fingerprints
# ---------------------------------------------------------------------------


def gate_type_histogram(netlist):
    """{gate type name: count} over all gates."""
    counts = collections.Counter(g.get_type().get_name() for g in netlist.get_gates())
    return dict(sorted(counts.items()))


def flip_flop_count(netlist):
    return sum(
        1
        for g in netlist.get_gates()
        if g.get_type().has_property(hal_py.GateTypeProperty.ff)
    )


def port_names(netlist):
    """(global input names, global output names), both sorted.

    Ports are the one naming contract a writer must honour, so these *are*
    compared across a round trip.
    """
    return (
        sorted(n.get_name() for n in netlist.get_global_input_nets()),
        sorted(n.get_name() for n in netlist.get_global_output_nets()),
    )


def _driver_descriptor(netlist, net):
    """A name-free description of what feeds a pin.

    Internal net names are not part of the round-trip contract, so a net is
    described by *what drives it* and by its arity instead of by its name.  A
    primary input is the exception: it is described by its port name, because
    port names are preserved by contract.
    """
    if net is None:
        return ("<unconnected>",)
    if net.is_global_input_net():
        return ("<port>", net.get_name())
    sources = net.get_sources()
    if not sources:
        return ("<undriven>", len(net.get_destinations()))
    source = sources[0]
    return (
        source.get_gate().get_type().get_name(),
        source.get_pin().get_name(),
        len(sources),
        len(net.get_destinations()),
    )


def fan_in_signature(netlist):
    """Order-insensitive structural fingerprint of the whole netlist.

    A multiset over gates of ``(gate type, sorted (input pin -> driver))``.  Two
    netlists with the same signature agree on every gate's type and on where each
    of its input pins is fed from, without agreeing on a single internal name.
    """
    counts = collections.Counter()
    for gate in netlist.get_gates():
        fan_in = tuple(
            sorted(
                (
                    pin.get_name(),
                    _driver_descriptor(netlist, gate.get_fan_in_net(pin.get_name())),
                )
                for pin in gate.get_type().get_input_pins()
            )
        )
        counts[(gate.get_type().get_name(), fan_in)] += 1
    return counts


def multi_gate_scc_sizes(netlist):
    """Sizes of the gate graph's strongly connected components with > 1 vertex.

    A purely combinational circuit is a DAG, so every such component is a
    feedback loop -- i.e. state.  Returned descending, so it compares as a
    multiset regardless of the order igraph happens to produce.
    """
    graph = graph_algorithm.NetlistGraph.from_netlist(netlist)
    if graph is None:
        raise AssertionError("NetlistGraph.from_netlist returned None")
    components = graph_algorithm.get_connected_components(graph, True, 2)
    if components is None:
        raise AssertionError("graph_algorithm.get_connected_components failed")
    return sorted(
        (len(graph.get_gates_from_vertices(list(c))) for c in components), reverse=True
    )


# ---------------------------------------------------------------------------
# corpus loading
# ---------------------------------------------------------------------------

_GATE_LIBRARY = None
_NETLIST_CACHE = {}


def gate_library():
    global _GATE_LIBRARY
    if _GATE_LIBRARY is None:
        _GATE_LIBRARY = hal_py.GateLibraryManager.load(GATE_LIBRARY_PATH)
        if _GATE_LIBRARY is None:
            raise AssertionError("could not load gate library %s" % GATE_LIBRARY_PATH)
    return _GATE_LIBRARY


def load_corpus_netlist(entry):
    """Import a corpus entry; cached, since every test class re-reads the same file."""
    if entry not in _NETLIST_CACHE:
        path = os.path.join(REPO_ROOT, ENTRIES[entry]["netlist"])
        netlist = hal_py.NetlistFactory.load_netlist(path, gate_library())
        if netlist is None:
            raise AssertionError("could not import %s" % path)
        _NETLIST_CACHE[entry] = netlist
    return _NETLIST_CACHE[entry]


# ---------------------------------------------------------------------------
# the parametrized tests
# ---------------------------------------------------------------------------


class CorpusRoundTrip(object):
    """Mixin holding the per-corpus-entry tests.

    One concrete ``unittest.TestCase`` subclass is generated per corpus entry at
    the bottom of this module, so a failure names the walkthrough it came from.
    """

    ENTRY = None
    SPEC = None

    # -- helpers ----------------------------------------------------------

    def netlist(self):
        return load_corpus_netlist(self.ENTRY)

    def assertSameCircuit(self, original, restored, what):
        """Structural equality: same gates, same types, same wiring, same ports."""
        self.assertEqual(
            len(restored.get_gates()),
            len(original.get_gates()),
            "%s: gate count changed" % what,
        )
        self.assertEqual(
            len(restored.get_nets()),
            len(original.get_nets()),
            "%s: net count changed" % what,
        )
        self.assertEqual(
            gate_type_histogram(restored),
            gate_type_histogram(original),
            "%s: gate-type histogram changed" % what,
        )
        self.assertEqual(
            len(restored.get_global_input_nets()),
            len(original.get_global_input_nets()),
            "%s: global input count changed" % what,
        )
        self.assertEqual(
            len(restored.get_global_output_nets()),
            len(original.get_global_output_nets()),
            "%s: global output count changed" % what,
        )
        # Ports -- and only ports -- are compared by name.  Internal net and gate
        # names are explicitly outside the round-trip contract.
        self.assertEqual(
            port_names(restored), port_names(original), "%s: port names changed" % what
        )
        self.assertEqual(
            fan_in_signature(restored),
            fan_in_signature(original),
            "%s: per-gate fan-in structure changed" % what,
        )

    # -- 1. writer / parser round trip ------------------------------------

    def test_verilog_writer_round_trip(self):
        """import -> Verilog writer -> re-import yields the same circuit."""
        original = self.netlist()
        with tempfile.TemporaryDirectory() as tmp:
            written = os.path.join(tmp, "%s.v" % self.ENTRY)
            self.assertTrue(
                hal_py.NetlistWriterManager.write(original, written),
                "the Verilog writer refused to write %s" % self.ENTRY,
            )
            reimported = hal_py.NetlistFactory.load_netlist(written, gate_library())
            self.assertIsNotNone(
                reimported, "could not re-import the written Verilog for %s" % self.ENTRY
            )
            self.assertSameCircuit(original, reimported, "verilog round trip")

    # -- 2. .hal project round trip ---------------------------------------

    def test_hal_project_round_trip(self):
        """import -> .hal project netlist -> load yields the same circuit.

        This is the single-gate-library case, which works today.  The
        multi-library and black-box cases are issue #54, below.
        """
        original = self.netlist()
        with tempfile.TemporaryDirectory() as tmp:
            saved = os.path.join(tmp, "%s.hal" % self.ENTRY)
            self.assertTrue(
                hal_py.NetlistSerializer.serialize_to_file(original, saved),
                "could not serialize %s to a .hal file" % self.ENTRY,
            )
            reloaded = hal_py.NetlistSerializer.deserialize_from_file(
                saved, gate_library()
            )
            self.assertIsNotNone(
                reloaded, "could not deserialize the .hal file for %s" % self.ENTRY
            )
            self.assertSameCircuit(original, reloaded, ".hal round trip")

    # -- 3. recovery invariants -------------------------------------------

    def test_published_gate_counts(self):
        """The gate count and gate-type histogram the walkthrough publishes."""
        netlist = self.netlist()
        self.assertEqual(len(netlist.get_gates()), self.SPEC["gates"])
        self.assertEqual(gate_type_histogram(netlist), self.SPEC["gate_types"])
        self.assertEqual(flip_flop_count(netlist), self.SPEC["ff"])

    def test_published_net_and_port_counts(self):
        """The net and port counts the walkthrough publishes, where it publishes them."""
        netlist = self.netlist()
        checked = False
        if "nets" in self.SPEC:
            self.assertEqual(len(netlist.get_nets()), self.SPEC["nets"])
            checked = True
        if "global_inputs" in self.SPEC:
            self.assertEqual(
                len(netlist.get_global_input_nets()), self.SPEC["global_inputs"]
            )
            checked = True
        if "global_outputs" in self.SPEC:
            self.assertEqual(
                len(netlist.get_global_output_nets()), self.SPEC["global_outputs"]
            )
            checked = True
        inputs, outputs = port_names(netlist)
        if "global_input_names" in self.SPEC:
            self.assertEqual(inputs, self.SPEC["global_input_names"])
            checked = True
        if "global_output_names" in self.SPEC:
            self.assertEqual(outputs, self.SPEC["global_output_names"])
            checked = True
        if not checked:
            self.skipTest(
                "%s publishes no net or port counts (%s)"
                % (self.ENTRY, self.SPEC.get("omitted", ""))
            )

    def test_published_feedback_structure(self):
        """The gate-graph SCCs the walkthrough publishes -- its state, found structurally."""
        if "sccs" not in self.SPEC:
            self.skipTest(
                "%s publishes no gate-level SCC (%s)"
                % (self.ENTRY, self.SPEC.get("omitted", ""))
            )
        self.assertEqual(multi_gate_scc_sizes(self.netlist()), self.SPEC["sccs"])

    def test_feedback_structure_survives_round_trips(self):
        """The recovered state structure is a property of the circuit, not of the file.

        Whatever SCCs the netlist has -- published or not -- both round trips have
        to preserve them, otherwise "recover from a saved project" would not mean
        the same thing as "recover from the original netlist".
        """
        original = self.netlist()
        expected = multi_gate_scc_sizes(original)
        with tempfile.TemporaryDirectory() as tmp:
            written = os.path.join(tmp, "%s.v" % self.ENTRY)
            self.assertTrue(hal_py.NetlistWriterManager.write(original, written))
            reimported = hal_py.NetlistFactory.load_netlist(written, gate_library())
            self.assertIsNotNone(reimported)
            self.assertEqual(
                multi_gate_scc_sizes(reimported),
                expected,
                "feedback structure changed across the verilog round trip",
            )

            saved = os.path.join(tmp, "%s.hal" % self.ENTRY)
            self.assertTrue(hal_py.NetlistSerializer.serialize_to_file(original, saved))
            reloaded = hal_py.NetlistSerializer.deserialize_from_file(
                saved, gate_library()
            )
            self.assertIsNotNone(reloaded)
            self.assertEqual(
                multi_gate_scc_sizes(reloaded),
                expected,
                "feedback structure changed across the .hal round trip",
            )


def _generate_corpus_test_cases():
    for entry in sorted(ENTRIES):
        name = "Test_%s" % entry
        globals()[name] = type(
            name,
            (CorpusRoundTrip, unittest.TestCase),
            {"ENTRY": entry, "SPEC": ENTRIES[entry]},
        )


_generate_corpus_test_cases()


# ---------------------------------------------------------------------------
# 4. issue #54 acceptance tests
# ---------------------------------------------------------------------------

_TWO_LIBRARY_NETLIST = """module top (net_global_in, net_global_out);
  input net_global_in;
  output net_global_out;
  wire net_0;
BUF gate_0 ( .I (net_global_in), .O (net_0) );
tennm_ff gate_1 ( .d (net_0), .q (net_global_out) );
endmodule
"""

_BLACK_BOX_NETLIST = """module top (net_global_in, net_global_out);
  input net_global_in;
  output net_global_out;
  wire net_0;
BUF gate_0 ( .I (net_global_in), .O (net_0) );
UNDEFINED_CELL cell_0 ( .A (net_0), .Z (net_global_out) );
endmodule
"""

# Reloads a .hal file in a *fresh* interpreter and reports whether it came back.
# Needed because the gate library manager caches libraries per process: a black
# box netlist reloads fine in the process that created it, since the synthesized
# black box gate types are still sitting in the cached library instance.  Only a
# new process re-parses the .hgl from disk and discovers they were never in it.
_FRESH_RELOAD_SNIPPET = """
import sys
import hal_py
hal_py.plugin_manager.load_all_plugins()
netlist = hal_py.NetlistSerializer.deserialize_from_file(sys.argv[1])
if netlist is None:
    sys.exit(1)
sys.exit(0 if len(netlist.get_gates()) == 2 else 2)
"""


def _reload_in_fresh_process(hal_file):
    env = os.environ.copy()
    hal_py_dir = os.path.dirname(os.path.abspath(hal_py.__file__))
    env["PYTHONPATH"] = os.pathsep.join(
        [hal_py_dir] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    result = subprocess.run(
        [sys.executable, "-c", _FRESH_RELOAD_SNIPPET, hal_file],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.returncode == 0


class MultiLibraryProjectRoundTrip(unittest.TestCase):
    """Issue #54: a netlist HAL can import but cannot save and reload.

    Both tests below are ``expectedFailure``.  They document, executably, the
    limitation the ``load_netlist`` docstring already warns about:

        "Such a netlist cannot be written to and read back from a .hal file yet,
         it has to be re-imported with the same search list and options."

    When #54 is fixed these become unexpected successes and the run goes red --
    at which point the ``expectedFailure`` decorators come off and these turn
    into ordinary round-trip tests.
    """

    def setUp(self):
        if not os.path.isfile(EXAMPLE_LIBRARY_PATH):
            self.skipTest("example_library.hgl not found at %s" % EXAMPLE_LIBRARY_PATH)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def _write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def test_search_list_import_is_supported(self):
        """Sanity check, not expected to fail: the *import* side already works.

        A cell that neither library alone defines is rejected; the ordered search
        list of both accepts it.  This is the precondition for the two tests
        below -- without it they would be failing for the wrong reason.
        """
        source = self._write("two_library.v", _TWO_LIBRARY_NETLIST)
        self.assertIsNone(
            hal_py.NetlistFactory.load_netlist(source, [EXAMPLE_LIBRARY_PATH], False),
            "a single library should not be able to instantiate a two-library netlist",
        )
        netlist = hal_py.NetlistFactory.load_netlist(
            source, [EXAMPLE_LIBRARY_PATH, GATE_LIBRARY_PATH], False
        )
        self.assertIsNotNone(netlist, "the two-library search list should import")
        self.assertTrue(netlist.get_gate_library().is_composite())
        self.assertEqual(len(netlist.get_gates()), 2)

    def test_black_box_fallback_import_is_supported(self):
        """Sanity check, not expected to fail: the black box fallback imports."""
        source = self._write("black_box.v", _BLACK_BOX_NETLIST)
        self.assertIsNone(
            hal_py.NetlistFactory.load_netlist(source, [EXAMPLE_LIBRARY_PATH], False),
            "an undefined cell should abort the import without the fallback",
        )
        netlist = hal_py.NetlistFactory.load_netlist(
            source, [EXAMPLE_LIBRARY_PATH], True
        )
        self.assertIsNotNone(netlist, "the black box fallback should import")
        self.assertEqual(
            sorted(g.get_type().get_name() for g in netlist.get_gates()),
            ["BUF", "UNDEFINED_CELL"],
        )

    @unittest.expectedFailure
    def test_54_composite_library_project_round_trip(self):
        """ISSUE #54 -- a composite-gate-library netlist does not survive save/reload.

        The .hal file records the composite library as the pseudo-path
        ``<multi>libA.hgl+libB.hgl``.  Nothing resolves that on the way back in:
        the gate library manager falls back to searching the default gate library
        directories for the *last* component's file name, loads that one library
        alone, and deserialization then fails on the first gate whose type came
        from the other library.  ``deserialize_from_file`` returns None.

        Passing the composite ``GateLibrary`` object explicitly to
        ``deserialize_from_file`` does work, which localizes the defect to the
        recorded path, not to the serialized netlist itself.
        """
        source = self._write("two_library.v", _TWO_LIBRARY_NETLIST)
        netlist = hal_py.NetlistFactory.load_netlist(
            source, [EXAMPLE_LIBRARY_PATH, GATE_LIBRARY_PATH], False
        )
        self.assertIsNotNone(netlist)
        saved = os.path.join(self.tmp, "two_library.hal")
        self.assertTrue(hal_py.NetlistSerializer.serialize_to_file(netlist, saved))

        reloaded = hal_py.NetlistSerializer.deserialize_from_file(saved)
        self.assertIsNotNone(
            reloaded,
            "issue #54: the composite gate library path in the .hal file does not resolve",
        )
        self.assertEqual(
            fan_in_signature(reloaded),
            fan_in_signature(netlist),
            "issue #54: the reloaded two-library netlist is not the same circuit",
        )

    @unittest.expectedFailure
    def test_54_black_box_fallback_project_round_trip(self):
        """ISSUE #54 -- a black box netlist does not survive save/reload either.

        The black box gate types the parser synthesizes for undefined cells are
        added to the in-memory gate library instance only; neither the .hgl on
        disk nor the .hal file records them, and the .hal file's gate library
        field points at the unmodified .hgl.  Reloading therefore has to happen
        in a fresh process to be observed -- within the writing process the gate
        library manager hands back the same mutated instance and the round trip
        appears to succeed.
        """
        source = self._write("black_box.v", _BLACK_BOX_NETLIST)
        netlist = hal_py.NetlistFactory.load_netlist(
            source, [EXAMPLE_LIBRARY_PATH], True
        )
        self.assertIsNotNone(netlist)
        saved = os.path.join(self.tmp, "black_box.hal")
        self.assertTrue(hal_py.NetlistSerializer.serialize_to_file(netlist, saved))

        self.assertTrue(
            _reload_in_fresh_process(saved),
            "issue #54: a black box netlist saved to .hal cannot be reloaded by a "
            "process that did not import it",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
