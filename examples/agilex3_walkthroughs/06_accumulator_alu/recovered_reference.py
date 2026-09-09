"""Behavioural model of `recovered.v` -- the claim, in executable form.

This is *not* a restatement of design.v.  It is a Python transcription of
`recovered_clean` in recovered.v, i.e. of what the walkthrough recovered from
the netlist.  `python tools/hal_agilex behavior netlist/accumulator_alu.vo
--reference recovered_reference.py` simulates the exported netlist with the
modelled ALM semantics and compares it against this model, which turns "I think
op=2'b10 is a subtract" into a checkable bounded claim.

If the recovery had the opcode encoding wrong, or missed the carry-in trick, or
got the flag polarity backwards, the comparison would print a counterexample.
"""

KIND = "sequential"

INPUTS = [("clk", 1), ("rst_n", 1), ("op", 2), ("operand", 8)]
OUTPUTS = [("acc", 8), ("carry", 1), ("zero", 1)]

#: The simulator steps the registers itself; there is no clock net to drive.
IGNORED_INPUTS = ["clk"]
#: tennm_ff.clrn is driven straight from rst_n: asynchronous, active low.
ASYNC_CLEAR_INPUT = "rst_n"

WIDTH = 8
MASK = (1 << WIDTH) - 1

OP_NOP = 0b00
OP_ADD = 0b01
OP_SUB = 0b10
OP_CLR = 0b11


def initial_state():
    return {"acc": 0, "carry": 0}


def outputs(state, values):
    # acc, carry and zero are all functions of the register state alone:
    # `zero` is the 8-input NOR of the accumulator outputs (reduce_or_0).
    return {
        "acc": state["acc"],
        "carry": state["carry"],
        "zero": 1 if state["acc"] == 0 else 0,
    }


def next_state(state, values):
    if not values["rst_n"]:
        return initial_state()

    op = values["op"]
    if op == OP_NOP:
        return dict(state)
    if op == OP_CLR:
        return {"acc": 0, "carry": 0}

    # ADD and SUB share the one adder: SUB inverts the operand and sets the
    # carry-in, which is a - b == a + ~b + 1.
    sub = 1 if op == OP_SUB else 0
    addend = (~values["operand"]) & MASK if sub else values["operand"]
    total = state["acc"] + addend + sub
    return {"acc": total & MASK, "carry": (total >> WIDTH) & 1}
