"""The behaviour of ``recovered.v`` -- the model built from the netlist alone.

Every constant below was read out of ``trivium_stream.vo`` by ``analysis.py``
and nothing was copied from ``design.v``, ``spec.md`` or ``reference.py``:

* the three segment lengths and their order come from the chain walk
  (``analysis.py chains``);
* the tap sets and the single AND term in each feedback come from the algebraic
  normal form of the three head cones (``analysis.py feedback``);
* the six bits of the output function come from the keystream cell's cone
  (``analysis.py output``);
* the 64 x 18 warm-up and the load pattern come from the counter cells and the
  load multiplexers (``analysis.py warmup``, ``analysis.py load``).

Indices are the netlist's: ``s[i]`` is the flip-flop the export calls ``s[i]``,
which is the specification's ``s(i+1)``.  Keeping the two numbering schemes
apart is the point -- ``reference.py`` is written in the specification's and
this one in the netlist's, so agreeing is evidence rather than bookkeeping.

The three tap tuples and ``NONLINEAR`` are on lines of their own because
``run_analysis.sh`` and ``check.py`` edit exactly those lines to build the
negative controls: move one tap by one stage, or replace the AND with an XOR
and watch a cipher turn into a (very long) linear feedback register.
"""

KIND = "sequential"

INPUTS = [
    ("clk", 1),
    ("rst_n", 1),
    ("start", 1),
    ("key", 80),
    ("iv", 80),
]

OUTPUTS = [("ks", 1), ("ks_valid", 1), ("busy", 1)]

IGNORED_INPUTS = ["clk"]
ASYNC_CLEAR_INPUT = "rst_n"

#: Where each segment starts in the 288-bit vector, and how long it is.
SEGMENTS = ((0, 93), (93, 84), (177, 111))

#: The linear taps of each segment head's feedback, then its one AND pair.
T1_TAPS = (65, 92, 170)
T1_AND = (90, 91)
T2_TAPS = (161, 176, 263)
T2_AND = (174, 175)
T3_TAPS = (242, 287, 68)
T3_AND = (285, 286)

#: The output cell reads these six bits and nothing else.
Z_TAPS = (65, 92, 161, 176, 242, 287)

#: Set to 0 to build the "what if the AND gates were XORs" negative control.
NONLINEAR = 1

#: The two warm-up counter moduli, and the load pattern.
WARMUP_LO = 64
WARMUP_HI = 18
KEY_AT = 0
IV_AT = 93
ONES_AT = 285


def _bit(word, index):
    return (word >> index) & 1


def _parity(word, taps):
    result = 0
    for tap in taps:
        result ^= _bit(word, tap)
    return result


def _head(word, taps, pair):
    """One segment's feedback: an XOR of taps plus one product of two stages."""
    left, right = _bit(word, pair[0]), _bit(word, pair[1])
    term = (left & right) if NONLINEAR else (left ^ right)
    return _parity(word, taps) ^ term


def initial_state():
    return {"s": 0, "lo": 0, "hi": 0, "warm": 0, "done": 0}


def _advance(word):
    heads = (
        _head(word, T3_TAPS, T3_AND),  # into segment 0
        _head(word, T1_TAPS, T1_AND),  # into segment 1
        _head(word, T2_TAPS, T2_AND),  # into segment 2
    )
    result = 0
    for (base, length), head in zip(SEGMENTS, heads):
        kept = (word >> base) & ((1 << (length - 1)) - 1)
        result |= (((kept << 1) | head) & ((1 << length) - 1)) << base
    return result


def outputs(state, values):
    return {
        "ks": _parity(state["s"], Z_TAPS),
        "ks_valid": state["done"],
        "busy": state["warm"],
    }


def next_state(state, values):
    if not values["rst_n"]:
        return initial_state()

    nxt = dict(state)
    if values["start"] and not state["warm"]:
        nxt["s"] = (
            ((values["key"] & ((1 << 80) - 1)) << KEY_AT)
            | ((values["iv"] & ((1 << 80) - 1)) << IV_AT)
            | (7 << ONES_AT)
        )
        nxt["lo"] = 0
        nxt["hi"] = 0
        nxt["warm"] = 1
        nxt["done"] = 0
        return nxt

    nxt["s"] = _advance(state["s"])
    lo_last = state["lo"] == WARMUP_LO - 1
    hi_last = state["hi"] == WARMUP_HI - 1
    if state["warm"]:
        nxt["lo"] = (state["lo"] + 1) % (1 << 6)
        if lo_last:
            nxt["hi"] = (state["hi"] + 1) % (1 << 5)
        if lo_last and hi_last:
            nxt["warm"] = 0
            nxt["done"] = 1
    return nxt
