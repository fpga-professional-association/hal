"""Reset fan-out and reset *release* (deassertion) screening.

Asserting an asynchronous reset is safe by construction; releasing it is not.
If the reset input of a register bank goes inactive close to a clock edge, some
registers leave reset a cycle before the others.  The standard fix is a reset
synchroniser: two flip-flops clocked by the destination domain, asynchronously
cleared by the raw reset, with a constant one shifted through them.

This module recognises exactly that structure and reports everything else as a
suspicious release pattern.  It says nothing about assertion, recovery/removal
timing, or reset trees -- those are timing questions.
"""

__all__ = [
    "RELEASE_DECLARED_SYNCHRONOUS",
    "RELEASE_SYNCHRONIZED",
    "RELEASE_SYNCHRONIZER_STAGE",
    "RELEASE_REREGISTERED",
    "RELEASE_ASYNCHRONOUS",
    "RELEASE_THROUGH_LOGIC",
    "RELEASE_UNKNOWN",
    "SAFE_RELEASES",
    "ResetTarget",
    "analyze_resets",
]

RELEASE_DECLARED_SYNCHRONOUS = "declared_synchronous"
RELEASE_SYNCHRONIZED = "synchronized_deassert"
RELEASE_SYNCHRONIZER_STAGE = "reset_synchronizer_stage"
RELEASE_REREGISTERED = "reregistered_deassert"
RELEASE_ASYNCHRONOUS = "asynchronous_unsynchronized_deassert"
RELEASE_THROUGH_LOGIC = "deassert_through_combinational_logic"
RELEASE_UNKNOWN = "unknown"

#: Release patterns this pass accepts without raising severity.
SAFE_RELEASES = (
    RELEASE_DECLARED_SYNCHRONOUS,
    RELEASE_SYNCHRONIZED,
    RELEASE_SYNCHRONIZER_STAGE,
)

_RESET_PIN_TYPES = ("reset", "set")


class ResetTarget(object):
    """One sequential gate whose reset/set pin a declared reset reaches."""

    __slots__ = ("reset_name", "gate_id", "pin", "net_id", "domain", "release", "reason",
                 "stage_gate_ids", "through_logic", "waiver")

    def __init__(self, reset_name, gate_id, pin, net_id, domain, release, reason,
                 stage_gate_ids=(), through_logic=False):
        self.waiver = None
        self.reset_name = reset_name
        self.gate_id = gate_id
        self.pin = pin
        self.net_id = net_id
        self.domain = domain
        self.release = release
        self.reason = reason
        self.stage_gate_ids = tuple(stage_gate_ids)
        self.through_logic = bool(through_logic)

    def to_json(self, view):
        gate = view.gate(self.gate_id)
        net = view.net(self.net_id)
        data = {
            "reset": self.reset_name,
            "gate": gate.name if gate is not None else "<gate {}>".format(self.gate_id),
            "pin": self.pin.name,
            "pin_type": self.pin.type,
            "net": net.name if net is not None else "<net {}>".format(self.net_id),
            "clock_domain": self.domain,
            "release": self.release,
        }
        if self.stage_gate_ids:
            data["reset_synchronizer_stages"] = [
                view.gate(gate_id).name
                for gate_id in self.stage_gate_ids
                if view.gate(gate_id) is not None
            ]
        if self.through_logic:
            data["reaches_through_combinational_logic"] = True
        if self.waiver is not None:
            data["waiver"] = self.waiver.id
        return data

    @property
    def is_alarming(self):
        return self.waiver is None and self.release not in SAFE_RELEASES


def _transparent_back(view, net_id, limits):
    """Walk back through buffers/inverters; return ``(net id, gate ids)``."""
    chain = []
    current = net_id
    seen = set()
    budget = limits.max_path_gates
    while budget > 0:
        budget -= 1
        if current in seen:
            return current, chain
        seen.add(current)
        net = view.net(current)
        if net is None or len(net.sources) != 1:
            return current, chain
        gate = view.gate(net.sources[0][0])
        if gate is None or not gate.type.is_clock_transparent:
            return current, chain
        inputs = [
            gate.fan_in[pin.name]
            for pin in gate.type.input_pins()
            if gate.fan_in.get(pin.name) is not None
        ]
        inputs = [net_id for net_id in inputs if not _is_constant(view, net_id)]
        if len(inputs) != 1:
            return current, chain
        chain.append(gate.id)
        current = inputs[0]
    return current, chain


def _is_constant(view, net_id):
    return view.is_constant_net(net_id)


def _reaches_reset_net(view, net_id, reset_net_id, limits):
    """True if *net_id* is the declared reset net, up to buffers and inverters."""
    root, _chain = _transparent_back(view, net_id, limits)
    return root == reset_net_id


def _forward_targets(view, reset_net_id, limits):
    """Every sequential reset/set pin the reset net reaches.

    Buffers and inverters are followed transparently; any other combinational
    gate is still followed, but the target is marked ``through_logic`` because
    a reset that is combined with something else is not the raw reset any more.
    """
    targets = []
    visited = set()
    stack = [(reset_net_id, False)]
    budget = limits.max_trace_nodes

    while stack and budget > 0:
        budget -= 1
        net_id, through_logic = stack.pop()
        key = (net_id, through_logic)
        if key in visited:
            continue
        visited.add(key)

        net = view.net(net_id)
        if net is None:
            continue
        for gate_id, pin_name in net.destinations:
            gate = view.gate(gate_id)
            if gate is None:
                continue
            pin = gate.type.pin(pin_name)
            if pin is None:
                continue
            if gate.type.is_sequential:
                if pin.type in _RESET_PIN_TYPES:
                    targets.append((gate_id, pin, net_id, through_logic))
                continue
            if gate.type.is_constant:
                continue
            next_through = through_logic or not gate.type.is_clock_transparent
            for output in gate.output_nets():
                stack.append((output, next_through))

    targets.sort(key=lambda entry: (entry[0], entry[1].name))
    return targets


def _is_reset_synchronizer_stage(view, gate, domain, reset_net_id, clock_resolutions, limits):
    """True if *gate* is a flip-flop in *domain* asynchronously cleared by the reset."""
    if gate is None or not gate.type.is_ff:
        return False
    resolution = clock_resolutions.get(gate.id)
    if resolution is None or resolution.domain != domain:
        return False
    for pin in gate.type.input_pins():
        if pin.type not in _RESET_PIN_TYPES:
            continue
        net_id = gate.fan_in.get(pin.name)
        if net_id is not None and _reaches_reset_net(view, net_id, reset_net_id, limits):
            return True
    return False


def _is_synchronizer_own_stage(view, gate, domain, reset_net_id, clock_resolutions, limits):
    """True if *gate* is one of the flip-flops that *form* a reset synchroniser.

    Those flip-flops are asynchronously cleared by the raw reset on purpose, so
    reporting them as an unsynchronised release would be exactly backwards.  The
    tell is what feeds them: a constant, or the preceding stage of the chain.
    """
    if not _is_reset_synchronizer_stage(
        view, gate, domain, reset_net_id, clock_resolutions, limits
    ):
        return False
    for pin in gate.type.input_pins():
        if pin.type not in ("data", "none"):
            continue
        net_id = gate.fan_in.get(pin.name)
        if net_id is None:
            continue
        if _is_constant(view, net_id):
            return True
        root, _chain = _transparent_back(view, net_id, limits)
        source = view.net(root)
        if source is None or not source.sources:
            continue
        if _is_reset_synchronizer_stage(
            view, view.gate(source.sources[0][0]), domain, reset_net_id,
            clock_resolutions, limits
        ):
            return True
    return False


def _classify_release(view, reset, reset_net_id, gate, domain, clock_resolutions,
                      limits, pin_net_id):
    if domain is None:
        return RELEASE_UNKNOWN, (
            "the clock of {!r} could not be resolved, so nothing can be said about how "
            "reset {!r} is released relative to it".format(gate.name, reset.name)
        ), ()

    if reset.synchronous_to == domain:
        return RELEASE_DECLARED_SYNCHRONOUS, (
            "reset {!r} is declared synchronous to {!r} and {!r} is clocked by it; the "
            "release is taken on the user's word, not verified".format(
                reset.name, domain, gate.name
            )
        ), ()

    if _is_synchronizer_own_stage(
        view, gate, domain, reset_net_id, clock_resolutions, limits
    ):
        return RELEASE_SYNCHRONIZER_STAGE, (
            "{!r} is itself a stage of a reset synchroniser for domain {}: a flip-flop "
            "clocked by {} and asynchronously cleared by {!r}, fed by a constant or by the "
            "preceding stage. Its asynchronous reset is the intended structure".format(
                gate.name, domain, domain, reset.name
            )
        ), ()

    root_net, _chain = _transparent_back(view, pin_net_id, limits)
    if root_net == reset_net_id:
        return RELEASE_ASYNCHRONOUS, (
            "reset {!r} reaches the {!r} pin of {!r} (clocked by {}) through buffers and "
            "inverters only. Asserting it is safe; *releasing* it is asynchronous to {}, so "
            "the registers of this bank can leave reset on different clock edges. The usual "
            "fix is a two-flop reset synchroniser clocked by {}".format(
                reset.name, _pin_name(view, gate, pin_net_id), gate.name, domain, domain,
                domain,
            )
        ), ()

    net = view.net(root_net)
    if net is None or not net.sources:
        return RELEASE_UNKNOWN, (
            "the driver of the reset pin of {!r} could not be resolved".format(gate.name)
        ), ()

    driver = view.gate(net.sources[0][0])
    if driver is None:
        return RELEASE_UNKNOWN, "the reset pin of {!r} has an unresolvable driver".format(
            gate.name
        ), ()

    if not driver.type.is_sequential:
        return RELEASE_THROUGH_LOGIC, (
            "the reset of {!r} is produced by the combinational gate {!r} ({}) rather than "
            "by a stage clocked in {}; the release is not synchronised to that "
            "domain".format(gate.name, driver.name, driver.type.name, domain)
        ), ()

    if not _is_reset_synchronizer_stage(
        view, driver, domain, reset_net_id, clock_resolutions, limits
    ):
        return RELEASE_REREGISTERED, (
            "the reset of {!r} comes from the sequential gate {!r}, but that stage is not "
            "asynchronously cleared by {!r} while being clocked by {}, so the recognised "
            "reset-synchroniser pattern does not match".format(
                gate.name, driver.name, reset.name, domain
            )
        ), (driver.id,)

    # second stage: the flip-flop that feeds this one
    stages = [driver.id]
    previous = None
    for pin in driver.type.input_pins():
        if pin.type not in ("data", "none"):
            continue
        net_id = driver.fan_in.get(pin.name)
        if net_id is None or _is_constant(view, net_id):
            continue
        root, _ = _transparent_back(view, net_id, limits)
        source_net = view.net(root)
        if source_net is None or not source_net.sources:
            continue
        candidate = view.gate(source_net.sources[0][0])
        if _is_reset_synchronizer_stage(
            view, candidate, domain, reset_net_id, clock_resolutions, limits
        ):
            previous = candidate
            break

    if previous is None:
        return RELEASE_REREGISTERED, (
            "the reset of {!r} is registered once by {!r} in domain {}, but no second "
            "asynchronously-cleared stage was found; a single stage does not make the "
            "release safe".format(gate.name, driver.name, domain)
        ), tuple(stages)

    stages.insert(0, previous.id)
    return RELEASE_SYNCHRONIZED, (
        "reset {!r} is released through the two-stage reset synchroniser {} -> {}, both "
        "clocked by {} and both asynchronously cleared by {!r}. Structural recognition "
        "only: recovery/removal timing is not checked".format(
            reset.name, previous.name, driver.name, domain, reset.name
        )
    ), tuple(stages)


def _pin_name(view, gate, net_id):
    for pin in gate.type.input_pins():
        if gate.fan_in.get(pin.name) == net_id:
            return pin.name
    return "reset"


def analyze_resets(view, bound, clock_resolutions, limits):
    """Classify how every declared reset is released in every domain it reaches.

    :returns: ``(targets, domain_map)`` -- the per-register verdicts and a map
        from reset name to the sorted set of clock domains it reaches.
    """
    targets = []
    domain_map = {}

    for net_id, reset in sorted(bound.reset_nets.items(), key=lambda item: item[1].name):
        domains = set()
        seen = set()
        seeds = [net_id]
        seeded = {net_id}
        # A reset synchroniser turns the raw reset into a *different* net, so the
        # registers it protects are not forward-reachable from the declared reset.
        # Every recognised synchroniser stage therefore seeds a new forward walk
        # from its own output -- that net still carries this reset, synchronised.
        while seeds:
            seed = seeds.pop(0)
            for gate_id, pin, pin_net_id, through_logic in _forward_targets(
                view, seed, limits
            ):
                if (gate_id, pin.name) in seen:
                    continue
                seen.add((gate_id, pin.name))
                gate = view.gate(gate_id)
                resolution = clock_resolutions.get(gate_id)
                domain = resolution.domain if resolution is not None else None
                domains.add(domain)
                release, reason, stages = _classify_release(
                    view, reset, net_id, gate, domain, clock_resolutions, limits,
                    pin_net_id
                )
                if through_logic and release == RELEASE_ASYNCHRONOUS:
                    release = RELEASE_THROUGH_LOGIC
                    reason = (
                        "reset {!r} reaches the {!r} pin of {!r} through combinational "
                        "logic, so the signal at the pin is not the raw reset and its "
                        "release is not synchronised to {}".format(
                            reset.name, pin.name, gate.name, domain
                        )
                    )
                if release == RELEASE_SYNCHRONIZER_STAGE and gate is not None:
                    for output in gate.output_nets():
                        if output not in seeded:
                            seeded.add(output)
                            seeds.append(output)
                targets.append(
                    ResetTarget(
                        reset.name, gate_id, pin, pin_net_id, domain, release, reason,
                        stage_gate_ids=stages, through_logic=through_logic,
                    )
                )
        domain_map[reset.name] = sorted(name for name in domains if name is not None)
        if None in domains:
            domain_map[reset.name].append(None)

    targets.sort(key=lambda target: (target.reset_name, target.gate_id, target.pin.name))
    return targets, domain_map
