"""Python restatement of ``recovered.v`` -- the *reverse-engineered* model.

``tools/hal_agilex behavior`` simulates the exported netlist with the modelled
Agilex primitive semantics and compares it against this model.  A clean run is
evidence that the reconstruction in ``recovered.v`` is behaviourally the same
circuit as the netlist it was recovered from -- which is the only claim the
walkthrough is entitled to make about it.

Note what this does *not* compare against: ``design.v``.  The reference here is
the netlist, because the netlist is all a reverse engineer has.

    python tools/hal_agilex behavior \\
        examples/agilex3_walkthroughs/02_traffic_fsm/traffic_fsm.vo \\
        --reference examples/agilex3_walkthroughs/02_traffic_fsm/artifacts/reference_recovered.py \\
        --cycles 400
"""

KIND = "sequential"

# Port names come from the export; the walkthrough only knows them as
# i0..i2 / o0..o2 until the un-blinding step.
INPUTS = [("clk", 1), ("rst_n", 1), ("hold", 1)]
OUTPUTS = [("red", 1), ("yellow", 1), ("green", 1)]

IGNORED_INPUTS = ["clk"]
ASYNC_CLEAR_INPUT = "rst_n"


def initial_state():
    """Asynchronous clear puts every flip-flop at 0, which is phase A."""
    return {"r": 0, "y": 0, "g": 0, "w": 0, "cnt": 0}


def _legal(s):
    r, y, g, w = s["r"], s["y"], s["g"], s["w"]
    return int(
        ((not r) and (not y) and (not g) and (not w))
        or (r and (not y) and (w ^ g))
        or (r and y and (not w) and (not g))
    )


def _limit(s):
    if not s["r"]:
        return 9        # phase A
    if s["y"]:
        return 2        # phase B
    if s["g"]:
        return 12       # phase C
    return 4            # phase D


def outputs(state, values):
    r, y, g, w = state["r"], state["y"], state["g"], state["w"]
    return {
        "red": int((not r) or y),
        "yellow": int(y or w),
        "green": int(g),
    }


def next_state(state, values):
    if not values["rst_n"]:
        return initial_state()

    r, y, g, w = state["r"], state["y"], state["g"], state["w"]
    cnt = state["cnt"]
    expired = int(cnt == _limit(state))
    freeze = values["hold"]

    if not _legal(state):
        nr = ny = ng = nw = 0
    elif expired and not freeze:
        nr, ny, ng, nw = int(not w), int(not r), int(y), int(g)
    else:
        nr, ny, ng, nw = r, y, g, w

    if freeze:
        ncnt = cnt
    else:
        ncnt = 0 if expired else (cnt + 1) & 0xF

    return {"r": nr, "y": ny, "g": ng, "w": nw, "cnt": ncnt}
