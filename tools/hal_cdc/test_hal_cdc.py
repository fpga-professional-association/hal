"""Unit tests for hal_cdc. No HAL, no hal_py, standard library only.

These are not stub tests: they read the *real* fixture Verilog and the *real*
``example_library.hgl`` through :mod:`hal_cdc.fixture_netlist`, run the *real*
analysis, and assert the ground truth written down in ``fixtures/README.md``.
``test_hal_cdc_hal.py`` re-asserts the same ground truth after loading the same
fixtures through HAL's own Verilog parser, so a divergence between the two
readers fails a build rather than changing an answer.

Run with::

    python -m unittest discover -s tools/hal_cdc -t tools -p "test_hal_cdc.py"
"""

import json
import os
import sys
import unittest

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from hal_findings import serialize, validate  # noqa: E402

from hal_cdc import audit as audit_module  # noqa: E402
from hal_cdc import cli, declarations, patterns, report, resets  # noqa: E402
from hal_cdc.crossings import ROLE_CONTROL, ROLE_DATA  # noqa: E402
from hal_cdc.domains import KIND_DECLARED, KIND_DERIVED, KIND_UNKNOWN, Limits  # noqa: E402
from hal_cdc.fixture_netlist import FixtureError, load_fixture  # noqa: E402

REPO_ROOT = os.path.dirname(_TOOLS_DIR)
FIXTURES = os.path.join(_TOOLS_DIR, "hal_cdc", "fixtures")
GATE_LIBRARY = os.path.join(
    REPO_ROOT, "plugins", "gate_libraries", "definitions", "example_library.hgl"
)


def run_fixture(name, declarations_name=None):
    """Load a fixture, bind its declarations and run the audit."""
    view = load_fixture(
        os.path.join(FIXTURES, name + ".v"), GATE_LIBRARY
    )
    document = declarations.load(
        os.path.join(FIXTURES, (declarations_name or name) + ".json")
    )
    bound = document.bind(view)
    return view, bound, audit_module.run_audit(view, bound, limits=Limits())


def by_name(view, result):
    """``{gate name: ClockResolution}`` for every sequential gate."""
    return {
        view.gate(gate_id).name: resolution
        for gate_id, resolution in result.clock_resolutions.items()
    }


def _names(view, gate_ids):
    return tuple(
        view.gate(gate_id).name for gate_id in gate_ids if view.gate(gate_id) is not None
    )


def audit_signature(view, result):
    """Everything an audit concluded, keyed by *name* rather than by HAL id.

    ``test_hal_cdc_hal.py`` compares the signature produced from HAL's own
    Verilog parser with the one produced by :mod:`hal_cdc.fixture_netlist`.
    Object ids differ between the two readers; nothing else may.
    """
    return {
        "domains": {
            domain: sorted(view.gate(gate_id).name for gate_id in gate_ids)
            for domain, gate_ids in result.registers_by_domain().items()
            if domain is not None
        },
        "unknown_domain_registers": sorted(
            view.gate(gate_id).name
            for gate_id, resolution in result.clock_resolutions.items()
            if not resolution.is_known
        ),
        "clock_resolution": {
            view.gate(gate_id).name: (
                resolution.kind,
                resolution.domain,
                resolution.ambiguous,
                resolution.reached_clocks,
            )
            for gate_id, resolution in result.clock_resolutions.items()
        },
        "crossings": sorted(
            (
                entry.crossing.source_domain,
                entry.crossing.destination_domain,
                entry.crossing.role,
                view.gate(entry.crossing.destination_gate_id).name,
                entry.crossing.destination_pin.name,
                entry.effective_class,
                _names(view, entry.crossing.source_gate_ids),
                _names(view, entry.crossing.path_gate_ids),
                _names(view, entry.classification.stage_gate_ids),
            )
            for entry in result.crossings
        ),
        "unknown_inputs": sorted(
            (view.gate(entry.gate_id).name, entry.pin.name, entry.reasons)
            for entry in result.unknown_inputs
        ),
        "resets": sorted(
            (
                target.reset_name,
                view.gate(target.gate_id).name,
                target.pin.name,
                str(target.domain),
                target.release,
                _names(view, target.stage_gate_ids),
            )
            for target in result.reset_targets
        ),
        "reset_domains": {
            name: [str(domain) for domain in domains]
            for name, domains in result.reset_domain_map.items()
        },
        "unused_waivers": [waiver.id for waiver in result.unused_waivers],
        "unsupported_gate_types": sorted(result.unsupported_gate_types),
    }


class GateLibraryTest(unittest.TestCase):
    """The fixture reader must not invent primitive semantics."""

    def test_library_is_the_shipped_one(self):
        self.assertTrue(
            os.path.isfile(GATE_LIBRARY),
            "the fixtures are written against the shipped example_library.hgl",
        )

    def test_pin_types_come_from_the_library(self):
        view = load_fixture(os.path.join(FIXTURES, "two_flop_sync.v"), GATE_LIBRARY)
        ff = view.gate_by_name("src_reg")
        self.assertEqual(ff.type.name, "FF")
        self.assertIn("ff", ff.type.properties)
        self.assertIn("sequential", ff.type.properties)
        self.assertEqual([pin.name for pin in ff.clock_pins()], ["C"])
        self.assertEqual(ff.type.pin("D").type, "data")
        self.assertEqual(ff.type.pin("CE").type, "enable")

    def test_unknown_cell_is_an_error(self):
        path = os.path.join(FIXTURES, "two_flop_sync.v")
        library, _name = __import__(
            "hal_cdc.fixture_netlist", fromlist=["load_gate_library"]
        ).load_gate_library(GATE_LIBRARY)
        library.pop("FF")
        with self.assertRaises(FixtureError):
            __import__("hal_cdc.fixture_netlist", fromlist=["load_verilog"]).load_verilog(
                path, library
            )

    def test_constant_nets_are_recognised_through_tie_cells(self):
        view = load_fixture(os.path.join(FIXTURES, "two_flop_sync.v"), GATE_LIBRARY)
        vdd = view.net_by_name("vdd")
        self.assertTrue(view.is_constant_net(vdd.id))
        self.assertFalse(view.is_constant_net(view.net_by_name("src_q").id))


class DeclarationsTest(unittest.TestCase):
    def test_version_must_match(self):
        with self.assertRaises(declarations.DeclarationError):
            declarations.parse({"version": 2, "clocks": []})

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(declarations.DeclarationError):
            declarations.parse({"version": 1, "clockz": []})

    def test_waiver_without_rationale_is_rejected(self):
        with self.assertRaises(declarations.DeclarationError) as context:
            declarations.parse(
                {"version": 1, "waivers": [{"id": "W1", "kind": "crossing"}]}
            )
        self.assertIn("rationale", str(context.exception))

    def test_duplicate_clock_names_are_rejected(self):
        with self.assertRaises(declarations.DeclarationError):
            declarations.parse(
                {
                    "version": 1,
                    "clocks": [{"name": "c", "net": "a"}, {"name": "c", "net": "b"}],
                }
            )

    def test_unbindable_net_becomes_a_problem_not_an_exception(self):
        view = load_fixture(os.path.join(FIXTURES, "two_flop_sync.v"), GATE_LIBRARY)
        bound = declarations.parse(
            {"version": 1, "clocks": [{"name": "clk_x", "net": "not_a_net"}]}
        ).bind(view)
        self.assertEqual(len(bound.problems), 1)
        self.assertIn("not_a_net", bound.problems[0])

    def test_reset_synchronous_to_undeclared_clock_is_a_problem(self):
        view = load_fixture(os.path.join(FIXTURES, "reset_release.v"), GATE_LIBRARY)
        bound = declarations.parse(
            {
                "version": 1,
                "resets": [{"name": "arst", "net": "arst", "synchronous_to": "nope"}],
            }
        ).bind(view)
        self.assertTrue(any("nope" in problem for problem in bound.problems))

    def test_waiver_scope_is_conjunctive(self):
        waiver = declarations.Waiver(
            "W", "because", kind="crossing", source_domain="a", gates=("g1",)
        )
        self.assertTrue(waiver.matches("crossing", source_domain="a", gate_name="g1"))
        self.assertFalse(waiver.matches("crossing", source_domain="b", gate_name="g1"))
        self.assertFalse(waiver.matches("crossing", source_domain="a", gate_name="g2"))
        self.assertFalse(waiver.matches("reset_release", source_domain="a", gate_name="g1"))


class TwoFlopSynchronizerTest(unittest.TestCase):
    """fixtures/two_flop_sync.v -- the one structure this pass recognises."""

    @classmethod
    def setUpClass(cls):
        cls.view, cls.bound, cls.result = run_fixture("two_flop_sync")

    def test_declarations_bind_cleanly(self):
        self.assertEqual(self.bound.problems, [])

    def test_domains(self):
        resolutions = by_name(self.view, self.result)
        self.assertEqual(resolutions["src_reg"].domain, "clk_a")
        self.assertEqual(resolutions["src_reg"].kind, KIND_DECLARED)
        self.assertEqual(resolutions["sync_meta_reg"].domain, "clk_b")
        self.assertEqual(resolutions["sync_out_reg"].domain, "clk_b")
        self.assertEqual(self.result.domains, ["clk_a", "clk_b"])

    def test_exactly_one_crossing_and_it_is_recognised(self):
        self.assertEqual(len(self.result.crossings), 1)
        entry = self.result.crossings[0]
        self.assertEqual(entry.crossing.source_domain, "clk_a")
        self.assertEqual(entry.crossing.destination_domain, "clk_b")
        self.assertEqual(entry.crossing.role, ROLE_DATA)
        self.assertEqual(
            self.view.gate(entry.crossing.destination_gate_id).name, "sync_meta_reg"
        )
        self.assertEqual(entry.classification.name, patterns.CLASS_TWO_FLOP)
        self.assertEqual(entry.classification.stages, 2)
        self.assertEqual(
            [self.view.gate(gid).name for gid in entry.classification.stage_gate_ids],
            ["sync_meta_reg", "sync_out_reg"],
        )
        self.assertTrue(entry.classification.is_recognized)
        self.assertFalse(entry.is_alarming)

    def test_supporting_gate_path_is_reported(self):
        entry = self.result.crossings[0]
        self.assertEqual(
            [self.view.gate(gid).name for gid in entry.crossing.source_gate_ids],
            ["src_reg"],
        )
        self.assertTrue(entry.crossing.direct)

    def test_no_unknown_inputs_and_no_resets(self):
        self.assertEqual(self.result.unknown_inputs, [])
        self.assertEqual(self.result.reset_targets, [])

    def test_declared_input_domain_is_honoured(self):
        din = self.view.net_by_name("din")
        self.assertEqual(self.result.net_domains[din.id].known, frozenset(["clk_a"]))


class DirectCrossingTest(unittest.TestCase):
    """fixtures/direct_crossing.v -- one crossing per failure mode."""

    @classmethod
    def setUpClass(cls):
        cls.view, cls.bound, cls.result = run_fixture("direct_crossing")
        cls.by_destination = {
            (
                cls.view.gate(entry.crossing.destination_gate_id).name,
                entry.crossing.destination_pin.name,
            ): entry
            for entry in cls.result.crossings
        }

    def test_four_crossings(self):
        self.assertEqual(len(self.result.crossings), 4)
        self.assertEqual(
            sorted(self.by_destination),
            [
                ("en_reg", "CE"),
                ("logic_reg", "D"),
                ("meta_reg", "D"),
                ("single_reg", "D"),
            ],
        )

    def test_none_is_recognised(self):
        for entry in self.result.crossings:
            self.assertFalse(
                entry.classification.is_recognized,
                "{} must not be accepted".format(entry.classification.name),
            )
            self.assertTrue(entry.is_alarming)
        self.assertEqual(len(self.result.alarming_crossings), 4)

    def test_single_stage_is_unsynchronized(self):
        entry = self.by_destination[("single_reg", "D")]
        self.assertEqual(entry.classification.name, patterns.CLASS_UNSYNCHRONIZED)
        self.assertTrue(entry.crossing.direct)

    def test_fanout_on_the_first_stage_breaks_recognition(self):
        entry = self.by_destination[("meta_reg", "D")]
        self.assertEqual(entry.classification.name, patterns.CLASS_UNSYNCHRONIZED)
        self.assertIn("drives 2 loads", entry.classification.reason)

    def test_combinational_logic_in_the_path_breaks_recognition(self):
        entry = self.by_destination[("logic_reg", "D")]
        self.assertEqual(entry.classification.name, patterns.CLASS_UNSYNCHRONIZED)
        self.assertFalse(entry.crossing.direct)
        self.assertEqual(
            [self.view.gate(gid).name for gid in entry.crossing.path_gate_ids], ["a_xor"]
        )
        self.assertEqual(
            [self.view.gate(gid).name for gid in entry.crossing.source_gate_ids],
            ["a_reg", "a_reg2"],
        )

    def test_control_pin_crossing_is_its_own_class(self):
        entry = self.by_destination[("en_reg", "CE")]
        self.assertEqual(entry.crossing.role, ROLE_CONTROL)
        self.assertEqual(
            entry.classification.name, patterns.CLASS_UNSYNCHRONIZED_CONTROL
        )

    def test_same_domain_paths_are_not_crossings(self):
        names = {name for name, _pin in self.by_destination}
        self.assertNotIn("a_reg2", names)
        self.assertNotIn("tail_reg", names)


class WaiverTest(unittest.TestCase):
    """The same netlist, with one scoped waiver and one stale waiver."""

    @classmethod
    def setUpClass(cls):
        cls.view, cls.bound, cls.result = run_fixture(
            "direct_crossing", "direct_crossing_waived"
        )

    def test_waived_crossing_is_still_reported_but_not_alarming(self):
        waived = [entry for entry in self.result.crossings if entry.waiver is not None]
        self.assertEqual(len(waived), 1)
        entry = waived[0]
        self.assertEqual(
            self.view.gate(entry.crossing.destination_gate_id).name, "logic_reg"
        )
        self.assertEqual(entry.effective_class, patterns.CLASS_WAIVED)
        self.assertEqual(entry.classification.name, patterns.CLASS_UNSYNCHRONIZED)
        self.assertFalse(entry.is_alarming)
        self.assertEqual(len(self.result.alarming_crossings), 3)

    def test_stale_waiver_is_reported(self):
        self.assertEqual([w.id for w in self.result.unused_waivers], ["W-STALE"])

    def test_waiver_rationale_survives_into_the_findings(self):
        document = report.build_document(self.result)
        validate.validate_document(document)
        rationales = [
            assumption["description"]
            for finding in document["findings"]
            for assumption in finding.get("assumptions", [])
            if assumption["id"].startswith("waiver/")
        ]
        self.assertTrue(any("quasi-static" in text for text in rationales))
        unused = [
            finding for finding in document["findings"] if finding["id"] == "cdc/waivers/unused"
        ]
        self.assertEqual(len(unused), 1)
        self.assertEqual(unused[0]["status"], "unknown")


class AmbiguousClockTest(unittest.TestCase):
    """fixtures/ambiguous_clock.v -- a clock mux and a clock gate."""

    @classmethod
    def setUpClass(cls):
        cls.view, cls.bound, cls.result = run_fixture("ambiguous_clock")
        cls.resolutions = by_name(cls.view, cls.result)

    def test_clock_mux_leaves_the_domain_unknown(self):
        resolution = self.resolutions["mux_reg"]
        self.assertIsNone(resolution.domain)
        self.assertEqual(resolution.kind, KIND_UNKNOWN)
        self.assertTrue(resolution.ambiguous)
        self.assertEqual(resolution.reached_clocks, ("clk_a", "clk_b"))
        self.assertIn("ambiguous generated clock", resolution.reason)

    def test_clock_gate_is_derived_and_flagged(self):
        resolution = self.resolutions["gated_reg"]
        self.assertEqual(resolution.domain, "clk_a")
        self.assertEqual(resolution.kind, KIND_DERIVED)
        self.assertTrue(resolution.ambiguous)
        self.assertEqual(
            [self.view.net(nid).name for nid in resolution.enable_nets], ["en"]
        )

    def test_declared_clock_without_registers_is_reported(self):
        # clk_b only reaches mux_reg through the clock mux, so it names no domain.
        self.assertNotIn("clk_b", self.result.domains)
        document = report.build_document(self.result)
        validate.validate_document(document)
        empty = [
            finding
            for finding in document["findings"]
            if finding["id"] == "cdc/domain/clk_b/empty"
        ]
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0]["status"], "unknown")

    def test_registers_with_an_unknown_clock_produce_no_crossings(self):
        self.assertEqual(self.result.crossings, [])
        self.assertEqual(self.result.summary()["registers_with_unknown_domain"], 1)

    def test_the_unknown_domain_is_reported_as_unknown_not_as_safe(self):
        document = report.build_document(self.result)
        validate.validate_document(document)
        unresolved = [
            finding
            for finding in document["findings"]
            if finding["id"].startswith("cdc/clock/unresolved/")
        ]
        self.assertTrue(unresolved)
        self.assertTrue(all(finding["status"] == "unknown" for finding in unresolved))
        self.assertTrue(
            any("mux_reg" in json.dumps(finding) for finding in unresolved)
        )


class ResetReleaseTest(unittest.TestCase):
    """fixtures/reset_release.v -- one reset, two release patterns."""

    @classmethod
    def setUpClass(cls):
        cls.view, cls.bound, cls.result = run_fixture("reset_release")
        cls.targets = {
            cls.view.gate(target.gate_id).name: target for target in cls.result.reset_targets
        }

    def test_every_reset_pin_is_classified(self):
        self.assertEqual(
            sorted(self.targets),
            ["a_reg", "b_reg", "rst_sync_reg1", "rst_sync_reg2"],
        )

    def test_reset_synchronizer_is_recognised(self):
        target = self.targets["a_reg"]
        self.assertEqual(target.release, resets.RELEASE_SYNCHRONIZED)
        self.assertEqual(target.domain, "clk_a")
        self.assertEqual(
            [self.view.gate(gid).name for gid in target.stage_gate_ids],
            ["rst_sync_reg1", "rst_sync_reg2"],
        )
        self.assertFalse(target.is_alarming)

    def test_the_synchronizer_flops_are_not_flagged_for_their_own_async_reset(self):
        for name in ("rst_sync_reg1", "rst_sync_reg2"):
            target = self.targets[name]
            self.assertEqual(target.release, resets.RELEASE_SYNCHRONIZER_STAGE)
            self.assertFalse(target.is_alarming)

    def test_raw_asynchronous_release_is_flagged(self):
        target = self.targets["b_reg"]
        self.assertEqual(target.release, resets.RELEASE_ASYNCHRONOUS)
        self.assertEqual(target.domain, "clk_b")
        self.assertTrue(target.is_alarming)
        self.assertIn("releasing", target.reason)

    def test_reset_spans_two_domains(self):
        self.assertEqual(self.result.reset_domain_map["arst"], ["clk_a", "clk_b"])
        document = report.build_document(self.result)
        validate.validate_document(document)
        multi = [
            finding
            for finding in document["findings"]
            if finding["id"] == "cdc/reset/arst/multi-domain"
        ]
        self.assertEqual(len(multi), 1)
        self.assertEqual(multi[0]["severity"], "medium")

    def test_async_reset_on_a_reset_pin_is_not_double_reported(self):
        # The reset findings own this structure; it must not also appear as an
        # "input with an unknown domain".
        for entry in self.result.unknown_inputs:
            self.assertNotIn(entry.pin.type, ("reset", "set"))


class FindingsDocumentTest(unittest.TestCase):
    """Every fixture must produce a schema-valid, honest document."""

    FIXTURES = ("two_flop_sync", "direct_crossing", "ambiguous_clock", "reset_release")

    def documents(self):
        for name in self.FIXTURES:
            _view, _bound, result = run_fixture(name)
            yield name, report.build_document(result)

    def test_all_documents_validate(self):
        for name, document in self.documents():
            validate.validate_document(document)
            validate.validate_document(document, prefer_jsonschema=True)
            self.assertEqual(document["schema_version"], "1.0.0", name)

    def test_no_finding_ever_claims_a_proof(self):
        for name, document in self.documents():
            for finding in document["findings"]:
                self.assertIn(
                    finding["status"],
                    ("heuristic", "unknown", "unsupported", "error"),
                    "{}: {} claims {}".format(name, finding["id"], finding["status"]),
                )
                self.assertNotIn("bounds", finding)
                self.assertEqual(finding["method"]["kind"], "structural")
                self.assertFalse(finding["method"]["bounded"])

    def test_limitations_are_always_reported(self):
        for name, document in self.documents():
            limitations = [
                finding for finding in document["findings"] if finding["id"] == "cdc/limitations"
            ]
            self.assertEqual(len(limitations), 1, name)
            finding = limitations[0]
            self.assertEqual(finding["status"], "unsupported")
            text = finding["unsupported"]["reason"].lower()
            for topic in ("coherency", "reconvergence", "gated", "black box", "metastability"):
                self.assertIn(topic, text, "{}: limitation {!r} missing".format(name, topic))

    def test_documents_are_deterministic(self):
        for name in self.FIXTURES:
            _view, _bound, first = run_fixture(name)
            _view, _bound, second = run_fixture(name)
            one = report.build_document(first, generated_at="2026-01-01T00:00:00Z")
            two = report.build_document(second, generated_at="2026-01-01T00:00:00Z")
            self.assertEqual(serialize.dumps(one), serialize.dumps(two), name)
            self.assertEqual(
                serialize.document_digest(one), serialize.document_digest(two), name
            )

    def test_crossing_findings_carry_domains_and_paths(self):
        _view, _bound, result = run_fixture("direct_crossing")
        document = report.build_document(result)
        crossings = [
            finding
            for finding in document["findings"]
            if finding["id"].startswith("cdc/crossing/")
        ]
        self.assertTrue(crossings)
        for finding in crossings:
            data = finding["data"]
            self.assertIn("source_domain", data)
            self.assertIn("destination_domain", data)
            for path in data["paths"]:
                self.assertIn("destination_gate", path)
                self.assertIn("source_gates", path)
                self.assertIn("path_gates", path)
        combinational = [
            path
            for finding in crossings
            for path in finding["data"]["paths"]
            if path["destination_gate"] == "logic_reg"
        ]
        self.assertEqual(len(combinational), 1)
        self.assertEqual(combinational[0]["path_gates"], ["a_xor"])
        self.assertEqual(combinational[0]["source_gates"], ["a_reg", "a_reg2"])

    def test_declaration_problems_become_error_findings(self):
        view = load_fixture(os.path.join(FIXTURES, "two_flop_sync.v"), GATE_LIBRARY)
        bound = declarations.parse(
            {"version": 1, "clocks": [{"name": "clk_x", "net": "missing"}]}
        ).bind(view)
        result = audit_module.run_audit(view, bound)
        document = report.build_document(result)
        validate.validate_document(document)
        errors = [
            finding for finding in document["findings"] if finding["status"] == "error"
        ]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"]["kind"], "invalid_input")

    def test_coherency_finding_appears_only_with_several_recognised_crossings(self):
        _view, _bound, result = run_fixture("two_flop_sync")
        document = report.build_document(result)
        self.assertFalse(
            [f for f in document["findings"] if f["id"].startswith("cdc/coherency/")]
        )


class DomainGraphTest(unittest.TestCase):
    def test_graph_has_a_node_per_domain_and_an_edge_per_group(self):
        _view, _bound, result = run_fixture("direct_crossing")
        dot = report.build_domain_graph(result).to_dot()
        self.assertTrue(dot.startswith("//") or dot.lstrip().startswith("digraph"))
        self.assertIn("clk_a", dot)
        self.assertIn("clk_b", dot)
        self.assertIn("->", dot)
        self.assertEqual(dot.count("{"), dot.count("}"))

    def test_unknown_domain_is_drawn_when_present(self):
        _view, _bound, result = run_fixture("ambiguous_clock")
        dot = report.build_domain_graph(result).to_dot()
        self.assertIn("unknown domain", dot)


class LimitsTest(unittest.TestCase):
    def test_a_tiny_clock_budget_yields_unknown_not_a_guess(self):
        view = load_fixture(os.path.join(FIXTURES, "ambiguous_clock.v"), GATE_LIBRARY)
        bound = declarations.load(os.path.join(FIXTURES, "ambiguous_clock.json")).bind(view)
        result = audit_module.run_audit(view, bound, limits=Limits(max_clock_depth=0))
        for resolution in result.clock_resolutions.values():
            self.assertIsNone(resolution.domain)
        self.assertEqual(result.domains, [])

    def test_propagation_budget_is_reported(self):
        view = load_fixture(os.path.join(FIXTURES, "direct_crossing.v"), GATE_LIBRARY)
        bound = declarations.load(os.path.join(FIXTURES, "direct_crossing.json")).bind(view)
        result = audit_module.run_audit(view, bound, limits=Limits(max_propagation_steps=1))
        self.assertTrue(result.propagation_limit_hit)
        document = report.build_document(result)
        validate.validate_document(document)
        limit_findings = [
            finding for finding in document["findings"] if finding["id"] == "cdc/limits/propagation"
        ]
        self.assertEqual(len(limit_findings), 1)
        self.assertEqual(limit_findings[0]["status"], "unknown")
        self.assertTrue(limit_findings[0]["limits"]["hit"])


class CliTest(unittest.TestCase):
    """The CLI contract: 0 clean, 1 findings, 2 could-not-run."""

    def _argv(self, fixture, declarations_name=None, *extra):
        return [
            "audit",
            os.path.join(FIXTURES, fixture + ".v"),
            "--fixture-reader",
            "--gate-library",
            GATE_LIBRARY,
            "--declarations",
            os.path.join(FIXTURES, (declarations_name or fixture) + ".json"),
            "--no-clock-tree",
            "-q",
        ] + list(extra)

    def test_clean_design_exits_zero(self):
        self.assertEqual(cli.main(self._argv("two_flop_sync")), cli.EXIT_OK)

    def test_unsynchronized_crossings_exit_one(self):
        self.assertEqual(cli.main(self._argv("direct_crossing")), cli.EXIT_FINDINGS)

    def test_fail_on_never_exits_zero(self):
        self.assertEqual(
            cli.main(self._argv("direct_crossing", None, "--fail-on", "never")), cli.EXIT_OK
        )

    def test_unsafe_reset_release_exits_one_only_with_any_alarm(self):
        self.assertEqual(cli.main(self._argv("reset_release")), cli.EXIT_OK)
        self.assertEqual(
            cli.main(self._argv("reset_release", None, "--fail-on", "any-alarm")),
            cli.EXIT_FINDINGS,
        )

    def test_missing_netlist_exits_two(self):
        self.assertEqual(
            cli.main(
                [
                    "audit",
                    os.path.join(FIXTURES, "does_not_exist.v"),
                    "--fixture-reader",
                    "--gate-library",
                    GATE_LIBRARY,
                    "--declarations",
                    os.path.join(FIXTURES, "two_flop_sync.json"),
                    "-q",
                ]
            ),
            cli.EXIT_ERROR,
        )

    def test_bad_declarations_exit_two(self):
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.write('{"version": 99}')
        handle.close()
        try:
            self.assertEqual(
                cli.main(
                    [
                        "audit",
                        os.path.join(FIXTURES, "two_flop_sync.v"),
                        "--fixture-reader",
                        "--gate-library",
                        GATE_LIBRARY,
                        "--declarations",
                        handle.name,
                        "-q",
                    ]
                ),
                cli.EXIT_ERROR,
            )
        finally:
            os.unlink(handle.name)

    def test_written_document_validates_and_dot_is_written(self):
        import tempfile

        directory = tempfile.mkdtemp(prefix="hal_cdc_")
        findings_path = os.path.join(directory, "findings.json")
        dot_path = os.path.join(directory, "domains.dot")
        try:
            code = cli.main(
                self._argv(
                    "direct_crossing", None, "-o", findings_path, "--dot", dot_path
                )
            )
            self.assertEqual(code, cli.EXIT_FINDINGS)
            document = serialize.read_document(findings_path)
            validate.validate_document(document)
            self.assertTrue(os.path.getsize(dot_path) > 0)
        finally:
            import shutil

            shutil.rmtree(directory, ignore_errors=True)

    def test_discover_emits_a_parseable_skeleton(self):
        import io
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(
                [
                    "discover",
                    os.path.join(FIXTURES, "reset_release.v"),
                    "--fixture-reader",
                    "--gate-library",
                    GATE_LIBRARY,
                ]
            )
        self.assertEqual(code, cli.EXIT_OK)
        skeleton = json.loads(buffer.getvalue())
        parsed = declarations.parse(skeleton)
        self.assertEqual(sorted(parsed.clock_names), ["clk_a", "clk_b"])
        self.assertEqual([reset.name for reset in parsed.resets], ["arst", "rst_a_sync"])


if __name__ == "__main__":
    unittest.main()
