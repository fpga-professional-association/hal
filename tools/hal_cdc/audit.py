"""The analysis entry point: netlist + declarations -> :class:`AuditResult`.

``run_audit`` does no I/O and imports no ``hal_py``.  It takes a
:class:`~hal_cdc.netlist_view.NetlistView`, so the same code path is exercised
by the fixture-driven unit tests and by the real hal_py-driven run.
"""

from . import patterns, resets
from .crossings import ROLE_CONTROL, enumerate_crossings
from .domains import Limits, propagate_domains, resolve_gate_clocks

__all__ = ["ClassifiedCrossing", "AuditResult", "run_audit"]


class ClassifiedCrossing(object):
    """A crossing plus its verdict and the waiver that covers it, if any."""

    __slots__ = ("crossing", "classification", "waiver")

    def __init__(self, crossing, classification, waiver=None):
        self.crossing = crossing
        self.classification = classification
        self.waiver = waiver

    @property
    def effective_class(self):
        if self.waiver is not None:
            return patterns.CLASS_WAIVED
        return self.classification.name

    @property
    def is_alarming(self):
        return self.waiver is None and self.classification.name in (
            patterns.CLASS_UNSYNCHRONIZED,
            patterns.CLASS_UNSYNCHRONIZED_CONTROL,
        )

    def to_json(self, view):
        data = self.crossing.to_json(view)
        data.update(self.classification.to_json(view))
        if self.waiver is not None:
            data["waiver"] = self.waiver.id
            data["waived_classification"] = self.classification.name
        return data


class AuditResult(object):
    """Everything one audit run learned, in a report-agnostic form."""

    def __init__(self, view, bound, limits):
        self.view = view
        self.bound = bound
        self.limits = limits
        self.clock_resolutions = {}
        self.clock_problems = []
        self.net_domains = {}
        self.propagation_steps = 0
        self.propagation_limit_hit = False
        self.crossings = []
        self.unknown_inputs = []
        self.reset_targets = []
        self.reset_domain_map = {}
        self.clock_tree = None
        self.unsupported_gate_types = {}
        self.unused_waivers = []

    # -- derived views ---------------------------------------------------

    @property
    def domains(self):
        """Sorted names of every domain a register was actually assigned to."""
        return sorted(
            {
                resolution.domain
                for resolution in self.clock_resolutions.values()
                if resolution.is_known
            }
        )

    def registers_by_domain(self):
        result = {}
        for gate_id, resolution in sorted(self.clock_resolutions.items()):
            result.setdefault(resolution.domain, []).append(gate_id)
        return result

    def ambiguous_clocks(self):
        """Gate ids whose clock resolution was ambiguous or unknown, by reason."""
        grouped = {}
        for gate_id, resolution in sorted(self.clock_resolutions.items()):
            if resolution.is_known and not resolution.ambiguous:
                continue
            grouped.setdefault(resolution.reason, []).append(gate_id)
        return grouped

    @property
    def alarming_crossings(self):
        return [entry for entry in self.crossings if entry.is_alarming]

    def summary(self):
        counts = {}
        for entry in self.crossings:
            counts[entry.effective_class] = counts.get(entry.effective_class, 0) + 1
        release_counts = {}
        for target in self.reset_targets:
            release_counts[target.release] = release_counts.get(target.release, 0) + 1
        return {
            "declared_clocks": len(self.bound.clock_nets),
            "declared_resets": len(self.bound.reset_nets),
            "sequential_gates": len(self.view.sequential_gates()),
            "domains": self.domains,
            "registers_with_unknown_domain": sum(
                1 for res in self.clock_resolutions.values() if not res.is_known
            ),
            "crossings": len(self.crossings),
            "crossings_by_classification": counts,
            "sequential_inputs_with_unknown_domain": len(self.unknown_inputs),
            "reset_targets": len(self.reset_targets),
            "reset_release_classification": release_counts,
        }


def _collect_unsupported(view):
    """Gate types this pass cannot reason about, with an example gate each."""
    unsupported = {}
    for gate in view.sorted_gates():
        gate_type = gate.type
        reason = None
        if gate_type.is_latch:
            reason = (
                "level-sensitive capture is not modelled; a latch on a crossing path is "
                "reported but never recognised as a synchroniser"
            )
        elif gate_type.is_black_box:
            reason = (
                "HAL classifies this gate type as neither combinational nor sequential, so "
                "hal_cdc cannot tell what it does to a domain; its outputs are treated as "
                "unknown"
            )
        elif gate_type.is_sequential and len(gate_type.pins_of_type("clock")) != 1:
            reason = (
                "the gate type has {} input pins of type 'clock'; hal_cdc only screens "
                "single-clock sequential primitives".format(
                    len(gate_type.pins_of_type("clock"))
                )
            )
        if reason is None:
            continue
        entry = unsupported.setdefault(
            gate_type.name,
            {
                "reason": reason,
                "count": 0,
                "properties": sorted(gate_type.properties),
                "gates": [],
            },
        )
        entry["count"] += 1
        if len(entry["gates"]) < 3:
            entry["gates"].append(gate.id)
    return unsupported


def run_audit(view, bound, limits=None, clock_tree=None):
    """Run the whole structural screen over *view*.

    :param view: a :class:`~hal_cdc.netlist_view.NetlistView`.
    :param bound: :class:`~hal_cdc.declarations.BoundDeclarations`.
    :param limits: search budgets; defaults to :class:`~hal_cdc.domains.Limits`.
    :param clock_tree: optional :class:`~hal_cdc.clock_tree.ClockTreeInfo`.
    """
    limits = limits or Limits()
    result = AuditResult(view, bound, limits)
    result.clock_tree = clock_tree

    result.clock_resolutions, result.clock_problems = resolve_gate_clocks(
        view, bound, limits
    )
    result.net_domains, result.propagation_steps, result.propagation_limit_hit = (
        propagate_domains(view, bound, result.clock_resolutions, limits)
    )

    raw_crossings, result.unknown_inputs = enumerate_crossings(
        view, bound, result.clock_resolutions, result.net_domains, limits
    )

    used_waivers = set()
    for crossing in raw_crossings:
        classification = patterns.classify_crossing(
            view, crossing, result.clock_resolutions, result.net_domains, limits
        )
        waiver = None
        if not classification.is_recognized:
            destination = view.gate(crossing.destination_gate_id)
            net = view.net(crossing.net_id)
            waiver = bound.find_waiver(
                "control_path" if crossing.role == ROLE_CONTROL else "crossing",
                source_domain=crossing.source_domain,
                destination_domain=crossing.destination_domain,
                gate_name=destination.name if destination is not None else None,
                net_name=net.name if net is not None else None,
            )
            if waiver is not None:
                used_waivers.add(waiver.id)
        result.crossings.append(ClassifiedCrossing(crossing, classification, waiver))

    result.reset_targets, result.reset_domain_map = resets.analyze_resets(
        view, bound, result.clock_resolutions, limits
    )
    for target in result.reset_targets:
        if target.release in resets.SAFE_RELEASES:
            continue
        gate = view.gate(target.gate_id)
        waiver = bound.find_waiver(
            "reset_release",
            source_domain=target.reset_name,
            destination_domain=target.domain,
            gate_name=gate.name if gate is not None else None,
            reset_name=target.reset_name,
        )
        if waiver is not None:
            used_waivers.add(waiver.id)
            target.waiver = waiver

    result.unused_waivers = [
        waiver for waiver in bound.waivers if waiver.id not in used_waivers
    ]
    result.unsupported_gate_types = _collect_unsupported(view)
    return result
