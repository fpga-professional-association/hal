"""Clock-source resolution and forward clock-domain propagation.

Two steps, both purely structural:

1. **Clock resolution** (:func:`resolve_clock_net`) answers "which declared
   clock, if any, does this net carry?".  It walks backwards from a clock pin
   through buffers and inverters -- the only gates that pass a clock through
   unchanged -- and stops at the first driver that is not clock-transparent.
   That driver is a *generated* clock, and it is resolved by recursively
   resolving each of its inputs:

   * exactly one input carries a clock  -> the generated clock is **derived**
     from it (a clock gate). The domain is inherited, but the resolution is
     flagged ``ambiguous`` because the enable's timing is not analysed.
   * more than one input carries a clock -> **unknown** (a clock mux, or clock
     mixing logic).  Nothing structural distinguishes which one is live.
   * no input carries a clock, or the driver is sequential (a divider) ->
     **unknown**.

   Anything the walk cannot pin down stays ``unknown``.  That is the whole
   point: an unknown domain must never silently become a known one.

2. **Domain propagation** (:func:`propagate_domains`) pushes those domains
   forward from every sequential gate's output and every declared primary
   input, through combinational logic, to a fixpoint.  A net ends up with a set
   of *known* source domains and a set of *unknown* reasons, and both are
   reported: a net fed by ``clk_a`` and by an undeclared primary input is not
   the same as a net fed by ``clk_a`` alone.
"""

from .netlist_view import CONTROL_PIN_TYPES

__all__ = [
    "KIND_DECLARED",
    "KIND_DERIVED",
    "KIND_UNKNOWN",
    "Limits",
    "ClockResolution",
    "NetDomains",
    "EMPTY_DOMAINS",
    "resolve_clock_net",
    "resolve_gate_clocks",
    "propagate_domains",
    "CONTROL_PIN_TYPES",
]

KIND_DECLARED = "declared"
KIND_DERIVED = "derived"
KIND_UNKNOWN = "unknown"


class Limits(object):
    """Search budgets.  Hitting one yields ``unknown``, never a guess."""

    __slots__ = ("max_clock_nodes", "max_clock_depth", "max_propagation_steps",
                 "max_path_gates", "max_trace_nodes")

    def __init__(self, max_clock_nodes=4096, max_clock_depth=32,
                 max_propagation_steps=200000, max_path_gates=64, max_trace_nodes=4096):
        self.max_clock_nodes = int(max_clock_nodes)
        self.max_clock_depth = int(max_clock_depth)
        self.max_propagation_steps = int(max_propagation_steps)
        self.max_path_gates = int(max_path_gates)
        self.max_trace_nodes = int(max_trace_nodes)

    def to_json(self):
        return {name: getattr(self, name) for name in self.__slots__}


class ClockResolution(object):
    """What a clock net was resolved to, and how."""

    __slots__ = ("net_id", "domain", "kind", "reason", "path", "reached_clocks",
                 "ambiguous", "limit_hit", "enable_nets")

    def __init__(self, net_id, domain=None, kind=KIND_UNKNOWN, reason="", path=(),
                 reached_clocks=(), ambiguous=False, limit_hit=False, enable_nets=()):
        self.net_id = net_id
        self.domain = domain
        self.kind = kind
        self.reason = reason
        #: gate ids walked from the clock pin back towards the source, in order
        self.path = tuple(path)
        self.reached_clocks = tuple(sorted(set(reached_clocks)))
        self.ambiguous = bool(ambiguous)
        self.limit_hit = bool(limit_hit)
        #: nets that gate a derived clock but do not carry it
        self.enable_nets = tuple(enable_nets)

    @property
    def is_known(self):
        return self.domain is not None

    def to_json(self, view=None):
        data = {
            "net_id": self.net_id,
            "domain": self.domain,
            "kind": self.kind,
            "reason": self.reason,
            "ambiguous": self.ambiguous,
        }
        if self.reached_clocks:
            data["reached_clocks"] = list(self.reached_clocks)
        if self.limit_hit:
            data["limit_hit"] = True
        if view is not None:
            if self.path:
                data["clock_path_gates"] = [
                    view.gate(gate_id).name for gate_id in self.path if view.gate(gate_id)
                ]
            if self.enable_nets:
                data["enable_nets"] = [
                    view.net(net_id).name for net_id in self.enable_nets if view.net(net_id)
                ]
        return data

    def __repr__(self):  # pragma: no cover - debugging aid
        return "ClockResolution(net={}, domain={!r}, kind={!r})".format(
            self.net_id, self.domain, self.kind
        )


class NetDomains(object):
    """The set of clock domains that can drive a net, plus why some are unknown."""

    __slots__ = ("known", "unknown")

    def __init__(self, known=(), unknown=()):
        self.known = frozenset(known)
        self.unknown = frozenset(unknown)

    @property
    def is_empty(self):
        return not self.known and not self.unknown

    def union(self, other):
        return NetDomains(self.known | other.known, self.unknown | other.unknown)

    def to_json(self):
        data = {}
        if self.known:
            data["domains"] = sorted(self.known)
        if self.unknown:
            data["unknown_sources"] = sorted(self.unknown)
        return data

    def __eq__(self, other):
        return (
            isinstance(other, NetDomains)
            and self.known == other.known
            and self.unknown == other.unknown
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self.known, self.unknown))

    def __repr__(self):  # pragma: no cover - debugging aid
        return "NetDomains(known={}, unknown={})".format(
            sorted(self.known), sorted(self.unknown)
        )


EMPTY_DOMAINS = NetDomains()


# ---------------------------------------------------------------------------
# step 1: clock resolution
# ---------------------------------------------------------------------------


class _ClockResolver(object):
    def __init__(self, view, bound, limits):
        self.view = view
        self.bound = bound
        self.limits = limits
        self.cache = {}
        self.budget = limits.max_clock_nodes
        self.limit_hit = False

    def resolve(self, net_id, depth=0, in_progress=None):
        if net_id is None:
            return ClockResolution(
                None, reason="the gate has no net connected to its clock pin"
            )
        cached = self.cache.get(net_id)
        if cached is not None:
            return cached
        in_progress = in_progress or frozenset()
        if net_id in in_progress:
            return ClockResolution(
                net_id,
                reason="cyclic clock definition: net {!r} is generated from itself".format(
                    self._net_name(net_id)
                ),
                ambiguous=True,
            )
        if depth > self.limits.max_clock_depth or self.budget <= 0:
            self.limit_hit = True
            return ClockResolution(
                net_id,
                reason=(
                    "clock-source search hit its budget (depth {} of {}, node budget {}); "
                    "the domain is reported as unknown rather than guessed".format(
                        depth, self.limits.max_clock_depth, self.limits.max_clock_nodes
                    )
                ),
                limit_hit=True,
            )

        resolution = self._resolve_uncached(net_id, depth, in_progress | {net_id})
        if not in_progress:
            # Only a top-level resolution is context-free.  A nested one may have
            # been cut short by the cycle guard, and caching that would leak the
            # guard's answer into an unrelated query.
            self.cache[net_id] = resolution
        return resolution

    # -- internals -------------------------------------------------------

    def _net_name(self, net_id):
        net = self.view.net(net_id)
        return net.name if net is not None else "<{}>".format(net_id)

    def _resolve_uncached(self, net_id, depth, in_progress):
        path = []
        current = net_id
        seen = set()
        while True:
            self.budget -= 1
            if self.budget <= 0:
                self.limit_hit = True
                return ClockResolution(
                    net_id,
                    path=path,
                    limit_hit=True,
                    reason="clock-source search exhausted its node budget of {}".format(
                        self.limits.max_clock_nodes
                    ),
                )
            if current in seen:
                return ClockResolution(
                    net_id,
                    path=path,
                    ambiguous=True,
                    reason="combinational loop on the clock path at net {!r}".format(
                        self._net_name(current)
                    ),
                )
            seen.add(current)

            clock = self.bound.clock_for_net(current)
            if clock is not None:
                return ClockResolution(
                    net_id,
                    domain=clock.name,
                    kind=KIND_DECLARED,
                    path=path,
                    reached_clocks=[clock.name],
                    reason="net {!r} is the declared clock {!r}".format(
                        self._net_name(current), clock.name
                    ),
                )

            net = self.view.net(current)
            if net is None:
                return ClockResolution(
                    net_id, path=path, reason="clock net {} is not in the netlist".format(current)
                )
            if self.view.is_constant_net(current):
                return ClockResolution(
                    net_id,
                    path=path,
                    reason="clock pin is tied to the constant net {!r}".format(net.name),
                )
            if not net.sources:
                if net.is_global_input:
                    return ClockResolution(
                        net_id,
                        path=path,
                        reason=(
                            "clock traced to the primary input {!r}, which is not a declared "
                            "clock; declare it to give this domain a name".format(net.name)
                        ),
                    )
                return ClockResolution(
                    net_id, path=path, reason="clock net {!r} is unrouted".format(net.name)
                )
            if len(net.sources) > 1:
                return ClockResolution(
                    net_id,
                    path=path,
                    ambiguous=True,
                    reason="clock net {!r} has {} drivers".format(net.name, len(net.sources)),
                )

            driver_id = net.sources[0][0]
            driver = self.view.gate(driver_id)
            if driver is None:
                return ClockResolution(
                    net_id, path=path, reason="clock net {!r} has an unresolvable driver".format(net.name)
                )
            path.append(driver.id)

            if driver.type.is_clock_transparent:
                inputs = [
                    net_ref
                    for _, net_ref in _connected_inputs(self.view, driver)
                    if not self._is_constant(net_ref)
                ]
                if len(inputs) == 1:
                    current = inputs[0]
                    continue
                return ClockResolution(
                    net_id,
                    path=path,
                    reason=(
                        "clock buffer/inverter {!r} has {} non-constant inputs; the clock "
                        "path cannot be followed".format(driver.name, len(inputs))
                    ),
                )

            return self._resolve_generated(net_id, driver, path, depth, in_progress)

    def _is_constant(self, net_id):
        return self.view.is_constant_net(net_id)

    def _resolve_generated(self, net_id, driver, path, depth, in_progress):
        if driver.type.is_sequential:
            parents = []
            for pin in driver.clock_pins():
                parent = self.resolve(driver.fan_in.get(pin.name), depth + 1, in_progress)
                if parent.is_known:
                    parents.append(parent.domain)
            return ClockResolution(
                net_id,
                path=path,
                reached_clocks=parents,
                ambiguous=True,
                reason=(
                    "clock is generated by the sequential gate {!r} ({}); a divided or "
                    "ripple clock has no structurally determinable phase relationship to "
                    "{}, so the domain stays unknown".format(
                        driver.name,
                        driver.type.name,
                        ", ".join(sorted(set(parents))) if parents else "any declared clock",
                    )
                ),
            )

        carriers, enables, reached = [], [], set()
        limit_hit = False
        for _, input_net in _connected_inputs(self.view, driver):
            if self._is_constant(input_net):
                continue
            resolution = self.resolve(input_net, depth + 1, in_progress)
            limit_hit = limit_hit or resolution.limit_hit
            reached.update(resolution.reached_clocks)
            if resolution.is_known:
                carriers.append((input_net, resolution))
            else:
                enables.append(input_net)

        if limit_hit:
            return ClockResolution(
                net_id,
                path=path,
                reached_clocks=reached,
                limit_hit=True,
                reason=(
                    "the clock generated by {!r} could not be resolved within the search "
                    "budget".format(driver.name)
                ),
            )
        if len(carriers) == 1:
            input_net, parent = carriers[0]
            return ClockResolution(
                net_id,
                domain=parent.domain,
                kind=KIND_DERIVED,
                path=tuple(path) + tuple(parent.path),
                reached_clocks=reached or [parent.domain],
                ambiguous=True,
                enable_nets=enables,
                reason=(
                    "generated clock: {!r} ({}) derives it from {!r}. It is screened as "
                    "domain {!r}, but the enable/select timing of the clock-gating logic is "
                    "not analysed".format(
                        driver.name, driver.type.name, parent.domain, parent.domain
                    )
                ),
            )
        if len(carriers) > 1:
            domains = sorted({parent.domain for _, parent in carriers})
            return ClockResolution(
                net_id,
                path=path,
                reached_clocks=reached,
                ambiguous=True,
                reason=(
                    "ambiguous generated clock: {!r} ({}) combines {} clock-carrying inputs "
                    "({}). Which one reaches the register is a function of the select/enable "
                    "logic, which this structural screen does not evaluate, so the domain "
                    "stays unknown".format(
                        driver.name, driver.type.name, len(carriers), ", ".join(domains)
                    )
                ),
            )
        return ClockResolution(
            net_id,
            path=path,
            reached_clocks=reached,
            reason=(
                "clock is generated by {!r} ({}) from inputs that carry no declared "
                "clock".format(driver.name, driver.type.name)
            ),
        )


def _connected_inputs(view, gate):
    """``(pin, net id)`` for every connected input pin of *gate*, pin-name ordered."""
    result = []
    for pin in gate.type.input_pins():
        net_id = gate.fan_in.get(pin.name)
        if net_id is not None:
            result.append((pin, net_id))
    result.sort(key=lambda entry: entry[0].name)
    return result


def resolve_clock_net(view, bound, net_id, limits=None):
    """Resolve a single net as a clock (convenience wrapper, no shared cache)."""
    return _ClockResolver(view, bound, limits or Limits()).resolve(net_id)


def resolve_gate_clocks(view, bound, limits=None):
    """Resolve the clock of every sequential gate.

    :returns: ``(resolutions, problems)`` where ``resolutions`` maps a gate id
        to a :class:`ClockResolution` and ``problems`` lists sequential gates
        whose clock pin count is not exactly one (those get an ``unknown``
        resolution and a recorded reason).
    """
    limits = limits or Limits()
    resolver = _ClockResolver(view, bound, limits)
    resolutions, problems = {}, []

    for gate in view.sequential_gates():
        clock_pins = gate.clock_pins()
        if len(clock_pins) != 1:
            reason = (
                "sequential gate {!r} ({}) has {} input pins of type 'clock'; hal_cdc only "
                "screens single-clock sequential primitives".format(
                    gate.name, gate.type.name, len(clock_pins)
                )
            )
            problems.append(reason)
            resolutions[gate.id] = ClockResolution(None, reason=reason)
            continue
        net_id = gate.fan_in.get(clock_pins[0].name)
        resolution = resolver.resolve(net_id)
        if net_id is None:
            resolution = ClockResolution(
                None,
                reason="sequential gate {!r} has nothing connected to its clock pin {!r}".format(
                    gate.name, clock_pins[0].name
                ),
            )
        resolutions[gate.id] = resolution

    return resolutions, problems


# ---------------------------------------------------------------------------
# step 2: forward propagation
# ---------------------------------------------------------------------------


def propagate_domains(view, bound, clock_resolutions, limits=None):
    """Propagate clock domains forward to every net.

    :returns: ``(domains, steps, limit_hit)`` where ``domains`` maps a net id to
        a :class:`NetDomains`.
    """
    limits = limits or Limits()
    domains = {net_id: EMPTY_DOMAINS for net_id in view.nets}
    fixed = set()

    def seed(net_id, value):
        if net_id is None or net_id not in domains:
            return
        domains[net_id] = value
        fixed.add(net_id)

    # constants carry no domain at all
    for net in view.sorted_nets():
        if net.is_constant:
            seed(net.id, EMPTY_DOMAINS)
    for gate in view.sorted_gates():
        if gate.type.is_constant:
            for net_id in gate.output_nets():
                seed(net_id, EMPTY_DOMAINS)

    # primary inputs
    for net in view.sorted_nets():
        if net.id in fixed or not net.is_global_input or net.sources:
            continue
        declared_input = bound.input_nets.get(net.id)
        clock = bound.clock_for_net(net.id)
        reset = bound.reset_nets.get(net.id)
        if clock is not None:
            seed(net.id, NetDomains(known=[clock.name]))
        elif declared_input is not None:
            seed(net.id, NetDomains(known=[declared_input.clock]))
        elif reset is not None and reset.synchronous_to:
            seed(net.id, NetDomains(known=[reset.synchronous_to]))
        elif reset is not None:
            seed(net.id, NetDomains(unknown=["asynchronous_reset:" + reset.name]))
        else:
            seed(net.id, NetDomains(unknown=["primary_input:" + net.name]))

    # declared clock/reset nets that are generated inside the design
    for net_id, clock in sorted(bound.clock_nets.items()):
        if net_id not in fixed:
            seed(net_id, NetDomains(known=[clock.name]))
    for net_id, reset in sorted(bound.reset_nets.items()):
        if net_id in fixed:
            continue
        if reset.synchronous_to:
            seed(net_id, NetDomains(known=[reset.synchronous_to]))

    # sequential and opaque gate outputs
    for gate in view.sorted_gates():
        if gate.type.is_latch:
            value = NetDomains(unknown=["latch:" + gate.name])
        elif gate.type.is_sequential:
            resolution = clock_resolutions.get(gate.id)
            if resolution is not None and resolution.is_known:
                value = NetDomains(known=[resolution.domain])
            else:
                value = NetDomains(unknown=["unresolved_clock:" + gate.name])
        elif gate.type.is_black_box:
            value = NetDomains(unknown=["black_box:" + gate.name])
        else:
            continue
        for net_id in gate.output_nets():
            if net_id not in bound.clock_nets:
                seed(net_id, value)

    # combinational fixpoint
    combinational = [
        gate
        for gate in view.sorted_gates()
        if not gate.type.is_sequential
        and not gate.type.is_constant
        and not gate.type.is_black_box
    ]
    driven_by = {}
    for gate in combinational:
        for _, net_id in _connected_inputs(view, gate):
            driven_by.setdefault(net_id, []).append(gate)

    queue = list(combinational)
    queued = {gate.id for gate in queue}
    steps = 0
    limit_hit = False
    while queue:
        if steps >= limits.max_propagation_steps:
            limit_hit = True
            break
        steps += 1
        gate = queue.pop(0)
        queued.discard(gate.id)
        value = EMPTY_DOMAINS
        for _, net_id in _connected_inputs(view, gate):
            if view.is_constant_net(net_id):
                continue
            value = value.union(domains.get(net_id, EMPTY_DOMAINS))
        for net_id in gate.output_nets():
            if net_id in fixed or net_id not in domains:
                continue
            merged = domains[net_id].union(value)
            if merged != domains[net_id]:
                domains[net_id] = merged
                for successor in driven_by.get(net_id, []):
                    if successor.id not in queued:
                        queue.append(successor)
                        queued.add(successor.id)

    return domains, steps, limit_hit
