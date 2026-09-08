"""Semantics of the Agilex 3 ``tennm_*`` primitives that this package covers.

Coverage is deliberately small and every entry is backed by evidence that lives
in the repository:

``tennm_lcell_comb``
    The Agilex ALM combinational cell.  Its ``lut_mask`` decoding (normal mode
    and arithmetic mode) is stated below, and every claim is cross-checked
    against the ``// Equation(s):`` comments that ``quartus_eda`` itself writes
    next to each instance of the shipped fixtures, plus an exhaustive
    netlist-versus-RTL simulation (see ``simulate.py`` and the fixture
    ``GROUND_TRUTH.md`` files).

``tennm_ff``
    The Agilex register.  Only the *pin configuration that the fixtures
    exercise* is modelled: synchronous data capture under ``ena`` and an
    asynchronous, active-low ``clrn``.  Any instance that drives ``sclr``,
    ``sclr1``, ``sload`` or ``aload`` with something other than a constant 0,
    or that ties ``devclrn``/``devpor`` low, is **out of coverage** and is
    reported as such -- it is never silently given the modelled behaviour.

Everything else Quartus can emit (``tennm_ram_block``, ``tennm_mac``,
``tennm_iopll``, transceiver and encrypted blocks, ...) is explicitly *not*
covered.  :data:`UNCOVERED_PRIMITIVE_REASONS` names the ones seen in the
shipped fixtures; anything unknown is reported as unknown rather than guessed.
"""

__all__ = [
    "FAMILY",
    "DEVICE",
    "QUARTUS_VERSION",
    "LCELL",
    "FF",
    "COVERED_PRIMITIVES",
    "UNCOVERED_PRIMITIVE_REASONS",
    "LCELL_DATA_PINS",
    "LCELL_OUTPUT_PINS",
    "FF_MUST_BE_ZERO_PINS",
    "FF_MUST_BE_ONE_PINS",
    "UnsupportedConfiguration",
    "lut_mask_bit",
    "combout",
    "arithmetic_outputs",
    "check_lcell_configuration",
    "absorb_input_inversions",
    "ff_next_state",
    "check_ff_configuration",
    "arithmetic_halves",
    "classify_arithmetic_cell",
]

#: The family/device/tool the shipped fixtures were produced for.  Nothing in
#: this package is claimed to hold for any other family: the ``tennm_`` atom
#: names, the pin lists and the mask decoding are all family specific.
FAMILY = "Agilex 3"
DEVICE = "A3CW135BM16AE6S"
QUARTUS_VERSION = "26.1.0 Build 110 03/26/2026 SC Pro Edition"

LCELL = "tennm_lcell_comb"
FF = "tennm_ff"

#: Primitive types this package models.  A netlist that contains anything else
#: is reported as partially uncovered; it is never imported as if it were
#: understood.
COVERED_PRIMITIVES = (LCELL, FF)

#: Reasons for the uncovered primitives the shipped fixtures actually contain.
#: The keys are the exact atom names ``quartus_eda`` emits.
UNCOVERED_PRIMITIVE_REASONS = {
    "tennm_ram_block": (
        "embedded memory block: behaviour depends on init data, port width and "
        "mixed-port read-during-write settings that are not modelled here"
    ),
    "tennm_mac": (
        "DSP/MAC block: arithmetic mode, pipelining and pre-adder configuration "
        "are not modelled here"
    ),
    "tennm_iopll": "PLL: clock generation is outside the scope of this package",
    "tennm_hssi_pma": "transceiver block: outside the scope of this package",
    "tennm_io_ibuf": "I/O buffer: outside the scope of this package",
    "tennm_io_obuf": "I/O buffer: outside the scope of this package",
}

#: The six ALM data inputs that address the LUT mask, least significant first.
LCELL_DATA_PINS = ("dataa", "datab", "datac", "datad", "datae", "dataf")
#: Inputs of the fracturable 7-input mode; this package requires them tied off.
LCELL_EXTENDED_PINS = ("datag", "datah")
LCELL_INPUT_PINS = LCELL_DATA_PINS + LCELL_EXTENDED_PINS + ("cin", "sharein")
LCELL_OUTPUT_PINS = ("combout", "sumout", "cout", "shareout")

FF_INPUT_PINS = (
    "clk",
    "d",
    "asdata",
    "clrn",
    "aload",
    "sclr",
    "sload",
    "ena",
    "sclr1",
    "devclrn",
    "devpor",
)
FF_OUTPUT_PINS = ("q",)

#: Pins that must be constant 0 for the modelled ``tennm_ff`` behaviour to apply.
FF_MUST_BE_ZERO_PINS = ("aload", "sload", "sclr", "sclr1")
#: Pins that must be constant 1 for the modelled ``tennm_ff`` behaviour to apply.
FF_MUST_BE_ONE_PINS = ("devclrn", "devpor")


class UnsupportedConfiguration(Exception):
    """A primitive instance is outside the validated coverage.

    Raised instead of returning a plausible-looking value, so that a caller can
    only ever get semantics this package actually stands behind.
    """


# ---------------------------------------------------------------------------
# tennm_lcell_comb
# ---------------------------------------------------------------------------
#
# Normal mode (extended_lut="off", shared_arith="off"):
#
#     combout = lut_mask[ a + 2*b + 4*c + 8*d + 16*e + 32*f ]
#
# with a..f the values on dataa..dataf.  Arithmetic mode uses the same mask as
# two 4-input halves and the dedicated carry input:
#
#     f0 = lut_mask[      a + 2*b + 4*c + 8*d ]      (mask bits  0..15)
#     f1 = lut_mask[ 16 + a + 2*b + 4*c + 8*d ]      (mask bits 16..31)
#
#     sumout = f0 XOR cin
#     cout   = (f0 AND cin) OR (NOT f0 AND f1)
#
# which is exactly the propagate/generate form Quartus prints next to every
# arithmetic instance of ``fixtures/agilex3_counter_adder``:
#
#     sumout = ( !count ^ (!addend) ) ^ (cin)
#     cout   = ( !count ^ (!addend) ) & cin | !( !count ^ (!addend) ) & (count & addend)
#
# ``datae``/``dataf`` select between the two halves, so they must be tied off
# for the arithmetic outputs to mean this; the checks below enforce that.


def lut_mask_bit(mask, index):
    """Bit *index* of a 64-bit ``lut_mask``."""
    if index < 0 or index > 63:
        raise ValueError("lut_mask index out of range: {}".format(index))
    return (mask >> index) & 1


def _address(values, pins):
    address = 0
    for position, pin in enumerate(pins):
        bit = values.get(pin, 0)
        if bit not in (0, 1):
            raise ValueError("pin {} has non-binary value {!r}".format(pin, bit))
        address |= bit << position
    return address


def combout(mask, values):
    """Normal-mode LUT output for the given ``dataa``..``dataf`` values."""
    return lut_mask_bit(mask, _address(values, LCELL_DATA_PINS))


def arithmetic_halves(mask, values):
    """``(f0, f1)``: the propagate and generate halves for ``dataa``..``datad``."""
    address = _address(values, LCELL_DATA_PINS[:4])
    return lut_mask_bit(mask, address), lut_mask_bit(mask, 16 + address)


def arithmetic_outputs(mask, values, cin):
    """``(sumout, cout)`` of an arithmetic-mode ALM cell."""
    f0, f1 = arithmetic_halves(mask, values)
    sumout = f0 ^ cin
    cout = (f0 & cin) | ((1 - f0) & f1)
    return sumout, cout


def check_lcell_configuration(
    parameters, driven_pins, uses_arithmetic_outputs, constant_inputs=None
):
    """Validate one ``tennm_lcell_comb`` instance against the modelled subset.

    ``parameters`` maps parameter name to value, ``driven_pins`` is the set of
    input pins connected to something other than a constant, and
    ``constant_inputs`` maps input pin -> constant value where known.

    Raises :class:`UnsupportedConfiguration` with a precise reason; returns
    ``None`` when the instance is inside the coverage.
    """
    constant_inputs = constant_inputs or {}

    extended = str(parameters.get("extended_lut", "off")).lower()
    if extended != "off":
        raise UnsupportedConfiguration(
            "extended_lut={!r}: the fracturable 7-input mode is not modelled".format(extended)
        )

    shared = str(parameters.get("shared_arith", "off")).lower()
    if shared != "off":
        raise UnsupportedConfiguration(
            "shared_arith={!r}: the shared arithmetic mode (sharein/shareout) is "
            "not modelled".format(shared)
        )

    for pin in LCELL_EXTENDED_PINS:
        if pin in driven_pins:
            raise UnsupportedConfiguration(
                "{} is driven by a signal: the 7-input mode is not modelled".format(pin)
            )
        if constant_inputs.get(pin, 0) != 0:
            raise UnsupportedConfiguration("{} is tied to 1, which is not modelled".format(pin))

    if "sharein" in driven_pins or constant_inputs.get("sharein", 0) != 0:
        raise UnsupportedConfiguration(
            "sharein carries a signal: the shared arithmetic mode is not modelled"
        )

    if not uses_arithmetic_outputs:
        return None

    mask = parameters.get("lut_mask")
    if mask is None:
        raise UnsupportedConfiguration("instance has no lut_mask parameter")
    if mask >> 32:
        raise UnsupportedConfiguration(
            "lut_mask has bits set above bit 31 while sumout/cout are used; the "
            "arithmetic decoding validated here only covers masks whose upper "
            "half is unused (lut_mask=0x{:016x})".format(mask)
        )
    for pin in ("datae", "dataf"):
        if pin in driven_pins:
            raise UnsupportedConfiguration(
                "{} is driven while sumout/cout are used; in arithmetic mode it "
                "selects between the two mask halves and must be tied off".format(pin)
            )
        if constant_inputs.get(pin, 0) != 0:
            raise UnsupportedConfiguration(
                "{} is tied to 1 while sumout/cout are used".format(pin)
            )
    return None


def absorb_input_inversions(mask, inverted_pins):
    """Return the ``lut_mask`` of the same function with inverted data inputs.

    ``quartus_eda`` writes ``.datac(~foo)`` instead of instantiating an
    inverter.  HAL's Verilog parser has no notion of an inverted port
    connection, so the import rewrites the mask instead: inverting data input
    *i* maps mask bit *j* to mask bit ``j ^ (1 << i)``, which is an exact
    relabelling of the truth table, not an approximation.

    ``inverted_pins`` may only name ``dataa``..``dataf``; the caller is
    responsible for refusing inversions on ``cin``/``sharein``, which do not
    address the mask.
    """
    positions = []
    for pin in inverted_pins:
        if pin not in LCELL_DATA_PINS:
            raise UnsupportedConfiguration(
                "cannot absorb an inversion on pin {!r}: only {} address the LUT "
                "mask".format(pin, ", ".join(LCELL_DATA_PINS))
            )
        positions.append(LCELL_DATA_PINS.index(pin))

    if not positions:
        return mask

    flip = 0
    for position in positions:
        flip |= 1 << position

    result = 0
    for index in range(64):
        if (mask >> index) & 1:
            result |= 1 << (index ^ flip)
    return result


def classify_arithmetic_cell(mask):
    """Describe an arithmetic-mode mask structurally, or return ``None``.

    Two shapes are recognised, both of which occur in the shipped counter/adder
    export:

    ``full_adder_slice``
        ``f0`` is the XOR of exactly two data inputs and ``f1`` is their AND,
        with both operands in the same polarity -- the propagate/generate pair
        of one bit of a ripple-carry adder.  ``operand_polarity`` is ``true``
        when ``f1 = p & q`` and ``inverted`` when ``f1 = !p & !q``, which is
        what Quartus emits when it pushes the inversions of ``.datac(~x)`` into
        the connection instead of the mask.

    ``carry_tap``
        Both halves are constant 0, so ``sumout = cin`` and ``cout = 0``: the
        cell only forwards the carry out of the chain.

    This is an observation about the mask alone.  It says what the cell
    computes, never what the surrounding circuit means.
    """
    if mask >> 32:
        return None

    low = mask & 0xFFFF
    high = (mask >> 16) & 0xFFFF
    if low == 0 and high == 0:
        return {"kind": "carry_tap"}

    used = []
    for position in range(4):
        for index in range(16):
            if ((index >> position) & 1) == 0:
                if lut_mask_bit(mask, index) != lut_mask_bit(mask, index | (1 << position)):
                    used.append(LCELL_DATA_PINS[position])
                    break
    if len(used) != 2:
        return None

    first, second = (LCELL_DATA_PINS.index(pin) for pin in used)
    for index in range(16):
        p = (index >> first) & 1
        q = (index >> second) & 1
        if lut_mask_bit(mask, index) != (p ^ q):
            return None

    for polarity, name in ((0, "true"), (1, "inverted")):
        for index in range(16):
            p = ((index >> first) & 1) ^ polarity
            q = ((index >> second) & 1) ^ polarity
            if lut_mask_bit(mask, 16 + index) != (p & q):
                break
        else:
            return {
                "kind": "full_adder_slice",
                "operands": tuple(used),
                "operand_polarity": name,
                "propagate": "xor",
                "generate": "and",
            }
    return None


# ---------------------------------------------------------------------------
# tennm_ff
# ---------------------------------------------------------------------------


def check_ff_configuration(driven_pins, constant_inputs):
    """Validate one ``tennm_ff`` instance against the modelled subset."""
    for pin in FF_MUST_BE_ZERO_PINS:
        if pin in driven_pins:
            raise UnsupportedConfiguration(
                "{} is driven by a signal; only the configuration with {} tied to 0 "
                "has been validated against the vendor export".format(
                    pin, ", ".join(FF_MUST_BE_ZERO_PINS)
                )
            )
        if constant_inputs.get(pin, 0) != 0:
            raise UnsupportedConfiguration("{} is tied to 1, which is not modelled".format(pin))

    for pin in FF_MUST_BE_ONE_PINS:
        if pin in driven_pins:
            raise UnsupportedConfiguration(
                "{} is driven by a design signal; the modelled behaviour assumes the "
                "global {} is released (constant 1)".format(pin, pin)
            )
        if constant_inputs.get(pin, 1) != 1:
            raise UnsupportedConfiguration("{} is tied to 0, which is not modelled".format(pin))

    if "clk" not in driven_pins:
        raise UnsupportedConfiguration("clk is not driven by a signal")
    return None


def ff_next_state(current, d, ena):
    """Next state of the modelled ``tennm_ff`` on a rising clock edge."""
    return d if ena else current
