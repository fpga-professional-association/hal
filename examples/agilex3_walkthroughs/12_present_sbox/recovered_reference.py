"""The reconstruction of 12_present_sbox as a behavioural model.

Every constant below was read out of the netlist by ``analysis.py`` -- the
substitution table from the LUT cones, the bit permutation from the wiring
between the cones and the flip-flops, the round count from the counter -- and
none of it from ``design.v``.  ``tools/hal_agilex behavior`` simulates the
*export* against this model, so agreement is evidence that the recovery is
right about the whole state machine and not just about the pieces that were
inspected structurally.

Reference-model contract: see ``tools/hal_agilex/behavior.py``.

The two negative controls in ``run_analysis.sh`` are one-line ``sed`` edits of
this file (one substitution entry, and the round count), which is the only
reason a passing run here means anything.
"""

KIND = "sequential"

INPUTS = [
    ("clk", 1),
    ("rst_n", 1),
    ("start", 1),
    ("plaintext", 64),
    ("key_in", 80),
]
OUTPUTS = [
    ("ciphertext", 64),
    ("busy", 1),
    ("done", 1),
]
IGNORED_INPUTS = ["clk"]
ASYNC_CLEAR_INPUT = "rst_n"

MASK64 = (1 << 64) - 1
MASK80 = (1 << 80) - 1

# Recovered from the 17 four-bit LUT cones (analysis.py, step "substitution").
SBOX = [
    0xC, 0x5, 0x6, 0xB,
    0x9, 0x0, 0xA, 0xD,
    0x3, 0xE, 0xF, 0x8,
    0x4, 0x7, 0x1, 0x2,
]

# Recovered from which cone output drives which flip-flop (step "permutation"):
# PLAYER[i] is the state bit that substitution-layer bit i drives.
PLAYER = [63 if i == 63 else (16 * i) % 63 for i in range(64)]

# Recovered from the 5-bit counter and the register that stops it.
ROUNDS = 31


def _sbox_layer(word):
    out = 0
    for nibble in range(16):
        out |= SBOX[(word >> (4 * nibble)) & 0xF] << (4 * nibble)
    return out


def _p_layer(word):
    out = 0
    for bit in range(64):
        if (word >> bit) & 1:
            out |= 1 << PLAYER[bit]
    return out


def _key_next(kreg, round_index):
    """One key-schedule step: rotate left 61, substitute the top nibble, XOR the counter."""
    rotated = ((kreg << 61) | (kreg >> 19)) & MASK80
    top = SBOX[(rotated >> 76) & 0xF]
    middle = ((rotated >> 15) & 0x1F) ^ (round_index & 0x1F)
    return ((top << 76)
            | (rotated & ~((0xF << 76) | (0x1F << 15)) & MASK80)
            | (middle << 15))


def initial_state():
    return {"state": 0, "kreg": 0, "round": 0, "running": 0, "done": 0}


def outputs(state, values):
    return {
        "ciphertext": state["state"],
        "busy": state["running"],
        "done": state["done"],
    }


def next_state(state, values):
    # The clear reaches every clrn pin, so a clock edge with rst_n low leaves
    # the whole design in its cleared state rather than advancing it.
    if not values["rst_n"]:
        return initial_state()

    running = state["running"]
    load = values["start"] and not running
    last = 1 if (running and state["round"] == ROUNDS) else 0

    new = dict(state)
    if load:
        key = values["key_in"] & MASK80
        new["state"] = (values["plaintext"] ^ (key >> 16)) & MASK64
        new["kreg"] = key
        new["round"] = 1
        new["running"] = 1
    elif running:
        knext = _key_next(state["kreg"], state["round"])
        new["state"] = (_p_layer(_sbox_layer(state["state"])) ^ (knext >> 16)) & MASK64
        new["kreg"] = knext
        new["round"] = 0 if last else state["round"] + 1
        new["running"] = 0 if last else 1

    if load or running:
        new["done"] = last
    return new
