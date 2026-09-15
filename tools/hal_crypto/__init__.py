"""Structural crypto identification for Agilex 3 netlists.

Six passes read a Quartus ``.vo`` export -- through the reader and the
primitive semantics that ``tools/hal_agilex`` already validates -- and answer
one question each:

``sbox``
    which LUT cones form an *n*-bit bijection, and does it equal a published
    S-box (exactly, up to an XOR constant, or up to a bit permutation)?
``shiftreg``
    which registers form a shift chain, is it closed by feedback, is that
    feedback linear (a polynomial) or not (an ANF), Fibonacci or Galois?
``arx``
    are adders, fixed rotations and an XOR layer present *and wired together*?
``permutation``
    which pure-wire bit maps exist, and do they equal a published pLayer or a
    published rotation set?
``ntt``
    is there an add/subtract butterfly over the same operands, and what
    modulus does the constant-operand chain reduce by?
``classify``
    all of the above, aggregated into one family verdict
    (``spn`` / ``arx`` / ``lfsr-stream`` / ``sponge`` / ``lattice-ntt`` /
    ``none-detected``) and one classical-vs-PQC-style verdict.

Everything except :mod:`hal_crypto.hal_adapter` runs on a plain CPython 3
interpreter with the standard library only, so the whole test suite runs
without a built HAL and without the vendor tool.

The wording discipline is the same everywhere: findings state what the netlist
is *wired to compute*.  Matching a published table is a fact about the
extracted function; it is never by itself an identification of an algorithm,
and ``none-detected`` is a result rather than a failure.
"""

__all__ = [
    "arith",
    "arx",
    "boolfunc",
    "classify",
    "findings",
    "known",
    "netlist_model",
    "ntt",
    "permutation",
    "sbox",
    "shiftreg",
]
