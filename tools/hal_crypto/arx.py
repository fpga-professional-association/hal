"""Add-Rotate-XOR round structure.

ARX is not one recognisable gate pattern; it is the *co-occurrence* of three
things that individually mean nothing:

* a **carry-chain adder** over two data words -- which every counter also has;
* a **fixed rotation** in the wiring between register banks -- which every
  barrel shifter also has;
* an **XOR layer**, a run of combinational cells each of which is a pure XOR of
  two or more signals -- which every parity tree also has.

So the pass reports an ARX candidate only when all three are present *and* they
are wired to each other: the XOR layer has to read the adder's sums, or the
rotation has to read one of the adder's operands.  A design with an adder and
nothing else -- the accumulator in ``fixtures/counter8.vo``, and the counter in
``examples/agilex3_walkthroughs/01_blinky_counter`` -- comes back ``not-arx``
with the missing ingredients named, which is the whole point of the pass.

The rotation amounts are matched against published quarter-round constants
(ChaCha, Salsa20, SPECK, BLAKE2s).  A hit means "the rotation amounts in this
wiring are exactly that published set"; it is not an identification of the
cipher, and the finding says so.
"""

from . import arith, known, permutation
from .boolfunc import TruthTable
from .netlist_model import ConeTooWide, UnsupportedCell

__all__ = [
    "MIN_ARX_WIDTH",
    "MIN_XOR_LAYER",
    "xor_nets",
    "identify",
]

#: Adders, rotations and XOR layers narrower than this are not a cipher word.
MIN_ARX_WIDTH = 4

#: How many XOR cells make a *layer*.  One XOR is a parity bit.
MIN_XOR_LAYER = 4


def xor_nets(model):
    """ALM cells that are a pure XOR of two or more of their own inputs.

    The test is deliberately *local*: the cell's own LUT function, not the cone
    behind it.  An XOR layer sitting on top of an adder has a cone that reaches
    back through the whole carry chain and is not an XOR of anything, while the
    cell itself plainly is one -- and it is the cell that makes the layer.
    """
    results = []
    for instance in model.lcells:
        bits = instance.connections.get("combout")
        if not bits:
            continue
        key = getattr(bits[0], "key", None)
        if key is None:
            continue
        try:
            keys, table, _, _, _ = model._lut_inputs(instance)  # noqa: SLF001
        except (UnsupportedCell, ConeTooWide):
            continue
        if len(keys) < 2:
            continue
        function = TruthTable(keys, table).restricted()
        if function.arity < 2:
            continue
        affine = function.linear_terms()
        if affine is None or len(affine[1]) != function.arity:
            continue
        results.append(
            {
                "net": key,
                "cell": instance.name,
                "inputs": list(function.inputs),
                "arity": function.arity,
                "complemented": bool(affine[0]),
            }
        )
    return results


def _base(key):
    if key.endswith("]") and "[" in key:
        return key[: key.index("[")]
    return key


def identify(model, adders=None, permutations=None):
    """Look for ARX rounds; always return the evidence, positive or not."""
    adders = arith.adders(model) if adders is None else adders
    permutations = (
        permutation.find_permutations(model) if permutations is None else permutations
    )

    wide_adders = [
        entry
        for entry in adders
        if entry["width"] >= MIN_ARX_WIDTH and entry["operation"] in ("add", "subtract")
    ]
    rotations = [
        entry
        for entry in permutations
        if entry.get("kind") == "rotation" and entry["width"] >= MIN_ARX_WIDTH
    ]
    xors = xor_nets(model)

    result = {
        "adders": [
            {
                "operation": entry["operation"],
                "width": entry["width"],
                "cells": entry["cells"],
                "left": entry["left_sources"],
                "right": entry["right_sources"],
                "checked": entry["checked"],
                "exhaustive": entry["exhaustive"],
            }
            for entry in wide_adders
        ],
        "rotations": [
            {
                "destination": entry["destination"],
                "source": entry["source"],
                "width": entry["width"],
                "rotation": entry["rotation"],
                "rotate_left_by": entry["rotate_left_by"],
                "all_inverted": entry["all_inverted"],
            }
            for entry in rotations
        ],
        "xor_cells": len(xors),
        "xor_layer_nets": [entry["net"] for entry in xors][:64],
    }

    missing = []
    if not wide_adders:
        missing.append("no carry-chain adder of at least {} bits".format(MIN_ARX_WIDTH))
    if not rotations:
        missing.append("no fixed rotation in the wiring between banks")
    if len(xors) < MIN_XOR_LAYER:
        missing.append(
            "only {} pure-XOR cell(s); a layer starts at {}".format(len(xors), MIN_XOR_LAYER)
        )

    sum_nets = set()
    operand_vectors = set()
    for entry in wide_adders:
        sum_nets.update(entry["sums"])
        for key in (entry["left_sources"] or []) + (entry["right_sources"] or []):
            operand_vectors.add(_base(key))

    xor_reads_sum = any(
        any(model.peel(name)[0] in sum_nets or name in sum_nets for name in entry["inputs"])
        for entry in xors
    )
    rotation_on_operand = any(
        entry["source"] in operand_vectors
        or _base(entry["destination"]) in operand_vectors
        for entry in rotations
    )
    result["xor_layer_reads_adder_output"] = xor_reads_sum
    result["rotation_on_adder_operand"] = rotation_on_operand
    if not missing and not (xor_reads_sum or rotation_on_operand):
        missing.append(
            "the adder, the rotation and the XOR layer are present but not wired to "
            "each other, so they are not composed into a round"
        )

    if missing:
        result["verdict"] = "not-arx"
        result["missing"] = missing
        return result

    amounts = set()
    widths = set()
    for entry in rotations:
        amounts.add(entry["rotate_left_by"])
        amounts.add(entry["rotation"])
        widths.add(entry["width"])
    families = []
    for width in sorted(widths):
        for name, entry in known.rotation_families(amounts, word_bits=width):
            families.append(
                {
                    "name": name,
                    "amounts": list(entry["amounts"]),
                    "word_bits": entry["word_bits"],
                    "family": entry["family"],
                    "reference": entry["reference"],
                }
            )
    result["verdict"] = "arx-candidate"
    result["rotation_families"] = families
    result["word_bits"] = sorted(widths)
    return result
