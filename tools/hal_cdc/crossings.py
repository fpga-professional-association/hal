"""Enumerate the paths that leave one clock domain and enter another.

A crossing is recorded per ``(capturing sequential gate, input pin, source
domain)``.  That is the smallest unit a reviewer can act on, and it is also the
unit a waiver can be scoped to.

Every crossing carries the gates that support it: the sequential gates in the
source domain that originate it, and the combinational gates on the path
between them and the capturing register.  Without those, a report that says
"clk_a reaches clk_b" is unactionable.
"""

from .domains import EMPTY_DOMAINS
from .netlist_view import CONTROL_PIN_TYPES

__all__ = [
    "ROLE_DATA",
    "ROLE_CONTROL",
    "Crossing",
    "UnknownInput",
    "trace_domain_sources",
    "enumerate_crossings",
]

ROLE_DATA = "data"
ROLE_CONTROL = "control"


class Crossing(object):
    """One path from ``source_domain`` into ``destination_domain``."""

    __slots__ = (
        "destination_gate_id",
        "destination_pin",
        "role",
        "source_domain",
        "destination_domain",
        "net_id",
        "source_gate_ids",
        "source_net_ids",
        "path_gate_ids",
        "direct",
        "mixed",
        "unknown_sources",
        "limit_hit",
    )

    def __init__(self, destination_gate_id, destination_pin, role, source_domain,
                 destination_domain, net_id, source_gate_ids=(), source_net_ids=(),
                 path_gate_ids=(), direct=True, mixed=False, unknown_sources=(),
                 limit_hit=False):
        self.destination_gate_id = destination_gate_id
        self.destination_pin = destination_pin
        self.role = role
        self.source_domain = source_domain
        self.destination_domain = destination_domain
        self.net_id = net_id
        self.source_gate_ids = tuple(source_gate_ids)
        self.source_net_ids = tuple(source_net_ids)
        self.path_gate_ids = tuple(path_gate_ids)
        self.direct = bool(direct)
        self.mixed = bool(mixed)
        self.unknown_sources = tuple(sorted(unknown_sources))
        self.limit_hit = bool(limit_hit)

    @property
    def key(self):
        return (
            self.source_domain,
            self.destination_domain,
            self.role,
            self.destination_gate_id,
            self.destination_pin.name,
        )

    def to_json(self, view):
        data = {
            "source_domain": self.source_domain,
            "destination_domain": self.destination_domain,
            "role": self.role,
            "destination_gate": _gate_name(view, self.destination_gate_id),
            "destination_pin": self.destination_pin.name,
            "destination_pin_type": self.destination_pin.type,
            "net": _net_name(view, self.net_id),
            "source_gates": [_gate_name(view, gid) for gid in self.source_gate_ids],
            "path_gates": [_gate_name(view, gid) for gid in self.path_gate_ids],
            "directly_captured": self.direct,
        }
        if self.source_net_ids:
            data["source_nets"] = [_net_name(view, nid) for nid in self.source_net_ids]
        if self.mixed:
            data["mixed_source_domains"] = True
        if self.unknown_sources:
            data["unknown_sources_on_net"] = list(self.unknown_sources)
        if self.limit_hit:
            data["path_search_truncated"] = True
        return data


class UnknownInput(object):
    """A sequential gate input fed by something whose domain is not known."""

    __slots__ = ("gate_id", "pin", "net_id", "reasons", "role", "destination_domain")

    def __init__(self, gate_id, pin, net_id, reasons, role, destination_domain):
        self.gate_id = gate_id
        self.pin = pin
        self.net_id = net_id
        self.reasons = tuple(sorted(reasons))
        self.role = role
        self.destination_domain = destination_domain

    def to_json(self, view):
        return {
            "gate": _gate_name(view, self.gate_id),
            "pin": self.pin.name,
            "pin_type": self.pin.type,
            "role": self.role,
            "net": _net_name(view, self.net_id),
            "destination_domain": self.destination_domain,
            "unknown_sources": list(self.reasons),
        }


def _gate_name(view, gate_id):
    gate = view.gate(gate_id)
    return gate.name if gate is not None else "<gate {}>".format(gate_id)


def _net_name(view, net_id):
    net = view.net(net_id)
    return net.name if net is not None else "<net {}>".format(net_id)


def trace_domain_sources(view, bound, net_id, source_domain, net_domains,
                         clock_resolutions, limits):
    """Walk back from *net_id* to the origins of *source_domain*.

    Only nets that actually carry ``source_domain`` are followed, so the walk
    stays inside the cone that supports the crossing instead of exploring the
    whole fan-in.

    :returns: ``(source_gate_ids, source_net_ids, path_gate_ids, direct, limit_hit)``
    """
    source_gates, source_nets, path_gates = set(), set(), set()
    direct = True
    limit_hit = False
    visited = set()
    stack = [net_id]
    budget = limits.max_trace_nodes

    while stack:
        if budget <= 0:
            limit_hit = True
            break
        budget -= 1
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)

        net = view.net(current)
        if net is None:
            continue

        clock = bound.clock_for_net(current)
        declared_input = bound.input_nets.get(current)
        if (clock is not None and clock.name == source_domain) or (
            declared_input is not None and declared_input.clock == source_domain
        ):
            source_nets.add(current)
            if not net.sources:
                continue

        for gate_id, _pin_name in net.sources:
            gate = view.gate(gate_id)
            if gate is None:
                continue
            if gate.type.is_sequential:
                resolution = clock_resolutions.get(gate_id)
                if resolution is not None and resolution.domain == source_domain:
                    source_gates.add(gate_id)
                continue
            if gate.type.is_constant:
                continue
            path_gates.add(gate_id)
            if not gate.type.is_clock_transparent:
                direct = False
            for pin in gate.type.input_pins():
                input_net = gate.fan_in.get(pin.name)
                if input_net is None or input_net in visited:
                    continue
                if source_domain in net_domains.get(input_net, EMPTY_DOMAINS).known:
                    stack.append(input_net)

    return (
        sorted(source_gates),
        sorted(source_nets),
        sorted(path_gates),
        direct,
        limit_hit,
    )


def _owned_by_reset_analysis(pin, reasons):
    """True when the reset-release pass already reports this unknown source.

    An asynchronous reset arriving at a reset/set pin is not an unclassified
    crossing -- :mod:`hal_cdc.resets` says exactly how it is released.  Emitting
    both would double-count the same structure.
    """
    if pin.type not in ("reset", "set"):
        return False
    return all(reason.startswith("asynchronous_reset:") for reason in reasons)


def enumerate_crossings(view, bound, clock_resolutions, net_domains, limits):
    """Find every domain crossing that lands on a sequential gate input.

    :returns: ``(crossings, unknown_inputs)``, both deterministically ordered.
    """
    crossings, unknown_inputs = [], []

    for gate in view.sequential_gates():
        resolution = clock_resolutions.get(gate.id)
        destination_domain = resolution.domain if resolution is not None else None
        if destination_domain is None:
            # The capturing register's own domain is unknown; reporting its inputs
            # as "crossings" would invent a destination domain.  The unresolved
            # clock is reported separately by the domain findings.
            continue

        for pin, net_id in gate.data_input_nets():
            if view.is_constant_net(net_id):
                continue
            role = ROLE_CONTROL if pin.type in CONTROL_PIN_TYPES else ROLE_DATA
            domains = net_domains.get(net_id, EMPTY_DOMAINS)
            foreign = sorted(domains.known - {destination_domain})

            if domains.unknown and not _owned_by_reset_analysis(pin, domains.unknown):
                unknown_inputs.append(
                    UnknownInput(
                        gate.id, pin, net_id, domains.unknown, role, destination_domain
                    )
                )

            for source_domain in foreign:
                sources, nets, path, direct, limit_hit = trace_domain_sources(
                    view, bound, net_id, source_domain, net_domains,
                    clock_resolutions, limits
                )
                crossings.append(
                    Crossing(
                        gate.id,
                        pin,
                        role,
                        source_domain,
                        destination_domain,
                        net_id,
                        source_gate_ids=sources,
                        source_net_ids=nets,
                        path_gate_ids=path,
                        direct=direct,
                        mixed=len(domains.known) > 1 or bool(domains.unknown),
                        unknown_sources=domains.unknown,
                        limit_hit=limit_hit,
                    )
                )

    crossings.sort(key=lambda crossing: crossing.key)
    unknown_inputs.sort(key=lambda entry: (entry.gate_id, entry.pin.name))
    return crossings, unknown_inputs
