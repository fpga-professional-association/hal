"""Plain-Python restatement of design.v -- ground truth for the export check.

`tools/hal_agilex behavior` simulates the *exported netlist* with the modelled
Agilex primitive semantics and compares it against this model.  A disagreement
would mean the export (or the primitive model) does not do what the RTL said,
which is exactly the assumption the whole walkthrough rests on.

Run it with:

    python tools/hal_agilex behavior \\
        examples/agilex3_walkthroughs/10_crc8_checker/netlist/crc8_checker.vo \\
        --reference examples/agilex3_walkthroughs/10_crc8_checker/reference.py
"""

KIND = "sequential"

INPUTS = [("clk", 1), ("rst_n", 1), ("en", 1), ("din", 1)]
OUTPUTS = [("crc", 8), ("match", 1)]

#: The netlist has no clock net to drive: the simulator steps registers itself.
IGNORED_INPUTS = ["clk"]
#: Active-low asynchronous reset.
ASYNC_CLEAR_INPUT = "rst_n"

POLY = 0x07
MASK = 0xFF


def initial_state():
    return {"crc": 0}


def outputs(state, values):
    return {"crc": state["crc"], "match": 1 if state["crc"] == 0 else 0}


def _step(crc, bit):
    feedback = ((crc >> 7) & 1) ^ (bit & 1)
    shifted = (crc << 1) & MASK
    return shifted ^ (POLY if feedback else 0)


def next_state(state, values):
    if not values["rst_n"]:
        return initial_state()
    if values["en"]:
        return {"crc": _step(state["crc"], values["din"])}
    return dict(state)
