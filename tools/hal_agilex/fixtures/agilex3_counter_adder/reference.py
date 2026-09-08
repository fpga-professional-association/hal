"""Reference model of ``counter_adder.v`` -- the ground truth for this fixture.

This is a plain Python restatement of the RTL that was handed to Quartus.  The
exported netlist is simulated with the modelled Agilex primitive semantics and
compared against it; a disagreement means the primitive model is wrong, not
that the design is.
"""

KIND = "sequential"

INPUTS = [("clk", 1), ("rst_n", 1), ("en", 1), ("addend", 8)]
OUTPUTS = [("count", 8), ("carry_out", 1)]

#: The netlist has no clock net to drive: the simulator steps registers itself.
IGNORED_INPUTS = ["clk"]
#: Active-low asynchronous reset.
ASYNC_CLEAR_INPUT = "rst_n"

WIDTH = 8
MASK = (1 << WIDTH) - 1


def initial_state():
    return {"count": 0}


def outputs(state, values):
    total = state["count"] + values["addend"]
    return {"count": state["count"], "carry_out": (total >> WIDTH) & 1}


def next_state(state, values):
    if not values["rst_n"]:
        return initial_state()
    if values["en"]:
        return {"count": (state["count"] + values["addend"]) & MASK}
    return dict(state)
