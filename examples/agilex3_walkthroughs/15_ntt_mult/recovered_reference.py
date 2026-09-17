"""The behaviour recovered from the netlist, as a ``hal_agilex`` reference model.

Every constant below came out of ``analysis.py`` and nothing came out of
``spec.md`` or ``design.v``.  Where ``reference.py`` is written in the ring's
arithmetic -- Python integers and ``% 257``, with the twiddle tables *computed*
from psi -- this file is written in the **netlist's** terms: nine-bit words, the
two-step reduction the correction logic actually performs, and every constant as
recovered *data*.

The two models agree.  That is worth something only because neither was copied
from the other, and because they are not even the same arithmetic: one divides,
the other subtracts once and looks at a sign bit.

Section 7 of the guide runs three deliberately wrong copies of this file -- one
twiddle constant moved, the modulus moved from 257 to 256, and one post-scale
constant moved -- so that "the netlist matched the model for N cycles" means
something.  All three knobs are single lines on purpose; ``run_analysis.sh`` and
``check.py`` patch them.
"""

KIND = "sequential"

INPUTS = [
    ("clk", 1),
    ("rst_n", 1),
    ("start", 1),
    ("a_in", 128),
    ("b_in", 128),
]

OUTPUTS = [("c_out", 144), ("done", 1), ("busy", 1)]

IGNORED_INPUTS = ["clk"]
ASYNC_CLEAR_INPUT = "rst_n"

#: analysis.py "modulus": there is no constant-operand carry chain in this
#: design, so this did not come off a chain's second operand.  It is the q for
#: which ``select ? sum - q : sum`` is a vector the netlist contains, checked on
#: every one of the 1023 sums the nine-bit adder can produce.
MODULUS = 257

#: analysis.py "registers": 288 coefficient flip-flops in two banks of sixteen
#: nine-bit words, and eight bits of input port per coefficient.
DEGREE = 16
WIDTH = 9

#: analysis.py "schedule": 144 cycles from an accepted start to `done`, in five
#: phases of 64, 16, 16, 32 and 16 steps, driven by a six-bit counter.
PHASE_NTT = 0
PHASE_POINT = 1
PHASE_BREV = 2
PHASE_INTT = 3
PHASE_POST = 4
PHASE_LENGTHS = (64, 16, 16, 32, 16)

#: analysis.py "twiddles": the multiplier's second operand at each step of the
#: 64-step phase, read by holding every coefficient register at 1.  Period 32:
#: the counter's top bit selects the register bank, not the constant.
TWIDDLE_FORWARD = (
    16, 16, 16, 16, 16, 16, 16, 16,
    253, 253, 253, 253, 193, 193, 193, 193,
    225, 225, 2, 2, 128, 128, 249, 249,
    15, 240, 197, 68, 34, 30, 121, 137,
)

#: analysis.py "twiddles": the same, for the 32-step phase.
TWIDDLE_INVERSE = (
    1, 1, 1, 1, 1, 1, 1, 1,
    1, 1, 1, 1, 241, 241, 241, 241,
    1, 1, 241, 241, 64, 64, 4, 4,
    1, 241, 64, 4, 8, 129, 255, 32,
)

#: analysis.py "twiddles": the 16-entry table of the last phase.
TWIDDLE_POST = (
    241, 256, 4, 193, 129, 249, 32, 2,
    136, 137, 223, 30, 60, 68, 242, 240,
)


def bitreverse(value, bits=4):
    """analysis.py "addresses": the map the two copy phases apply."""
    result = 0
    for position in range(bits):
        if value & (1 << position):
            result |= 1 << (bits - 1 - position)
    return result


def butterfly_addresses(step):
    """analysis.py "addresses": ``(j, j + len)`` for butterfly *step*.

    The recovered pairs are ``len`` apart with ``len`` equal to 8 for the first
    eight steps of a transform, then 4, then 2, then 1 -- and within a block the
    low index runs over the eight coefficients that are not on the high side.
    That is a zero inserted into the step number at bit ``3 - stage``.
    """
    stage = step >> 3
    position = step & 7
    shift = 3 - stage
    group = position >> shift
    low = position & ((1 << shift) - 1)
    first = (group << (shift + 1)) | low
    return first, first | (1 << shift)


# ---------------------------------------------------------------------------
# the datapath, in the netlist's own arithmetic
# ---------------------------------------------------------------------------

MASK = (1 << WIDTH) - 1


def modular_product(first, second):
    """The multiplier: a 17-bit product folded by 256 = -1 (mod 2**8 + 1).

    Written the way the cells are wired rather than as ``a * b % q``: the high
    half is subtracted from the low half and the result corrected once.  It
    agrees with ``%`` for every pair of canonical residues, which is a claim
    about the *modulus* being a Fermat prime, not about Python.
    """
    product = (first * second) & 0x1FFFF
    folded = (product & 0xFF) - (product >> 8)
    if folded < 0:
        folded += MODULUS
    return folded & MASK


def modular_sum(first, second):
    """The adder plus its conditional subtract of q."""
    total = first + second
    if total >= MODULUS:
        total -= MODULUS
    return total & MASK


def modular_difference(first, second):
    """The subtracter plus its conditional add of q."""
    total = first - second
    if total < 0:
        total += MODULUS
    return total & MASK


# ---------------------------------------------------------------------------
# the cycle-accurate model
# ---------------------------------------------------------------------------


def initial_state():
    return {
        "pa": [0] * DEGREE,
        "pb": [0] * DEGREE,
        "cnt": 0,
        "ph": PHASE_NTT,
        "run": 0,
        "fin": 0,
    }


def outputs(state, values):
    word = 0
    for index in range(DEGREE):
        word |= (state["pb"][index] & MASK) << (WIDTH * index)
    return {"c_out": word, "done": state["fin"], "busy": state["run"]}


def _last_step(phase, count):
    """The three kept compare terms: cnt == 15, == 31 and == 63."""
    equal15 = (count & 0xF) == 0xF
    equal31 = equal15 and bool(count & 0x10)
    equal63 = equal31 and bool(count & 0x20)
    if phase == PHASE_NTT:
        return equal63
    if phase == PHASE_INTT:
        return equal31
    return equal15


def _step(state):
    phase = state["ph"]
    count = state["cnt"]
    butterfly = phase in (PHASE_NTT, PHASE_INTT)
    index = count & 0xF
    first, second = butterfly_addresses(count & 0x1F)
    bank = (count >> 5) & 1

    read_upper = first if butterfly else index
    read_lower = second if butterfly else index
    upper_from_b = phase == PHASE_NTT and bank
    lower_from_b = bank if phase == PHASE_NTT else phase in (PHASE_POINT, PHASE_BREV)
    write_to_b = bank if phase == PHASE_NTT else phase in (PHASE_POINT, PHASE_POST)

    upper = 0
    if butterfly:
        upper = (state["pb"] if upper_from_b else state["pa"])[read_upper]
    lower = (state["pb"] if lower_from_b else state["pa"])[read_lower]

    if phase == PHASE_POINT:
        twiddle = state["pa"][read_upper]
    elif phase == PHASE_NTT:
        twiddle = TWIDDLE_FORWARD[count & 0x1F]
    elif phase == PHASE_INTT:
        twiddle = TWIDDLE_INVERSE[count & 0x1F]
    elif phase == PHASE_POST:
        twiddle = TWIDDLE_POST[index]
    else:
        twiddle = 1

    product = modular_product(twiddle, lower)
    total = modular_sum(upper, product)
    difference = modular_difference(upper, product)

    if butterfly:
        write_sum = first
    elif phase in (PHASE_BREV, PHASE_POST):
        write_sum = bitreverse(index)
    else:
        write_sum = index

    nxt = dict(state)
    nxt["pa"] = list(state["pa"])
    nxt["pb"] = list(state["pb"])
    target = nxt["pb"] if write_to_b else nxt["pa"]
    target[write_sum] = total
    if butterfly:
        target[second] = difference

    last = _last_step(state["ph"], state["cnt"])
    nxt["cnt"] = 0 if last else (state["cnt"] + 1) & 0x3F
    if last:
        nxt["ph"] = (state["ph"] + 1) & 7
        if state["ph"] == PHASE_POST:
            nxt["run"] = 0
            nxt["fin"] = 1
    return nxt


def next_state(state, values):
    if not values["rst_n"]:
        return initial_state()

    if values["start"] and not state["run"]:
        return {
            "pa": [(values["a_in"] >> (8 * i)) & 0xFF for i in range(DEGREE)],
            "pb": [(values["b_in"] >> (8 * i)) & 0xFF for i in range(DEGREE)],
            "cnt": 0,
            "ph": PHASE_NTT,
            "run": 1,
            "fin": 0,
        }

    if not state["run"]:
        return state
    return _step(state)


def negacyclic_multiply(first, second):
    """The whole product, through the same 144 cycles the hardware runs."""
    state = initial_state()
    state["pa"] = list(first)
    state["pb"] = list(second)
    state["run"] = 1
    for _ in range(sum(PHASE_LENGTHS)):
        state = _step(state)
    return list(state["pb"])
