"""Turning two traces into a verdict, and refusing to over-read either of them.

Everything here is a pure function of recorded traces, which is the point: a
manifest can be re-checked without re-simulating, and the classification rules
can be unit tested on hand-written traces with no HAL anywhere near them.

A trace is ``{signal_name: [value_per_cycle]}`` with the four values HAL's
simulator can produce: ``0``, ``1``, ``-1`` (X, undefined) and ``-2`` (Z).

The three classes the issue asks for, plus the one it implies
-------------------------------------------------------------
* ``detected`` -- a detection signal is active inside the window in the faulty
  run at a cycle where the baseline left it inactive.  Detection wins over
  divergence: a fault that both corrupts an output and raises the alarm is
  detected, and the divergence is still recorded so the latency of each is
  visible separately.
* ``silent_divergence`` -- an observed output differs from the baseline inside
  the window and no detection signal ever fired.  This is the dangerous class,
  and the only one that refutes anything.
* ``unobserved_in_window`` -- neither happened.  Named for the window on
  purpose: it is not "masked".
* ``indeterminate`` -- the faulty run produced X or Z where the baseline was
  defined.  Silicon would produce *some* level there; the simulation does not
  know which, so neither divergence nor its absence can be claimed.  Counting
  this as "no divergence" would silently turn an unknown into a clean bill of
  health, which is exactly the failure mode the findings schema exists to stop.

Baseline alarms are subtracted rather than ignored: if the detection signal is
already active in the fault-free run at some cycle, that cycle cannot evidence
detection of the injected fault, and the campaign reports the baseline activity
separately so a mis-specified detection signal is visible instead of inflating
the detected count.
"""

from .faultmodel import (
    CLASS_DETECTED,
    CLASS_INDETERMINATE,
    CLASS_SILENT,
    CLASS_UNOBSERVED,
)

__all__ = [
    "ClassificationError",
    "VALUE_X",
    "VALUE_Z",
    "is_defined",
    "window_cycles",
    "compare_traces",
    "classify",
    "summarize",
]

#: BooleanFunction::Value as the bindings report it (boolean_function.h).
VALUE_X = -1
VALUE_Z = -2


class ClassificationError(ValueError):
    """Raised when two traces cannot be compared at all (missing signal, length)."""


def is_defined(value):
    return value in (0, 1)


def window_cycles(window, injection_cycle, total_cycles):
    """Resolve an observation window to a concrete, clamped list of cycles.

    ``{"mode": "absolute", "start_cycle": a, "end_cycle": b}`` is the same window
    for every fault; ``{"mode": "relative", "start_offset": s, "length": n}`` is
    ``n`` cycles starting ``s`` cycles after the injection.  Both are clamped to
    the simulated range, and the clamping is reported by the caller rather than
    hidden: a window that runs past the end of the workload observes fewer
    cycles than it asks for, and a result that says otherwise would be a lie.
    """
    mode = (window or {}).get("mode", "absolute")
    if mode == "absolute":
        start = int((window or {}).get("start_cycle", 0))
        end = int((window or {}).get("end_cycle", total_cycles - 1))
    elif mode == "relative":
        start = injection_cycle + int((window or {}).get("start_offset", 0))
        length = int((window or {}).get("length", 1))
        if length < 1:
            raise ClassificationError("a relative window needs length >= 1")
        end = start + length - 1
    else:
        raise ClassificationError("unknown observation window mode {!r}".format(mode))

    requested = (start, end)
    start = max(0, start)
    end = min(total_cycles - 1, end)
    if end < start:
        return [], requested, True
    return list(range(start, end + 1)), requested, (start, end) != requested


def _series(trace, signal, where):
    if signal not in trace:
        raise ClassificationError(
            "the {} trace has no signal {!r}; observed signals are {}".format(
                where, signal, sorted(trace)
            )
        )
    return trace[signal]


def compare_traces(baseline, faulty, signals, cycles):
    """Per-cycle comparison of ``signals`` over ``cycles``.

    Returns ``(diverged, indeterminate, details)``: the cycles where a defined
    value differs, the cycles where the faulty run is undefined but the baseline
    is not, and a per-cycle record of what differed.
    """
    diverged = []
    indeterminate = []
    details = []
    for cycle in cycles:
        differing = []
        undefined = []
        for signal in signals:
            base_series = _series(baseline, signal, "baseline")
            fault_series = _series(faulty, signal, "faulty")
            if cycle >= len(base_series) or cycle >= len(fault_series):
                raise ClassificationError(
                    "cycle {} is outside the recorded trace of {!r} (baseline {} cycles, "
                    "faulty {} cycles)".format(
                        cycle, signal, len(base_series), len(fault_series)
                    )
                )
            base_value = base_series[cycle]
            fault_value = fault_series[cycle]
            if base_value == fault_value:
                continue
            if is_defined(base_value) and not is_defined(fault_value):
                undefined.append({"signal": signal, "baseline": base_value,
                                  "faulty": fault_value})
            else:
                differing.append({"signal": signal, "baseline": base_value,
                                  "faulty": fault_value})
        if differing:
            diverged.append(cycle)
        if undefined:
            indeterminate.append(cycle)
        if differing or undefined:
            details.append(
                {"cycle": cycle, "differs": differing, "undefined": undefined}
            )
    return diverged, indeterminate, details


def _detection_cycles(baseline, faulty, signals, cycles, active_value):
    """Cycles where a detection signal is active in the faulty run only."""
    fired = []
    baseline_active = []
    for cycle in cycles:
        faulty_hit = []
        base_hit = []
        for signal in signals:
            base_series = _series(baseline, signal, "baseline")
            fault_series = _series(faulty, signal, "faulty")
            if cycle >= len(base_series) or cycle >= len(fault_series):
                raise ClassificationError(
                    "cycle {} is outside the recorded trace of detection signal "
                    "{!r}".format(cycle, signal)
                )
            if base_series[cycle] == active_value:
                base_hit.append(signal)
            elif fault_series[cycle] == active_value:
                faulty_hit.append(signal)
        if base_hit:
            baseline_active.append({"cycle": cycle, "signals": base_hit})
        if faulty_hit:
            fired.append({"cycle": cycle, "signals": faulty_hit})
    return fired, baseline_active


def classify(baseline, faulty, injection_cycle, total_cycles, outputs,
             detection_signals, window=None, detection_active_value=1):
    """Classify one injection. Pure: traces in, verdict out.

    The returned record is what a finding, a manifest entry and a replay check
    are all built from, so it carries the evidence and not just the verdict.
    """
    cycles, requested, clamped = window_cycles(window, injection_cycle, total_cycles)

    diverged, indeterminate, details = compare_traces(
        baseline, faulty, list(outputs), cycles
    )
    fired, baseline_active = _detection_cycles(
        baseline, faulty, list(detection_signals), cycles, detection_active_value
    )

    detection_latency = None
    if fired:
        detection_latency = fired[0]["cycle"] - injection_cycle
    divergence_latency = None
    if diverged:
        divergence_latency = diverged[0] - injection_cycle

    if fired:
        classification = CLASS_DETECTED
    elif diverged:
        classification = CLASS_SILENT
    elif indeterminate:
        classification = CLASS_INDETERMINATE
    else:
        classification = CLASS_UNOBSERVED

    return {
        "classification": classification,
        "injection_cycle": injection_cycle,
        "window": {
            "cycles": [cycles[0], cycles[-1]] if cycles else [],
            "requested": list(requested),
            "clamped": bool(clamped),
            "observed_cycles": len(cycles),
        },
        "detected": bool(fired),
        "diverged": bool(diverged),
        "indeterminate": bool(indeterminate),
        "detection_cycles": [entry["cycle"] for entry in fired],
        "detection_signals_fired": sorted(
            {signal for entry in fired for signal in entry["signals"]}
        ),
        "detection_latency_cycles": detection_latency,
        "divergence_cycles": diverged,
        "divergence_latency_cycles": divergence_latency,
        "indeterminate_cycles": indeterminate,
        "baseline_detection_active": baseline_active,
        "differences": details,
    }


def summarize(results):
    """Class counts plus latency statistics over a list of :func:`classify` records."""
    counts = {
        CLASS_DETECTED: 0,
        CLASS_SILENT: 0,
        CLASS_UNOBSERVED: 0,
        CLASS_INDETERMINATE: 0,
    }
    detection_latencies = []
    divergence_latencies = []
    for result in results:
        counts[result["classification"]] = counts.get(result["classification"], 0) + 1
        if result.get("detection_latency_cycles") is not None:
            detection_latencies.append(result["detection_latency_cycles"])
        if result.get("divergence_latency_cycles") is not None:
            divergence_latencies.append(result["divergence_latency_cycles"])

    def stats(values):
        if not values:
            return None
        ordered = sorted(values)
        return {
            "count": len(ordered),
            "min": ordered[0],
            "max": ordered[-1],
            "median": ordered[len(ordered) // 2],
            "mean": round(sum(ordered) / float(len(ordered)), 3),
        }

    return {
        "total": len(results),
        "counts": counts,
        "detection_latency_cycles": stats(detection_latencies),
        "divergence_latency_cycles": stats(divergence_latencies),
        "baseline_detection_active": sum(
            1 for result in results if result.get("baseline_detection_active")
        ),
    }
