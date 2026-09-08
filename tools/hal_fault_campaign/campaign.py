"""Deterministic enumeration of a campaign: which register, which cycle.

A campaign is a subset of the ``(fault site) x (injection cycle)`` grid.  Two
things have to be true of the way that subset is chosen, or replay is worthless:

1. **The order is a function of the inputs, not of the filesystem.**  Sites are
   sorted by gate *name* (with the gate ID as a tie-break), never by iteration
   order of a HAL container or by ID alone -- IDs are assigned by the parser and
   a re-parse of the same file is only incidentally stable.
2. **The sample is a function of the seed.**  ``random.sample`` is not part of
   Python's compatibility contract, so this module does not use it: it runs an
   explicit Fisher-Yates selection on top of ``random.Random(seed).randrange``,
   which *is* specified (Mersenne Twister, documented reproducible sequence).
   The algorithm is named in the manifest so that a future change is visible
   rather than silent.

The result is that ``enumerate_faults`` returns the same list, in the same
order, with the same fault IDs, on any machine -- and that a manifest can simply
record the list and have replay agree with it.
"""

import random

__all__ = [
    "EnumerationError",
    "Site",
    "FaultSpec",
    "sort_sites",
    "select_sites",
    "resolve_cycles",
    "enumerate_faults",
    "fault_from_dict",
]

#: Named so that a change of selection algorithm invalidates old manifests.
SAMPLING_ALGORITHM = "fisher-yates/mt19937/1"


class EnumerationError(ValueError):
    """Raised when a campaign cannot be enumerated (no sites, empty grid, ...)."""


class Site(object):
    """One candidate fault site: a sequential gate's state output."""

    def __init__(self, gate_name, gate_id, gate_type, output_pin, net_name, net_id,
                 control_net=None):
        self.gate_name = gate_name
        self.gate_id = int(gate_id)
        self.gate_type = gate_type
        self.output_pin = output_pin
        self.net_name = net_name
        self.net_id = int(net_id)
        self.control_net = control_net or control_net_name(gate_id)

    @property
    def key(self):
        return (self.gate_name, self.gate_id, self.output_pin)

    def as_dict(self):
        return {
            "gate_name": self.gate_name,
            "gate_id": self.gate_id,
            "gate_type": self.gate_type,
            "output_pin": self.output_pin,
            "net_name": self.net_name,
            "net_id": self.net_id,
            "control_net": self.control_net,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            data["gate_name"],
            data["gate_id"],
            data.get("gate_type", ""),
            data.get("output_pin", ""),
            data.get("net_name", ""),
            data.get("net_id", 0),
            data.get("control_net"),
        )


def control_net_name(gate_id):
    """Name of the injection control input created for the gate with ``gate_id``.

    Derived from the gate ID rather than the gate name because a gate name may
    contain characters a net name should not, and because the ID is unique
    inside the netlist by construction.  The prefix is long and ugly on purpose:
    it must not collide with anything a real design contains.
    """
    return "__hal_fi_ctrl_{}".format(int(gate_id))


def raw_net_name(gate_id, pin):
    """Name of the net that carries the *uncorrupted* register output."""
    return "__hal_fi_raw_{}_{}".format(int(gate_id), pin)


def injector_gate_name(gate_id, pin):
    """Name of the XOR gate that performs the injection."""
    return "__hal_fi_xor_{}_{}".format(int(gate_id), pin)


class FaultSpec(object):
    """One injection: a site, a cycle, and how long the flip is held."""

    def __init__(self, fault_id, site_name, cycle, hold_cycles=1, gate_id=None,
                 control_net=None):
        self.fault_id = fault_id
        self.site_name = site_name
        self.cycle = int(cycle)
        self.hold_cycles = int(hold_cycles)
        self.gate_id = gate_id
        self.control_net = control_net

    def as_dict(self):
        record = {
            "id": self.fault_id,
            "site": self.site_name,
            "cycle": self.cycle,
            "hold_cycles": self.hold_cycles,
        }
        if self.gate_id is not None:
            record["gate_id"] = int(self.gate_id)
        if self.control_net:
            record["control_net"] = self.control_net
        return record

    def __eq__(self, other):
        return isinstance(other, FaultSpec) and self.as_dict() == other.as_dict()

    def __repr__(self):  # pragma: no cover - debugging aid
        return "FaultSpec({!r}, {!r}, {}, hold={})".format(
            self.fault_id, self.site_name, self.cycle, self.hold_cycles
        )


def fault_from_dict(data):
    return FaultSpec(
        data["id"],
        data["site"],
        data["cycle"],
        data.get("hold_cycles", 1),
        gate_id=data.get("gate_id"),
        control_net=data.get("control_net"),
    )


def sort_sites(sites):
    """Canonical site order: gate name, then gate ID, then pin."""
    return sorted(sites, key=lambda site: site.key)


def _matches(name, patterns):
    import fnmatch

    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def select_sites(sites, include=("*",), exclude=()):
    """Filter sites by shell-style glob on the gate name, keeping canonical order."""
    include = list(include) if include else ["*"]
    exclude = list(exclude or [])
    selected = [
        site
        for site in sort_sites(sites)
        if _matches(site.gate_name, include) and not _matches(site.gate_name, exclude)
    ]
    return selected


def resolve_cycles(spec, workload_cycles):
    """Turn the configuration's ``cycles`` block into a sorted list of cycles."""
    if spec is None:
        cycles = list(range(workload_cycles))
    elif isinstance(spec, list):
        cycles = [int(value) for value in spec]
    elif isinstance(spec, dict):
        start = int(spec.get("from", 0))
        stop = int(spec.get("to", workload_cycles - 1))
        step = int(spec.get("step", 1))
        if step <= 0:
            raise EnumerationError("cycles.step must be positive, got {}".format(step))
        cycles = list(range(start, stop + 1, step))
    else:
        raise EnumerationError("cycles must be a list or an object, got {!r}".format(spec))

    cycles = sorted(set(cycles))
    if not cycles:
        raise EnumerationError("the campaign selects no injection cycle")
    for cycle in cycles:
        if cycle < 0 or cycle >= workload_cycles:
            raise EnumerationError(
                "injection cycle {} is outside the workload's 0..{}".format(
                    cycle, workload_cycles - 1
                )
            )
    return cycles


def _fisher_yates_prefix(count, total, rng):
    """Indices of a uniform ``count``-subset of ``range(total)``, sorted.

    A partial Fisher-Yates shuffle: exactly ``count`` calls to ``randrange``, in
    a fixed order, so the selection is fully determined by the seed and by this
    function -- not by a standard-library implementation detail.
    """
    order = list(range(total))
    for index in range(count):
        pick = index + rng.randrange(total - index)
        order[index], order[pick] = order[pick], order[index]
    return sorted(order[:count])


def enumerate_faults(sites, cycles, hold_cycles=1, sampling=None):
    """Enumerate the campaign.

    Returns ``(faults, enumeration_record)``.  ``enumeration_record`` is what the
    manifest stores: the size of the full grid, how many faults were selected,
    and exactly how.
    """
    sites = sort_sites(sites)
    cycles = sorted(set(int(cycle) for cycle in cycles))
    if not sites:
        raise EnumerationError(
            "the campaign has no fault site: no sequential gate matched the site filter"
        )
    if not cycles:
        raise EnumerationError("the campaign has no injection cycle")

    grid = [(site, cycle) for site in sites for cycle in cycles]
    sampling = dict(sampling or {"mode": "exhaustive"})
    mode = sampling.get("mode", "exhaustive")

    if mode == "exhaustive":
        chosen = list(range(len(grid)))
        record = {"mode": "exhaustive"}
    elif mode == "random":
        count = sampling.get("count")
        seed = sampling.get("seed")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise EnumerationError("random sampling needs a positive integer 'count'")
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise EnumerationError(
                "random sampling needs an integer 'seed'; an unseeded campaign cannot be "
                "replayed"
            )
        if count > len(grid):
            raise EnumerationError(
                "random sampling asks for {} faults but the grid holds {} "
                "(|sites|={} x |cycles|={}); use mode 'exhaustive' instead".format(
                    count, len(grid), len(sites), len(cycles)
                )
            )
        chosen = _fisher_yates_prefix(count, len(grid), random.Random(seed))
        record = {
            "mode": "random",
            "count": count,
            "seed": seed,
            "algorithm": SAMPLING_ALGORITHM,
        }
    else:
        raise EnumerationError("unknown sampling mode {!r}".format(mode))

    faults = []
    for position, index in enumerate(chosen):
        site, cycle = grid[index]
        faults.append(
            FaultSpec(
                "f{:05d}".format(position),
                site.gate_name,
                cycle,
                hold_cycles,
                gate_id=site.gate_id,
                control_net=site.control_net,
            )
        )

    record.update(
        {
            "grid_size": len(grid),
            "site_count": len(sites),
            "cycle_count": len(cycles),
            "selected": len(faults),
            "hold_cycles": int(hold_cycles),
            "cycles": cycles,
            "sites": [site.gate_name for site in sites],
        }
    )
    return faults, record
