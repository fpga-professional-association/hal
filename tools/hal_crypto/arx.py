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

## What a real export does to the R and the X

Two of the three ingredients survive synthesis in a shape the plain reading
above does not find, and both were measured on the Quartus export in
``examples/agilex3_walkthroughs/11_speck_toy``:

* **the rotation is not a vector.**  A pure-wire rotation is not a signal the
  synthesiser has any reason to keep, so it is gone from the net list and lives
  only in the order the adder's slices read the register bank.  Those layer
  rotations come from :func:`hal_crypto.permutation.vector_rotation` and
  :func:`hal_crypto.permutation.register_bank_rotations` and are merged in
  here, each carrying where it was read from;
* **the XOR is packed with the multiplexer next to it.**  ``x <= load ? pt :
  (sum ^ k)`` is four inputs and fits in one ALM, so the cell is a
  multiplexer, not an XOR, and every one of the sixteen XORs of the Speck round
  disappears under the plain test.  Holding the select input at the value that
  chooses the round result brings the XOR back exactly -- see the ``cofactor``
  entries :func:`xor_nets` reports.  A conditional XOR is weaker evidence than
  a standalone one and is counted separately.
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


def _pure_xor(function):
    """``(constant, inputs)`` when *function* XORs every one of its inputs."""
    affine = function.linear_terms()
    if affine is None or len(affine[1]) != function.arity:
        return None
    return affine


def _conditional_xor(function):
    """``(position, value, restricted)`` when one held input leaves an XOR.

    Only a *single* held input is tried.  That is enough for the shape this
    exists for -- a two-way multiplexer packed on top of an XOR, where holding
    the select recovers the XOR -- and it keeps the test from degenerating:
    freeze enough inputs and almost any function becomes affine.
    """
    for position in range(function.arity):
        for value in (0, 1):
            restricted = function.cofactor(position, value).restricted()
            if restricted.arity < 2:
                continue
            if _pure_xor(restricted) is not None:
                return position, value, restricted
    return None


def xor_nets(model, conditional=True):
    """ALM cells that are an XOR of two or more of their own inputs.

    The test is deliberately *local*: the cell's own LUT function, not the cone
    behind it.  An XOR layer sitting on top of an adder has a cone that reaches
    back through the whole carry chain and is not an XOR of anything, while the
    cell itself plainly is one -- and it is the cell that makes the layer.

    With *conditional* set, a cell that becomes an XOR once one of its inputs
    is held at a constant counts too, and says so: ``conditional`` is ``True``
    and ``cofactor`` names the input and the value.  That is what a packed
    ``load ? port : (a ^ b)`` looks like from the outside, and refusing to see
    it means refusing to see the XOR layer of any loadable cipher core.
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
        affine = _pure_xor(function)
        if affine is not None:
            results.append(
                {
                    "net": key,
                    "cell": instance.name,
                    "inputs": list(function.inputs),
                    "arity": function.arity,
                    "complemented": bool(affine[0]),
                    "conditional": False,
                }
            )
            continue
        if not conditional:
            continue
        found = _conditional_xor(function)
        if found is None:
            continue
        position, value, restricted = found
        results.append(
            {
                "net": key,
                "cell": instance.name,
                "inputs": list(restricted.inputs),
                "arity": restricted.arity,
                "complemented": bool(_pure_xor(restricted)[0]),
                "conditional": True,
                "cofactor": {"net": function.inputs[position], "value": int(value)},
            }
        )
    return results


def _base(key):
    if key.endswith("]") and "[" in key:
        return key[: key.index("[")]
    return key


def adder_operand_rotations(model, adders):
    """Rotations visible only in the order an adder's slices read a bank.

    The carry chain is an ordered layer: slice *i* computes bit *i* of the sum,
    so the operand net at slice *i* is whatever the wiring put there.  When
    that is bit ``(i + c) mod w`` of one register bank for a fixed non-zero
    *c*, the rotation is in the netlist even though no vector carries it.
    """
    results = []
    for entry in adders:
        for side in ("left_sources", "right_sources"):
            sources = entry.get(side)
            if not sources:
                continue
            names = {source.partition("[")[0] for source in sources}
            width = None
            if len(names) == 1:
                width = permutation.vector_width(model.netlist, next(iter(names)))
            found = permutation.vector_rotation(sources, width=width)
            if found is None:
                continue
            found = dict(found)
            found["destination"] = "{} operand of the carry chain at {}".format(
                "first" if side == "left_sources" else "second", entry["cells"][0]
            )
            found["read_from"] = "carry-chain operand order"
            found["all_inverted"] = bool(
                entry.get("left_inverted" if side == "left_sources" else "right_inverted")
            )
            found["adder_cells"] = entry["cells"]
            results.append(found)
    return results


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
    named = [
        dict(entry, read_from="named vector")
        for entry in permutations
        if entry.get("kind") == "rotation" and entry["width"] >= MIN_ARX_WIDTH
    ]
    # A vendor export names neither of the other two; see the module docstring.
    layered = adder_operand_rotations(model, wide_adders)
    layered += permutation.register_bank_rotations(model)
    # One rotation can be visible in two places at once -- a named vector that
    # the adder then reads, say.  Report it once, preferring the reading that
    # needed the least inference, so the count stays a count of rotations.
    rotations = []
    seen = set()
    for entry in named + [item for item in layered if item["width"] >= MIN_ARX_WIDTH]:
        signature = (entry["source"], entry["width"], entry["rotation"])
        if signature in seen:
            continue
        seen.add(signature)
        rotations.append(entry)
    xors = xor_nets(model)
    standalone_xors = [entry for entry in xors if not entry["conditional"]]

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
                "read_from": entry["read_from"],
                "bits_observed": entry.get("bits_observed", entry["width"]),
            }
            for entry in rotations
        ],
        "xor_cells": len(xors),
        "standalone_xor_cells": len(standalone_xors),
        "conditional_xor_cells": len(xors) - len(standalone_xors),
        "xor_layer_nets": [entry["net"] for entry in xors][:64],
    }

    missing = []
    if not wide_adders:
        missing.append("no carry-chain adder of at least {} bits".format(MIN_ARX_WIDTH))
    if not rotations:
        missing.append(
            "no fixed rotation, in a named vector or in the order a cell layer "
            "reads a register bank"
        )
    if len(xors) < MIN_XOR_LAYER:
        missing.append(
            "only {} XOR cell(s), {} of them standalone; a layer starts at {}".format(
                len(xors), len(standalone_xors), MIN_XOR_LAYER
            )
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
