"""The behaviour recovered from the netlist, as a ``hal_agilex`` reference model.

Every constant below came out of ``analysis.py`` and nothing came out of
``spec.md`` or ``design.v``.  Where ``reference.py`` is written in the
permutation's published ``A[x][y]`` lane algebra with its own rho table and its
own round-constant LFSR, this file is written in the **netlist's** flat 200-bit
word with the tables as *recovered data*: a 5 x 5 x 8 grid whose shape came from
the forty column-parity cells, twenty-five rotation amounts read off which theta
net each chi cell reads, and eighteen round constants enumerated out of the
round-counter cones.

The two models agree.  That is worth something only because neither was copied
from the other.

Section 7 of the guide also runs two deliberately wrong copies of this file --
one rotation offset moved by one, and the AND in chi replaced by an XOR -- so
that "the netlist matched the model for N cycles" means something.  Both knobs
are single lines on purpose; ``run_analysis.sh`` and ``check.py`` patch them.
"""

KIND = "sequential"

INPUTS = [
    ("clk", 1),
    ("rst_n", 1),
    ("start", 1),
    ("din", 200),
]

OUTPUTS = [("dout", 200), ("done", 1), ("busy", 1)]

IGNORED_INPUTS = ["clk"]
ASYNC_CLEAR_INPUT = "rst_n"

#: analysis.py "parity": the forty column-parity cells partition the 200
#: flip-flops into forty five-element classes ``{i, i+40, i+80, i+120, i+160}``,
#: the unrotated parity neighbour has order five and the rotated one composes
#: with it into an eight-cycle.  Five columns, eight bit positions, five rows:
#: flip-flop ``i`` is lane ``(x, y) = ((i // 8) % 5, i // 40)`` at bit ``i % 8``.
W = 8
LANES = 25
STATE_BITS = LANES * W  # 200

#: analysis.py "rounds": one five-input terminal-count cell over the five
#: counter flip-flops, true for exactly one value, 17.
NROUNDS = 18

#: analysis.py "rhopi": the bit-index difference across each lane of the
#: composite rho-then-pi map, which is constant per lane or the recovery fails.
#: ``RHO[x][y]``, already reduced mod 8 because that is all the netlist has.
RHO = (
    (0, 4, 3, 1, 2),
    (1, 4, 2, 5, 2),
    (6, 6, 3, 7, 5),
    (4, 7, 1, 5, 0),
    (3, 4, 7, 0, 6),
)

#: analysis.py "iota": eight round-constant cells over the round counter, four
#: of them constant zero, enumerated for counter values 0..17.
RC = (
    0x01, 0x82, 0x8A, 0x00, 0x8B, 0x01, 0x81, 0x09, 0x8A,
    0x88, 0x09, 0x0A, 0x8B, 0x8B, 0x89, 0x03, 0x02, 0x80,
)

#: analysis.py "chi": the 5-bit row map has algebraic degree two -- one AND
#: term.  Setting this to 0 replaces it with an XOR, which makes the whole
#: permutation linear over GF(2); that is the second negative control.
NONLINEAR = 1


def _lane(x, y):
    return x + 5 * y


def _rotl(value, amount):
    amount %= W
    if amount == 0:
        return value & 0xFF
    return ((value << amount) | (value >> (W - amount))) & 0xFF


def _unpack(word):
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
    lanes = _unpack(word)

    # theta: one parity lane per column, fed back one column left unrotated and
    # one column right rotated by a single bit.
    column = [
        lanes[x][0] ^ lanes[x][1] ^ lanes[x][2] ^ lanes[x][3] ^ lanes[x][4]
        for x in range(5)
    ]
    delta = [column[(x - 1) % 5] ^ _rotl(column[(x + 1) % 5], 1) for x in range(5)]
    for x in range(5):
        for y in range(5):
            lanes[x][y] ^= delta[x]

    # rho and pi: no cells at all in the netlist -- this is the wiring.
    moved = [[0] * 5 for _ in range(5)]
    for x in range(5):
        for y in range(5):
            moved[y][(2 * x + 3 * y) % 5] = _rotl(lanes[x][y], RHO[x][y])

    # chi: the 5-bit row map, the one nonlinear layer.
    for x in range(5):
        for y in range(5):
            near = moved[(x + 1) % 5][y]
            far = moved[(x + 2) % 5][y]
            mixed = ((~near & 0xFF) & far) if NONLINEAR else (near ^ far)
            lanes[x][y] = (moved[x][y] ^ mixed) & 0xFF

    # iota: one constant into the lane the round counter reaches.
    lanes[0][0] ^= RC[index]
    return _pack(lanes)


def keccak_f200(word):
    for index in range(NROUNDS):
        word = keccak_round(word, index)
    return word


def initial_state():
    return {"s": 0, "rnd": 0, "run": 0, "fin": 0}


def outputs(state, values):
    return {"dout": state["s"], "done": state["fin"], "busy": state["run"]}


def next_state(state, values):
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
    return nxt
