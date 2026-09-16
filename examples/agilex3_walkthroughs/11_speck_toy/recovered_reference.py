"""Behavioural model of what the netlist was *believed* to be after step 8.

This file is written from the reverse-engineering result only -- it is the
Python restatement of ``recovered.v``, not of ``design.v``.  Feeding it to

    python tools/hal_agilex behavior speck_toy.vo \\
        --reference recovered_reference.py --cycles 2000

simulates the *exported netlist* with the validated Agilex primitive semantics
and compares it, cycle by cycle, against this model.  A disagreement would mean
the reverse engineering is wrong; agreement over 2000 cycles is a bounded
check, never a proof.

Every constant below has a provenance, and each is a place the recovery could
have gone wrong:

* ``ALPHA = 7``  -- the offset in the order the data-path carry chain reads the
  ``x`` register bank (step 5).  Slice *i* of the chain reads ``x[(i+7) mod
  16]``.
* ``BETA = 2``   -- the offset in the single ``y`` bit each ``y`` next-state
  cell reads (step 6): bit *i* reads ``y[(i-2) mod 16]``.
* ``WORD = 16``  -- the width of the register banks the chains read.
* ``ROUNDS = 22``-- the value the round counter reaches before the busy flag
  falls (step 7), measured from the counter's own logic, not assumed.
* the load order of ``pt`` and ``key`` -- which port bit reaches which register
  bank's load multiplexer (step 4).

``run_analysis.sh`` runs two deliberately broken copies of this file (``ALPHA``
one too large, ``BETA`` one too large) and requires both to be *caught*; a
bounded agreement with no negative control beside it proves nothing.
"""

KIND = "sequential"

INPUTS = [
    ("clk", 1),
    ("rst_n", 1),
    ("start", 1),
    ("pt", 32),
    ("key", 64),
]

OUTPUTS = [("ct", 32), ("busy", 1), ("done", 1)]

IGNORED_INPUTS = ["clk"]
#: The net that reaches every flip-flop's ``clrn`` pin and nothing else.
ASYNC_CLEAR_INPUT = "rst_n"

#: Recovered constants -- see the module docstring for where each came from.
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
        # the data path: one carry chain, one XOR layer, two rotations
        x_new = ((_ror(x, ALPHA) + y) & MASK) ^ k
        y_new = _rol(y, BETA) ^ x_new
        # the key schedule: the same shape, with the round counter where the
        # key would be
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
