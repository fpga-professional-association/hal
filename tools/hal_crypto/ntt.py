"""Modular arithmetic and NTT butterflies.

The number-theoretic transform is what makes a lattice scheme fast, and in
hardware it is unmistakable at the structural level: the same two operands feed
one adder and one subtracter, and the two results are reduced modulo a small
prime before they are registered.  This pass looks for exactly that, in three
independent pieces:

``butterfly``
    an ``a + b`` chain and an ``a - b`` chain over the *same* operand
    registers.  Both are verified arithmetically by :mod:`hal_crypto.arith`
    before they are paired, so "butterfly" here means the two chains really do
    compute the sum and the difference, not that the wiring looks like it;
``modulus candidate``
    a carry chain one of whose operands is entirely constant.  A subtraction of
    *q* is an addition of ``2**w - q``, so the constant is reported both ways
    and the value is looked up in :data:`hal_crypto.known.NTT_MODULI`;
``stage depth``
    how many butterflies are chained through registers.  One butterfly is an
    add/sub pair; a transform is a log-depth stack of them.

The verdict deliberately stops short of naming a scheme.  ``lattice-style``
means *butterflies over a small modulus are present*; "this is ML-KEM" is not a
structural claim and this pass never makes it -- a design can reduce modulo
3329 for reasons that have nothing to do with Kyber, and Kyber-sized hardware
can use a modulus the library has never heard of.
"""

from . import arith, known

__all__ = [
    "butterflies",
    "modulus_candidates",
    "identify",
]


def _operand_set(entry):
    keys = list(entry["left_sources"] or [])
    keys.extend(entry["right_sources"] or [])
    return frozenset(keys)


def butterflies(adders):
    """Pairs of verified chains that compute ``a + b`` and ``a - b``."""
    additions = [entry for entry in adders if entry["operation"] == "add"]
    subtractions = [entry for entry in adders if entry["operation"] == "subtract"]
    pairs = []
    used = set()
    for addition in additions:
        operands = _operand_set(addition)
        if not operands:
            continue
        for index, subtraction in enumerate(subtractions):
            if index in used:
                continue
            if _operand_set(subtraction) != operands:
                continue
            used.add(index)
            pairs.append(
                {
                    "operands": sorted(operands),
                    "width": addition["width"],
                    "sum_nets": addition["sums"],
                    "difference_nets": subtraction["sums"],
                    "sum_cells": addition["cells"],
                    "difference_cells": subtraction["cells"],
                    "verified_exhaustively": bool(
                        addition["exhaustive"] and subtraction["exhaustive"]
                    ),
                    "vectors_checked": min(addition["checked"], subtraction["checked"]),
                }
            )
            break
    return pairs


def modulus_candidates(adders):
    """Constant-operand chains, read as ``+k`` and as ``-(2**w - k)``."""
    candidates = []
    for entry in adders:
        if entry["operation"] != "add_constant":
            continue
        width = entry["width"]
        addend = entry.get("addend", 0)
        subtrahend = entry.get("subtrahend", 0)
        named = dict(known.modulus_candidates([addend, subtrahend]))
        candidates.append(
            {
                "cells": entry["cells"],
                "width": width,
                "addend": addend,
                "equivalent_subtrahend": subtrahend,
                "operand": entry["left_sources"],
                "library_moduli": [
                    {"value": value, "reference": reference}
                    for value, reference in sorted(named.items())
                ],
            }
        )
    return candidates


def _stage_depth(model, pairs):
    """How many butterflies are chained through registers, longest run."""
    if not pairs:
        return 0
    produced = {}
    for index, pair in enumerate(pairs):
        for net in list(pair["sum_nets"]) + list(pair["difference_nets"]):
            produced[net] = index
    consumed = {}
    for index, pair in enumerate(pairs):
        sources = set()
        for key in pair["operands"]:
            register = model.register_of(key)
            if register is None:
                continue
            data = register.connections.get("d")
            if not data:
                continue
            resolved = model.peel_bit(data[0])
            if resolved[0] == "net":
                sources.add(resolved[1])
        consumed[index] = {produced[net] for net in sources if net in produced}

    depth = {}

    def walk(index, seen):
        if index in depth:
            return depth[index]
        if index in seen:
            return 1
        best = 1
        for parent in consumed.get(index, ()):
            if parent == index:
                continue
            best = max(best, 1 + walk(parent, seen | {index}))
        depth[index] = best
        return best

    return max(walk(index, set()) for index in range(len(pairs)))


def identify(model, adders=None):
    """The modular-arithmetic pass; the evidence is returned either way."""
    adders = arith.adders(model) if adders is None else adders
    pairs = butterflies(adders)
    moduli = modulus_candidates(adders)
    named = [
        entry for entry in moduli if entry["library_moduli"]
    ]
    result = {
        "butterflies": pairs,
        "butterfly_count": len(pairs),
        "modulus_candidates": moduli,
        "named_moduli": sorted(
            {
                item["value"]
                for entry in named
                for item in entry["library_moduli"]
            }
        ),
        "stage_depth": _stage_depth(model, pairs),
    }
    missing = []
    if not pairs:
        missing.append("no add/subtract pair over the same operand registers")
    if not moduli:
        missing.append("no carry chain with a constant operand, so no modulus to read")
    if missing:
        result["verdict"] = "no-modular-transform"
        result["missing"] = missing
        return result
    result["verdict"] = (
        "lattice-style-modular-transform" if named else "modular-arithmetic-candidate"
    )
    return result
