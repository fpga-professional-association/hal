"""Cycles, times and stimulus -- the one place that knows what "cycle 7" means.

HAL's simulator works in picoseconds; a fault campaign works in clock cycles.
Every conversion between the two lives here so that the injector, the sampler
and the reference model cannot disagree about it.

The convention, with ``P`` the clock period in picoseconds::

    cycle k occupies  [k*P, (k+1)*P)
    its rising edge is at  k*P + P/2          (add_clock_period starts the clock
                                               low at t=0 and toggles every P/2)
    stimulus for cycle k is applied at  k*P   (just after the falling edge, so it
                                               is stable across the rising edge)
    cycle k is sampled at  k*P + 3P/4         (after the rising edge and before
                                               the next falling edge: registers
                                               hold what they captured at edge k
                                               and combinational logic has
                                               settled -- the simulator is
                                               zero-delay)

"The value of signal S in cycle k" therefore always means: S at ``sample_time(k)``,
i.e. the settled state *after* cycle k's clock edge.  An injection window for
cycle k is ``[k*P, (k+1)*P)``: the control input is high for the whole cycle,
which covers exactly one clock edge, so the flipped value is both observed in
cycle k and captured by everything that clocks in cycle k.

The period must be divisible by 4 so that all of the above are whole
picoseconds; a period that is not is rejected rather than rounded.
"""

__all__ = [
    "ClockGrid",
    "WorkloadError",
    "resolve_stimulus",
    "input_events",
    "merge_event_maps",
    "event_schedule",
]


class WorkloadError(ValueError):
    """Raised when a workload cannot be turned into a simulation schedule."""


class ClockGrid(object):
    """Maps cycle indices to picosecond times for one clock period."""

    def __init__(self, period_ps):
        period_ps = int(period_ps)
        if period_ps <= 0:
            raise WorkloadError("clock period must be positive, got {}".format(period_ps))
        if period_ps % 4:
            raise WorkloadError(
                "clock period {} ps is not divisible by 4; the cycle grid (edge at P/2, "
                "sample at 3P/4) would not land on whole picoseconds".format(period_ps)
            )
        self.period_ps = period_ps

    def cycle_start(self, cycle):
        """First picosecond of ``cycle``; when stimulus for it is applied."""
        return int(cycle) * self.period_ps

    def edge_time(self, cycle):
        """The rising clock edge inside ``cycle``."""
        return int(cycle) * self.period_ps + self.period_ps // 2

    def sample_time(self, cycle):
        """When ``cycle`` is observed: after its edge, before the next cycle."""
        return int(cycle) * self.period_ps + (3 * self.period_ps) // 4

    def total_time(self, cycles):
        """Length of a run of ``cycles`` cycles, in picoseconds."""
        return int(cycles) * self.period_ps

    def as_dict(self):
        return {
            "period_ps": self.period_ps,
            "edge_offset_ps": self.period_ps // 2,
            "sample_offset_ps": (3 * self.period_ps) // 4,
        }


def resolve_stimulus(stimulus, cycles):
    """Turn a ``[{"cycle": k, "inputs": {...}}]`` list into ``{cycle: {net: value}}``.

    Later entries for the same cycle are merged into the earlier one; a net
    assigned twice *in the same cycle* is an error rather than a last-one-wins
    surprise.  Values must be 0 or 1: the MVP drives defined logic levels only,
    and silently mapping anything else onto X would hide it.
    """
    resolved = {}
    for index, entry in enumerate(stimulus or []):
        cycle = entry.get("cycle")
        if not isinstance(cycle, int) or isinstance(cycle, bool):
            raise WorkloadError("stimulus[{}] has no integer 'cycle'".format(index))
        if cycle < 0 or cycle >= cycles:
            raise WorkloadError(
                "stimulus[{}] targets cycle {}, outside the workload's 0..{}".format(
                    index, cycle, cycles - 1
                )
            )
        bucket = resolved.setdefault(cycle, {})
        for net, value in sorted((entry.get("inputs") or {}).items()):
            if value not in (0, 1) or isinstance(value, bool):
                raise WorkloadError(
                    "stimulus[{}] assigns {!r} to {!r}; only the integers 0 and 1 are "
                    "accepted (X/Z stimulus is not supported by this MVP)".format(
                        index, value, net
                    )
                )
            if net in bucket:
                raise WorkloadError(
                    "stimulus assigns {!r} twice in cycle {}".format(net, cycle)
                )
            bucket[net] = value
    return resolved


def input_events(grid, stimulus_by_cycle):
    """``{time_ps: {net: value}}`` for the workload's primary-input stimulus."""
    events = {}
    for cycle, assignments in stimulus_by_cycle.items():
        if not assignments:
            continue
        events.setdefault(grid.cycle_start(cycle), {}).update(assignments)
    return events


def merge_event_maps(*event_maps):
    """Merge ``{time: {net: value}}`` maps; a net set twice at one time is an error."""
    merged = {}
    for event_map in event_maps:
        for time, assignments in event_map.items():
            bucket = merged.setdefault(time, {})
            for net, value in assignments.items():
                if net in bucket and bucket[net] != value:
                    raise WorkloadError(
                        "conflicting assignments for net {!r} at t={}: {} and {}".format(
                            net, time, bucket[net], value
                        )
                    )
                bucket[net] = value
    return merged


def event_schedule(grid, events, cycles):
    """``[(time_ps, {net: value}, duration_ps)]`` ready to drive the controller.

    The controller API is ``set_input`` followed by ``simulate(duration)``, so
    the schedule is a sequence of assignment points plus the distance to the
    next one.  A run always starts at t=0 (even with no stimulus there, so that
    the simulation is anchored) and always ends exactly at ``cycles * P``.
    """
    end = grid.total_time(cycles)
    times = sorted(set(list(events.keys()) + [0]))
    if times and times[-1] >= end:
        raise WorkloadError(
            "an input event at t={} ps falls outside the {} cycle run (ends at t={} ps)".format(
                times[-1], cycles, end
            )
        )
    schedule = []
    for index, time in enumerate(times):
        next_time = times[index + 1] if index + 1 < len(times) else end
        schedule.append((time, dict(events.get(time, {})), next_time - time))
    return schedule
