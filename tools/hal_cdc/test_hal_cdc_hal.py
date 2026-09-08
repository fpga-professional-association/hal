"""Integration tests that need a built HAL; skipped everywhere else.

``test_hal_cdc.py`` reads the fixtures with :mod:`hal_cdc.fixture_netlist`, a
small dependency-free reader that exists so the analysis can be tested on a
machine that cannot build HAL. That reader is *not* the authority on what a
fixture means -- HAL's ``verilog_parser`` is.

This module closes the gap. For every fixture it:

1. loads the same ``.v`` and the same ``example_library.hgl`` through
   ``hal_py.NetlistFactory.load_netlist``;
2. builds a :class:`~hal_cdc.netlist_view.NetlistView` with
   :func:`hal_cdc.netlist_view.from_hal_netlist` -- the code path a real run
   uses -- and runs the same audit;
3. asserts the ground truth from ``fixtures/README.md`` *and* asserts that the
   audit signature is byte-for-byte the one the fixture reader produces.

So a divergence between the two readers, or a renamed ``hal_py`` binding, fails
a build instead of quietly changing an answer.

Run inside the project's verification container, from the repo root::

    export HAL_PY_PATH=/path/to/hal/build/lib
    export HAL_BASE_PATH=/path/to/hal/build
    python -m unittest discover -s tools/hal_cdc -t tools -p "test_*_hal.py"

``HAL_CDC_OUTPUT_DIR`` keeps the produced findings documents and DOT files (as
a CI artifact) instead of discarding them.
"""

import os
import sys
import unittest

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_findings import serialize, validate  # noqa: E402

from hal_cdc import audit as audit_module  # noqa: E402
from hal_cdc import clock_tree, declarations, patterns, report, resets  # noqa: E402
from hal_cdc.domains import KIND_DERIVED, Limits  # noqa: E402
from hal_cdc.netlist_view import from_hal_netlist  # noqa: E402
from hal_cdc.test_hal_cdc import (  # noqa: E402
    FIXTURES,
    GATE_LIBRARY,
    audit_signature,
    run_fixture,
)

OUTPUT_ENV = "HAL_CDC_OUTPUT_DIR"

FIXTURE_NAMES = ("two_flop_sync", "direct_crossing", "ambiguous_clock", "reset_release")


def _requirements():
    try:
        from hal_viz import halenv
    except ImportError as exc:
        return None, "tools/hal_viz is not importable: {}".format(exc)
    try:
        hal_py = halenv.import_hal_py()
    except Exception as exc:  # noqa: BLE001
        return None, "hal_py unavailable: {}".format(exc)
    if not os.path.isfile(GATE_LIBRARY):
        return None, "{} is missing".format(GATE_LIBRARY)
    return (hal_py, halenv), None


_REQUIREMENTS, _SKIP_REASON = _requirements()


def _load(name):
    """Load a fixture through HAL and run the audit on it."""
    hal_py, _halenv = _REQUIREMENTS
    netlist = hal_py.NetlistFactory.load_netlist(
        os.path.join(FIXTURES, name + ".v"), GATE_LIBRARY
    )
    if netlist is None:
        raise AssertionError(
            "hal_py.NetlistFactory.load_netlist() could not parse {}.v against {}; see the "
            "HAL log above. The fixture and the library are both in this repository, so "
            "this is a real failure, not a setup problem.".format(name, GATE_LIBRARY)
        )
    view = from_hal_netlist(netlist)
    document = declarations.load(os.path.join(FIXTURES, name + ".json"))
    bound = document.bind(view)
    result = audit_module.run_audit(view, bound, limits=Limits())
    return netlist, view, bound, result


@unittest.skipIf(_REQUIREMENTS is None, _SKIP_REASON or "hal_py unavailable")
class HalParserAgreesWithFixtureReaderTest(unittest.TestCase):
    """The two readers must produce identical audits."""

    def test_netlist_shape_matches(self):
        for name in FIXTURE_NAMES:
            _netlist, hal_view, _bound, _result = _load(name)
            fixture_view, _fbound, _fresult = run_fixture(name)
            self.assertEqual(
                hal_view.gate_type_histogram(),
                fixture_view.gate_type_histogram(),
                "{}: HAL and the fixture reader disagree on the gate types".format(name),
            )
            self.assertEqual(
                sorted(gate.name for gate in hal_view.sorted_gates()),
                sorted(gate.name for gate in fixture_view.sorted_gates()),
                "{}: instance names differ".format(name),
            )

    def test_audit_signature_matches(self):
        for name in FIXTURE_NAMES:
            _netlist, hal_view, _bound, hal_result = _load(name)
            fixture_view, _fbound, fixture_result = run_fixture(name)
            self.assertEqual(
                audit_signature(hal_view, hal_result),
                audit_signature(fixture_view, fixture_result),
                "{}: the audit differs between HAL's parser and the fixture "
                "reader".format(name),
            )

    def test_declarations_bind_without_problems(self):
        for name in FIXTURE_NAMES:
            _netlist, _view, bound, _result = _load(name)
            self.assertEqual(bound.problems, [], "{}: {}".format(name, bound.problems))


@unittest.skipIf(_REQUIREMENTS is None, _SKIP_REASON or "hal_py unavailable")
class GroundTruthThroughHalTest(unittest.TestCase):
    """The ground truth of fixtures/README.md, re-asserted through hal_py."""

    def test_two_flop_synchronizer_is_recognised(self):
        _netlist, view, _bound, result = _load("two_flop_sync")
        self.assertEqual(result.domains, ["clk_a", "clk_b"])
        self.assertEqual(len(result.crossings), 1)
        entry = result.crossings[0]
        self.assertEqual(entry.classification.name, patterns.CLASS_TWO_FLOP)
        self.assertEqual(
            [view.gate(gid).name for gid in entry.classification.stage_gate_ids],
            ["sync_meta_reg", "sync_out_reg"],
        )
        self.assertEqual(result.unknown_inputs, [])

    def test_direct_crossings_are_all_flagged(self):
        _netlist, view, _bound, result = _load("direct_crossing")
        self.assertEqual(len(result.crossings), 4)
        self.assertEqual(len(result.alarming_crossings), 4)
        by_destination = {
            view.gate(entry.crossing.destination_gate_id).name: entry
            for entry in result.crossings
        }
        self.assertEqual(
            by_destination["en_reg"].classification.name,
            patterns.CLASS_UNSYNCHRONIZED_CONTROL,
        )
        for name in ("single_reg", "meta_reg", "logic_reg"):
            self.assertEqual(
                by_destination[name].classification.name, patterns.CLASS_UNSYNCHRONIZED, name
            )
        self.assertFalse(by_destination["logic_reg"].crossing.direct)
        self.assertEqual(
            [
                view.gate(gid).name
                for gid in by_destination["logic_reg"].crossing.path_gate_ids
            ],
            ["a_xor"],
        )

    def test_ambiguous_clock_stays_unknown(self):
        _netlist, view, _bound, result = _load("ambiguous_clock")
        resolutions = {
            view.gate(gate_id).name: resolution
            for gate_id, resolution in result.clock_resolutions.items()
        }
        self.assertIsNone(resolutions["mux_reg"].domain)
        self.assertTrue(resolutions["mux_reg"].ambiguous)
        self.assertEqual(resolutions["mux_reg"].reached_clocks, ("clk_a", "clk_b"))
        self.assertEqual(resolutions["gated_reg"].domain, "clk_a")
        self.assertEqual(resolutions["gated_reg"].kind, KIND_DERIVED)
        self.assertEqual(result.crossings, [])

    def test_reset_release_classification(self):
        _netlist, view, _bound, result = _load("reset_release")
        targets = {view.gate(t.gate_id).name: t for t in result.reset_targets}
        self.assertEqual(
            sorted(targets), ["a_reg", "b_reg", "rst_sync_reg1", "rst_sync_reg2"]
        )
        self.assertEqual(targets["a_reg"].release, resets.RELEASE_SYNCHRONIZED)
        self.assertEqual(targets["b_reg"].release, resets.RELEASE_ASYNCHRONOUS)
        self.assertTrue(targets["b_reg"].is_alarming)
        for name in ("rst_sync_reg1", "rst_sync_reg2"):
            self.assertEqual(targets[name].release, resets.RELEASE_SYNCHRONIZER_STAGE)
        self.assertEqual(result.reset_domain_map["arst"], ["clk_a", "clk_b"])

    def test_waivers_apply_through_hal(self):
        hal_py, _halenv = _REQUIREMENTS
        netlist = hal_py.NetlistFactory.load_netlist(
            os.path.join(FIXTURES, "direct_crossing.v"), GATE_LIBRARY
        )
        self.assertIsNotNone(netlist)
        view = from_hal_netlist(netlist)
        bound = declarations.load(
            os.path.join(FIXTURES, "direct_crossing_waived.json")
        ).bind(view)
        result = audit_module.run_audit(view, bound, limits=Limits())
        waived = [entry for entry in result.crossings if entry.waiver is not None]
        self.assertEqual(len(waived), 1)
        self.assertEqual(
            view.gate(waived[0].crossing.destination_gate_id).name, "logic_reg"
        )
        self.assertEqual(len(result.alarming_crossings), 3)
        self.assertEqual([w.id for w in result.unused_waivers], ["W-STALE"])


@unittest.skipIf(_REQUIREMENTS is None, _SKIP_REASON or "hal_py unavailable")
class DocumentThroughHalTest(unittest.TestCase):
    def test_documents_validate_and_are_written(self):
        output_dir = os.environ.get(OUTPUT_ENV)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        for name in FIXTURE_NAMES:
            _netlist, _view, _bound, result = _load(name)
            document = report.build_document(
                result, netlist_path=os.path.join(FIXTURES, name + ".v")
            )
            validate.validate_document(document)
            validate.validate_document(document, prefer_jsonschema=True)
            artifact = document["artifacts"][0]
            self.assertIn("sha256", artifact, "{}: the .v fixture must be hashed".format(name))
            self.assertTrue(
                any(
                    finding["id"] == "cdc/limitations" and finding["status"] == "unsupported"
                    for finding in document["findings"]
                ),
                "{}: the limitations finding is missing".format(name),
            )
            for finding in document["findings"]:
                self.assertIn(
                    finding["status"], ("heuristic", "unknown", "unsupported", "error")
                )
            if output_dir:
                serialize.write_document(
                    document, os.path.join(output_dir, name + ".findings.json")
                )
                report.build_domain_graph(result).write(
                    os.path.join(output_dir, name + ".domains.dot")
                )


@unittest.skipIf(_REQUIREMENTS is None, _SKIP_REASON or "hal_py unavailable")
class ClockTreeExtractorCrossCheckTest(unittest.TestCase):
    """The clock_tree_extractor integration must never break the audit."""

    def test_cross_check_runs_or_degrades_cleanly(self):
        hal_py, halenv = _REQUIREMENTS
        halenv.load_all_plugins(hal_py)
        netlist = hal_py.NetlistFactory.load_netlist(
            os.path.join(FIXTURES, "two_flop_sync.v"), GATE_LIBRARY
        )
        self.assertIsNotNone(netlist)
        view = from_hal_netlist(netlist)
        bound = declarations.load(os.path.join(FIXTURES, "two_flop_sync.json")).bind(view)

        output_dir = os.environ.get(OUTPUT_ENV)
        dot_path = os.path.join(output_dir, "two_flop_sync.clock_tree.dot") if output_dir else None
        info = clock_tree.extract_clock_tree(netlist, dot_path=dot_path)
        self.assertIsInstance(info, clock_tree.ClockTreeInfo)

        result = audit_module.run_audit(view, bound, limits=Limits(), clock_tree=info)
        # The cross-check is corroborating evidence only: hal_cdc's own answer
        # must be the same with and without it.
        without = audit_module.run_audit(view, bound, limits=Limits())
        self.assertEqual(
            audit_signature(view, result), audit_signature(view, without)
        )

        document = report.build_document(result)
        validate.validate_document(document)
        cross_check = [
            finding
            for finding in document["findings"]
            if finding["id"].startswith("cdc/clock-tree/")
        ]
        self.assertEqual(len(cross_check), 1)
        if info.available:
            self.assertEqual(cross_check[0]["id"], "cdc/clock-tree/cross-check")
            self.assertEqual(cross_check[0]["status"], "heuristic")
            self.assertIn("clock_tree_gates", cross_check[0]["metrics"])
        else:
            self.assertEqual(cross_check[0]["id"], "cdc/clock-tree/unavailable")
            self.assertEqual(cross_check[0]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
