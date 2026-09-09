"""Python restatement of ``design.v`` -- the ORIGINAL RTL, for comparison only.

This is the answer key, not part of the reverse-engineering walk.  It is here so
that two things can be checked mechanically:

* that Quartus's netlist still behaves like the RTL it was synthesised from
  (``tools/hal_agilex behavior`` against the export), and
* that ``recovered.v`` agrees with it on every reachable state (``check.py``),
  which is what makes the "what was lost" section of the guide a statement of
  fact rather than an impression.
"""

KIND = "sequential"

INPUTS = [("clk", 1), ("rst_n", 1), ("hold", 1)]
OUTPUTS = [("red", 1), ("yellow", 1), ("green", 1)]

IGNORED_INPUTS = ["clk"]
ASYNC_CLEAR_INPUT = "rst_n"

S_RED, S_RED_YELLOW, S_GREEN, S_YELLOW = 0, 1, 2, 3
LIMIT = {S_RED: 9, S_RED_YELLOW: 2, S_GREEN: 12, S_YELLOW: 4}
NEXT = {S_RED: S_RED_YELLOW, S_RED_YELLOW: S_GREEN,
        S_GREEN: S_YELLOW, S_YELLOW: S_RED}


def initial_state():
    return {"state": S_RED, "tick": 0}


def outputs(state, values):
    s = state["state"]
    return {
        "red": int(s in (S_RED, S_RED_YELLOW)),
        "yellow": int(s in (S_RED_YELLOW, S_YELLOW)),
        "green": int(s == S_GREEN),
    }


def next_state(state, values):
    if not values["rst_n"]:
        return initial_state()
    s, tick = state["state"], state["tick"]
    expired = tick == LIMIT[s]
    hold = values["hold"]
    ns = NEXT[s] if (expired and not hold) else s
    if hold:
        ntick = tick
    elif expired:
        ntick = 0
    else:
        ntick = (tick + 1) & 0xF
    return {"state": ns, "tick": ntick}
