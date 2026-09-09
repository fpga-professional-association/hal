"""Reference model of ``design.v`` -- the ground truth for this walkthrough.

A plain Python restatement of the RTL that was handed to Quartus.  It is used
two ways:

*   ``python tools/hal_agilex behavior shift_debouncer.vo --reference reference.py``
    simulates the *exported netlist* with the modelled Agilex primitive
    semantics and compares it against this model.  That is what lets the guide
    say "the netlist really does behave like the RTL" instead of assuming it.

*   ``check.py`` reuses it to compare ``recovered.v`` -- the RTL reconstructed
    from the netlist alone -- against the original design's behaviour.
"""

KIND = "sequential"

INPUTS = [("clk", 1), ("rst_n", 1), ("btn_raw", 1)]
OUTPUTS = [("btn_state", 1), ("btn_rise", 1)]

#: The netlist has no clock net to drive: the simulator steps registers itself.
IGNORED_INPUTS = ["clk"]
#: Active-low asynchronous reset.
ASYNC_CLEAR_INPUT = "rst_n"

CNT_MAX = 0xF
CNT_MIN = 0x0


def initial_state():
    return {"sync": 0, "cnt": 0, "state": 0, "state_d": 0}


def outputs(state, values):
    return {
        "btn_state": state["state"],
        "btn_rise": state["state"] & (~state["state_d"] & 1),
    }


def next_state(state, values):
    if not values["rst_n"]:
        return initial_state()

    # 1. two-flop synchronizer: {sync[0], btn_raw}
    sync = ((state["sync"] << 1) | (values["btn_raw"] & 1)) & 0x3
    sync_level = (state["sync"] >> 1) & 1  # sync[1] BEFORE the shift

    # 2. saturating up/down counter
    cnt = state["cnt"]
    at_max = cnt == CNT_MAX
    at_min = cnt == CNT_MIN
    if sync_level and not at_max:
        cnt = (cnt + 1) & 0xF
    elif not sync_level and not at_min:
        cnt = (cnt - 1) & 0xF

    # 3. hysteresis output (uses the OLD counter value, like the RTL)
    if at_max:
        new_state = 1
    elif at_min:
        new_state = 0
    else:
        new_state = state["state"]

    # 4. edge-detect delay flop
    return {
        "sync": sync,
        "cnt": cnt,
        "state": new_state,
        "state_d": state["state"],
    }
