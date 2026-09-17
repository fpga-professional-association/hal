"""Python model of ``design.v`` -- the RTL the export was synthesised from.

This is the *spec-side* model, written from ``spec.md``.  It is used once, in
section 2 of the guide, to establish that the exported netlist behaves like the
design that was handed to Quartus::

    python tools/hal_agilex behavior keccak_toy.vo --reference reference.py \\
        --cycles 600

That run simulates the export with the validated Agilex primitive semantics and
compares every output, every cycle, against this model.  It is *not* part of the
reverse engineering -- see ``recovered_reference.py`` for the model that is.

Deliberately written in the **specification's** ``A[x][y]`` lane algebra, with
its own rho-offset table and its own round-constant LFSR, so that it and
``recovered_reference.py`` -- which is written as a flat 200-bit permutation of
netlist bit indices, because that is all the netlist offers -- are visibly two
independent transcriptions.  They agree; that is worth something only because
neither was copied from the other.

The comparison is bounded, never a proof.  Section 7 also runs two deliberately
wrong copies of ``recovered_reference.py`` so that "it agreed for 600 cycles"
means something: see ``run_analysis.sh``.
"""

KIND = "sequential"

INPUTS = [
    ("clk", 1),
    ("rst_n", 1),
    ("start", 1),
    ("din", 200),
]

#: ``dout`` is the state register, driven continuously, so a wrong rotation
#: offset shows up on it one cycle after a load rather than only after round 18.
OUTPUTS = [("dout", 200), ("done", 1), ("busy", 1)]

#: The netlist has no clock net for the simulator to drive: it steps registers.
IGNORED_INPUTS = ["clk"]
#: Active-low asynchronous reset.
ASYNC_CLEAR_INPUT = "rst_n"

#: Lane width w, and l = log2(w).
W = 8
L = 3
#: Keccak-f[b] has 12 + 2*l rounds.
NROUNDS = 12 + 2 * L  # 18
STATE_BITS = 25 * W  # 200

#: The published rho offsets, stated at w = 64; at w = 8 only the residue
#: matters.  ``RHO[x][y]``.
RHO = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


def _rc_bit(t):
    """``rc(t)`` -- the 8-bit LFSR of FIPS 202 algorithm 5, x^8+x^6+x^5+x^4+1."""
    if t % 255 == 0:
        return 1
    register = 0x01
    for _ in range(t % 255):
        register <<= 1
        if register & 0x100:
            register ^= 0x171
        register &= 0xFF
    return register & 1


def _round_constants():
    """``RC[i]`` has bit 2^j - 1 equal to ``rc(j + 7i)``, for 0 <= j <= l."""
    constants = []
    for index in range(NROUNDS):
        value = 0
        for power in range(L + 1):
            if _rc_bit(power + 7 * index):
                value |= 1 << ((1 << power) - 1)
        constants.append(value)
    return tuple(constants)


#: 01 82 8a 00 8b 01 81 09 8a 88 09 0a 8b 8b 89 03 02 80.
RC = _round_constants()


def _lane(x, y):
    """Lane (x, y) is byte ``x + 5y`` of the state string."""
    return x + 5 * y


def _rotl(value, amount):
    amount %= W
    if amount == 0:
        return value & 0xFF
    return ((value << amount) | (value >> (W - amount))) & 0xFF


def _unpack(word):
    """The packed 200-bit port word as ``A[x][y]``."""
    return [
        [(word >> (W * _lane(x, y))) & 0xFF for y in range(5)] for x in range(5)
    ]


def _pack(lanes):
    word = 0
    for x in range(5):
        for y in range(5):
            word |= (lanes[x][y] & 0xFF) << (W * _lane(x, y))
    return word


def keccak_round(word, index):
    """One round of Keccak-f[200]: theta, rho, pi, chi, iota."""
    lanes = _unpack(word)

    # theta -- five column parities, each fed back one column left and one
    # column right rotated by one bit.
    column = [
        lanes[x][0] ^ lanes[x][1] ^ lanes[x][2] ^ lanes[x][3] ^ lanes[x][4]
        for x in range(5)
    ]
    delta = [column[(x - 1) % 5] ^ _rotl(column[(x + 1) % 5], 1) for x in range(5)]
    for x in range(5):
        for y in range(5):
            lanes[x][y] ^= delta[x]

    # rho and pi -- rotate each lane by its own offset, then move it to
    # (y, 2x + 3y).  Pure relabelling; no logic.
    moved = [[0] * 5 for _ in range(5)]
    for x in range(5):
        for y in range(5):
            moved[y][(2 * x + 3 * y) % 5] = _rotl(lanes[x][y], RHO[x][y])

    # chi -- the 5-bit row map, the only nonlinear step.
    for x in range(5):
        for y in range(5):
            lanes[x][y] = (
                moved[x][y] ^ ((~moved[(x + 1) % 5][y] & 0xFF) & moved[(x + 2) % 5][y])
            ) & 0xFF

    # iota -- one round constant into lane (0, 0).
    lanes[0][0] ^= RC[index]
    return _pack(lanes)


def keccak_f200(word):
    """The whole permutation, for the published test vectors."""
    for index in range(NROUNDS):
        word = keccak_round(word, index)
    return word


def initial_state():
    return {"s": 0, "rnd": 0, "run": 0, "fin": 0}


def outputs(state, values):
    return {"dout": state["s"], "done": state["fin"], "busy": state["run"]}


def next_state(state, values):
    # The clear is asynchronous and dominates: while it is low the flip-flops
    # are held at 0 no matter what the clock or the round logic is doing.
    if not values["rst_n"]:
        return initial_state()

    nxt = dict(state)
    load = values["start"] and not state["run"]

    if load:
        nxt["s"] = values["din"] & ((1 << STATE_BITS) - 1)
        nxt["rnd"] = 0
        nxt["run"] = 1
        nxt["fin"] = 0
        return nxt

    if state["run"]:
        nxt["s"] = keccak_round(state["s"], state["rnd"])
        nxt["rnd"] = (state["rnd"] + 1) % (1 << 5)
        if state["rnd"] == NROUNDS - 1:
            nxt["run"] = 0
            nxt["fin"] = 1
    # Otherwise the clock enable is low and every register holds.
    return nxt
