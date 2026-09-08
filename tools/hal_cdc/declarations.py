"""User clock/reset declarations and scoped waivers.

A structural CDC screen is only as good as what it is told.  HAL netlists carry
no SDC, so ``hal_cdc`` takes a small, explicit, versioned JSON document:

.. code-block:: json

    {
      "version": 1,
      "clocks":  [{"name": "clk_a", "net": "clk_a", "period_ns": 10.0}],
      "resets":  [{"name": "rst_n", "net": "rst_n", "active_low": true,
                   "synchronous_to": null}],
      "inputs":  [{"net": "din", "clock": "clk_a"}],
      "waivers": [{"id": "W-1", "rationale": "...", "kind": "crossing",
                   "source_domain": "clk_a", "destination_domain": "clk_b",
                   "gates": ["sync_meta_reg"]}]
    }

Nothing here is optional-by-omission in a dangerous direction: a declaration
that names a net the netlist does not contain becomes an ``error`` finding, and
a waiver without a non-empty ``rationale`` is rejected outright rather than
silently suppressing a crossing.
"""

import json
import os

__all__ = [
    "DECLARATIONS_VERSION",
    "WAIVER_KINDS",
    "DeclarationError",
    "Clock",
    "Reset",
    "InputDomain",
    "Waiver",
    "Declarations",
    "BoundDeclarations",
    "parse",
    "load",
]

#: The only declaration document version this build understands.
DECLARATIONS_VERSION = 1

#: What a waiver may be scoped to.  ``any`` matches every finding kind.
WAIVER_KINDS = ("crossing", "control_path", "reset_release", "any")

_ALLOWED_TOP_LEVEL = {"version", "clocks", "resets", "inputs", "waivers", "description"}
_ALLOWED_CLOCK = {"name", "net", "net_id", "period_ns", "description"}
_ALLOWED_RESET = {"name", "net", "net_id", "active_low", "synchronous_to", "description"}
_ALLOWED_INPUT = {"net", "net_id", "clock", "description"}
_ALLOWED_WAIVER = {
    "id",
    "rationale",
    "kind",
    "source_domain",
    "destination_domain",
    "gates",
    "nets",
    "resets",
    "expires",
}


class DeclarationError(ValueError):
    """The declaration document is malformed; the audit must not guess."""


def _check_keys(mapping, allowed, where):
    if not isinstance(mapping, dict):
        raise DeclarationError("{} must be an object, got {}".format(where, type(mapping).__name__))
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise DeclarationError(
            "{} has unknown key(s) {}; allowed keys are {}".format(
                where, ", ".join(repr(key) for key in unknown), ", ".join(sorted(allowed))
            )
        )


def _require_str(mapping, key, where):
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DeclarationError("{} needs a non-empty string {!r}".format(where, key))
    return value


class Clock(object):
    """A declared clock: a name for a domain plus the net that carries it."""

    __slots__ = ("name", "net", "net_id", "period_ns", "description")

    def __init__(self, name, net=None, net_id=None, period_ns=None, description=None):
        self.name = name
        self.net = net
        self.net_id = net_id
        self.period_ns = period_ns
        self.description = description

    def to_json(self):
        data = {"name": self.name}
        for key in ("net", "net_id", "period_ns", "description"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


class Reset(object):
    """A declared reset, optionally already known to be synchronous to a clock."""

    __slots__ = ("name", "net", "net_id", "active_low", "synchronous_to", "description")

    def __init__(self, name, net=None, net_id=None, active_low=None, synchronous_to=None,
                 description=None):
        self.name = name
        self.net = net
        self.net_id = net_id
        self.active_low = active_low
        self.synchronous_to = synchronous_to
        self.description = description

    def to_json(self):
        data = {"name": self.name}
        for key in ("net", "net_id", "active_low", "synchronous_to", "description"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


class InputDomain(object):
    """A primary input net the user says is already synchronous to a clock."""

    __slots__ = ("net", "net_id", "clock", "description")

    def __init__(self, net=None, net_id=None, clock=None, description=None):
        self.net = net
        self.net_id = net_id
        self.clock = clock
        self.description = description

    def to_json(self):
        data = {"clock": self.clock}
        for key in ("net", "net_id", "description"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


class Waiver(object):
    """A scoped, justified suppression.

    A waiver never deletes a finding; it moves it to ``severity: info`` and
    attaches the rationale as an *undischarged* assumption, so a report reader
    can still see what was waived and on whose word.
    """

    __slots__ = (
        "id",
        "rationale",
        "kind",
        "source_domain",
        "destination_domain",
        "gates",
        "nets",
        "resets",
        "expires",
    )

    def __init__(self, waiver_id, rationale, kind="any", source_domain=None,
                 destination_domain=None, gates=(), nets=(), resets=(), expires=None):
        self.id = waiver_id
        self.rationale = rationale
        self.kind = kind
        self.source_domain = source_domain
        self.destination_domain = destination_domain
        self.gates = tuple(gates)
        self.nets = tuple(nets)
        self.resets = tuple(resets)
        self.expires = expires

    def matches(self, kind, source_domain=None, destination_domain=None, gate_name=None,
                net_name=None, reset_name=None):
        """True if this waiver covers the described finding.

        Every field that the waiver *sets* must match; fields it leaves unset
        are wildcards.  A waiver with no scope at all still has to match on
        ``kind``, which is why ``kind: "any"`` plus no other field is the only
        way to write a blanket waiver -- and it is visible as such in a report.
        """
        if self.kind != "any" and self.kind != kind:
            return False
        if self.source_domain is not None and self.source_domain != source_domain:
            return False
        if self.destination_domain is not None and self.destination_domain != destination_domain:
            return False
        if self.gates and gate_name not in self.gates:
            return False
        if self.nets and net_name not in self.nets:
            return False
        if self.resets and reset_name not in self.resets:
            return False
        return True

    def to_json(self):
        data = {"id": self.id, "rationale": self.rationale, "kind": self.kind}
        for key in ("source_domain", "destination_domain", "expires"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        for key in ("gates", "nets", "resets"):
            value = getattr(self, key)
            if value:
                data[key] = list(value)
        return data


class Declarations(object):
    """The parsed declaration document, before it is bound to a netlist."""

    def __init__(self, clocks=(), resets=(), inputs=(), waivers=(), description=None,
                 source_path=None):
        self.clocks = tuple(clocks)
        self.resets = tuple(resets)
        self.inputs = tuple(inputs)
        self.waivers = tuple(waivers)
        self.description = description
        self.source_path = source_path

    @property
    def clock_names(self):
        return tuple(clock.name for clock in self.clocks)

    def clock(self, name):
        for clock in self.clocks:
            if clock.name == name:
                return clock
        return None

    def to_json(self):
        data = {"version": DECLARATIONS_VERSION}
        if self.description:
            data["description"] = self.description
        if self.clocks:
            data["clocks"] = [clock.to_json() for clock in self.clocks]
        if self.resets:
            data["resets"] = [reset.to_json() for reset in self.resets]
        if self.inputs:
            data["inputs"] = [entry.to_json() for entry in self.inputs]
        if self.waivers:
            data["waivers"] = [waiver.to_json() for waiver in self.waivers]
        return data

    # -- binding ---------------------------------------------------------

    def bind(self, view):
        """Resolve every declared net against *view*.

        Returns a :class:`BoundDeclarations`.  Unresolvable declarations are
        collected in ``problems`` instead of raising: the audit still runs, and
        each problem becomes an ``error`` finding so the report says *why* a
        domain came out unknown.
        """
        problems = []
        clock_nets, reset_nets, input_nets = {}, {}, {}

        def resolve(entry, label):
            if entry.net_id is not None:
                net = view.net(entry.net_id)
                if net is None:
                    problems.append(
                        "{} declares net_id {} which does not exist in this netlist".format(
                            label, entry.net_id
                        )
                    )
                    return None
                return net
            if entry.net is None:
                problems.append("{} declares neither 'net' nor 'net_id'".format(label))
                return None
            net = view.net_by_name(entry.net)
            if net is None:
                problems.append(
                    "{} declares net {!r}, which is not a uniquely named net in this "
                    "netlist".format(label, entry.net)
                )
            return net

        for clock in self.clocks:
            net = resolve(clock, "clock {!r}".format(clock.name))
            if net is not None:
                if net.id in clock_nets:
                    problems.append(
                        "clocks {!r} and {!r} both resolve to net {!r}".format(
                            clock_nets[net.id].name, clock.name, net.name
                        )
                    )
                    continue
                clock_nets[net.id] = clock

        for reset in self.resets:
            net = resolve(reset, "reset {!r}".format(reset.name))
            if net is not None:
                reset_nets[net.id] = reset
            if reset.synchronous_to is not None and self.clock(reset.synchronous_to) is None:
                problems.append(
                    "reset {!r} is declared synchronous_to {!r}, which is not a declared "
                    "clock".format(reset.name, reset.synchronous_to)
                )

        for entry in self.inputs:
            label = "input domain for net {!r}".format(entry.net if entry.net else entry.net_id)
            net = resolve(entry, label)
            if self.clock(entry.clock) is None:
                problems.append(
                    "{} names clock {!r}, which is not declared".format(label, entry.clock)
                )
                continue
            if net is not None:
                input_nets[net.id] = entry

        return BoundDeclarations(self, clock_nets, reset_nets, input_nets, problems)


class BoundDeclarations(object):
    """Declarations resolved against one netlist."""

    def __init__(self, declarations, clock_nets, reset_nets, input_nets, problems):
        self.declarations = declarations
        #: net id -> :class:`Clock`
        self.clock_nets = dict(clock_nets)
        #: net id -> :class:`Reset`
        self.reset_nets = dict(reset_nets)
        #: net id -> :class:`InputDomain`
        self.input_nets = dict(input_nets)
        self.problems = list(problems)

    @property
    def waivers(self):
        return self.declarations.waivers

    def clock_for_net(self, net_id):
        return self.clock_nets.get(net_id)

    def find_waiver(self, kind, **scope):
        for waiver in self.waivers:
            if waiver.matches(kind, **scope):
                return waiver
        return None


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def parse(document, source_path=None):
    """Validate and convert a declaration document (a plain ``dict``)."""
    _check_keys(document, _ALLOWED_TOP_LEVEL, "the declaration document")

    version = document.get("version")
    if version != DECLARATIONS_VERSION:
        raise DeclarationError(
            "declaration document has version {!r}; this build understands version {}. "
            "A document with an unknown version is rejected rather than read on a "
            "best-effort basis.".format(version, DECLARATIONS_VERSION)
        )

    clocks, seen_clocks = [], set()
    for index, entry in enumerate(document.get("clocks", []) or []):
        where = "clocks[{}]".format(index)
        _check_keys(entry, _ALLOWED_CLOCK, where)
        name = _require_str(entry, "name", where)
        if name in seen_clocks:
            raise DeclarationError("duplicate clock name {!r}".format(name))
        seen_clocks.add(name)
        period = entry.get("period_ns")
        if period is not None and not isinstance(period, (int, float)):
            raise DeclarationError("{}: 'period_ns' must be a number".format(where))
        clocks.append(
            Clock(
                name,
                net=entry.get("net"),
                net_id=entry.get("net_id"),
                period_ns=period,
                description=entry.get("description"),
            )
        )

    resets, seen_resets = [], set()
    for index, entry in enumerate(document.get("resets", []) or []):
        where = "resets[{}]".format(index)
        _check_keys(entry, _ALLOWED_RESET, where)
        name = _require_str(entry, "name", where)
        if name in seen_resets:
            raise DeclarationError("duplicate reset name {!r}".format(name))
        seen_resets.add(name)
        active_low = entry.get("active_low")
        if active_low is not None and not isinstance(active_low, bool):
            raise DeclarationError("{}: 'active_low' must be a boolean".format(where))
        resets.append(
            Reset(
                name,
                net=entry.get("net"),
                net_id=entry.get("net_id"),
                active_low=active_low,
                synchronous_to=entry.get("synchronous_to"),
                description=entry.get("description"),
            )
        )

    inputs = []
    for index, entry in enumerate(document.get("inputs", []) or []):
        where = "inputs[{}]".format(index)
        _check_keys(entry, _ALLOWED_INPUT, where)
        inputs.append(
            InputDomain(
                net=entry.get("net"),
                net_id=entry.get("net_id"),
                clock=_require_str(entry, "clock", where),
                description=entry.get("description"),
            )
        )

    waivers, seen_waivers = [], set()
    for index, entry in enumerate(document.get("waivers", []) or []):
        where = "waivers[{}]".format(index)
        _check_keys(entry, _ALLOWED_WAIVER, where)
        waiver_id = _require_str(entry, "id", where)
        if waiver_id in seen_waivers:
            raise DeclarationError("duplicate waiver id {!r}".format(waiver_id))
        seen_waivers.add(waiver_id)
        rationale = entry.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise DeclarationError(
                "{}: waiver {!r} has no rationale. A waiver without a written reason is "
                "indistinguishable from a bug, so it is rejected.".format(where, waiver_id)
            )
        kind = entry.get("kind", "any")
        if kind not in WAIVER_KINDS:
            raise DeclarationError(
                "{}: waiver kind {!r} must be one of {}".format(
                    where, kind, ", ".join(WAIVER_KINDS)
                )
            )
        for key in ("gates", "nets", "resets"):
            value = entry.get(key, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise DeclarationError("{}: {!r} must be a list of strings".format(where, key))
        waivers.append(
            Waiver(
                waiver_id,
                rationale.strip(),
                kind=kind,
                source_domain=entry.get("source_domain"),
                destination_domain=entry.get("destination_domain"),
                gates=entry.get("gates", []),
                nets=entry.get("nets", []),
                resets=entry.get("resets", []),
                expires=entry.get("expires"),
            )
        )

    return Declarations(
        clocks,
        resets,
        inputs,
        waivers,
        description=document.get("description"),
        source_path=source_path,
    )


def load(path):
    """Read and validate a declaration document from *path*."""
    path = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(path):
        raise DeclarationError("declaration file does not exist: {}".format(path))
    with open(path, "r", encoding="utf-8") as handle:
        try:
            document = json.load(handle)
        except ValueError as exc:
            raise DeclarationError("{} is not valid JSON: {}".format(path, exc))
    return parse(document, source_path=path)
