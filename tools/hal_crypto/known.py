"""The built-in library of published cryptographic constants.

Everything here is a *published* table or a formula from a published
specification, together with a note naming where it comes from.  Two rules keep
the library honest:

1. Nothing is added because it "looks like" it could occur -- an entry exists
   only if a named algorithm defines it.
2. Entries that can be *derived* are derived (the AES S-box from the GF(2^8)
   inverse and its affine map, the chi maps from their algebraic definition,
   the PRESENT/GIFT bit permutations from their index formulas) rather than
   copied as opaque digits, so a transcription slip cannot hide in the table.
   The literal tables that cannot be derived (PRESENT, GIFT, SKINNY, Ascon, the
   DES rows) are checked for bijectivity by the unit tests.

Matching against this library says *the extracted structure equals a published
one*.  It does not say the design is that algorithm: the same S-box appears in
more than one construction, and an S-box on its own is not a cipher.
"""

from .boolfunc import Sbox

__all__ = [
    "SBOXES",
    "PERMUTATIONS",
    "ROTATION_SETS",
    "NTT_MODULI",
    "sbox_entries",
    "permutation_entries",
    "rotation_families",
    "modulus_candidates",
]


# ---------------------------------------------------------------------------
# helpers that derive rather than transcribe
# ---------------------------------------------------------------------------


def _chi(bits):
    """The Keccak/Ascon chi map on *bits* lanes: ``y_i = x_i ^ (~x_{i+1} & x_{i+2})``.

    Defined in FIPS 202 section 3.2.4 for a row of five lanes; the same formula
    is used at other widths in the sponge literature.  It is a bijection for
    odd *bits*.
    """
    size = 1 << bits
    table = []
    for x in range(size):
        y = 0
        for position in range(bits):
            a = (x >> position) & 1
            b = (x >> ((position + 1) % bits)) & 1
            c = (x >> ((position + 2) % bits)) & 1
            y |= (a ^ ((1 - b) & c)) << position
        table.append(y)
    return Sbox(bits, table)


def _gf256_inverse_table():
    """Multiplicative inverses in GF(2^8) modulo ``x^8 + x^4 + x^3 + x + 1``."""
    exponent = [0] * 256
    logarithm = [0] * 256
    value = 1
    for power in range(255):
        exponent[power] = value
        logarithm[value] = power
        # multiply by the generator 3 = x + 1
        value ^= (value << 1) ^ (0x11B if value & 0x80 else 0)
        value &= 0xFF
    inverse = [0] * 256
    for element in range(1, 256):
        inverse[element] = exponent[(255 - logarithm[element]) % 255]
    return inverse


def _aes_sbox():
    """The AES S-box (FIPS 197 section 5.1.1), derived, not transcribed."""
    inverse = _gf256_inverse_table()
    table = []
    for element in range(256):
        b = inverse[element]
        result = 0
        for bit in range(8):
            value = (
                ((b >> bit) & 1)
                ^ ((b >> ((bit + 4) % 8)) & 1)
                ^ ((b >> ((bit + 5) % 8)) & 1)
                ^ ((b >> ((bit + 6) % 8)) & 1)
                ^ ((b >> ((bit + 7) % 8)) & 1)
                ^ ((0x63 >> bit) & 1)
            )
            result |= value << bit
        table.append(result)
    return Sbox(8, table)


def _present_player():
    """PRESENT's bit permutation: ``P(i) = 16 * i mod 63`` with ``P(63) = 63``."""
    return tuple(63 if index == 63 else (16 * index) % 63 for index in range(64))


def _gift64_player():
    """GIFT-64's bit permutation, from the index formula in the GIFT paper."""
    permutation = []
    for index in range(64):
        quotient, remainder = divmod(index, 16)
        low = remainder % 4
        mid = remainder // 4
        permutation.append(4 * quotient + 16 * ((3 * mid + low) % 4) + low)
    return tuple(permutation)


#: Keccak-f rho rotation offsets, indexed ``[x][y]`` (FIPS 202 table 2).
_KECCAK_RHO = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)


# ---------------------------------------------------------------------------
# literal tables
# ---------------------------------------------------------------------------

_PRESENT_SBOX = (
    0xC, 0x5, 0x6, 0xB, 0x9, 0x0, 0xA, 0xD,
    0x3, 0xE, 0xF, 0x8, 0x4, 0x7, 0x1, 0x2,
)

_GIFT_SBOX = (
    0x1, 0xA, 0x4, 0xC, 0x6, 0xF, 0x3, 0x9,
    0x2, 0xD, 0xB, 0x7, 0x5, 0x0, 0x8, 0xE,
)

_SKINNY64_SBOX = (
    0xC, 0x6, 0x9, 0x0, 0x1, 0xA, 0x2, 0xB,
    0x3, 0x8, 0x5, 0xD, 0x4, 0xE, 0x7, 0xF,
)

_ASCON_SBOX = (
    0x04, 0x0B, 0x1F, 0x14, 0x1A, 0x15, 0x09, 0x02,
    0x1B, 0x05, 0x08, 0x12, 0x1D, 0x03, 0x06, 0x1C,
    0x1E, 0x13, 0x07, 0x0E, 0x00, 0x0D, 0x11, 0x18,
    0x10, 0x0C, 0x01, 0x19, 0x16, 0x0A, 0x0F, 0x17,
)

#: The eight DES S-boxes (FIPS 46-3), each as its four 4-bit-to-4-bit rows.
_DES_SBOXES = {
    "S1": (
        (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7),
        (0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8),
        (4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0),
        (15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    ),
    "S2": (
        (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10),
        (3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5),
        (0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15),
        (13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    ),
    "S3": (
        (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8),
        (13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1),
        (13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7),
        (1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    ),
    "S4": (
        (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15),
        (13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9),
        (10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4),
        (3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    ),
    "S5": (
        (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9),
        (14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6),
        (4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14),
        (11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    ),
    "S6": (
        (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11),
        (10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8),
        (9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6),
        (4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    ),
    "S7": (
        (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1),
        (13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6),
        (1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2),
        (6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    ),
    "S8": (
        (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7),
        (1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2),
        (7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8),
        (2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
    ),
}


def _build_sboxes():
    entries = {}

    def add(name, sbox, family, note):
        entries[name] = {"sbox": sbox, "family": family, "reference": note}

    add(
        "present",
        Sbox(4, _PRESENT_SBOX),
        "spn",
        "PRESENT (Bogdanov et al., CHES 2007), table 1",
    )
    add(
        "gift",
        Sbox(4, _GIFT_SBOX),
        "spn",
        "GIFT (Banik et al., CHES 2017), GS box",
    )
    add(
        "skinny64",
        Sbox(4, _SKINNY64_SBOX),
        "spn",
        "SKINNY-64 4-bit S-box (Beierle et al., CRYPTO 2016)",
    )
    add(
        "aes",
        _aes_sbox(),
        "spn",
        "AES S-box, derived from the GF(2^8) inverse and the FIPS 197 affine map",
    )
    add(
        "ascon",
        Sbox(5, _ASCON_SBOX),
        "sponge",
        "Ascon 5-bit S-box (NIST SP 800-232 / Ascon v1.2)",
    )
    add(
        "keccak_chi_5",
        _chi(5),
        "sponge",
        "Keccak chi over a five-lane row (FIPS 202 section 3.2.4)",
    )
    add(
        "keccak_chi_3",
        _chi(3),
        "sponge",
        "Keccak chi at width 3 (FIPS 202 section 3.2.4, three-lane row)",
    )
    for name in sorted(_DES_SBOXES):
        for row_index, row in enumerate(_DES_SBOXES[name]):
            add(
                "des_{}_row{}".format(name.lower(), row_index),
                Sbox(4, row),
                "feistel",
                "DES {} row {} (FIPS 46-3 appendix 1)".format(name, row_index),
            )
    return entries


def _build_permutations():
    entries = {}
    entries["present_player"] = {
        "width": 64,
        "permutation": _present_player(),
        "family": "spn",
        "reference": "PRESENT pLayer, P(i) = 16i mod 63 (CHES 2007)",
    }
    entries["gift64_player"] = {
        "width": 64,
        "permutation": _gift64_player(),
        "family": "spn",
        "reference": "GIFT-64 bit permutation (CHES 2017)",
    }
    return entries


#: Named sets of fixed rotation amounts that identify an ARX round function.
#: A match means "the rotation amounts found in the wiring are exactly this
#: published set", which is evidence about the wiring, not an identification of
#: the cipher.
_ROTATION_SETS = {
    "chacha": {
        "amounts": (16, 12, 8, 7),
        "word_bits": 32,
        "family": "arx",
        "reference": "ChaCha quarter-round rotations (Bernstein, 2008)",
    },
    "salsa20": {
        "amounts": (7, 9, 13, 18),
        "word_bits": 32,
        "family": "arx",
        "reference": "Salsa20 quarter-round rotations (Bernstein, 2005)",
    },
    "speck_64": {
        "amounts": (8, 3),
        "word_bits": 32,
        "family": "arx",
        "reference": "SPECK round rotations alpha=8, beta=3 (Beaulieu et al., 2013)",
    },
    "speck_32": {
        "amounts": (7, 2),
        "word_bits": 16,
        "family": "arx",
        "reference": "SPECK-32/64 round rotations alpha=7, beta=2 (Beaulieu et al., 2013)",
    },
    "blake2s": {
        "amounts": (16, 12, 8, 7),
        "word_bits": 32,
        "family": "arx",
        "reference": "BLAKE2s G-function rotations (Aumasson et al., 2013)",
    },
    "keccak_rho": {
        "amounts": tuple(sorted({offset for row in _KECCAK_RHO for offset in row})),
        "word_bits": 64,
        "family": "sponge",
        "reference": "Keccak-f[1600] rho offsets (FIPS 202 table 2)",
    },
}


#: Moduli that identify a lattice/ring arithmetic unit when they show up as the
#: constant of a conditional subtraction.  Reported as *candidates*: a design
#: can reduce modulo 3329 without being Kyber.
_NTT_MODULI = {
    3329: "ML-KEM / Kyber ring modulus q = 3329 (FIPS 203)",
    8380417: "ML-DSA / Dilithium modulus q = 8380417 (FIPS 204)",
    12289: "Falcon / NTRU-family modulus q = 12289",
    7681: "Kyber round-1 modulus q = 7681",
    4591: "NTRU Prime modulus q = 4591",
    2048: "power-of-two ring modulus q = 2048 (NTRU-family)",
    3457: "NewHope-style modulus q = 3457",
}


SBOXES = _build_sboxes()
PERMUTATIONS = _build_permutations()
ROTATION_SETS = _ROTATION_SETS
NTT_MODULI = _NTT_MODULI


def sbox_entries(bits=None):
    """Library S-boxes, optionally restricted to one width."""
    return [
        (name, entry)
        for name, entry in sorted(SBOXES.items())
        if bits is None or entry["sbox"].bits == bits
    ]


def permutation_entries(width=None):
    return [
        (name, entry)
        for name, entry in sorted(PERMUTATIONS.items())
        if width is None or entry["width"] == width
    ]


def rotation_families(amounts, word_bits=None):
    """Named rotation sets whose amounts are all present in *amounts*.

    The test is containment, not equality: a netlist round may expose only part
    of a quarter-round, and reporting "these published amounts all occur" is a
    weaker and more defensible statement than "this is that cipher".
    """
    found = set(int(amount) for amount in amounts)
    matches = []
    for name, entry in sorted(ROTATION_SETS.items()):
        if word_bits is not None and entry["word_bits"] != word_bits:
            continue
        if set(entry["amounts"]) <= found:
            matches.append((name, entry))
    return matches


def modulus_candidates(constants):
    """Library moduli present among *constants*."""
    return [
        (value, NTT_MODULI[value])
        for value in sorted({int(constant) for constant in constants})
        if value in NTT_MODULI
    ]
