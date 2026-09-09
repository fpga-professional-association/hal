"""Behavioural model of what the netlist was *believed* to be after step 6.

This file is written from the reverse-engineering result only -- it is the
Python restatement of ``recovered.v``, not of ``design.v``.  Feeding it to

    python tools/hal_agilex behavior blinky_counter.vo --reference recovered_reference.py

simulates the *exported netlist* with the validated Agilex primitive semantics
and compares it, cycle by cycle, against this model.  A disagreement would mean
the reverse engineering is wrong; agreement over N cycles is a bounded check,
never a proof (a divergence at cycle N+1 is not excluded).

Note what is deliberately *not* here: the name ``count``, the width 24 and the
bit index 23 are all things that were read out of the netlist structure.  If
any of them had been recovered wrongly, the comparison below would fail --
which is exactly why it is worth running.
"""

KIND = "sequential"

INPUTS = [("clk", 1), ("rst_n", 1)]

#: ``led`` is the design's only real output -- and comparing it alone would be
#: nearly vacuous: it is bit 23 of the counter, so over any number of cycles a
#: Python simulator can run it stays 0 in both models and a wrong bit index
#: would go unnoticed.  The export also *declares* the 24-bit bus the registers
#: drive (``wire [23:0] count;``), and the simulator can read any declared
#: signal by name, so the check below compares all 24 state bits every cycle.
#: That is a white-box observation -- state you can see because you have the
#: netlist, not because the design exposes it.
OUTPUTS = [("led", 1), ("count", 24)]

#: The netlist has no clock net for the simulator to drive: it steps registers.
IGNORED_INPUTS = ["clk"]
#: Active-low asynchronous reset, recovered from the net that reaches every
#: flip-flop's ``clrn`` pin and nothing else.
ASYNC_CLEAR_INPUT = "rst_n"

#: Recovered from the size of the strongly connected component: 24 registers.
WIDTH = 24
MASK = (1 << WIDTH) - 1
#: Recovered from which register's q net leaves the module: the top bit.
LED_BIT = WIDTH - 1


def initial_state():
    return {"count": 0}


def outputs(state, values):
    return {"led": (state["count"] >> LED_BIT) & 1, "count": state["count"]}


def next_state(state, values):
    if not values["rst_n"]:
        return initial_state()
    return {"count": (state["count"] + 1) & MASK}
