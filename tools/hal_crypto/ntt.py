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
``reduction modulus``
    the same *q*, read out of the **correction logic** instead, for the designs
    where the constant chain does not exist.  A conditional subtract of *q* is
    only a carry chain when *q* is expensive to add: at a Fermat-prime modulus
    like ``257 = 2**8 + 1`` the correction is an increment plus one bit flip,
    and Quartus builds it out of ordinary LUTs, leaving no constant operand
    anywhere.  The modulus is still in the netlist -- as the *function* the
    correction computes.  This tier finds the net that decides whether the
    correction fires and builds *q* one bit at a time, keeping a prefix only
    while every bit of ``select ? a + b - q : a + b`` so far exists as a net
    of the netlist -- and, where the difference is corrected too, the same
    *q* added back on ``a - b``.  It is a derivation out of the reduction
    logic, not a pattern match;
``stage depth``
    how many butterflies are chained through registers.  One butterfly is an
    add/sub pair; a transform is a log-depth stack of them.

The verdict deliberately stops short of naming a scheme.  ``lattice-style``
means *butterflies over a small modulus are present*; "this is ML-KEM" is not a
structural claim and this pass never makes it -- a design can reduce modulo
3329 for reasons that have nothing to do with Kyber, and Kyber-sized hardware
can use a modulus the library has never heard of.
"""

import random

from . import arith, known
from .netlist_model import ConeTooWide, UnsupportedCell

__all__ = [
    "REDUCTION_SAMPLES",
    "butterflies",
    "modulus_candidates",
    "reduction_moduli",
    "identify",
]

#: Operand vectors drawn when reading a modulus out of the correction logic.
#: Every candidate *q* has to reproduce ``width`` whole net functions on all of
#: them, so this is the cost of a false positive, not of a miss.
REDUCTION_SAMPLES = 256

#: A select net has to be true on at least this many of the drawn vectors and
#: false on at least this many before it is offered as a correction select;
#: a net that is almost constant decides almost nothing.
REDUCTION_MINIMUM_SPLIT = 8

#: A netlist that keeps more modulus prefixes than this alive at one bit is
#: not being pinned down by the search; it is abandoned rather than ground
#: through, because a derivation that admits thousands of answers is not one.
REDUCTION_MAXIMUM_LIVE = 8192


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


def _operand_word(operand, values, index):
    """The value one operand vector presents to its chain on sample *index*."""
    word = 0
    for position, entry in enumerate(operand):
        if entry[0] == "const":
            bit = entry[1]
        else:
            bit = values[entry[0]][index] ^ (1 if entry[1] else 0)
        word |= bit << position
    return word


def _chain_table(model):
    """Every classified carry chain, keyed by its cell names."""
    table = {}
    for chain in arith.chains(model):
        info = arith.classify_chain(model, chain)
        if info is not None:
            table[tuple(info["cells"])] = info
    return table


def _reduction_candidates(model, cut):
    """Every combinational net that is a function of *cut* and nothing else.

    These are exactly the nets the correction logic can be built from: the
    chain's own sums, the corrected vector, and whatever sits between.  ``cout``
    counts -- "the sum reached *q*" is most cheaply written as the carry out of
    ``sum + (2**w - q)``, and that net is the select, not a data bit.
    """
    nets = []
    seen = set()
    for instance in model.lcells:
        for pin in ("combout", "sumout", "cout"):
            for bit in instance.connections.get(pin) or ():
                key = getattr(bit, "key", None)
                if key is None or key in cut or key in seen:
                    continue
                seen.add(key)
                try:
                    support = model.cut_support(key, cut)
                except (UnsupportedCell, ConeTooWide):
                    continue
                if support and support <= cut:
                    nets.append(key)
    return sorted(nets)


def _signature(values):
    word = 0
    for index, value in enumerate(values):
        if value:
            word |= 1 << index
    return word


def _predicates(candidates, values, samples):
    """Every net and its complement, as a sample bit-mask, minus the constants.

    Which way round the correction's select sits is a synthesiser's choice --
    ``sum >= q`` and the borrow out of ``sum - q`` are the same decision with
    opposite polarity -- so both orientations are offered and the search says
    which one it used.
    """
    all_ones = (1 << samples) - 1
    seen = set()
    result = []
    for key in candidates:
        signature = _signature(values[key])
        if signature in (0, all_ones):
            continue
        ones = bin(signature).count("1")
        if min(ones, samples - ones) < REDUCTION_MINIMUM_SPLIT:
            continue
        for inverted in (False, True):
            mask = signature ^ all_ones if inverted else signature
            if mask in seen:
                continue
            seen.add(mask)
            result.append((key, inverted, mask))
    return result


def _search_moduli(signatures, results, predicates, width, samples, sign):
    """Every ``(modulus, predicate)`` the corrected vector is consistent with.

    The correction computes ``result + sign * q`` on the vectors its predicate
    selects and ``result`` on the rest, so bit *k* of it is a function of bits
    0..*k* of *q* alone -- a borrow only ever travels up.  The modulus is
    therefore built one bit at a time, and a prefix no net in the netlist can
    complete is dropped at that bit rather than carried to the end.  What comes
    back is a *derivation*: every bit of the vector was found, or the modulus
    was not.
    """
    all_ones = (1 << samples) - 1
    plain = [
        _signature([(value >> position) & 1 for value in results])
        for position in range(width)
    ]
    cache = {}

    def corrected(prefix, position):
        key = (prefix, position)
        got = cache.get(key)
        if got is None:
            got = _signature(
                [((value + sign * prefix) >> position) & 1 for value in results]
            )
            cache[key] = got
        return got

    live = [(0, entry, []) for entry in predicates]
    for position in range(width):
        following = []
        for prefix, entry, nets in live:
            for bit in (0, 1):
                modulus = prefix | (bit << position)
                selected = entry[2]
                signature = (plain[position] & (all_ones ^ selected)) | (
                    corrected(modulus, position) & selected
                )
                if signature in signatures:
                    following.append((modulus, entry, nets + [signatures[signature]]))
        live = following
        if not live or len(live) > REDUCTION_MAXIMUM_LIVE:
            return []
    return [
        (modulus, entry, nets)
        for modulus, entry, nets in live
        if modulus >= 2
    ]


def _sweep_vectors(info, width):
    """One operand split per result the chain can produce, in order.

    A random draw finds the modulus quickly and then cannot rule out its
    near neighbours: two candidates differ only on the handful of sums either
    side of the threshold, and those sums are exactly the ones a uniform draw
    over operand *bits* almost never produces.  So the survivors are re-checked
    on a sweep of every attainable chain result, one operand split each --
    the variable the correction actually reads, end to end.

    ``None`` when the chain's two operands are not independent vectors of nets
    (a constant slice, or a vector added to itself), which is when a result
    cannot be dialled in.
    """
    left, right = info["left"], info["right"]
    if any(entry[0] == "const" for entry in left + right):
        return None
    if {entry[0] for entry in left} & {entry[0] for entry in right}:
        return None
    mask = (1 << width) - 1
    vectors = []
    for total in range(2 * mask + 1):
        first = min(total, mask)
        second = total - first
        assignment = {}
        for position, entry in enumerate(left):
            assignment[entry[0]] = ((first >> position) & 1) ^ (1 if entry[1] else 0)
        for position, entry in enumerate(right):
            assignment[entry[0]] = ((second >> position) & 1) ^ (1 if entry[1] else 0)
        vectors.append(assignment)
    return vectors


def _sweep(model, info, width, candidates):
    """The sweep, evaluated once: results, net values and a signature index."""
    vectors = _sweep_vectors(info, width)
    if vectors is None:
        return None
    try:
        values = model.evaluate_samples(candidates, vectors)
    except (UnsupportedCell, ConeTooWide):
        return None
    mask = (1 << width) - 1
    results = []
    for index in range(len(vectors)):
        first = min(index, mask)
        results.append(first + (index - first) + info["carry_in"])
    signatures = {}
    for key in candidates:
        signatures.setdefault(_signature(values[key]), key)
    return {
        "count": len(vectors),
        "values": values,
        "results": results,
        "signatures": signatures,
    }


def _locate_on_sweep(sweep, width, modulus, select, sign):
    """The corrected vector, located again over every attainable result.

    The random draw's job was to *propose* a modulus and a select net; this
    redoes the location from scratch on the sweep, so a net that merely looked
    right on 256 draws -- and two nets that differ only on the sums a uniform
    draw never produces are exactly what goes wrong here -- is not carried
    forward.  ``None`` means the vector is not in the netlist.
    """
    key, inverted = select
    if key not in sweep["values"]:
        return None
    fired = sweep["values"][key]
    targets = [
        result + sign * modulus * (value ^ (1 if inverted else 0))
        for result, value in zip(sweep["results"], fired)
    ]
    nets = []
    for position in range(width):
        signature = _signature([(target >> position) & 1 for target in targets])
        name = sweep["signatures"].get(signature)
        if name is None:
            return None
        nets.append(name)
    return nets


def reduction_moduli(model, pairs, samples=REDUCTION_SAMPLES, seed=0):
    """Moduli read off the conditional correction that follows a butterfly.

    The constant-operand tier only sees a modulus that was expensive enough to
    need a carry chain of its own.  This one asks the question the other way
    round: the butterfly's sum and difference are known exactly on every drawn
    operand vector, so once a candidate *q* and a candidate select net are
    fixed, the corrected vector is known too -- and either the netlist contains
    it, bit for bit, or it does not.
    """
    table = _chain_table(model)
    results = []
    for index, pair in enumerate(pairs):
        addition = table.get(tuple(pair["sum_cells"]))
        subtraction = table.get(tuple(pair["difference_cells"]))
        if addition is None:
            continue
        width = addition["width"]
        cut = frozenset(pair["operands"])
        variables = sorted(cut)
        generator = random.Random(seed + index)
        vectors = [
            {name: generator.randint(0, 1) for name in variables}
            for _ in range(samples)
        ]
        try:
            source = model.evaluate_samples(variables, vectors)
            candidates = _reduction_candidates(model, cut)
            values = model.evaluate_samples(candidates, vectors)
        except (UnsupportedCell, ConeTooWide):
            continue

        signatures = {}
        for key in candidates:
            signatures.setdefault(_signature(values[key]), key)
        predicates = _predicates(candidates, values, samples)

        sums = [
            _operand_word(addition["left"], source, sample)
            + _operand_word(addition["right"], source, sample)
            + addition["carry_in"]
            for sample in range(samples)
        ]
        found = {}
        sum_sweep = None
        for modulus, entry, nets in _search_moduli(
            signatures, sums, predicates, width, samples, -1
        ):
            # A reduction makes its argument *smaller*.  Without this the
            # search also returns the tautology "subtract 2**w when the carry
            # out is set", which is not a modulus, it is what a w-bit vector
            # does on its own.
            if any(
                (entry[2] >> sample) & 1 and sums[sample] < modulus
                for sample in range(samples)
            ):
                continue
            if modulus in found:
                continue
            if sum_sweep is None:
                sum_sweep = _sweep(model, addition, width, candidates)
            if not sum_sweep:
                break
            located = _locate_on_sweep(
                sum_sweep, width, modulus, (entry[0], entry[1]), -1
            )
            if located is None:
                continue
            found[modulus] = {
                "modulus": modulus,
                "width": width,
                "butterfly": index,
                "select_net": entry[0],
                "select_inverted": entry[1],
                "sum_nets": located,
                "difference_nets": None,
                "difference_select_net": None,
                "difference_select_inverted": None,
                "vectors_checked": sum_sweep["count"],
                "checked_every_sum": True,
                "library_moduli": [
                    {"value": value, "reference": reference}
                    for value, reference in known.modulus_candidates([modulus])
                ],
            }

        if subtraction is not None and subtraction["width"] == width and found:
            differences = [
                _operand_word(subtraction["left"], source, sample)
                + _operand_word(subtraction["right"], source, sample)
                + subtraction["carry_in"]
                for sample in range(samples)
            ]
            difference_sweep = _sweep(model, subtraction, width, candidates)
            for modulus, entry, nets in _search_moduli(
                signatures, differences, predicates, width, samples, +1
            ):
                record = found.get(modulus)
                if record is None or record["difference_nets"] is not None:
                    continue
                if not difference_sweep:
                    break
                located = _locate_on_sweep(
                    difference_sweep, width, modulus, (entry[0], entry[1]), +1
                )
                if located is None:
                    continue
                record["difference_nets"] = located
                record["difference_select_net"] = entry[0]
                record["difference_select_inverted"] = entry[1]

        results.extend(found[modulus] for modulus in sorted(found))
    return results


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
    reductions = reduction_moduli(model, pairs) if pairs else []
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
            | {
                item["value"]
                for entry in reductions
                for item in entry["library_moduli"]
            }
        ),
        "stage_depth": _stage_depth(model, pairs),
    }
    # Only reported when the tier fired: a pass that found nothing this way
    # says so through `missing`, and an empty list in every document that has
    # no butterfly at all would be noise.
    if reductions:
        result["reduction_moduli"] = reductions
        result["recovered_moduli"] = sorted({entry["modulus"] for entry in reductions})
    missing = []
    if not pairs:
        missing.append("no add/subtract pair over the same operand registers")
    if not moduli and not reductions:
        if pairs:
            missing.append(
                "no carry chain with a constant operand and no conditional "
                "correction over the butterfly, so no modulus to read"
            )
        else:
            missing.append(
                "no carry chain with a constant operand, so no modulus to read"
            )
    if missing:
        result["verdict"] = "no-modular-transform"
        result["missing"] = missing
        return result
    result["verdict"] = (
        "lattice-style-modular-transform"
        if result["named_moduli"]
        else "modular-arithmetic-candidate"
    )
    return result
