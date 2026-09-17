"""Python model of ``design.v`` -- the RTL the export was synthesised from.

This is the *spec-side* model, written from ``spec.md`` and ``design.v``.  It is
used once, in section 2 of the guide, to establish that the exported netlist
behaves like the design that was handed to Quartus:

    python tools/hal_agilex behavior speck_toy.vo --reference reference.py \\
        --cycles 2000

That run simulates the export with the validated Agilex primitive semantics and
compares every output, every cycle, against this model.  It is *not* part of the
reverse engineering -- see ``recovered_reference.py`` for the model that is.

The comparison is bounded, never a proof.  Section 2 also runs two deliberately
wrong copies of ``recovered_reference.py`` so that "it agreed for 2000 cycles"
means something: see ``run_analysis.sh``.
"""

KIND = "sequential"

INPUTS = [
    ("clk", 1),
    ("rst_n", 1),
    ("start", 1),
    ("pt", 32),
    ("key", 64),
]

#: Everything the design exposes.  ``ct`` is the block register and is driven
#: continuously, so a wrong round function shows up on it immediately rather
#: than only at the end of an encryption.
OUTPUTS = [("ct", 32), ("busy", 1), ("done", 1)]

#: The netlist has no clock net for the simulator to drive: it steps registers.
IGNORED_INPUTS = ["clk"]
#: Active-low asynchronous reset.
ASYNC_CLEAR_INPUT = "rst_n"

ROUNDS = 22
ALPHA = 7
BETA = 2
WORD = 16
MASK = (1 << WORD) - 1


def _ror(value, amount):
    return ((value >> amount) | (value << (WORD - amount))) & MASK


def _rol(value, amount):
    return ((value << amount) | (value >> (WORD - amount))) & MASK


def initial_state():
    return {
        "x": 0,
        "y": 0,
        "k": 0,
        "l0": 0,
        "l1": 0,
        "l2": 0,
        "rnd": 0,
        "run": 0,
        "pen": 0,
        "fin": 0,
    }


def outputs(state, values):
    return {
        "ct": ((state["x"] << WORD) | state["y"]) & 0xFFFFFFFF,
        "busy": state["run"],
        "done": state["fin"],
    }


def next_state(state, values):
    # The clear is asynchronous and dominates: while it is low the flip-flops
    # are held at 0 no matter what the clock or the round logic is doing.
    if not values["rst_n"]:
        return initial_state()

    nxt = dict(state)
    load = values["start"] and not state["run"]

    if load:
        pt = values["pt"]
        key = values["key"]
        nxt["x"] = (pt >> WORD) & MASK
        nxt["y"] = pt & MASK
        nxt["k"] = key & MASK
        nxt["l0"] = (key >> 16) & MASK
        nxt["l1"] = (key >> 32) & MASK
        nxt["l2"] = (key >> 48) & MASK
    elif state["run"]:
        x, y, k = state["x"], state["y"], state["k"]
        x_new = ((_ror(x, ALPHA) + y) & MASK) ^ k
        y_new = _rol(y, BETA) ^ x_new
        l_new = ((_ror(state["l0"], ALPHA) + k) & MASK) ^ state["rnd"]
        k_new = _rol(k, BETA) ^ l_new
        nxt["x"] = x_new
        nxt["y"] = y_new
        nxt["k"] = k_new
        nxt["l0"] = state["l1"]
        nxt["l1"] = state["l2"]
        nxt["l2"] = l_new

    if state["run"]:
        nxt["rnd"] = 0 if state["pen"] else (state["rnd"] + 1) & 0x1F

    nxt["pen"] = 1 if (state["run"] and state["rnd"] == ROUNDS - 2) else 0
    if load:
        nxt["run"] = 1
        nxt["fin"] = 0
    elif state["run"] and state["pen"]:
        nxt["run"] = 0
        nxt["fin"] = 1

    return nxt
