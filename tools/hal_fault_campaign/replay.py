"""Replaying a campaign from its manifest, and re-checking it without one.

Two different things, deliberately separate:

``recheck``
    Pure.  Re-derives every verdict from the *recorded traces* and compares it
    with what the manifest claims.  No HAL, no simulator, no netlist -- so it
    runs in CI on any machine and catches the failure mode where the
    classification code and the manifest disagree.  It cannot catch a wrong
    trace, and it says so.

``replay``
    Re-simulates.  Re-pins the inputs by content first: a manifest describes a
    campaign against a *specific* netlist, and re-running it against a different
    one is not a replay, so a digest mismatch stops the run instead of producing
    a confidently wrong comparison.  Then it injects exactly the faults the
    manifest names -- not a fresh sample -- and compares classification and both
    latencies fault by fault.

``verify_enumeration`` is the third leg: it re-derives the fault *list* from the
recorded configuration, sites and seed, and checks that it is the list the
manifest holds.  That is what proves the seed, and not just the stored list,
reproduces the campaign.
"""

import os

from hal_runner.hashing import describe_input

from . import campaign as campaign_module
from . import classify as classify_module
from . import manifest as manifest_module

__all__ = [
    "ReplayError",
    "Mismatch",
    "compare_verdicts",
    "recheck_from_traces",
    "verify_enumeration",
    "verify_inputs",
    "faults_named",
]

#: The fields a replay has to reproduce exactly.
VERDICT_FIELDS = (
    "classification",
    "detection_latency_cycles",
    "divergence_latency_cycles",
)


class ReplayError(RuntimeError):
    """Raised when a replay cannot be trusted to be a replay."""


class Mismatch(object):
    """One fault whose replayed verdict differs from the recorded one."""

    def __init__(self, fault_id, field, expected, actual):
        self.fault_id = fault_id
        self.field = field
        self.expected = expected
        self.actual = actual

    def __str__(self):
        return "{}: {} recorded {!r}, replayed {!r}".format(
            self.fault_id, self.field, self.expected, self.actual
        )

    def as_dict(self):
        return {
            "fault_id": self.fault_id,
            "field": self.field,
            "recorded": self.expected,
            "replayed": self.actual,
        }


def _index(records):
    return {record["id"]: record for record in records}


def compare_verdicts(recorded, replayed, fields=VERDICT_FIELDS):
    """Compare two fault-record lists. Returns ``(mismatches, compared_ids)``."""
    left = _index(recorded)
    right = _index(replayed)
    mismatches = []
    compared = []
    for fault_id in sorted(right):
        if fault_id not in left:
            mismatches.append(Mismatch(fault_id, "presence", "absent", "present"))
            continue
        compared.append(fault_id)
        for field in fields:
            expected = left[fault_id].get(field)
            actual = right[fault_id].get(field)
            if expected != actual:
                mismatches.append(Mismatch(fault_id, field, expected, actual))
        for field in ("site", "cycle", "hold_cycles"):
            if left[fault_id].get(field) != right[fault_id].get(field):
                mismatches.append(
                    Mismatch(
                        fault_id,
                        field,
                        left[fault_id].get(field),
                        right[fault_id].get(field),
                    )
                )
    for fault_id in sorted(set(left) - set(right)):
        mismatches.append(Mismatch(fault_id, "presence", "present", "absent"))
    return mismatches, compared


def faults_named(manifest, names=None):
    """The manifest's fault records, optionally narrowed to ``names``.

    ``names`` accepts fault IDs (``f00003``) and ``site@cycle`` (``CNT_reg_0@5``),
    because a human reading a report has the second and not the first.
    """
    records = manifest.get("faults", [])
    if not names:
        return list(records)
    by_id = {record["id"]: record for record in records}
    by_site = {
        "{}@{}".format(record["site"], record["cycle"]): record for record in records
    }
    chosen = []
    for name in names:
        record = by_id.get(name) or by_site.get(name)
        if record is None:
            raise ReplayError(
                "the manifest holds no fault {!r}; use a fault id (f00000...) or "
                "'<register>@<cycle>'".format(name)
            )
        if record not in chosen:
            chosen.append(record)
    return chosen


def verify_inputs(manifest, config):
    """Re-hash the campaign's inputs and compare with the manifest.

    Returns the list of problems; empty means the replay is against the same
    bytes the original run saw.
    """
    problems = []
    recorded = {entry["role"]: entry for entry in manifest.get("inputs", [])}
    current = {"netlist": config.netlist}
    if config.gate_library:
        current["gate_library"] = config.gate_library
    for role, path in sorted(current.items()):
        if role not in recorded:
            problems.append("the manifest records no {} input".format(role))
            continue
        if not os.path.exists(path):
            problems.append("the {} input {} no longer exists".format(role, path))
            continue
        entry = describe_input(path, role=role)
        if entry["digest_algorithm"] != recorded[role].get("digest_algorithm"):
            problems.append(
                "the {} input is now a {} and was a {}".format(
                    role, entry["kind"], recorded[role].get("kind")
                )
            )
        elif entry["digest"] != recorded[role].get("digest"):
            problems.append(
                "the {} input changed: manifest {} {}, now {}".format(
                    role,
                    recorded[role].get("digest_algorithm"),
                    recorded[role].get("digest"),
                    entry["digest"],
                )
            )
    for role in sorted(set(recorded) - set(current)):
        problems.append(
            "the manifest records a {} input that the configuration no longer "
            "names".format(role)
        )
    return problems


def verify_enumeration(manifest):
    """Re-derive the fault list from configuration + sites + seed.

    Returns the list of problems.  This is what makes the recorded seed
    meaningful: without it, a manifest's fault list is just a list.
    """
    config = manifest_module.resolved_config_of(manifest)
    sites = [campaign_module.Site.from_dict(entry) for entry in manifest.get("sites", [])]
    if not sites:
        return ["the manifest records no fault sites, so the enumeration cannot be re-derived"]

    enumeration = manifest.get("enumeration", {})
    if enumeration.get("replayed"):
        return [
            "this manifest describes a replay with an explicit fault list; re-derive the "
            "enumeration from the original campaign's manifest instead"
        ]

    faults_config = config.get("faults", {})
    selected = campaign_module.select_sites(
        sites,
        faults_config.get("sites", {}).get("include") or ["*"],
        faults_config.get("sites", {}).get("exclude") or [],
    )
    try:
        cycles = campaign_module.resolve_cycles(
            faults_config.get("cycles"), config["workload"]["cycles"]
        )
        rebuilt, record = campaign_module.enumerate_faults(
            selected,
            cycles,
            faults_config.get("hold_cycles", 1),
            faults_config.get("sampling"),
        )
    except campaign_module.EnumerationError as exc:
        return ["the enumeration could not be re-derived: {}".format(exc)]

    problems = []
    if record.get("algorithm") and record["algorithm"] != enumeration.get("algorithm"):
        problems.append(
            "the manifest was produced with sampling algorithm {!r}, this build uses "
            "{!r}".format(enumeration.get("algorithm"), record["algorithm"])
        )
    recorded = [
        (entry["id"], entry["site"], entry["cycle"], entry["hold_cycles"])
        for entry in manifest.get("faults", [])
    ]
    derived = [
        (fault.fault_id, fault.site_name, fault.cycle, fault.hold_cycles)
        for fault in rebuilt
    ]
    if recorded != derived:
        only_recorded = [entry for entry in recorded if entry not in derived]
        only_derived = [entry for entry in derived if entry not in recorded]
        problems.append(
            "the re-derived fault list differs from the recorded one "
            "({} recorded, {} derived, {} only in the manifest, {} only in the "
            "re-derivation)".format(
                len(recorded), len(derived), len(only_recorded), len(only_derived)
            )
        )
        for entry in (only_recorded + only_derived)[:5]:
            problems.append("  differing entry: {}".format(entry))
    return problems


def recheck_from_traces(manifest, traces):
    """Re-classify every fault from the recorded traces. Pure.

    Returns ``(mismatches, checked)``.  A manifest whose verdicts do not follow
    from its own evidence is a broken manifest, whatever the simulator did.
    """
    config = manifest_module.resolved_config_of(manifest)
    observation = config["observation"]
    total_cycles = config["workload"]["cycles"]
    baseline = traces["baseline"]

    replayed = []
    for record in manifest.get("faults", []):
        trace = traces.get("faults", {}).get(record["id"])
        if trace is None:
            raise ReplayError(
                "the trace file holds no trace for fault {!r}; recheck needs the "
                "traces.json the campaign wrote".format(record["id"])
            )
        result = classify_module.classify(
            baseline,
            trace,
            record["cycle"],
            total_cycles,
            observation["outputs"],
            observation["detection_signals"],
            window=observation.get("window"),
            detection_active_value=observation.get("detection_active_value", 1),
        )
        replayed.append(
            {
                "id": record["id"],
                "site": record["site"],
                "cycle": record["cycle"],
                "hold_cycles": record["hold_cycles"],
                "classification": result["classification"],
                "detection_latency_cycles": result["detection_latency_cycles"],
                "divergence_latency_cycles": result["divergence_latency_cycles"],
            }
        )
    return compare_verdicts(manifest.get("faults", []), replayed)
