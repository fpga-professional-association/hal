"""Unit tests for hal_fault_campaign. No HAL, no netlist, no simulator.

The split that makes this possible is the one the package is built around: only
``steps/`` touches ``hal_py``, so the cycle grid, the enumeration, the
classification, the findings, the manifest and the whole host-side orchestrator
can be exercised on any machine -- which is where most of the ways to be wrong
live anyway.

The cases that matter are the negative ones: a window that runs off the end of
the run, a detection signal that is already active in the baseline, an X where
the baseline was defined, a random campaign without a seed, a replay against a
netlist that changed, a step that exits 0 without writing anything.

``test_ground_truth_*`` closes the loop differently: it classifies the traces of
an independent model of the fixture (``fixtures/reference_model.py``) and checks
the verdicts against the committed ``fixtures/ground_truth.json``.
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))

from hal_findings import validate as findings_validate  # noqa: E402

from hal_fault_campaign import campaign, classify, config, faultmodel  # noqa: E402
from hal_fault_campaign import findings as findings_module  # noqa: E402
from hal_fault_campaign import manifest as manifest_module  # noqa: E402
from hal_fault_campaign import protocol, replay, workload  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_config(name):
    return config.load(os.path.join(FIXTURES, name))


# ---------------------------------------------------------------------------
# workload / cycle grid
# ---------------------------------------------------------------------------


class ClockGridTest(unittest.TestCase):
    def test_sample_point_is_after_the_edge_and_inside_the_cycle(self):
        grid = workload.ClockGrid(8000)
        for cycle in range(4):
            self.assertLess(grid.cycle_start(cycle), grid.edge_time(cycle))
            self.assertLess(grid.edge_time(cycle), grid.sample_time(cycle))
            self.assertLess(grid.sample_time(cycle), grid.cycle_start(cycle + 1))

    def test_period_must_land_on_the_grid(self):
        with self.assertRaises(workload.WorkloadError):
            workload.ClockGrid(9)
        with self.assertRaises(workload.WorkloadError):
            workload.ClockGrid(0)

    def test_schedule_covers_the_whole_run_and_nothing_more(self):
        grid = workload.ClockGrid(8000)
        events = {0: {"RST": 1}, 8000: {"RST": 0}}
        schedule = workload.event_schedule(grid, events, 4)
        self.assertEqual([entry[0] for entry in schedule], [0, 8000])
        self.assertEqual(sum(entry[2] for entry in schedule), 4 * 8000)

    def test_schedule_rejects_an_event_past_the_end(self):
        grid = workload.ClockGrid(8000)
        with self.assertRaises(workload.WorkloadError):
            workload.event_schedule(grid, {32000: {"RST": 0}}, 4)

    def test_stimulus_rejects_non_binary_values(self):
        with self.assertRaises(workload.WorkloadError):
            workload.resolve_stimulus([{"cycle": 0, "inputs": {"A": "X"}}], 4)
        with self.assertRaises(workload.WorkloadError):
            workload.resolve_stimulus([{"cycle": 0, "inputs": {"A": True}}], 4)

    def test_stimulus_rejects_a_cycle_outside_the_workload(self):
        with self.assertRaises(workload.WorkloadError):
            workload.resolve_stimulus([{"cycle": 9, "inputs": {"A": 1}}], 4)

    def test_merge_rejects_conflicting_assignments(self):
        with self.assertRaises(workload.WorkloadError):
            workload.merge_event_maps({0: {"A": 1}}, {0: {"A": 0}})


# ---------------------------------------------------------------------------
# enumeration
# ---------------------------------------------------------------------------


def make_sites(names):
    return [
        campaign.Site(name, index + 1, "FFR", "Q", "{}_q".format(name), 100 + index)
        for index, name in enumerate(names)
    ]


class EnumerationTest(unittest.TestCase):
    def setUp(self):
        self.sites = make_sites(["b_reg", "a_reg", "c_reg"])

    def test_sites_are_ordered_by_name_not_by_id(self):
        ordered = campaign.sort_sites(self.sites)
        self.assertEqual([site.gate_name for site in ordered], ["a_reg", "b_reg", "c_reg"])

    def test_exhaustive_covers_the_whole_grid_in_a_stable_order(self):
        faults, record = campaign.enumerate_faults(self.sites, [1, 2])
        self.assertEqual(record["grid_size"], 6)
        self.assertEqual(record["selected"], 6)
        self.assertEqual(
            [(fault.site_name, fault.cycle) for fault in faults],
            [("a_reg", 1), ("a_reg", 2), ("b_reg", 1), ("b_reg", 2), ("c_reg", 1),
             ("c_reg", 2)],
        )
        self.assertEqual([fault.fault_id for fault in faults][:2], ["f00000", "f00001"])

    def test_random_sampling_is_a_function_of_the_seed(self):
        sampling = {"mode": "random", "count": 3, "seed": 7}
        first, record = campaign.enumerate_faults(self.sites, [1, 2, 3], sampling=sampling)
        second, _ = campaign.enumerate_faults(self.sites, [1, 2, 3], sampling=sampling)
        self.assertEqual([fault.as_dict() for fault in first],
                         [fault.as_dict() for fault in second])
        self.assertEqual(record["algorithm"], campaign.SAMPLING_ALGORITHM)
        self.assertEqual(len(first), 3)

    def test_a_different_seed_gives_a_different_sample(self):
        grid_cycles = list(range(10))
        first, _ = campaign.enumerate_faults(
            self.sites, grid_cycles, sampling={"mode": "random", "count": 5, "seed": 1}
        )
        second, _ = campaign.enumerate_faults(
            self.sites, grid_cycles, sampling={"mode": "random", "count": 5, "seed": 2}
        )
        self.assertNotEqual(
            [(f.site_name, f.cycle) for f in first],
            [(f.site_name, f.cycle) for f in second],
        )

    def test_random_sampling_without_a_seed_is_refused(self):
        with self.assertRaises(campaign.EnumerationError):
            campaign.enumerate_faults(
                self.sites, [1], sampling={"mode": "random", "count": 1}
            )

    def test_sampling_more_than_the_grid_is_refused(self):
        with self.assertRaises(campaign.EnumerationError):
            campaign.enumerate_faults(
                self.sites, [1], sampling={"mode": "random", "count": 99, "seed": 1}
            )

    def test_site_filters_are_globs_on_the_gate_name(self):
        selected = campaign.select_sites(self.sites, ["*_reg"], ["b_*"])
        self.assertEqual([site.gate_name for site in selected], ["a_reg", "c_reg"])

    def test_cycles_outside_the_workload_are_refused(self):
        with self.assertRaises(campaign.EnumerationError):
            campaign.resolve_cycles({"from": 0, "to": 20}, 10)

    def test_no_site_is_an_error_not_an_empty_campaign(self):
        with self.assertRaises(campaign.EnumerationError):
            campaign.enumerate_faults([], [1])


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def trace(**series):
    return {name: list(values) for name, values in series.items()}


class WindowTest(unittest.TestCase):
    def test_relative_window_is_anchored_at_the_injection(self):
        cycles, requested, clamped = classify.window_cycles(
            {"mode": "relative", "start_offset": 1, "length": 3}, 4, 20
        )
        self.assertEqual(cycles, [5, 6, 7])
        self.assertEqual(requested, (5, 7))
        self.assertFalse(clamped)

    def test_a_window_running_past_the_run_is_clamped_and_says_so(self):
        cycles, requested, clamped = classify.window_cycles(
            {"mode": "relative", "start_offset": 0, "length": 5}, 8, 10
        )
        self.assertEqual(cycles, [8, 9])
        self.assertEqual(requested, (8, 12))
        self.assertTrue(clamped)

    def test_unknown_window_mode_is_refused(self):
        with self.assertRaises(classify.ClassificationError):
            classify.window_cycles({"mode": "sliding"}, 0, 4)


class ClassifyTest(unittest.TestCase):
    WINDOW = {"mode": "relative", "start_offset": 0, "length": 3}

    def classify(self, baseline, faulty, injection=1, **kwargs):
        return classify.classify(
            baseline,
            faulty,
            injection,
            4,
            kwargs.pop("outputs", ["OUT"]),
            kwargs.pop("detection", ["ERR"]),
            window=kwargs.pop("window", self.WINDOW),
            **kwargs
        )

    def test_detection_wins_and_records_both_latencies(self):
        base = trace(OUT=[0, 0, 0, 0], ERR=[0, 0, 0, 0])
        fault = trace(OUT=[0, 0, 1, 1], ERR=[0, 0, 0, 1])
        result = self.classify(base, fault)
        self.assertEqual(result["classification"], faultmodel.CLASS_DETECTED)
        self.assertEqual(result["detection_latency_cycles"], 2)
        self.assertEqual(result["divergence_latency_cycles"], 1)

    def test_divergence_without_detection_is_silent(self):
        base = trace(OUT=[0, 0, 0, 0], ERR=[0, 0, 0, 0])
        fault = trace(OUT=[0, 1, 0, 0], ERR=[0, 0, 0, 0])
        result = self.classify(base, fault)
        self.assertEqual(result["classification"], faultmodel.CLASS_SILENT)
        self.assertEqual(result["divergence_latency_cycles"], 0)
        self.assertIsNone(result["detection_latency_cycles"])

    def test_silence_inside_the_window_is_unobserved_not_masked(self):
        base = trace(OUT=[0, 0, 0, 1], ERR=[0, 0, 0, 0])
        fault = trace(OUT=[0, 0, 0, 0], ERR=[0, 0, 0, 0])
        result = self.classify(base, fault, window={"mode": "relative", "start_offset": 0,
                                                    "length": 2})
        self.assertEqual(result["classification"], faultmodel.CLASS_UNOBSERVED)
        self.assertFalse(result["diverged"])

    def test_the_very_same_fault_becomes_visible_with_a_longer_window(self):
        base = trace(OUT=[0, 0, 0, 1], ERR=[0, 0, 0, 0])
        fault = trace(OUT=[0, 0, 0, 0], ERR=[0, 0, 0, 0])
        short = self.classify(base, fault,
                              window={"mode": "relative", "start_offset": 0, "length": 2})
        long = self.classify(base, fault,
                             window={"mode": "relative", "start_offset": 0, "length": 3})
        self.assertEqual(short["classification"], faultmodel.CLASS_UNOBSERVED)
        self.assertEqual(long["classification"], faultmodel.CLASS_SILENT)

    def test_an_undefined_value_is_indeterminate_not_clean(self):
        base = trace(OUT=[0, 0, 0, 0], ERR=[0, 0, 0, 0])
        fault = trace(OUT=[0, classify.VALUE_X, 0, 0], ERR=[0, 0, 0, 0])
        result = self.classify(base, fault)
        self.assertEqual(result["classification"], faultmodel.CLASS_INDETERMINATE)
        self.assertEqual(result["indeterminate_cycles"], [1])
        self.assertFalse(result["diverged"])

    def test_a_baseline_alarm_cannot_evidence_detection(self):
        base = trace(OUT=[0, 0, 0, 0], ERR=[0, 1, 1, 1])
        fault = trace(OUT=[0, 0, 0, 0], ERR=[0, 1, 1, 1])
        result = self.classify(base, fault)
        self.assertEqual(result["classification"], faultmodel.CLASS_UNOBSERVED)
        self.assertEqual(result["detection_cycles"], [])
        self.assertTrue(result["baseline_detection_active"])

    def test_a_missing_signal_is_an_error_not_a_pass(self):
        base = trace(OUT=[0, 0, 0, 0])
        fault = trace(OUT=[0, 0, 0, 0])
        with self.assertRaises(classify.ClassificationError):
            self.classify(base, fault)

    def test_summarize_counts_and_latency_statistics(self):
        results = [
            self.classify(trace(OUT=[0] * 4, ERR=[0] * 4),
                          trace(OUT=[0, 0, 1, 1], ERR=[0, 0, 1, 1])),
            self.classify(trace(OUT=[0] * 4, ERR=[0] * 4),
                          trace(OUT=[0, 1, 1, 1], ERR=[0] * 4)),
        ]
        summary = classify.summarize(results)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["counts"][faultmodel.CLASS_DETECTED], 1)
        self.assertEqual(summary["counts"][faultmodel.CLASS_SILENT], 1)
        self.assertEqual(summary["detection_latency_cycles"]["count"], 1)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


class ConfigTest(unittest.TestCase):
    def base_document(self):
        with open(os.path.join(FIXTURES, "campaign_short_window.json"),
                  "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_the_shipped_configurations_validate(self):
        for name in ("campaign_short_window.json", "campaign_long_window.json"):
            parsed = load_config(name)
            self.assertEqual(parsed.cycles, 20)
            self.assertEqual(parsed.engine, "hal_simulator")

    def test_resolved_configuration_makes_every_default_explicit(self):
        resolved = load_config("campaign_short_window.json").resolved()
        self.assertEqual(resolved["faults"]["hold_cycles"], 1)
        self.assertEqual(resolved["observation"]["detection_active_value"], 1)
        self.assertIn("grid", resolved)
        self.assertNotIn("netlist", resolved)  # paths are pinned by digest, not by name

    def test_unknown_version_is_refused_outright(self):
        document = self.base_document()
        document["config_version"] = "2.0.0"
        with self.assertRaises(config.ConfigError):
            config.parse(document)

    def test_unknown_key_is_refused(self):
        document = self.base_document()
        document["engien"] = "hal_simulator"
        with self.assertRaises(config.ConfigError):
            config.parse(document)

    def test_a_signal_cannot_be_output_and_detector_at_once(self):
        document = self.base_document()
        document["observation"]["detection_signals"] = ["CNT0"]
        with self.assertRaises(config.ConfigError) as raised:
            config.parse(document)
        self.assertIn("cannot be evidence", str(raised.exception))

    def test_clock_period_off_the_grid_is_refused(self):
        document = self.base_document()
        document["clock"]["period_ps"] = 4001
        with self.assertRaises(config.ConfigError):
            config.parse(document)

    def test_injection_cycle_outside_the_workload_is_refused(self):
        document = self.base_document()
        document["faults"]["cycles"] = [3, 99]
        with self.assertRaises(config.ConfigError):
            config.parse(document)

    def test_random_sampling_needs_a_seed(self):
        document = self.base_document()
        document["faults"]["sampling"] = {"mode": "random", "count": 4}
        with self.assertRaises(config.ConfigError) as raised:
            config.parse(document)
        self.assertIn("seed", str(raised.exception))

    def test_exhaustive_sampling_refuses_a_stray_seed(self):
        document = self.base_document()
        document["faults"]["sampling"] = {"mode": "exhaustive", "seed": 1}
        with self.assertRaises(config.ConfigError):
            config.parse(document)


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


def sample_campaign():
    """A small campaign result, shaped exactly like the step produces one."""
    parsed = load_config("campaign_short_window.json")
    resolved = parsed.resolved()
    sites = [
        campaign.Site("CNT_reg_0_inst", 12, "FFR", "Q", "CNT0", 4).as_dict(),
        campaign.Site("AUX_reg_inst", 16, "FFR", "Q", "AUX_O", 8).as_dict(),
        campaign.Site("PIPE_reg_0_inst", 17, "FFR", "Q", "pipe1", 9).as_dict(),
    ]
    faults = [
        {"id": "f00000", "site": "CNT_reg_0_inst", "cycle": 3, "hold_cycles": 1,
         "gate_id": 12, "control_net": "__hal_fi_ctrl_12"},
        {"id": "f00001", "site": "AUX_reg_inst", "cycle": 3, "hold_cycles": 1,
         "gate_id": 16, "control_net": "__hal_fi_ctrl_16"},
        {"id": "f00002", "site": "PIPE_reg_0_inst", "cycle": 3, "hold_cycles": 1,
         "gate_id": 17, "control_net": "__hal_fi_ctrl_17"},
    ]
    base = {name: [0] * 20 for name in
            ["CNT0", "CNT1", "CNT2", "AUX_O", "PIPE_O", "ERR"]}

    detected = copy.deepcopy(base)
    detected["ERR"][3] = 1
    silent = copy.deepcopy(base)
    silent["AUX_O"][3] = 1
    quiet = copy.deepcopy(base)

    results = {}
    for fault_id, faulty in (("f00000", detected), ("f00001", silent), ("f00002", quiet)):
        results[fault_id] = classify.classify(
            base, faulty, 3, 20, resolved["observation"]["outputs"],
            resolved["observation"]["detection_signals"],
            window=resolved["observation"]["window"],
        )
    enumeration = {
        "mode": "exhaustive", "grid_size": 36, "selected": 3, "site_count": 9,
        "cycle_count": 4, "hold_cycles": 1,
    }
    summary = classify.summarize(list(results.values()))
    artifact = {
        "artifact_id": "netlist",
        "kind": "netlist",
        "path": "parity_counter.v",
        "sha256": "a" * 64,
        "design_name": "parity_counter",
        "gate_count": 21,
        "net_count": 24,
    }
    return parsed, resolved, artifact, sites, faults, results, enumeration, summary


class FindingsTest(unittest.TestCase):
    def setUp(self):
        (self.config, self.resolved, self.artifact, self.sites, self.faults,
         self.results, self.enumeration, self.summary) = sample_campaign()
        self.document = findings_module.build_document(
            self.artifact, self.resolved, "hal_simulator", self.sites, self.faults,
            self.results, self.enumeration, self.summary,
            skipped=[{"gate_name": "LATCH_inst", "gate_id": 40, "gate_type": "DLATCH",
                      "reason": "no state output pin"}],
            analysis_extra={"plugin_version": "0.1"},
            generated_at="2026-09-07T00:00:00Z",
        )

    def test_the_document_validates(self):
        findings_validate.validate_document(self.document)

    def test_the_document_validates_against_jsonschema_when_available(self):
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("jsonschema is not installed")
        findings_validate.validate_document(self.document, prefer_jsonschema=True)

    def statuses(self):
        return {finding["id"]: finding["status"] for finding in self.document["findings"]}

    def test_a_silent_divergence_is_the_only_refutation(self):
        statuses = self.statuses()
        self.assertEqual(
            statuses["fault-campaign/silent-divergence/f00001"], "bounded_counterexample"
        )
        self.assertEqual(statuses["fault-campaign/detected/f00000"], "proven_bounded")
        self.assertEqual(
            statuses["fault-campaign/unobserved-in-window/f00002"], "proven_bounded"
        )

    def test_no_finding_claims_an_unbounded_proof(self):
        from hal_findings.model import is_unbounded_proof

        for finding in self.document["findings"]:
            self.assertFalse(is_unbounded_proof(finding), finding["id"])

    def test_the_unobserved_finding_refuses_to_say_masked(self):
        finding = next(
            item for item in self.document["findings"]
            if item["id"].startswith("fault-campaign/unobserved")
        )
        self.assertIn("NOT shown to be masked", finding["summary"])
        self.assertNotIn("masked", finding["title"])

    def test_the_counterexample_carries_a_witness_within_the_bound(self):
        finding = next(
            item for item in self.document["findings"]
            if item["status"] == "bounded_counterexample"
        )
        self.assertTrue(finding["counterexample"]["witness"])
        self.assertLessEqual(
            finding["counterexample"]["cycle_bound"], finding["bounds"]["cycle_bound"]
        )

    def test_the_coverage_gap_names_the_gate_type(self):
        finding = next(
            item for item in self.document["findings"]
            if item["status"] == "unsupported"
        )
        self.assertEqual(
            finding["unsupported"]["primitives"][0]["gate_type"], "DLATCH"
        )

    def test_every_finding_carries_the_fault_model_assumptions(self):
        expected = {identifier for identifier, _, _ in faultmodel.ASSUMPTIONS}
        for finding in self.document["findings"]:
            if finding["status"] == "unsupported":
                continue
            got = {item["id"] for item in finding["assumptions"]}
            self.assertTrue(expected.issubset(got), finding["id"])

    def test_a_sampled_campaign_summary_is_heuristic_not_a_proof(self):
        enumeration = dict(self.enumeration, mode="random", seed=1, count=3,
                           algorithm=campaign.SAMPLING_ALGORITHM)
        document = findings_module.build_document(
            self.artifact, self.resolved, "hal_simulator", self.sites, self.faults,
            self.results, enumeration, self.summary,
        )
        findings_validate.validate_document(document)
        summary = next(
            item for item in document["findings"] if item["id"] == "fault-campaign/summary"
        )
        self.assertEqual(summary["status"], "heuristic")
        self.assertEqual(summary["method"]["kind"], "statistical")
        self.assertIn("not a failure rate", summary["summary"])

    def test_a_replay_summary_is_not_reported_as_an_exhaustive_sweep(self):
        enumeration = dict(self.enumeration, mode="replay", original_mode="exhaustive",
                           replayed=True, selected=1)
        document = findings_module.build_document(
            self.artifact, self.resolved, "hal_simulator", self.sites, self.faults[:1],
            {"f00000": self.results["f00000"]}, enumeration,
            classify.summarize([self.results["f00000"]]),
        )
        findings_validate.validate_document(document)
        summary = next(
            item for item in document["findings"] if item["id"] == "fault-campaign/summary"
        )
        self.assertEqual(summary["status"], "heuristic")
        self.assertIn("Replayed", summary["title"])
        self.assertIn("replayed by name", " ".join(
            item["description"] for item in summary["assumptions"]
        ))

    def test_the_summary_reports_coverage_and_the_limitations(self):
        summary = next(
            item for item in self.document["findings"]
            if item["id"] == "fault-campaign/summary"
        )
        self.assertAlmostEqual(summary["data"]["coverage_fraction"], 3 / 36.0, places=6)
        self.assertTrue(summary["data"]["limitations"])
        self.assertIn("not a masking claim",
                      " ".join(summary["data"]["classification_meaning"].values()))


class InjectionScheduleTest(unittest.TestCase):
    """The control-input pulse, which is the whole fault model in three events."""

    def setUp(self):
        from hal_fault_campaign.steps import campaign_step

        self.step = campaign_step
        self.grid = workload.ClockGrid(8000)
        self.controls = ["__hal_fi_ctrl_1", "__hal_fi_ctrl_2"]

    def test_every_control_is_driven_to_zero_at_time_zero(self):
        events = self.step._control_events(self.grid, None, 0, 0, self.controls, 20)
        self.assertEqual(events[0], {"__hal_fi_ctrl_1": 0, "__hal_fi_ctrl_2": 0})

    def test_a_one_cycle_pulse_covers_exactly_its_cycle(self):
        events = self.step._control_events(
            self.grid, "__hal_fi_ctrl_1", 3, 1, self.controls, 20
        )
        self.assertEqual(events[self.grid.cycle_start(3)]["__hal_fi_ctrl_1"], 1)
        self.assertEqual(events[self.grid.cycle_start(4)]["__hal_fi_ctrl_1"], 0)

    def test_a_held_flip_covers_every_cycle_of_the_window(self):
        events = self.step._control_events(
            self.grid, "__hal_fi_ctrl_1", 3, 3, self.controls, 20
        )
        self.assertNotIn(self.grid.cycle_start(4), events)
        self.assertEqual(events[self.grid.cycle_start(6)]["__hal_fi_ctrl_1"], 0)

    def test_a_pulse_reaching_the_end_of_the_run_drops_its_falling_edge(self):
        events = self.step._control_events(
            self.grid, "__hal_fi_ctrl_1", 19, 1, self.controls, 20
        )
        self.assertEqual(sorted(events), [0, self.grid.cycle_start(19)])
        schedule = workload.event_schedule(self.grid, events, 20)
        self.assertEqual(sum(entry[2] for entry in schedule), 20 * 8000)

    def test_the_baseline_holds_every_control_low_for_the_whole_run(self):
        events = self.step._control_events(self.grid, None, 0, 0, self.controls, 20)
        schedule = workload.event_schedule(self.grid, events, 20)
        self.assertEqual(len(schedule), 1)
        self.assertEqual(schedule[0][2], 20 * 8000)

    def test_the_terminator_transitions_every_driven_net_past_the_last_sample(self):
        events = workload.merge_event_maps(
            {0: {"RST": 1, "PIPE_IN": 0}, 8000: {"RST": 0, "PIPE_IN": 1}},
            self.step._control_events(self.grid, "__hal_fi_ctrl_1", 3, 1,
                                      self.controls, 20),
        )
        terminator = self.step._terminator_events(self.grid, events, 20)
        time = self.grid.cycle_start(20)
        self.assertEqual(list(terminator), [time])
        self.assertGreater(time, self.grid.sample_time(19))
        # every one of them is the complement of the value it currently holds, so the
        # waveform really gains an event rather than a dropped no-op write.
        self.assertEqual(terminator[time]["RST"], 1)
        self.assertEqual(terminator[time]["PIPE_IN"], 0)
        self.assertEqual(terminator[time]["__hal_fi_ctrl_1"], 1)
        self.assertEqual(terminator[time]["__hal_fi_ctrl_2"], 1)

    def test_the_terminator_complements_a_flip_still_held_at_the_end(self):
        events = workload.merge_event_maps(
            {0: {"RST": 1}},
            self.step._control_events(self.grid, "__hal_fi_ctrl_1", 19, 1,
                                      self.controls, 20),
        )
        terminator = self.step._terminator_events(self.grid, events, 20)
        self.assertEqual(terminator[self.grid.cycle_start(20)]["__hal_fi_ctrl_1"], 0)

    def test_a_workload_that_drives_nothing_is_refused(self):
        with self.assertRaises(self.step.StepError):
            self.step._terminator_events(self.grid, {}, 20)

    def test_the_terminated_schedule_fits_the_guard_cycle(self):
        events = workload.merge_event_maps(
            {0: {"RST": 1}},
            self.step._control_events(self.grid, "__hal_fi_ctrl_1", 3, 1,
                                      self.controls, 20),
        )
        full = workload.merge_event_maps(
            events, self.step._terminator_events(self.grid, events, 20)
        )
        schedule = workload.event_schedule(self.grid, full, 21)
        self.assertEqual(sum(entry[2] for entry in schedule), 21 * 8000)
        self.assertEqual(schedule[-1][0], self.grid.cycle_start(20))


# ---------------------------------------------------------------------------
# manifest, replay and recheck
# ---------------------------------------------------------------------------


class FakeConfig(object):
    """Just enough of CampaignConfig for the manifest builder."""

    def __init__(self, parsed):
        self._parsed = parsed
        self.name = parsed.name
        self.description = parsed.description
        self.document = parsed.document

    def resolved(self):
        return self._parsed.resolved()


def build_manifest(status="success"):
    (parsed, resolved, artifact, sites, faults, results, enumeration,
     summary) = sample_campaign()
    fault_records = []
    for fault in faults:
        result = results[fault["id"]]
        fault_records.append(
            dict(
                fault,
                classification=result["classification"],
                detection_latency_cycles=result["detection_latency_cycles"],
                divergence_latency_cycles=result["divergence_latency_cycles"],
            )
        )
    return manifest_module.build(
        FakeConfig(parsed),
        [{"role": "netlist", "kind": "file", "digest_algorithm": "sha256",
          "digest": "b" * 64, "sha256": "b" * 64, "size_bytes": 10,
          "resolved_path": "/somewhere/parity_counter.v"}],
        {"hal": {"version": "4.0.0"}},
        enumeration,
        sites,
        fault_records,
        None,
        summary,
        status,
        "hal_simulator",
        started_at="2026-09-07T00:00:00Z",
        finished_at="2026-09-07T00:00:10Z",
        duration_s=10.0,
        output_dir="/tmp/out",
    )


class ManifestTest(unittest.TestCase):
    def test_round_trip(self):
        manifest = build_manifest()
        directory = tempfile.mkdtemp()
        try:
            path = manifest_module.write(manifest, os.path.join(directory, "manifest.json"))
            self.assertEqual(manifest_module.read(path)["config_digest"],
                             manifest["config_digest"])
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_digest_ignores_timing_and_absolute_paths(self):
        first = build_manifest()
        second = build_manifest()
        second["campaign"]["duration_s"] = 99.0
        second["campaign"]["started_at"] = "2027-01-01T00:00:00Z"
        second["output_dir"] = "/elsewhere"
        second["inputs"][0]["resolved_path"] = "/elsewhere/parity_counter.v"
        self.assertEqual(manifest_module.manifest_digest(first),
                         manifest_module.manifest_digest(second))

    def test_digest_changes_when_a_verdict_changes(self):
        first = build_manifest()
        second = build_manifest()
        second["faults"][0]["classification"] = "silent_divergence"
        self.assertNotEqual(manifest_module.manifest_digest(first),
                            manifest_module.manifest_digest(second))

    def test_an_unknown_manifest_version_is_refused(self):
        manifest = build_manifest()
        manifest["manifest_version"] = "9.9.9"
        directory = tempfile.mkdtemp()
        try:
            path = os.path.join(directory, "manifest.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle)
            with self.assertRaises(manifest_module.ManifestError):
                manifest_module.read(path)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_summary_text_mentions_the_classes(self):
        text = manifest_module.summarize_text(build_manifest())
        self.assertIn("detected", text)
        self.assertIn("manifest digest", text)


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.manifest = build_manifest()

    def test_identical_verdicts_compare_clean(self):
        mismatches, compared = replay.compare_verdicts(
            self.manifest["faults"], copy.deepcopy(self.manifest["faults"])
        )
        self.assertEqual(mismatches, [])
        self.assertEqual(len(compared), 3)

    def test_a_changed_classification_is_a_mismatch(self):
        replayed = copy.deepcopy(self.manifest["faults"])
        replayed[0]["classification"] = "unobserved_in_window"
        mismatches, _ = replay.compare_verdicts(self.manifest["faults"], replayed)
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0].field, "classification")

    def test_a_changed_latency_is_a_mismatch(self):
        replayed = copy.deepcopy(self.manifest["faults"])
        replayed[0]["detection_latency_cycles"] = 7
        mismatches, _ = replay.compare_verdicts(self.manifest["faults"], replayed)
        self.assertEqual([mismatch.field for mismatch in mismatches],
                         ["detection_latency_cycles"])

    def test_a_missing_fault_is_a_mismatch(self):
        mismatches, _ = replay.compare_verdicts(self.manifest["faults"],
                                                self.manifest["faults"][:1])
        self.assertEqual(len(mismatches), 2)
        self.assertTrue(all(mismatch.field == "presence" for mismatch in mismatches))

    def test_faults_can_be_named_by_id_or_by_site_at_cycle(self):
        chosen = replay.faults_named(self.manifest, ["f00001"])
        self.assertEqual(chosen[0]["site"], "AUX_reg_inst")
        chosen = replay.faults_named(self.manifest, ["AUX_reg_inst@3"])
        self.assertEqual(chosen[0]["id"], "f00001")

    def test_an_unknown_fault_name_is_refused(self):
        with self.assertRaises(replay.ReplayError):
            replay.faults_named(self.manifest, ["nope"])

    def test_input_drift_is_detected(self):
        directory = tempfile.mkdtemp()
        try:
            netlist = os.path.join(directory, "parity_counter.v")
            with open(netlist, "w", encoding="utf-8") as handle:
                handle.write("// not the original\n")

            class Stub(object):
                pass

            stub = Stub()
            stub.netlist = netlist
            stub.gate_library = None
            problems = replay.verify_inputs(self.manifest, stub)
            self.assertTrue(problems)
            self.assertIn("changed", problems[0])
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_recheck_reproduces_the_recorded_verdicts(self):
        _, resolved, _, _, faults, results, _, _ = sample_campaign()
        base = {name: [0] * 20 for name in
                ["CNT0", "CNT1", "CNT2", "AUX_O", "PIPE_O", "ERR"]}
        detected = copy.deepcopy(base)
        detected["ERR"][3] = 1
        silent = copy.deepcopy(base)
        silent["AUX_O"][3] = 1
        traces = {
            "baseline": base,
            "faults": {"f00000": detected, "f00001": silent,
                       "f00002": copy.deepcopy(base)},
        }
        mismatches, compared = replay.recheck_from_traces(self.manifest, traces)
        self.assertEqual(mismatches, [])
        self.assertEqual(len(compared), 3)

    def test_recheck_catches_a_manifest_that_lies_about_its_own_traces(self):
        base = {name: [0] * 20 for name in
                ["CNT0", "CNT1", "CNT2", "AUX_O", "PIPE_O", "ERR"]}
        traces = {"baseline": base,
                  "faults": {fault["id"]: copy.deepcopy(base)
                             for fault in self.manifest["faults"]}}
        mismatches, _ = replay.recheck_from_traces(self.manifest, traces)
        self.assertTrue(mismatches)

    def test_recheck_without_the_trace_is_an_error(self):
        with self.assertRaises(replay.ReplayError):
            replay.recheck_from_traces(self.manifest, {"baseline": {}, "faults": {}})


class VerifyEnumerationTest(unittest.TestCase):
    def manifest_with_full_enumeration(self, sampling=None):
        parsed = load_config("campaign_short_window.json")
        document = copy.deepcopy(parsed.document)
        if sampling:
            document["faults"]["sampling"] = sampling
        parsed = config.parse(document, path=parsed.path)

        names = [
            "AUX_reg_inst", "CNT_reg_0_inst", "CNT_reg_1_inst", "CNT_reg_2_inst",
            "PAR_reg_inst", "PIPE_reg_0_inst", "PIPE_reg_1_inst", "PIPE_reg_2_inst",
            "PIPE_reg_3_inst",
        ]
        sites = make_sites(names)
        faults, enumeration = campaign.enumerate_faults(
            sites, campaign.resolve_cycles(parsed.fault_cycles, parsed.cycles),
            parsed.hold_cycles, parsed.sampling,
        )
        return manifest_module.build(
            FakeConfig(parsed),
            [],
            {"hal": {"version": "4.0.0"}},
            enumeration,
            [site.as_dict() for site in sites],
            [fault.as_dict() for fault in faults],
            None,
            {},
            "success",
            "hal_simulator",
        )

    def test_an_exhaustive_enumeration_re_derives_identically(self):
        self.assertEqual(
            replay.verify_enumeration(self.manifest_with_full_enumeration()), []
        )

    def test_a_seeded_sample_re_derives_identically(self):
        manifest = self.manifest_with_full_enumeration(
            {"mode": "random", "count": 12, "seed": 20260907}
        )
        self.assertEqual(len(manifest["faults"]), 12)
        self.assertEqual(replay.verify_enumeration(manifest), [])

    def test_a_tampered_fault_list_is_caught(self):
        manifest = self.manifest_with_full_enumeration()
        manifest["faults"][0]["cycle"] = 99
        problems = replay.verify_enumeration(manifest)
        self.assertTrue(problems)
        self.assertIn("differs", problems[0])


# ---------------------------------------------------------------------------
# the host-side orchestrator, against a stub executor
# ---------------------------------------------------------------------------


class StubExecution(object):
    def __init__(self, exit_code=0, timed_out=False):
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.duration_s = 0.1
        self.memory_limit_enforced = False
        self.killed = False


class StubExecutor(object):
    """Writes what a real campaign step would write, without HAL."""

    def __init__(self, behaviour="ok"):
        self.behaviour = behaviour
        self.commands = []
        self.environments = []

    def execute(self, command, cwd=None, env=None, timeout_s=None, memory_mb=None,
                stdout_path=None, stderr_path=None):
        self.commands.append(command)
        self.environments.append(env)
        for path in (stdout_path, stderr_path):
            if path:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("")
        request = protocol.read_request(env[protocol.REQUEST_ENV])
        output_dir = request["output_dir"]

        if self.behaviour == "exit_nonzero":
            return StubExecution(exit_code=3)
        if self.behaviour == "no_result":
            return StubExecution(exit_code=0)
        if self.behaviour == "timeout":
            return StubExecution(exit_code=-9, timed_out=True)

        (parsed, resolved, artifact, sites, faults, results, enumeration,
         summary) = sample_campaign()
        document = findings_module.build_document(
            artifact, resolved, "hal_simulator", sites, faults, results, enumeration,
            summary, generated_at="2026-09-07T00:00:00Z",
        )
        artifacts = [{"path": "findings.json", "role": "findings"},
                     {"path": "traces.json", "role": "traces"}]
        if self.behaviour != "missing_artifact":
            from hal_findings import serialize

            if self.behaviour == "invalid_findings":
                document["findings"][0]["status"] = "not_a_status"
                with open(os.path.join(output_dir, "findings.json"), "w",
                          encoding="utf-8") as handle:
                    json.dump(document, handle)
            else:
                serialize.write_document(document,
                                         os.path.join(output_dir, "findings.json"))
            protocol.write_json({"baseline": {}, "faults": {}},
                                os.path.join(output_dir, "traces.json"))

        fault_records = [
            dict(fault,
                 classification=results[fault["id"]]["classification"],
                 detection_latency_cycles=results[fault["id"]]["detection_latency_cycles"],
                 divergence_latency_cycles=results[fault["id"]]["divergence_latency_cycles"])
            for fault in faults
        ]
        protocol.write_json(
            protocol.result(
                "ok", artifacts=artifacts, summary=summary, sites=sites,
                faults=fault_records, enumeration=enumeration,
                instrumentation={"injector_gate_type": "XOR", "sites": [], "skipped": []},
                engine="hal_simulator",
            ),
            os.path.join(output_dir, "result.json"),
        )
        return StubExecution(exit_code=0)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        from hal_fault_campaign import runner as runner_module

        self.runner_module = runner_module
        self.directory = tempfile.mkdtemp()
        self.hal_binary = os.path.join(self.directory, "hal")
        with open(self.hal_binary, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        self.config = load_config("campaign_short_window.json")
        self.config.output_dir = os.path.join(self.directory, "out")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def make(self, behaviour="ok"):
        return self.runner_module.CampaignRunner(
            self.config, hal_binary=self.hal_binary, executor=StubExecutor(behaviour)
        )

    def test_a_successful_run_writes_a_manifest_and_exits_zero(self):
        runner = self.make()
        exit_code, manifest = runner.run()
        self.assertEqual(exit_code, 0)
        self.assertEqual(manifest["campaign"]["status"], "success")
        self.assertTrue(os.path.isfile(os.path.join(self.config.output_dir,
                                                    "manifest.json")))
        self.assertEqual(len(manifest["faults"]), 3)
        self.assertIn("findings", manifest)

    def test_the_request_travels_in_the_environment_not_on_the_command_line(self):
        runner = self.make()
        runner.run()
        command = " ".join(runner.executor.commands[0])
        self.assertIn("--python-script", command)
        self.assertNotIn("request.json", command)
        self.assertIn(protocol.REQUEST_ENV, runner.executor.environments[0])

    def test_a_nonzero_exit_fails_the_campaign(self):
        exit_code, manifest = self.make("exit_nonzero").run()
        self.assertEqual(exit_code, 1)
        self.assertEqual(manifest["campaign"]["status"], "failure")
        self.assertEqual(manifest["diagnostics"][0]["kind"], "exit_code")

    def test_exit_zero_without_a_result_is_still_a_failure(self):
        exit_code, manifest = self.make("no_result").run()
        self.assertEqual(exit_code, 1)
        self.assertEqual(manifest["diagnostics"][0]["kind"], "missing_result")

    def test_a_timeout_is_recorded_as_one(self):
        exit_code, manifest = self.make("timeout").run()
        self.assertEqual(exit_code, 1)
        self.assertEqual(manifest["campaign"]["status"], "timeout")
        self.assertEqual(manifest["diagnostics"][0]["kind"], "timeout")

    def test_a_declared_but_unwritten_artifact_fails_the_campaign(self):
        exit_code, manifest = self.make("missing_artifact").run()
        self.assertEqual(exit_code, 1)
        self.assertTrue(
            any(entry["kind"] == "missing_artifact" for entry in manifest["diagnostics"])
        )

    def test_findings_that_do_not_validate_fail_the_campaign(self):
        exit_code, manifest = self.make("invalid_findings").run()
        self.assertEqual(exit_code, 1)
        self.assertTrue(
            any(entry["kind"] == "invalid_findings" for entry in manifest["diagnostics"])
        )

    def test_a_missing_netlist_is_reported_before_hal_is_started(self):
        self.config.netlist = os.path.join(self.directory, "absent.v")
        runner = self.make()
        with self.assertRaises(self.runner_module.CampaignError):
            runner.run()


# ---------------------------------------------------------------------------
# the fixture's ground truth
# ---------------------------------------------------------------------------


class GroundTruthTest(unittest.TestCase):
    """The committed ground truth has to follow from the committed model."""

    @classmethod
    def setUpClass(cls):
        import reference_model

        cls.model = reference_model
        cls.truth = reference_model.load_ground_truth()

    def test_the_committed_file_matches_a_fresh_generation(self):
        self.assertEqual(self.model.build_ground_truth(), self.truth)

    def test_the_fault_free_baseline_is_a_counter_with_a_clean_detector(self):
        self.assertEqual(self.truth["baseline"]["ERR"], "0" * 20)
        self.assertEqual(self.truth["baseline"]["CNT0"], "01" * 10)
        self.assertEqual(self.truth["baseline"]["CNT2"][:8], "00001111")

    def test_every_class_the_issue_asks_for_appears_in_the_short_window(self):
        verdicts = self.truth["expected"]["short_window"]["verdicts"]
        classes = {entry["classification"] for entry in verdicts.values()}
        self.assertEqual(
            classes,
            {faultmodel.CLASS_DETECTED, faultmodel.CLASS_SILENT,
             faultmodel.CLASS_UNOBSERVED},
        )

    def test_the_parity_protected_registers_are_detected_with_zero_latency(self):
        verdicts = self.truth["expected"]["short_window"]["verdicts"]
        for register in ("CNT_reg_0_inst", "CNT_reg_1_inst", "CNT_reg_2_inst",
                         "PAR_reg_inst"):
            for cycle in self.truth["injection_cycles"]:
                entry = verdicts["{}@{}".format(register, cycle)]
                self.assertEqual(entry["classification"], faultmodel.CLASS_DETECTED)
                self.assertEqual(entry["detection_latency_cycles"], 0)

    def test_the_unprotected_and_late_pipeline_registers_are_silent(self):
        verdicts = self.truth["expected"]["short_window"]["verdicts"]
        for register in ("PIPE_reg_2_inst", "PIPE_reg_3_inst"):
            entry = verdicts["{}@4".format(register)]
            self.assertEqual(entry["classification"], faultmodel.CLASS_SILENT)
            self.assertIsNone(entry["detection_latency_cycles"])

    def test_early_pipeline_stages_are_unobserved_only_because_of_the_window(self):
        short = self.truth["expected"]["short_window"]["verdicts"]
        long = self.truth["expected"]["long_window"]["verdicts"]
        for register, latency in (("PIPE_reg_0_inst", 2), ("PIPE_reg_1_inst", 1),
                                  ("AUX_reg_inst", 1)):
            key = "{}@4".format(register)
            self.assertEqual(short[key]["classification"], faultmodel.CLASS_UNOBSERVED)
            self.assertEqual(long[key]["classification"], faultmodel.CLASS_SILENT)
            self.assertEqual(long[key]["divergence_latency_cycles"], latency)

    def test_the_verdicts_follow_from_the_recorded_traces(self):
        baseline = self.model.decode_trace(self.truth["baseline"])
        for name, expected in self.truth["expected"].items():
            for entry in self.truth["faults"]:
                key = "{}@{}".format(entry["site"], entry["cycle"])
                result = classify.classify(
                    baseline,
                    self.model.decode_trace(entry["trace"]),
                    entry["cycle"],
                    self.truth["workload"]["cycles"],
                    self.truth["observation"]["outputs"],
                    self.truth["observation"]["detection_signals"],
                    window=expected["window"],
                )
                self.assertEqual(
                    result["classification"],
                    expected["verdicts"][key]["classification"],
                    "{} in {}".format(key, name),
                )

    def test_the_class_counts_over_the_whole_grid_are_the_documented_ones(self):
        import collections

        counts = {
            name: dict(
                collections.Counter(
                    entry["classification"] for entry in window["verdicts"].values()
                )
            )
            for name, window in self.truth["expected"].items()
        }
        self.assertEqual(
            counts["short_window"],
            {faultmodel.CLASS_DETECTED: 16, faultmodel.CLASS_SILENT: 8,
             faultmodel.CLASS_UNOBSERVED: 12},
        )
        self.assertEqual(
            counts["long_window"],
            {faultmodel.CLASS_DETECTED: 16, faultmodel.CLASS_SILENT: 20},
        )

    def test_the_seeded_sample_draws_faults_that_change_class(self):
        parsed = load_config("campaign_long_window.json")
        sites = make_sites(self.truth["registers"])
        faults, record = campaign.enumerate_faults(
            campaign.select_sites(sites, parsed.site_include, parsed.site_exclude),
            campaign.resolve_cycles(parsed.fault_cycles, parsed.cycles),
            parsed.hold_cycles,
            parsed.sampling,
        )
        self.assertEqual(record["selected"], 12)
        short = self.truth["expected"]["short_window"]["verdicts"]
        long = self.truth["expected"]["long_window"]["verdicts"]
        changed = [
            "{}@{}".format(fault.site_name, fault.cycle)
            for fault in faults
            if short["{}@{}".format(fault.site_name, fault.cycle)]["classification"]
            != long["{}@{}".format(fault.site_name, fault.cycle)]["classification"]
        ]
        self.assertEqual(changed, ["AUX_reg_inst@3", "AUX_reg_inst@4",
                                   "PIPE_reg_1_inst@4"])

    def test_the_shipped_campaign_covers_exactly_the_ground_truth_grid(self):
        parsed = load_config("campaign_short_window.json")
        cycles = campaign.resolve_cycles(parsed.fault_cycles, parsed.cycles)
        self.assertEqual(cycles, self.truth["injection_cycles"])
        self.assertEqual(parsed.cycles, self.truth["workload"]["cycles"])
        self.assertEqual(parsed.window,
                         self.truth["expected"]["short_window"]["window"])
        self.assertEqual(
            parsed.document["workload"]["stimulus"], self.truth["workload"]["stimulus"]
        )

    def test_the_site_filter_matches_every_register_of_the_fixture(self):
        parsed = load_config("campaign_short_window.json")
        sites = make_sites(self.truth["registers"])
        selected = campaign.select_sites(sites, parsed.site_include, parsed.site_exclude)
        self.assertEqual(
            sorted(site.gate_name for site in selected), sorted(self.truth["registers"])
        )


if __name__ == "__main__":
    unittest.main()
