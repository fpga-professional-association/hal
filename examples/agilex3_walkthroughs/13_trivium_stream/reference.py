"""Python model of ``design.v`` -- the RTL the export was synthesised from.

This is the *spec-side* model, written from ``spec.md``.  It is used once, in
section 2 of the guide, to establish that the exported netlist behaves like the
design that was handed to Quartus:

    python tools/hal_agilex behavior trivium_stream.vo --reference reference.py \\
        --cycles 2400

That run simulates the export with the validated Agilex primitive semantics and
compares every output, every cycle, against this model.  It is *not* part of the
reverse engineering -- see ``recovered_reference.py`` for the model that is.

Deliberately written in the **specification's** 1-based numbering (``s1`` ..
``s288``), not the netlist's 0-based vector, so that it and
``recovered_reference.py`` -- which is written in the netlist's numbering,
because that is all the netlist offers -- are visibly two independent
transcriptions.  They agree; that is worth something only because neither was
copied from the other.

The comparison is bounded, never a proof.  Section 7 also runs two deliberately
wrong copies of ``recovered_reference.py`` so that "it agreed for 2400 cycles"
means something: see ``run_analysis.sh``.
"""

KIND = "sequential"

INPUTS = [
    ("clk", 1),
    ("rst_n", 1),
    ("start", 1),
    ("key", 80),
    ("iv", 80),
]

#: ``ks`` is driven continuously, so a wrong feedback tap shows up on it within
#: tens of cycles rather than only after the 1152-cycle warm-up.
OUTPUTS = [("ks", 1), ("ks_valid", 1), ("busy", 1)]

#: The netlist has no clock net for the simulator to drive: it steps registers.
IGNORED_INPUTS = ["clk"]
#: Active-low asynchronous reset.
ASYNC_CLEAR_INPUT = "rst_n"

STATE_BITS = 288
WARMUP_STEPS = 4 * STATE_BITS  # 1152
WARMUP_LO = 64
WARMUP_HI = 18


def _s(state, index):
    """Specification bit ``s<index>``, 1-based, out of the packed state word."""
    return (state >> (index - 1)) & 1


def initial_state():
    return {"s": 0, "lo": 0, "hi": 0, "warm": 0, "done": 0}


def _keystream(s):
    """z = s66 + s93 + s162 + s177 + s243 + s288 (Trivium's output function)."""
    return (
        _s(s, 66) ^ _s(s, 93) ^ _s(s, 162) ^ _s(s, 177) ^ _s(s, 243) ^ _s(s, 288)
    )


def _advance(s):
    """One Trivium step of the 288-bit state, spec section 'One cycle'."""
    t1 = _s(s, 66) ^ _s(s, 93) ^ (_s(s, 91) & _s(s, 92)) ^ _s(s, 171)
    t2 = _s(s, 162) ^ _s(s, 177) ^ (_s(s, 175) & _s(s, 176)) ^ _s(s, 264)
    t3 = _s(s, 243) ^ _s(s, 288) ^ (_s(s, 286) & _s(s, 287)) ^ _s(s, 69)

    # (s1..s93) = (t3, s1..s92); (s94..s177) = (t1, s94..s176);
    # (s178..s288) = (t2, s178..s287).  Bit i-1 of the word is spec bit si.
    segment_a = ((s & ((1 << 92) - 1)) << 1) | t3
    segment_b = ((((s >> 93) & ((1 << 83) - 1)) << 1) | t1) << 93
    segment_c = ((((s >> 177) & ((1 << 110) - 1)) << 1) | t2) << 177
    return segment_a | segment_b | segment_c


def _load(key, iv):
    """(K1..K80, 0x13) | (IV1..IV80, 0x4) | (0x108, 1, 1, 1)."""
    return (key & ((1 << 80) - 1)) | ((iv & ((1 << 80) - 1)) << 93) | (7 << 285)


def outputs(state, values):
    return {
        "ks": _keystream(state["s"]),
        "ks_valid": state["done"],
        "busy": state["warm"],
    }


def next_state(state, values):
    # The clear is asynchronous and dominates: while it is low the flip-flops
    # are held at 0 no matter what the clock or the feedback is doing.
    if not values["rst_n"]:
        return initial_state()

    nxt = dict(state)
    load = values["start"] and not state["warm"]

    if load:
        nxt["s"] = _load(values["key"], values["iv"])
        nxt["lo"] = 0
        nxt["hi"] = 0
        nxt["warm"] = 1
        nxt["done"] = 0
        return nxt

    # Not a load: one cipher step, always.  Shifting the all-zero state gives
    # the all-zero state, so an idle core needs no hold.
    nxt["s"] = _advance(state["s"])

    lo_last = state["lo"] == WARMUP_LO - 1
    hi_last = state["hi"] == WARMUP_HI - 1
    if state["warm"]:
        nxt["lo"] = (state["lo"] + 1) % (1 << 6)
        if lo_last:
            nxt["hi"] = (state["hi"] + 1) % (1 << 5)
        if lo_last and hi_last:
            nxt["warm"] = 0
            nxt["done"] = 1
    return nxt
