"""Carry chains, read as arithmetic rather than as a mask pattern.

``hal_agilex.recognize`` already recognises the Agilex ripple adder, and this
module reuses its chain walk verbatim (``cout`` -> ``cin`` links between
``tennm_lcell_comb`` cells).  What it adds is a classification that survives
the two shapes a cipher datapath produces and a plain counter does not:

* an operand tied to a **constant**, which is how a modular reduction subtracts
  *q*.  A mask-pattern match rejects those cells because the mask no longer
  looks like a propagate/generate pair; reading the cell's *effective* two mask
  halves, after the constant pins have been folded away, does not;
* an operand that arrives **through an inverter**, which together with a
  carry-in of one is how ``a - b`` is built out of an adder -- the other half of
  an NTT butterfly.  Buffers and inverters between the operand register and the
  adder are peeled, so the operand names the register, not the copy.

Nothing here is trusted on the strength of the pattern.  A recognized chain is
*evaluated*: its sum bits are compared against ``a + b + cin`` on the effective
operand values, exhaustively when the operand space is small enough and on a
fixed, seeded sample otherwise, and the answer carries which of the two it was.
The arithmetic label (``add``, ``subtract``, ``add_constant``) is attached only
after that check passes.
"""

import random

from hal_agilex import primitives
from hal_agilex.recognize import carry_chains

from .netlist_model import UnsupportedCell

__all__ = [
    "DEFAULT_SAMPLES",
    "EXHAUSTIVE_VARIABLE_LIMIT",
    "chains",
    "classify_chain",
    "check_chain_function",
    "adders",
]

#: Operand vectors drawn when the operand space is too large to enumerate.
#: Seeded per chain, so a run is reproducible and a regression is repeatable.
DEFAULT_SAMPLES = 256

#: Up to this many distinct operand *bits*, the check enumerates instead of
#: sampling and says so.
EXHAUSTIVE_VARIABLE_LIMIT = 12


def chains(model):
    """Ordered ``cout`` -> ``cin`` chains, from ``hal_agilex.recognize``."""
    return carry_chains(model.netlist)


def _slice_operands(model, cell):
    """``(operands, constant)`` for one arithmetic cell, or ``None``.

    ``operands`` is a list of ``(net key, inverted)`` after buffers and
    inverters have been peeled; ``constant`` is the value of the folded-away
    second operand when only one net is left.
    """
    try:
        keys, low, high = model._arithmetic_tables(cell)  # noqa: SLF001
    except UnsupportedCell:
        return None

    def peeled(key, polarity):
        source, inverted = model.peel(key)
        return source, bool(inverted) ^ bool(polarity)

    if len(keys) == 2:
        for polarity in (0, 1):
            for index in range(4):
                first = (index >> 0) & 1
                second = (index >> 1) & 1
                if low[index] != (first ^ second):
                    break
                if high[index] != ((first ^ polarity) & (second ^ polarity)):
                    break
            else:
                return [peeled(keys[0], polarity), peeled(keys[1], polarity)], None
        return None
    if len(keys) == 1:
        # a + 0: f0 = a, f1 = 0.   a + 1: f0 = !a, f1 = a.
        if low == [0, 1] and high == [0, 0]:
            return [peeled(keys[0], 0)], 0
        if low == [1, 0] and high == [0, 1]:
            return [peeled(keys[0], 0)], 1
        # the same two with the net arriving inverted
        if low == [1, 0] and high == [0, 0]:
            return [peeled(keys[0], 1)], 0
        if low == [0, 1] and high == [1, 0]:
            return [peeled(keys[0], 1)], 1
        return None
    return None


def classify_chain(model, chain):
    """Describe one carry chain as an adder over two operand vectors.

    Returns ``None`` when the chain is not shaped like an adder at all.  A
    trailing cell that only forwards the carry is dropped, the way Quartus
    writes the top of a chain.
    """
    left = []
    right = []
    sums = []
    for position, cell in enumerate(chain):
        classified = _slice_operands(model, cell)
        sum_bit = cell.single("sumout")
        if classified is None or sum_bit is None:
            if position == len(chain) - 1:
                break  # trailing carry tap
            return None
        operands, constant = classified
        if len(operands) == 2:
            first, second = sorted(operands, key=lambda entry: (entry[1], entry[0]))
            left.append(first)
            right.append(second)
        elif len(operands) == 1 and constant is not None:
            left.append(operands[0])
            right.append(("const", constant))
        else:
            return None
        sums.append(sum_bit.key)
    if len(sums) < 2:
        return None

    carry = chain[0].single("cin")
    if carry is None:
        carry_in = 0
    else:
        resolved = model.resolve(carry)
        carry_in = resolved[1] if resolved[0] == "const" else None

    return {
        "cells": [cell.name for cell in chain],
        "width": len(sums),
        "left": left,
        "right": right,
        "sums": sums,
        "carry_in": carry_in,
    }


def _operand_sources(operand):
    """The net keys of an operand vector, or ``None`` if every bit is constant."""
    keys = [entry[0] for entry in operand if entry[0] != "const"]
    return keys or None


def _constant_value(operand):
    """The operand's value when every bit of it is a constant, else ``None``."""
    value = 0
    for position, entry in enumerate(operand):
        if entry[0] != "const":
            return None
        value |= entry[1] << position
    return value


def check_chain_function(model, info, samples=DEFAULT_SAMPLES, seed=0):
    """Evaluate a classified chain against ``a + b + cin`` and label it.

    ``operation`` is ``None`` -- with a ``reason`` -- when the evaluation
    disagrees or when the chain cannot be driven, which is a real answer: a
    carry chain that is not addition is not an adder.
    """
    width = info["width"]
    left_keys = _operand_sources(info["left"])
    right_keys = _operand_sources(info["right"])
    constant = _constant_value(info["right"])
    if left_keys is None:
        return {"operation": None, "reason": "the first operand is entirely constant"}
    if info["carry_in"] is None:
        return {"operation": None, "reason": "the carry into the chain is not a constant"}

    variables = sorted(set(left_keys) | set(right_keys or []))
    exhaustive = len(variables) <= EXHAUSTIVE_VARIABLE_LIMIT
    if exhaustive:
        vectors = [
            {name: (index >> position) & 1 for position, name in enumerate(variables)}
            for index in range(1 << len(variables))
        ]
    else:
        generator = random.Random(seed)
        vectors = [
            {name: generator.randint(0, 1) for name in variables} for _ in range(samples)
        ]

    source_values = model.evaluate_samples(variables, vectors)
    sum_values = model.evaluate_samples(info["sums"], vectors)

    def word(operand, index):
        """The value an operand vector presents to the chain, mixed bits and all."""
        value = 0
        for position, entry in enumerate(operand):
            if entry[0] == "const":
                bit = entry[1]
            else:
                bit = source_values[entry[0]][index] ^ (1 if entry[1] else 0)
            value |= bit << position
        return value

    modulus = 1 << width
    for index in range(len(vectors)):
        got = 0
        for position, key in enumerate(info["sums"]):
            got |= sum_values[key][index] << position
        a = word(info["left"], index)
        b = word(info["right"], index)
        expected = (a + b + info["carry_in"]) % modulus
        if got != expected:
            return {
                "operation": None,
                "checked": index + 1,
                "exhaustive": exhaustive,
                "reason": (
                    "the chain does not add: on operand vector {} it produced {} "
                    "where {} was expected".format(index, got, expected)
                ),
            }

    left_inverted = all(entry[0] != "const" and entry[1] for entry in info["left"])
    right_inverted = right_keys is not None and all(
        entry[0] != "const" and entry[1] for entry in info["right"]
    )
    if right_keys is None:
        operation = "add_constant"
    elif right_inverted and not left_inverted and info["carry_in"] == 1:
        operation = "subtract"
    else:
        operation = "add"

    result = {
        "operation": operation,
        "checked": len(vectors),
        "exhaustive": exhaustive,
        "width": width,
        "left_sources": left_keys,
        "right_sources": right_keys,
        "constant": constant if right_keys is None else None,
        "carry_in": info["carry_in"],
        "left_inverted": left_inverted,
        "right_inverted": right_inverted,
    }
    if operation == "add_constant":
        result["addend"] = (constant + info["carry_in"]) % modulus
        result["subtrahend"] = (modulus - result["addend"]) % modulus
    return result


def adders(model, samples=DEFAULT_SAMPLES):
    """Every carry chain that really computes an addition, with its operands."""
    results = []
    for index, chain in enumerate(chains(model)):
        info = classify_chain(model, chain)
        if info is None:
            continue
        checked = check_chain_function(model, info, samples=samples, seed=index)
        if checked.get("operation") is None:
            continue
        entry = {
            "cells": info["cells"],
            "sums": info["sums"],
        }
        entry.update(checked)
        results.append(entry)
    return results


#: Re-exported so callers do not have to import hal_agilex for the type name.
LCELL_TYPE = primitives.LCELL
