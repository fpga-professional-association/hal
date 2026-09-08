"""An independent cycle model of ``parity_counter.v``, and the ground truth it makes.

This is not a second copy of HAL's simulator.  It is a hand-written model of the
*fixture* -- eight lines of next-state logic taken straight off the Verilog --
written so that three separate things can be checked against each other:

* the classification rules, on traces nobody had to simulate;
* HAL's simulation of the fixture, in the end-to-end test, cycle by cycle;
* the campaign's verdicts, against ``ground_truth.json``.

If HAL and this model disagree, one of them is wrong and the test says so
instead of trusting whichever ran last.

The fault model is the one :mod:`hal_fault_campaign.faultmodel` describes: while
a register is flipped, everything that reads it -- combinational logic, other
registers at the clock edge, and the observation at the cycle's sample point --
sees the complement.  Cycle timing follows
:class:`hal_fault_campaign.workload.ClockGrid`: a cycle is sampled *after* its
clock edge, so ``sample(k)`` shows the state captured at edge ``k``.

Run it to regenerate ``ground_truth.json``::

    python tools/hal_fault_campaign/fixtures/reference_model.py --write
"""

import argparse
import json
import os
import sys

#: Registers by their Verilog instance name -- which is what HAL calls the gate.
CNT0, CNT1, CNT2 = "CNT_reg_0_inst", "CNT_reg_1_inst", "CNT_reg_2_inst"
PAR = "PAR_reg_inst"
AUX = "AUX_reg_inst"
PIPE = ["PIPE_reg_0_inst", "PIPE_reg_1_inst", "PIPE_reg_2_inst", "PIPE_reg_3_inst"]

REGISTERS = [CNT0, CNT1, CNT2, PAR, AUX] + PIPE

#: The nets the campaign observes, and which register (or function) drives them.
OUTPUT_NETS = ["CNT0", "CNT1", "CNT2", "AUX_O", "PIPE_O"]
DETECTION_NETS = ["ERR"]
SIGNALS = OUTPUT_NETS + DETECTION_NETS

_VALUE_CHARS = {0: "0", 1: "1", -1: "x", -2: "z"}
_CHAR_VALUES = {char: value for value, char in _VALUE_CHARS.items()}


class ModelError(ValueError):
    """Raised when the workload is outside what this model can stand in for."""


def encode(series):
    """Encode a per-cycle value list as a compact string ('0', '1', 'x', 'z')."""
    return "".join(_VALUE_CHARS[value] for value in series)


def decode(text):
    return [_CHAR_VALUES[char] for char in text]


def encode_trace(trace):
    return {signal: encode(series) for signal, series in trace.items()}


def decode_trace(trace):
    return {signal: decode(text) for signal, text in trace.items()}


def expand_stimulus(stimulus, cycles):
    """``[{cycle, inputs}]`` -> a per-cycle value for every input, held forward."""
    held = {}
    by_cycle = {}
    for entry in sorted(stimulus, key=lambda item: item["cycle"]):
        by_cycle.setdefault(entry["cycle"], {}).update(entry["inputs"])
    expanded = []
    for cycle in range(cycles):
        held.update(by_cycle.get(cycle, {}))
        expanded.append(dict(held))
    return expanded


def simulate(stimulus, cycles, flip=None):
    """Run the fixture. ``flip`` is ``(register, cycle, hold_cycles)`` or ``None``.

    Returns ``{signal: [value per cycle]}`` sampled the way the campaign samples:
    after each cycle's clock edge.
    """
    inputs = expand_stimulus(stimulus, cycles)
    if not inputs or inputs[0].get("RST") != 1:
        raise ModelError(
            "this model needs RST asserted in cycle 0: driven through "
            "NetlistSimulatorController there is no way to preload sequential state, so "
            "without the reset every register starts at X and the model would not be "
            "modelling the same thing HAL simulates"
        )

    flip_register, flip_cycle, flip_hold = (flip or (None, 0, 0))
    if flip_register is not None and flip_register not in REGISTERS:
        raise ModelError("{!r} is not a register of this fixture".format(flip_register))

    state = {name: 0 for name in REGISTERS}
    trace = {signal: [] for signal in SIGNALS}

    for cycle in range(cycles):
        active = (
            flip_register is not None
            and flip_cycle <= cycle < flip_cycle + flip_hold
        )

        def observed(name, values):
            return values[name] ^ 1 if (active and name == flip_register) else values[name]

        reset = inputs[cycle].get("RST", 0) == 1
        pipe_in = inputs[cycle].get("PIPE_IN", 0)

        pre = {name: 0 for name in REGISTERS} if reset else dict(state)
        c0, c1, c2 = (observed(name, pre) for name in (CNT0, CNT1, CNT2))
        aux = observed(AUX, pre)
        p0, p1, p2 = (observed(name, pre) for name in PIPE[:3])

        c0n = c0 ^ 1
        c1n = c1 ^ c0
        c2n = c2 ^ (c0 & c1)
        post = {
            CNT0: c0n,
            CNT1: c1n,
            CNT2: c2n,
            PAR: c0n ^ c1n ^ c2n,
            AUX: aux ^ 1,
            PIPE[0]: pipe_in,
            PIPE[1]: p0,
            PIPE[2]: p1,
            PIPE[3]: p2,
        }
        if reset:
            post = {name: 0 for name in REGISTERS}
        state = post

        out = {name: observed(name, state) for name in REGISTERS}
        trace["CNT0"].append(out[CNT0])
        trace["CNT1"].append(out[CNT1])
        trace["CNT2"].append(out[CNT2])
        trace["AUX_O"].append(out[AUX])
        trace["PIPE_O"].append(out[PIPE[3]])
        trace["ERR"].append(out[PAR] ^ out[CNT0] ^ out[CNT1] ^ out[CNT2])

    return trace


# ---------------------------------------------------------------------------
# the shipped workload and the ground truth built from it
# ---------------------------------------------------------------------------

WORKLOAD_CYCLES = 20

#: Reset for one cycle, then a deliberately irregular data stream into the
#: pipeline so that a corrupted bit is not accidentally equal to the correct one.
STIMULUS = [
    {"cycle": 0, "inputs": {"RST": 1, "PIPE_IN": 0}},
    {"cycle": 1, "inputs": {"RST": 0, "PIPE_IN": 1}},
    {"cycle": 2, "inputs": {"PIPE_IN": 0}},
    {"cycle": 3, "inputs": {"PIPE_IN": 1}},
    {"cycle": 4, "inputs": {"PIPE_IN": 1}},
    {"cycle": 5, "inputs": {"PIPE_IN": 0}},
    {"cycle": 6, "inputs": {"PIPE_IN": 1}},
    {"cycle": 7, "inputs": {"PIPE_IN": 0}},
    {"cycle": 8, "inputs": {"PIPE_IN": 0}},
    {"cycle": 9, "inputs": {"PIPE_IN": 1}},
    {"cycle": 10, "inputs": {"PIPE_IN": 0}},
    {"cycle": 12, "inputs": {"PIPE_IN": 1}},
    {"cycle": 14, "inputs": {"PIPE_IN": 0}},
    {"cycle": 16, "inputs": {"PIPE_IN": 1}},
]

#: The injection cycles the shipped campaigns use.
INJECTION_CYCLES = [3, 4, 5, 6]

HOLD_CYCLES = 1

#: The two shipped windows. The pair is the point: the same faults, the same
#: traces, a different window, and three of them change class.
WINDOWS = {
    "short_window": {"mode": "relative", "start_offset": 0, "length": 1},
    "long_window": {"mode": "relative", "start_offset": 0, "length": 4},
}


def build_ground_truth():
    """Baseline, per-fault traces and expected verdicts for both windows."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    from hal_fault_campaign.classify import classify

    baseline = simulate(STIMULUS, WORKLOAD_CYCLES)
    faults = []
    for register in REGISTERS:
        for cycle in INJECTION_CYCLES:
            trace = simulate(STIMULUS, WORKLOAD_CYCLES, (register, cycle, HOLD_CYCLES))
            faults.append(
                {
                    "site": register,
                    "cycle": cycle,
                    "hold_cycles": HOLD_CYCLES,
                    "trace": encode_trace(trace),
                }
            )

    expected = {}
    for name, window in sorted(WINDOWS.items()):
        verdicts = {}
        for entry in faults:
            result = classify(
                baseline,
                decode_trace(entry["trace"]),
                entry["cycle"],
                WORKLOAD_CYCLES,
                OUTPUT_NETS,
                DETECTION_NETS,
                window=window,
                detection_active_value=1,
            )
            verdicts["{}@{}".format(entry["site"], entry["cycle"])] = {
                "classification": result["classification"],
                "detection_latency_cycles": result["detection_latency_cycles"],
                "divergence_latency_cycles": result["divergence_latency_cycles"],
            }
        expected[name] = {"window": window, "verdicts": verdicts}

    return {
        "ground_truth_version": "1.0.0",
        "fixture": "parity_counter.v",
        "gate_library": "example_library.hgl",
        "generated_by": "tools/hal_fault_campaign/fixtures/reference_model.py",
        "note": (
            "Produced by an independent model of parity_counter.v, not by HAL. The "
            "end-to-end test asserts that HAL's simulation of the same fixture "
            "reproduces these traces exactly."
        ),
        "value_encoding": {"0": "ZERO", "1": "ONE", "x": "X", "z": "Z"},
        "workload": {"cycles": WORKLOAD_CYCLES, "stimulus": STIMULUS},
        "observation": {
            "outputs": OUTPUT_NETS,
            "detection_signals": DETECTION_NETS,
            "detection_active_value": 1,
        },
        "registers": REGISTERS,
        "injection_cycles": INJECTION_CYCLES,
        "hold_cycles": HOLD_CYCLES,
        "baseline": encode_trace(baseline),
        "faults": faults,
        "expected": expected,
    }


def ground_truth_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ground_truth.json")


def load_ground_truth():
    with open(ground_truth_path(), "r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write", action="store_true", help="rewrite fixtures/ground_truth.json"
    )
    args = parser.parse_args(argv)
    document = build_ground_truth()
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.write:
        with open(ground_truth_path(), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        sys.stdout.write("wrote {}\n".format(ground_truth_path()))
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
