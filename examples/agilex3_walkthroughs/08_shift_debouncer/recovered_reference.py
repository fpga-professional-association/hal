"""Executable version of ``recovered.v`` -- the RTL rebuilt from the netlist.

This is *not* a copy of ``reference.py``.  It is written from the claims in
``recovered.v``, each of which cites an artifact:

*   the register groups come from ``artifacts/05_register_sccs.txt``;
*   the counter's transition relation, including which bit is the LSB, comes
    from ``artifacts/07_word_semantics.txt`` part A (all 32 transitions
    enumerated);
*   the two values that move the hysteresis bit come from part B (all 32
    (flag, counter) combinations enumerated);
*   the output equation comes from part C (4 rows).

That it ends up almost identical to ``reference.py`` is the *result* of the
walkthrough, not a shortcut taken while writing it.  ``check.py`` runs both
against the exported netlist so the claim is checked rather than asserted.

    python3 tools/hal_agilex behavior shift_debouncer.vo \\
        --reference recovered_reference.py --cycles 2000
"""

KIND = "sequential"

INPUTS = [("clk", 1), ("rst_n", 1), ("btn_raw", 1)]
OUTPUTS = [("btn_state", 1), ("btn_rise", 1)]

#: The netlist has no clock net to drive: the simulator steps registers itself.
IGNORED_INPUTS = ["clk"]
#: artifacts/02_clock_reset.txt: one clrn net, active low, on all eight flops.
ASYNC_CLEAR_INPUT = "rst_n"

#: artifacts/07_word_semantics.txt part A: the up path ends at 1111 and holds,
#: the down path ends at 0000 and holds.  These are the two fixed points.
TOP = 0xF
BOTTOM = 0x0


def initial_state():
    # Every flop clears to 0; there is no set pin anywhere in the export.
    return {"sync": 0, "cnt": 0, "state": 0, "state_d": 0}


def outputs(state, values):
    # btn_state is a flip-flop q pin wired straight to the port.
    # btn_rise is the one ALM whose combout leaves the module: dataa & !datab
    # over (state, state_d).
    return {
        "btn_state": state["state"],
        "btn_rise": state["state"] & (~state["state_d"] & 1),
    }


def next_state(state, values):
    if not values["rst_n"]:
        return initial_state()

    # Group A -- two pure delay flops in a chain, head fed by the raw pin.
    sync = ((state["sync"] << 1) | (values["btn_raw"] & 1)) & 0x3
    direction = (state["sync"] >> 1) & 1     # the tail stage, before it moves

    # Group B -- the 4-flop feedback group.  Walk one step along whichever of
    # the two enumerated paths `direction` selects, and stop at the end.
    cnt = state["cnt"]
    if direction:
        cnt = cnt + 1 if cnt != TOP else TOP
    else:
        cnt = cnt - 1 if cnt != BOTTOM else BOTTOM

    # Group C -- the self-holding bit.  SET at the top of the path, CLEAR at
    # the bottom, HOLD at the fourteen values in between.  The table is over
    # the counter value BEFORE this step, because the cell reads the flops'
    # current outputs.
    if state["cnt"] == TOP:
        new_state = 1
    elif state["cnt"] == BOTTOM:
        new_state = 0
    else:
        new_state = state["state"]

    # Group D -- one more delay flop.
    return {
        "sync": sync,
        "cnt": cnt,
        "state": new_state,
        "state_d": state["state"],
    }
