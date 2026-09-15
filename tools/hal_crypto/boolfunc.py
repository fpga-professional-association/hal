"""Boolean functions, truth tables and S-box algebra -- standard library only.

Everything the recognition passes need to say something exact about a piece of
combinational logic lives here, and nothing here knows about netlists.  Two
representations are used:

``TruthTable``
    One Boolean output over *n* ordered inputs, held as a tuple of ``2**n``
    bits.  It can report its *functional* support (the inputs it actually
    depends on, which is often smaller than the syntactic fan-in of the cone it
    came from), its algebraic normal form, and whether it is affine over GF(2).

``Sbox``
    An *n*-bit to *n*-bit map, held as a tuple of ``2**n`` integers.  It knows
    whether it is a bijection, what its per-output-bit ANF is, and how it
    compares to another S-box under the three equivalences the crypto
    identification uses.

The three match tiers are deliberately kept apart, because they are not the
same claim:

``exact``
    the tables are equal bit for bit, in the bit order the extraction chose;

``xor_constant``
    ``S(x) = R(x ^ a) ^ b`` for constants *a*, *b* -- an affine offset, which is
    what a key/round-constant XOR folded into the LUTs produces;

``bit_permutation``
    ``S = Q . R . P`` for input and output *bit* permutations *P*, *Q* -- which
    is what an arbitrary choice of which netlist bit is "bit 0" produces.

A caller that reports a ``bit_permutation`` hit as if it were an ``exact`` one
is making a claim the evidence does not support, so the tier travels with every
match and the finding text always names it.
"""

import itertools

__all__ = [
    "MAX_SBOX_BITS",
    "PERMUTATION_SEARCH_MAX_BITS",
    "TruthTable",
    "Sbox",
    "MatchResult",
    "anf_terms",
    "anf_string",
    "polynomial_string",
    "polynomial_exponents",
    "polynomial_from_exponents",
    "reciprocal_exponents",
    "match_sbox",
]

#: Widths the S-box machinery accepts.  Eight bits is 256 entries, which every
#: operation below handles comfortably; wider maps are refused rather than
#: attempted, because the equivalence searches stop being affordable.
MAX_SBOX_BITS = 8

#: Bit-permutation equivalence is searched by enumerating ``n!`` input
#: permutations and deriving the output permutation, so the cost is
#: ``n! * 2**n``.  Six bits (46k steps) is affordable; seven (645k) and eight
#: (10M) are not, and are reported as *not searched* instead of being silently
#: treated as "no match".
PERMUTATION_SEARCH_MAX_BITS = 6


# ---------------------------------------------------------------------------
# single-output functions
# ---------------------------------------------------------------------------


class TruthTable(object):
    """One Boolean output over ``inputs``, as ``2**len(inputs)`` bits.

    ``values[j]`` is the output when input *i* takes the value ``(j >> i) & 1``,
    i.e. the first name in ``inputs`` is the least significant address bit.
    """

    __slots__ = ("inputs", "values", "_anf")

    def __init__(self, inputs, values):
        inputs = tuple(inputs)
        values = tuple(int(value) & 1 for value in values)
        if len(values) != 1 << len(inputs):
            raise ValueError(
                "a function of {} inputs needs {} values, got {}".format(
                    len(inputs), 1 << len(inputs), len(values)
                )
            )
        self.inputs = inputs
        self.values = values
        self._anf = None

    # -- basics -----------------------------------------------------------

    @property
    def arity(self):
        return len(self.inputs)

    def __eq__(self, other):
        return (
            isinstance(other, TruthTable)
            and self.inputs == other.inputs
            and self.values == other.values
        )

    def __hash__(self):
        return hash((self.inputs, self.values))

    def __repr__(self):
        return "TruthTable({}, 0x{:x})".format(list(self.inputs), self.as_int())

    def as_int(self):
        """The table packed into one integer, ``values[j]`` at bit *j*."""
        packed = 0
        for index, value in enumerate(self.values):
            packed |= value << index
        return packed

    def evaluate(self, assignment):
        """Evaluate for ``assignment``: input name -> 0/1."""
        address = 0
        for position, name in enumerate(self.inputs):
            address |= (int(assignment[name]) & 1) << position
        return self.values[address]

    # -- support ----------------------------------------------------------

    def depends_on(self, position):
        """True when the output changes with input *position*."""
        step = 1 << position
        for index in range(len(self.values)):
            if index & step:
                continue
            if self.values[index] != self.values[index | step]:
                return True
        return False

    def support(self):
        """The inputs the function actually depends on, in input order."""
        return tuple(
            name for position, name in enumerate(self.inputs) if self.depends_on(position)
        )

    def restricted(self):
        """The same function over its functional support only."""
        keep = [position for position in range(self.arity) if self.depends_on(position)]
        if len(keep) == self.arity:
            return self
        inputs = tuple(self.inputs[position] for position in keep)
        values = []
        for index in range(1 << len(keep)):
            address = 0
            for new_position, old_position in enumerate(keep):
                address |= ((index >> new_position) & 1) << old_position
            values.append(self.values[address])
        return TruthTable(inputs, values)

    def is_constant(self):
        return len(set(self.values)) == 1

    # -- algebra ----------------------------------------------------------

    def anf(self):
        """Algebraic normal form as a sorted list of monomials.

        Each monomial is a tuple of input names; the empty tuple is the
        constant term.  Computed with the Moebius transform, so the result is
        exact, not a fit -- and cached, because the affinity test, the degree
        and the printed form all want it and a wide table costs real time.
        """
        if self._anf is not None:
            return self._anf
        coefficients = list(self.values)
        step = 1
        while step < len(coefficients):
            for start in range(0, len(coefficients), step << 1):
                for offset in range(start, start + step):
                    coefficients[offset + step] ^= coefficients[offset]
            step <<= 1
        terms = []
        for index, coefficient in enumerate(coefficients):
            if not coefficient:
                continue
            terms.append(
                tuple(
                    self.inputs[position]
                    for position in range(self.arity)
                    if (index >> position) & 1
                )
            )
        terms.sort(key=lambda term: (len(term), term))
        self._anf = terms
        return terms

    def algebraic_degree(self):
        terms = self.anf()
        return max((len(term) for term in terms), default=0)

    def is_affine(self):
        """True when the function is ``c ^ x_i1 ^ ... ^ x_ik`` over GF(2)."""
        return self.algebraic_degree() <= 1

    def linear_terms(self):
        """``(constant, [input names])`` for an affine function, else ``None``."""
        if not self.is_affine():
            return None
        constant = 0
        names = []
        for term in self.anf():
            if term:
                names.append(term[0])
            else:
                constant = 1
        return constant, names

    def is_xor_of_all_inputs(self):
        """True when the function is the (possibly negated) XOR of every input."""
        affine = self.linear_terms()
        if affine is None:
            return False
        return set(affine[1]) == set(self.support())


def anf_terms(table):
    """``TruthTable.anf`` as a list, for callers that only have the object."""
    return table.anf()


def anf_string(table):
    """Human-readable ANF, e.g. ``x0 ^ x1 ^ (x2 & x3) ^ 1``."""
    terms = table.anf()
    if not terms:
        return "0"
    pieces = []
    for term in terms:
        if not term:
            pieces.append("1")
        elif len(term) == 1:
            pieces.append(term[0])
        else:
            pieces.append("(" + " & ".join(term) + ")")
    return " ^ ".join(pieces)


def polynomial_from_exponents(exponents):
    """``x^16 + x^15 + x^13 + x^4 + 1`` from ``[16, 15, 13, 4, 0]``."""
    pieces = []
    for exponent in sorted(set(exponents), reverse=True):
        if exponent == 0:
            pieces.append("1")
        elif exponent == 1:
            pieces.append("x")
        else:
            pieces.append("x^{}".format(exponent))
    return " + ".join(pieces) if pieces else "0"


def polynomial_exponents(taps):
    """Exponents of the feedback polynomial: a tap at stage *t* gives ``x^(t+1)``.

    This is the convention the walkthrough specifications use -- stage 0 is the
    chain head, the stage the feedback drives -- and it is *a* convention, not
    the only one.  Numbering the register from the other end gives the
    reciprocal polynomial, which is why :func:`reciprocal_exponents` exists and
    why both are reported.
    """
    return sorted({tap + 1 for tap in taps} | {0}, reverse=True)


def reciprocal_exponents(exponents):
    """The same recurrence with the register numbered from the other end."""
    degree = max(exponents)
    return sorted({degree - exponent for exponent in exponents}, reverse=True)


def polynomial_string(length, taps):
    """The feedback polynomial of a length-*L* LFSR with 0-based ``taps``."""
    return polynomial_from_exponents(polynomial_exponents(taps))


# ---------------------------------------------------------------------------
# n-bit to n-bit maps
# ---------------------------------------------------------------------------


class Sbox(object):
    """An *n*-bit to *n*-bit substitution, as ``2**n`` integers."""

    __slots__ = ("bits", "table")

    def __init__(self, bits, table):
        bits = int(bits)
        table = tuple(int(entry) for entry in table)
        if bits < 1 or bits > MAX_SBOX_BITS:
            raise ValueError("S-box width {} is outside 1..{}".format(bits, MAX_SBOX_BITS))
        if len(table) != 1 << bits:
            raise ValueError(
                "a {}-bit S-box needs {} entries, got {}".format(bits, 1 << bits, len(table))
            )
        if any(entry < 0 or entry >= (1 << bits) for entry in table):
            raise ValueError("an S-box entry is outside the {}-bit range".format(bits))
        self.bits = bits
        self.table = table

    def __eq__(self, other):
        return isinstance(other, Sbox) and self.bits == other.bits and self.table == other.table

    def __hash__(self):
        return hash((self.bits, self.table))

    def __repr__(self):
        return "Sbox({}, {})".format(self.bits, list(self.table))

    def is_bijective(self):
        return len(set(self.table)) == len(self.table)

    def inverse(self):
        if not self.is_bijective():
            raise ValueError("a non-bijective S-box has no inverse")
        table = [0] * len(self.table)
        for index, value in enumerate(self.table):
            table[value] = index
        return Sbox(self.bits, table)

    def coordinate(self, position, names=None):
        """Output bit *position* as a :class:`TruthTable` of the input bits."""
        names = tuple(names) if names else tuple("x{}".format(i) for i in range(self.bits))
        return TruthTable(names, [(entry >> position) & 1 for entry in self.table])

    def coordinates(self, names=None):
        return [self.coordinate(position, names) for position in range(self.bits)]

    def algebraic_degree(self):
        return max(table.algebraic_degree() for table in self.coordinates())

    def is_affine(self):
        """True when every output bit is affine in the input bits."""
        return all(table.is_affine() for table in self.coordinates())

    def differential_uniformity(self):
        """max over non-zero input differences of the largest DDT row entry.

        A permutation with a low value resists differential cryptanalysis; the
        number is reported as a *property of the extracted table*, never as
        evidence that the surrounding design is a cipher.
        """
        size = 1 << self.bits
        best = 0
        for delta in range(1, size):
            counts = {}
            for x in range(size):
                out = self.table[x] ^ self.table[x ^ delta]
                counts[out] = counts.get(out, 0) + 1
            best = max(best, max(counts.values()))
        return best

    # -- transformations used by the equivalence searches -----------------

    def permute_inputs(self, permutation):
        """``T(x) = S(pi(x))`` where bit *i* of x becomes bit ``permutation[i]``."""
        size = 1 << self.bits
        table = [0] * size
        for x in range(size):
            moved = 0
            for source in range(self.bits):
                if (x >> source) & 1:
                    moved |= 1 << permutation[source]
            table[x] = self.table[moved]
        return Sbox(self.bits, table)

    def permute_outputs(self, permutation):
        """``T(x)`` is ``S(x)`` with output bit *i* moved to ``permutation[i]``."""
        size = 1 << self.bits
        table = []
        for x in range(size):
            value = self.table[x]
            moved = 0
            for source in range(self.bits):
                if (value >> source) & 1:
                    moved |= 1 << permutation[source]
            table.append(moved)
        return Sbox(self.bits, table)

    def xor_offsets(self, input_constant, output_constant):
        """``T(x) = S(x ^ a) ^ b``."""
        size = 1 << self.bits
        return Sbox(
            self.bits,
            [self.table[x ^ input_constant] ^ output_constant for x in range(size)],
        )


class MatchResult(object):
    """One S-box library hit, with the tier that produced it."""

    __slots__ = ("name", "tier", "detail", "reference")

    def __init__(self, name, tier, detail=None, reference=None):
        self.name = name
        self.tier = tier
        self.detail = detail or {}
        self.reference = reference

    def as_dict(self):
        entry = {"name": self.name, "tier": self.tier}
        if self.detail:
            entry["detail"] = dict(self.detail)
        if self.reference:
            entry["reference"] = self.reference
        return entry

    def __repr__(self):
        return "MatchResult({!r}, {!r})".format(self.name, self.tier)


def _bit_permutation_of(sbox):
    """If *sbox* moves each input bit to one output bit, return that mapping."""
    bits = sbox.bits
    if sbox.table[0] != 0:
        return None
    images = []
    for position in range(bits):
        image = sbox.table[1 << position]
        if image == 0 or (image & (image - 1)):
            return None
        images.append(image.bit_length() - 1)
    if len(set(images)) != bits:
        return None
    # A bit permutation is linear, so checking the basis is not enough on its
    # own -- verify the whole table before claiming it.
    for x in range(1 << bits):
        moved = 0
        for source in range(bits):
            if (x >> source) & 1:
                moved |= 1 << images[source]
        if sbox.table[x] != moved:
            return None
    return tuple(images)


def _compose(outer, inner):
    """``outer . inner`` as a table-level composition."""
    return Sbox(outer.bits, [outer.table[inner.table[x]] for x in range(1 << inner.bits)])


def match_sbox(candidate, reference, name=None, reference_note=None,
               permutation_budget_bits=PERMUTATION_SEARCH_MAX_BITS):
    """Compare *candidate* against *reference*, returning a :class:`MatchResult`.

    Tiers are tried in increasing generality and the *first* hit is returned, so
    a table that is literally equal is never reported as merely permutation
    equivalent.  ``None`` means no tier matched; when the width exceeds
    ``permutation_budget_bits`` the bit-permutation tier is *not searched*, and
    the caller learns that from :func:`permutation_search_note` rather than
    from a silent negative.
    """
    if candidate.bits != reference.bits:
        return None
    name = name or "reference"

    if candidate.table == reference.table:
        return MatchResult(name, "exact", reference=reference_note)

    # xor-constant: b is forced by a, so this is 2**n checks, not 4**n.
    size = 1 << candidate.bits
    for a in range(size):
        b = candidate.table[0] ^ reference.table[a]
        if all(candidate.table[x] == reference.table[x ^ a] ^ b for x in range(size)):
            return MatchResult(
                name,
                "xor_constant",
                detail={"input_constant": a, "output_constant": b},
                reference=reference_note,
            )

    if candidate.bits > permutation_budget_bits:
        return None
    if not (candidate.is_bijective() and reference.is_bijective()):
        return None

    reference_inverse = reference.inverse()
    for permutation in itertools.permutations(range(candidate.bits)):
        # candidate = Q . reference . P  <=>  Q = candidate . P^-1 . reference^-1
        inverse_p = [0] * candidate.bits
        for source, destination in enumerate(permutation):
            inverse_p[destination] = source
        # permute_inputs(inverse_p) applies P^-1 on the input side.
        rotated = candidate.permute_inputs(tuple(inverse_p))
        q = _compose(rotated, reference_inverse)
        images = _bit_permutation_of(q)
        if images is not None:
            return MatchResult(
                name,
                "bit_permutation",
                detail={
                    "input_permutation": list(permutation),
                    "output_permutation": list(images),
                },
                reference=reference_note,
            )
    return None


def permutation_search_note(bits, permutation_budget_bits=PERMUTATION_SEARCH_MAX_BITS):
    """Why the bit-permutation tier was or was not searched at this width."""
    if bits <= permutation_budget_bits:
        return "bit-permutation equivalence searched exhaustively ({}! input permutations)".format(
            bits
        )
    return (
        "bit-permutation equivalence NOT searched at {} bits: the exhaustive search costs "
        "{}! * 2^{} steps, above the {}-bit budget, so a negative result at this width means "
        "'exact and xor-constant did not match', not 'no equivalence exists'".format(
            bits, bits, bits, permutation_budget_bits
        )
    )
