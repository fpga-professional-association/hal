"""Python model of ``design.v`` -- the RTL the export was synthesised from.

This is the *spec-side* model, written from ``spec.md``.  It is used once, in
section 2 of the guide, to establish that the exported netlist behaves like the
design that was handed to Quartus::

    python tools/hal_agilex behavior ntt_mult.vo --reference reference.py \\
        --cycles 400

That run simulates the export with the validated Agilex primitive semantics and
compares every output, every cycle, against this model.  It is *not* part of the
reverse engineering -- see ``recovered_reference.py`` for the model that is.

Deliberately written in the **ring's** arithmetic: coefficients are Python
integers, the reduction is ``% 257``, and the twiddle tables are *computed*
from psi = 15 rather than transcribed.  ``recovered_reference.py`` is written
the other way round -- 9-bit words, the netlist's own two-step reduction, and
the constants as recovered data -- so that the two are visibly independent
transcriptions.  They agree because every value in the datapath is a canonical
residue in ``[0, 256]``, which is why the design takes *byte* inputs: a byte is
already reduced, so nothing ever has to be.

The comparison is bounded, never a proof.  Section 7 runs three deliberately
wrong copies of ``recovered_reference.py`` so that "it agreed for 400 cycles"
means something: see ``run_analysis.sh``.
"""

KIND = "sequential"

INPUTS = [
    ("clk", 1),
    ("rst_n", 1),
    ("start", 1),
    ("a_in", 128),
    ("b_in", 128),
]

#: ``c_out`` is the pb register bank, driven continuously, so a wrong twiddle
#: constant shows up on it inside the transform that used it.
OUTPUTS = [("c_out", 144), ("done", 1), ("busy", 1)]

#: The netlist has no clock net for the simulator to drive: it steps registers.
IGNORED_INPUTS = ["clk"]
#: Active-low asynchronous reset.
ASYNC_CLEAR_INPUT = "rst_n"

#: The ring: R = Z_q[x] / (x^n + 1).
Q = 257
N = 16
LOGN = 4
#: psi has order 2n = 32 modulo 257, so psi^16 = -1 and omega = psi^2 is a
#: primitive 16th root of unity.
PSI = 15
OMEGA = PSI * PSI % Q

PHASE_NTT = 0
PHASE_POINT = 1
PHASE_BREV = 2
PHASE_INTT = 3
PHASE_POST = 4


def bitreverse(value, bits=LOGN):
    """The bit-reversal permutation on *bits* bits."""
    result = 0
    for position in range(bits):
        if value & (1 << position):
            result |= 1 << (bits - 1 - position)
    return result


def butterfly_addresses(step):
    """``(stage, group, j, j + len)`` for butterfly *step* of a transform.

    Stage ``s`` = ``step >> 3`` has ``2**s`` groups of ``8 >> s`` butterflies,
    and the pair it touches is ``m`` with a zero inserted at bit ``3 - s`` and
    the same index with a one there.
    """
    stage = step >> 3
    position = step & 7
    shift = 3 - stage
    group = position >> shift
    low = position & ((1 << shift) - 1)
    first = (group << (shift + 1)) | low
    return stage, group, first, first | (1 << shift)


def _forward_twiddles():
    """psi^brv(2^s + g): the merged negacyclic Cooley-Tukey constants."""
    return tuple(
        pow(PSI, bitreverse((1 << stage) + group), Q)
        for stage, group, _, _ in (butterfly_addresses(step) for step in range(32))
    )


def _inverse_twiddles():
    """omega^-brv3(g): the plain cyclic Cooley-Tukey constants, inverted."""
    return tuple(
        pow(OMEGA, -bitreverse(group, LOGN - 1), Q)
        for _, group, _, _ in (butterfly_addresses(step) for step in range(32))
    )


def _post_twiddles():
    """n^-1 * psi^-brv(j): the post-twist, folded together with the scaling."""
    inverse = pow(PSI, -1, Q)
    scale = pow(N, -1, Q)
    return tuple(scale * pow(inverse, bitreverse(index), Q) % Q for index in range(N))


TWIDDLE_FORWARD = _forward_twiddles()
TWIDDLE_INVERSE = _inverse_twiddles()
TWIDDLE_POST = _post_twiddles()


def schoolbook(first, second):
    """``a(x) * b(x) mod (x^16 + 1, 257)``, straight from the definition.

    Nothing in the datapath computes this; it is what the datapath is *for*,
    and ``check.py`` uses it as the independent statement of the answer.
    """
    result = [0] * N
    for i in range(N):
        for j in range(N):
            total = i + j
            if total < N:
                result[total] = (result[total] + first[i] * second[j]) % Q
            else:
                result[total - N] = (result[total - N] - first[i] * second[j]) % Q
    return result


def negacyclic_multiply(first, second):
    """The product, through the same five phases the hardware runs."""
    state = initial_state()
    state["pa"] = list(first)
    state["pb"] = list(second)
    state["run"] = 1
    for _ in range(144):
        state = _step(state)
    return list(state["pb"])


# ---------------------------------------------------------------------------
# the cycle-accurate model
# ---------------------------------------------------------------------------


def _phase_last(phase, count):
    """The terminal count of *phase*, as the three kept compare terms find it."""
    equal15 = (count & 0xF) == 0xF
    equal31 = equal15 and bool(count & 0x10)
    equal63 = equal31 and bool(count & 0x20)
    if phase == PHASE_NTT:
        return equal63
    if phase == PHASE_INTT:
        return equal31
    return equal15


def _datapath(state):
    """One cycle of the shared butterfly, as ``(sum, difference, writes)``."""
    phase = state["ph"]
    count = state["cnt"]
    butterfly = phase in (PHASE_NTT, PHASE_INTT)
    index = count & 0xF
    _, _, first, second = butterfly_addresses(count & 0x1F)

    read_a = first if butterfly else index
    read_b = second if butterfly else index
    bank = (count >> 5) & 1

    from_b_u = phase == PHASE_NTT and bank
    from_b_v = bank if phase == PHASE_NTT else phase in (PHASE_POINT, PHASE_BREV)
    to_b = bank if phase == PHASE_NTT else phase in (PHASE_POINT, PHASE_POST)

    upper = 0
    if butterfly:
        upper = state["pb"][read_a] if from_b_u else state["pa"][read_a]
    lower = state["pb"][read_b] if from_b_v else state["pa"][read_b]

    if phase == PHASE_POINT:
        twiddle = state["pa"][read_a]
    elif butterfly:
        twiddle = (
            TWIDDLE_INVERSE[count & 0x1F]
            if phase == PHASE_INTT
            else TWIDDLE_FORWARD[count & 0x1F]
        )
    elif phase == PHASE_POST:
        twiddle = TWIDDLE_POST[index]
    else:
        twiddle = 1

    product = twiddle * lower % Q
    total = (upper + product) % Q
    difference = (upper - product) % Q

    if butterfly:
        write_sum = first
    elif phase in (PHASE_BREV, PHASE_POST):
        write_sum = bitreverse(index)
    else:
        write_sum = index
    return total, difference, write_sum, second, to_b, butterfly


def initial_state():
    return {
        "pa": [0] * N,
        "pb": [0] * N,
        "cnt": 0,
        "ph": PHASE_NTT,
        "run": 0,
        "fin": 0,
    }


def outputs(state, values):
    word = 0
    for index in range(N):
        word |= (state["pb"][index] & 0x1FF) << (9 * index)
    return {"c_out": word, "done": state["fin"], "busy": state["run"]}


def _step(state):
    total, difference, write_sum, write_difference, to_b, butterfly = _datapath(state)
    nxt = dict(state)
    nxt["pa"] = list(state["pa"])
    nxt["pb"] = list(state["pb"])
    bank = nxt["pb"] if to_b else nxt["pa"]
    bank[write_sum] = total
    if butterfly:
        bank[write_difference] = difference

    last = _phase_last(state["ph"], state["cnt"])
    nxt["cnt"] = 0 if last else (state["cnt"] + 1) & 0x3F
    if last:
        nxt["ph"] = (state["ph"] + 1) & 7
        if state["ph"] == PHASE_POST:
            nxt["run"] = 0
            nxt["fin"] = 1
    return nxt


def next_state(state, values):
    # The clear is asynchronous and dominates.
    if not values["rst_n"]:
        return initial_state()

    if values["start"] and not state["run"]:
        return {
            "pa": [(values["a_in"] >> (8 * i)) & 0xFF for i in range(N)],
            "pb": [(values["b_in"] >> (8 * i)) & 0xFF for i in range(N)],
            "cnt": 0,
            "ph": PHASE_NTT,
            "run": 1,
            "fin": 0,
        }

    if not state["run"]:
        # Every register's clock enable is low.
        return state
    return _step(state)
