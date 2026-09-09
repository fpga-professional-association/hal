"""The behavioural model recovered from the netlist -- not from design.v.

Everything in here was read out of ``lfsr_prng.vo`` / ``netlist.hal.v`` by the
steps in ``guide.html``:

* the 16 flip-flops and the shift order came from the next-state functions,
* the tap set {15, 14, 12, 3} came from the one 4-input XOR in the netlist,
* the seed 0xACE1 came from *which* bits are stored inverted (every inverted
  bit is a bit whose reset value is 1, because the flip-flop only has an
  asynchronous clear),
* the enable and the active-low asynchronous reset came from the ``ena`` and
  ``clrn`` pins of the flip-flops.

It is written in the format ``tools/hal_agilex behavior`` consumes, so the
claim "my reconstruction behaves like the netlist" is checked by simulating the
exported netlist against this file:

    python tools/hal_agilex behavior \\
        examples/agilex3_walkthroughs/05_lfsr_prng/lfsr_prng.vo \\
        --reference examples/agilex3_walkthroughs/05_lfsr_prng/recovered_model.py \\
        --cycles 400
"""

KIND = "sequential"

INPUTS = [("clk", 1), ("rst_n", 1), ("en", 1)]
OUTPUTS = [("rand_out", 16), ("bit_out", 1)]

#: the netlist has no clock net to drive; the simulator steps the registers
IGNORED_INPUTS = ["clk"]
#: active-low asynchronous reset, recovered from the flip-flops' clrn pins
ASYNC_CLEAR_INPUT = "rst_n"

WIDTH = 16
MASK = (1 << WIDTH) - 1

#: recovered from the four data inputs of the single XOR cell
TAPS = (15, 14, 12, 3)
#: recovered from the set of bits that are stored inverted
SEED = 0xACE1


def initial_state():
    return {"state": SEED}


def outputs(state, values):
    return {
        "rand_out": state["state"],
        "bit_out": (state["state"] >> 15) & 1,
    }


def next_state(state, values):
    if not values["rst_n"]:
        return initial_state()
    if not values["en"]:
        return dict(state)
    word = state["state"]
    feedback = 0
    for tap in TAPS:
        feedback ^= (word >> tap) & 1
    return {"state": ((word << 1) | feedback) & MASK}
