"""Word membership and bit order recovered from a carry chain, not from a name.

A rotation costs no cell, so the only thing a netlist can show is the *order* in
which an ordered layer of cells reads a word -- and "order" needs two things
that a vendor export does not hand over: which flip-flops are one word, and
which bit of that word each of them is.  :mod:`hal_crypto.permutation` takes
both from the net names, which is the right reading when the names are there:
``x[3]`` says word ``x``, bit 3, and nothing structural says it better.

The names are not always there.  The anonymiser of
``examples/agilex3_walkthroughs/10_crc8_checker/anonymize.py`` splits every
internal vector declaration into unrelated scalars, a netlist recovered from a
bitstream never had vector declarations at all, and neither does one built with
name mangling on.  Those are precisely the netlists a structural crypto
identifier is worth having, and on them the name-keyed reading finds nothing:
``examples/agilex3_walkthroughs/16_mystery_cores`` measured a Speck32/64 export
losing all four of its rotations to the blinding while both carry chains and all
54 XOR cells survived.

This module recovers the same two facts from the **carry chain**, which is the
one ordered object a vendor export contains unambiguously: a dedicated ``cout ->
cin`` wire needs no heuristic, and slice *i* of it computes bit *i*.  Two
readings follow from that, and both are offsets between *two* orderings of the
same flip-flops, which is what makes them rotations rather than relabellings:

``operand_rotations``
    the chain reads a register at slice *i* that it *writes* at slice
    ``(i + c) mod w``.  The word's own bit order is where the chain writes it --
    bit *i* of the word is the flip-flop that takes sum bit *i* -- so a constant
    non-zero *c* is a rotation in the wiring, read without a single name.  This
    is ``x <= ROR(x, 7) + y`` seen from the adder's side;
``word_rotations``
    a flip-flop the chain writes at position *i* has a next-state cell that
    reads exactly one other flip-flop the same chain writes, at position
    ``(i + c) mod w``.  That is ``y <= ROL(y, 2) ^ ...`` seen from the register
    bank's side -- the reading
    :func:`hal_crypto.permutation.register_bank_rotations` does off the cell
    pins, with the bank recovered from the chain instead of from a vector name.

The width is the chain's, not the count of flip-flops that happen to be labelled:
Quartus drops the dead generate half of a chain's top slice, so
:func:`hal_crypto.arith.adders` reports 15 slices for a 16-bit add while the top
``sumout`` is still there and still drives bit 15 of the destination word.
Taking the width from the sum outputs of the chain cells recovers the 16, which
is the difference between matching a published 16-bit rotation set and matching
nothing.

Both readings are weaker than a named vector and are labelled as such.  Neither
invents a word: a flip-flop is a member of one only because a chain writes it,
and a rotation is reported only when one offset explains most of the word, the
flip-flops it maps are distinct, and the amount is not 0 (straight wiring) --
and, for ``word_rotations``, not +-1 either, because a bank whose every bit
reads its neighbour is a shift chain, which :mod:`hal_crypto.shiftreg` names
properly and with its feedback.
"""

from . import arith
from .boolfunc import TruthTable
from .netlist_model import ConeTooWide, UnsupportedCell

__all__ = [
    "MIN_WORD_WIDTH",
    "MIN_ROTATION_COVERAGE",
    "chain_frames",
    "written_positions",
    "operand_rotations",
    "word_rotations",
    "structural_rotations",
]

#: Narrower than this, a "word" is not a cipher word and an offset in it is not
#: a rotation.  The same threshold :mod:`hal_crypto.permutation` uses.
MIN_WORD_WIDTH = 4

#: How much of the word one offset has to explain before it is reported.  Below
#: half, a constant offset over a handful of bits is a coincidence, not a layer.
MIN_ROTATION_COVERAGE = 0.5


def chain_frames(model):
    """Every carry chain as an ordered list of the bits it writes.

    ``(cell names of the whole chain, sum net key per position)``.  Cells with
    no ``sumout`` -- the carry seed Quartus prefixes a subtracter with -- carry
    no bit and are skipped, so position *i* of the second list is bit *i* of
    whatever word the chain drives.  The list is deliberately *longer* than
    :func:`hal_crypto.arith.adders`'s operand lists: the top slice's sum is real
    even when its generate half is dead.
    """
    frames = []
    for chain in arith.chains(model):
        keys = []
        for cell in chain:
            bit = cell.single("sumout")
            if bit is None:
                continue
            keys.append(bit.key)
        frames.append(([cell.name for cell in chain], keys))
    return frames


def written_positions(model, frames):
    """``{flip-flop name: (frame, position)}`` -- where the chain writes it.

    A flip-flop is placed only when its next-state cone reads **exactly one**
    sum bit of exactly one chain.  That is the whole claim: this register takes
    bit *i* of what that chain computed, whatever else is mixed into it on the
    way -- a round key, a load multiplexer, a second word XORed on top.  A
    register that reads two sum bits is not a bit of the result and is left
    unplaced rather than guessed at.
    """
    cut = frozenset(key for _cells, keys in frames for key in keys)
    if not cut:
        return {}
    where = {}
    for number, (_cells, keys) in enumerate(frames):
        for position, key in enumerate(keys):
            where[key] = (number, position)
    placed = {}
    for ff in model.ff_instances:
        data = ff.connections.get("d")
        if not data:
            continue
        resolved = model.resolve(data[0])
        if resolved[0] != "net":
            continue
        try:
            support = model.cut_support(resolved[1], cut, expand_root=True)
        except (UnsupportedCell, ConeTooWide):
            continue
        hits = [key for key in support if key in cut]
        if len(hits) != 1:
            continue
        placed[ff.name] = where[hits[0]]
    return placed


def _cell_register_inputs(model, ff):
    """The registers the flip-flop's next-state *cell* reads on its own pins.

    The cell, not the cone: an ARX round's cone reaches back through the whole
    carry chain and reads half the state, while the cell that drives the
    register reads one bit of the bank and the sum.  It is the cell that carries
    the rotation.  Dead LUT inputs are dropped first, so a pin the function does
    not depend on cannot manufacture a link.
    """
    data = ff.connections.get("d")
    if not data:
        return []
    resolved = model.resolve(data[0])
    if resolved[0] != "net":
        return []
    driver = model.driver(resolved[1])
    if driver is None or driver[1] != "combout":
        return []
    try:
        keys, table, _, _, _ = model._lut_inputs(driver[0])  # noqa: SLF001
    except (UnsupportedCell, ConeTooWide):
        return []
    if not keys:
        return []
    function = TruthTable(keys, table).restricted()
    registers = []
    for key in function.inputs:
        register = model.register_of(model.peel(key)[0])
        if register is not None:
            registers.append(register.name)
    return registers


def _frame_of(frames, cells):
    """The frame whose chain ends in *cells*, or ``None``.

    ``arith.classify_chain`` may drop a leading carry-seed cell, so an adder's
    cell list is a suffix of the chain's.
    """
    for number, (chain_cells, keys) in enumerate(frames):
        if cells and chain_cells[-len(cells):] == list(cells):
            return number, keys
    return None


def _enough(members, width):
    return len(members) >= max(MIN_WORD_WIDTH, MIN_ROTATION_COVERAGE * width)


def _entry(width, rotation, registers, destination, read_from, observed):
    return {
        "kind": "rotation",
        "source": "a {}-bit register word with no name in the netlist".format(width),
        "destination": destination,
        "width": width,
        "rotation": rotation,
        "rotate_left_by": (width - rotation) % width,
        "bits_observed": observed,
        "all_inverted": False,
        "read_from": read_from,
        "registers": sorted(registers),
    }


def operand_rotations(model, adders, frames=None, positions=None):
    """Rotations between what a carry chain reads and what the same chain writes.

    Which of an adder's two operand vectors a slice's two inputs belong to is
    not asked, and must not be: ``arith.classify_chain`` splits them by sorting
    the net keys, which is stable but arbitrary once the keys are meaningless.
    Every operand net of every slice is placed independently, and the offsets
    that come back are grouped -- a genuine rotation shows up as one offset that
    explains one net of nearly every slice.
    """
    frames = chain_frames(model) if frames is None else frames
    positions = written_positions(model, frames) if positions is None else positions
    results = []
    for entry in adders:
        found = _frame_of(frames, entry.get("cells") or ())
        if found is None:
            continue
        frame, keys = found
        width = len(keys)
        if width < MIN_WORD_WIDTH:
            continue
        groups = {}
        for side in ("left_sources", "right_sources"):
            sources = entry.get(side) or []
            # Position i of the list has to be slice i; a side with a constant
            # bit in it is shorter than the chain and would shift every index.
            if len(sources) != entry["width"]:
                continue
            for slice_index, key in enumerate(sources):
                register = model.register_of(model.peel(key)[0])
                if register is None:
                    continue
                placed = positions.get(register.name)
                if placed is None or placed[0] != frame:
                    continue
                offset = (placed[1] - slice_index) % width
                groups.setdefault(offset, {})[slice_index] = register.name
        for offset in sorted(groups):
            if offset == 0:
                continue  # straight wiring; every netlist is full of these
            members = groups[offset]
            if not _enough(members, width):
                continue
            if len(set(members.values())) != len(members):
                continue
            results.append(
                _entry(
                    width,
                    offset,
                    members.values(),
                    "operand order of the carry chain at {}".format(entry["cells"][0]),
                    "carry-chain operand order against the chain's own bit order",
                    len(members),
                )
            )
    return results


def word_rotations(model, frames=None, positions=None):
    """Rotations in the next-state fan-in of a word a carry chain writes.

    :func:`hal_crypto.permutation.register_bank_rotations` asks this of a
    register bank named by a vector; here the bank is the set of flip-flops one
    chain writes, and the bit index is the slice that writes each of them.  A
    chain usually writes more than one word -- Speck's round writes both halves
    of the state from one adder -- so the flip-flops are not partitioned up
    front: each is asked which single sibling its cell reads, and the word falls
    out as the set of flip-flops that answer with the same offset.
    """
    frames = chain_frames(model) if frames is None else frames
    positions = written_positions(model, frames) if positions is None else positions
    by_name = {ff.name: ff for ff in model.ff_instances}
    results = []
    for frame, (cells, keys) in enumerate(frames):
        width = len(keys)
        if width < MIN_WORD_WIDTH:
            continue
        members = {
            name: place[1] for name, place in positions.items() if place[0] == frame
        }
        groups = {}
        for name in sorted(members):
            inside = sorted(
                {
                    other
                    for other in _cell_register_inputs(model, by_name[name])
                    if other in members and other != name
                }
            )
            if len(inside) != 1:
                continue
            offset = (members[inside[0]] - members[name]) % width
            groups.setdefault(offset, []).append((members[name], name))
        for offset in sorted(groups):
            # 0 is the identity; 1 and w-1 are the shift-chain shape, and a word
            # whose every bit reads its neighbour is a shift register, which
            # hal_crypto.shiftreg names properly and with its feedback.
            if offset in (0, 1, width - 1):
                continue
            placed = groups[offset]
            if not _enough(placed, width):
                continue
            if len({position for position, _ in placed}) != len(placed):
                continue
            results.append(
                _entry(
                    width,
                    offset,
                    (name for _position, name in placed),
                    "next-state fan-in of the word the carry chain at {} writes".format(
                        cells[0]
                    ),
                    "next-state cells of a carry chain's destination word",
                    len(placed),
                )
            )
    return results


def structural_rotations(model, adders):
    """Both readings, operand order first, sharing one pass over the chains."""
    frames = chain_frames(model)
    if not any(keys for _cells, keys in frames):
        return []
    positions = written_positions(model, frames)
    if not positions:
        return []
    results = operand_rotations(model, adders, frames=frames, positions=positions)
    for entry in results:
        entry["on_adder_operand"] = True
    return results + word_rotations(model, frames=frames, positions=positions)
