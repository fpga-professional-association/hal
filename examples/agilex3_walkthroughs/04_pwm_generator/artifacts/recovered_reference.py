"""Behavioural model of what the netlist was *reverse engineered* to be.

This file is written from `recovered.v` -- i.e. from the hypothesis the guide
builds out of the netlist alone -- and is then checked against the exported
netlist with `tools/hal_agilex behavior`.  It is deliberately NOT derived from
`design.v`: if the recovery were wrong, the comparison has to fail.

Interface expected by hal_agilex.behavior:
    KIND, INPUTS, OUTPUTS, IGNORED_INPUTS, ASYNC_CLEAR_INPUT,
    initial_state(), outputs(state, values), next_state(state, values)
"""

KIND = "sequential"

INPUTS = [
    ("clk", 1),
    ("rst_n", 1),
    ("run", 1),
    ("cfg_we", 1),
    ("cfg_addr", 2),
    ("cfg_wdata", 8),
]
OUTPUTS = [("pwm_out", 1), ("period_tick", 1)]

#: The netlist has no clock net to drive; the simulator steps registers itself.
IGNORED_INPUTS = ["clk"]
#: Active-low asynchronous clear on every flip-flop (recovered from clrn).
ASYNC_CLEAR_INPUT = "rst_n"

WIDTH = 8
MASK = (1 << WIDTH) - 1

#: Recovered from the decode LUT: enable = cfg_we & cfg_addr[0] & !cfg_addr[1].
ADDR_DUTY = 0b01


def initial_state():
    return {"cnt": 0, "duty": 0}


def outputs(state, values):
    return {
        "pwm_out": 1 if state["cnt"] < state["duty"] else 0,
        "period_tick": 1 if state["cnt"] == MASK else 0,
    }


def next_state(state, values):
    if not values["rst_n"]:
        return initial_state()
    cnt = state["cnt"]
    duty = state["duty"]
    if values["run"]:
        cnt = (cnt + 1) & MASK
    if values["cfg_we"] and values["cfg_addr"] == ADDR_DUTY:
        duty = values["cfg_wdata"] & MASK
    return {"cnt": cnt, "duty": duty}
