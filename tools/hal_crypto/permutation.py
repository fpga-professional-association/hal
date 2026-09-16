"""Pure-wire bit permutations between layers.

A permutation layer costs no logic, which is exactly what makes it invisible to
a gate-level eye and cheap for this pass to find: it is a set of destination
bits each of which peels back, through nothing but buffers and inverters, to a
distinct bit of one source vector of the same width.

Three destinations are looked at, because a permutation can land in any of
them: a declared wire vector, a module output vector, and the data inputs of a
register bank.  Each map is classified before it is matched --

``identity``
    the wiring is straight.  Reported for completeness and then ignored: every
    netlist is full of these and none of them is evidence of anything;
``rotation``
    ``dest[i] = src[(i + k) mod w]`` for one non-zero *k*.  This is the R of
    ARX and the rho step of a sponge, so the amount is what gets matched;
``reversal`` / ``general``
    everything else; a general map of a width the library knows (64 for the
    PRESENT and GIFT pLayers) is compared against it in both directions,
    because "bit *i* goes to *P(i)*" and "bit *i* comes from *P(i)*" are the
    two conventions the literature uses and they are inverses.

Inversions along the way are recorded, not ignored: a rotation whose bits
arrive complemented is still a rotation of the bit positions, but it is not the
same function, and the finding says which.

## Rotations a vendor export does not name

The three destinations above all need the permuted word to *exist* as a named
vector.  In a hand-built netlist it does; in a Quartus export of a real ARX
round it does not, because a pure-wire rotation is not a signal a synthesiser
has any reason to keep.  What survives instead is the *order in which an
ordered layer of cells reads one register bank*:

* the slices of a carry chain, whose operand for bit *i* is the bank bit the
  rotation put there -- :func:`vector_rotation` over an adder's operand list;
* the next-state cells of a register bank, when each bit's cell reads exactly
  one bit of that same bank -- :func:`register_bank_rotations`.

Both are weaker evidence than a named vector, and both are reported with the
place they were read from, so a finding never claims a wire that is not there.

## Permutations behind one cell per link

The three destinations above, and both rotation readers, need the link itself
to be a *wire*.  In any real SPN it is not: a round-key XOR sits between the
substitution layer and the registers, and a parallel key load puts a
multiplexer on every link of the key register.  One 3-input ALM per bit is
enough to hide a 64-bit pLayer and an 80-bit rotation from a pass that requires
pure wiring, which is exactly what happened in
``examples/agilex3_walkthroughs/12_present_sbox``: the pLayer and the key
schedule's rotation by 61 are both fully present in that netlist and both had
to be recovered by hand.

:func:`cone_support_maps` automates that recovery.  For every register bank it
asks, of each bit's next-state cone, *which bits of vector V does this cone
read* -- with the walk cut at every declared vector, so the answer is about the
layer above the logic and not about the registers behind it.  When that is
exactly one bit for a bit of the bank, the pair is a **link**; when the links
form a bijection of the bank, or agree on a single rotation amount, the map is
proposed.  What the rest of the cone contains -- a round key, a load path, a
mode select -- is reported, never hidden: the claim is

    each destination bit's next state reads exactly one bit of the source, and
    the index map is this one,

which is strictly weaker than the wiring tier's *the map is the wiring*, and
the two are kept apart.  Three rules keep it from over-claiming:

* a map the wiring tier already found is not reported twice; the wiring
  statement is the stronger one and it stands alone;
* when two different sources both explain the same bank, nothing is reported:
  that is a multiplexer choosing between two operands, not a permutation
  layer;
* a *partial* map is only ever reported as a rotation, never as a general
  permutation, and never when the amount is 1 or -1: a bank whose bits each
  read their neighbour, with the head left over, is an open shift chain, which
  :mod:`hal_crypto.shiftreg` names properly and with its feedback.
"""

from . import known
from .boolfunc import TruthTable
from .netlist_model import MAX_CONE_SOURCES, ConeTooWide, UnsupportedCell

__all__ = [
    "MIN_PERMUTATION_WIDTH",
    "MIN_ROTATION_COVERAGE",
    "vector_bits",
    "find_permutations",
    "classify_permutation",
    "match_permutation",
    "vector_rotation",
    "register_bank_rotations",
    "cone_support_maps",
]

#: Narrower than this, "permutation" is not a useful word: any two-bit swap
#: qualifies and none of them is a cipher layer.
MIN_PERMUTATION_WIDTH = 4

#: Fraction of a register bank that has to link back to one source vector
#: before a *partial* map is reported as a rotation of it.  A complete map
#: needs no such rule -- it covers the bank -- but a partial one is an
#: extrapolation, and below half the bank it is a guess.
MIN_ROTATION_COVERAGE = 0.5


def _split(key):
    """``('state', 3)`` for ``'state[3]'``; ``(key, None)`` for a scalar."""
    if key.endswith("]") and "[" in key:
        name, _, index = key[:-1].partition("[")
        try:
            return name, int(index)
        except ValueError:
            return key, None
    return key, None


def vector_bits(netlist):
    """Declared vector name -> ordered list of bit keys, least significant first."""
    vectors = {}
    for name, entry in netlist.declarations.items():
        if entry[1] is None:
            continue
        low, high = min(entry[1], entry[2]), max(entry[1], entry[2])
        if high - low + 1 < MIN_PERMUTATION_WIDTH:
            continue
        vectors[name] = ["{}[{}]".format(name, index) for index in range(low, high + 1)]
    return vectors


def _register_banks(model):
    """Vector name -> ``{index: ff}`` for register banks whose ``q`` is a vector."""
    banks = {}
    for ff in model.ff_instances:
        bits = ff.connections.get("q")
        if not bits:
            continue
        name, index = _split(getattr(bits[0], "key", ""))
        if index is None:
            continue
        banks.setdefault(name, {})[index] = ff
    return {
        name: bank
        for name, bank in banks.items()
        if len(bank) >= MIN_PERMUTATION_WIDTH
    }


def _peel_entry(model, bit):
    resolved = model.peel_bit(bit)
    if resolved[0] == "const":
        return None
    return resolved[1], resolved[2]


def find_permutations(model):
    """Every pure-wire bit map between two equally wide vectors."""
    netlist = model.netlist
    vectors = vector_bits(netlist)
    results = []

    def consider(destination, width, sources):
        """``sources`` is an ordered list of ``(key, inverted)`` or ``None``."""
        if any(entry is None for entry in sources):
            return
        names = {_split(entry[0])[0] for entry in sources}
        if len(names) != 1:
            return
        source_name = names.pop()
        if source_name == destination:
            return
        indices = [_split(entry[0])[1] for entry in sources]
        if any(index is None for index in indices):
            return
        if len(set(indices)) != width:
            return
        if sorted(indices) != list(range(min(indices), min(indices) + width)):
            return
        offset = min(indices)
        permutation = [index - offset for index in indices]
        inverted = [entry[1] for entry in sources]
        entry = classify_permutation(permutation)
        entry.update(
            {
                "destination": destination,
                "source": source_name,
                "width": width,
                "permutation": permutation,
                "inversions": [position for position, flag in enumerate(inverted) if flag],
                "all_inverted": all(inverted),
            }
        )
        entry["matches"] = match_permutation(permutation)
        results.append(entry)

    for name, keys in sorted(vectors.items()):
        sources = []
        for key in keys:
            driver = model.driver(key)
            if driver is not None and driver[1] == "q":
                sources.append(None)  # the vector *is* a register bank output
                continue
            peeled = model.peel(key)
            sources.append(peeled)
        consider(name, len(keys), sources)

    for name, bank in sorted(_register_banks(model).items()):
        indices = sorted(bank)
        if indices != list(range(min(indices), min(indices) + len(indices))):
            continue
        sources = []
        for index in indices:
            data = bank[index].connections.get("d")
            sources.append(_peel_entry(model, data[0]) if data else None)
        consider("register bank {}".format(name), len(indices), sources)

    return results


def classify_permutation(permutation):
    """Name the shape of a permutation given as ``dest[i] = src[P[i]]``."""
    width = len(permutation)
    if permutation == list(range(width)):
        return {"kind": "identity"}
    for amount in range(1, width):
        if all(permutation[index] == (index + amount) % width for index in range(width)):
            # dest[i] = src[(i + amount) mod w] is the same wiring as
            # dest = src rotated left by (w - amount); both spellings are in
            # the literature, so both are reported.
            return {
                "kind": "rotation",
                "rotation": amount,
                "rotate_left_by": (width - amount) % width,
            }
    if permutation == list(range(width - 1, -1, -1)):
        return {"kind": "reversal"}
    return {"kind": "general"}


def match_permutation(permutation):
    """Library permutations equal to this one, in either index convention."""
    width = len(permutation)
    inverse = [0] * width
    for index, source in enumerate(permutation):
        inverse[source] = index
    matches = []
    for name, entry in known.permutation_entries(width=width):
        published = list(entry["permutation"])
        if published == list(permutation):
            convention = "dest[i] = src[P(i)]"
        elif published == inverse:
            convention = "src[i] drives dest[P(i)]"
        else:
            continue
        matches.append(
            {
                "name": name,
                "convention": convention,
                "family": entry["family"],
                "reference": entry["reference"],
            }
        )
    return matches


def vector_width(netlist, name):
    """The declared width of vector *name*, or ``None`` when it is not one."""
    entry = netlist.declarations.get(name)
    if entry is None or entry[1] is None:
        return None
    return abs(entry[2] - entry[1]) + 1


def vector_rotation(keys, width=None):
    """Classify what an ordered cell layer reads as a rotation of one vector.

    ``keys[i]`` is the source net position *i* of the layer reads, least
    significant position first.  Unlike :func:`classify_permutation` the layer
    does **not** have to cover the whole source vector: a carry chain's top
    slice is classified separately (its generate half is dead, so the chain
    walk stops one short), which would leave a hole in the index set.  The
    offset is therefore taken modulo *width* -- the source vector's declared
    width -- and how many positions were actually seen is reported.

    Returns ``None`` unless every key is a bit of one vector, the bits are
    distinct, and one non-zero offset explains every position.
    """
    keys = list(keys)
    if len(keys) < MIN_PERMUTATION_WIDTH:
        return None
    names = {_split(key)[0] for key in keys}
    if len(names) != 1:
        return None
    source = names.pop()
    indices = [_split(key)[1] for key in keys]
    if any(index is None for index in indices):
        return None
    if len(set(indices)) != len(indices):
        return None
    if width is None:
        width = max(indices) + 1
    if width < MIN_PERMUTATION_WIDTH or max(indices) >= width:
        return None
    for amount in range(width):
        if all(indices[i] == (i + amount) % width for i in range(len(indices))):
            if amount == 0:
                return None  # straight wiring; every netlist is full of these
            return {
                "kind": "rotation",
                "source": source,
                "width": width,
                "rotation": amount,
                "rotate_left_by": (width - amount) % width,
                "bits_observed": len(indices),
            }
    return None


def _bank_self_source(model, ff, bank_name):
    """The single bit of *bank_name* the flip-flop's next-state cell reads.

    ``None`` when the ``d`` pin is not driven by an ALM's ``combout``, when the
    cell cannot be decoded, or when it reads no bit of the bank or more than
    one.  "Exactly one" is the point: it is what makes the map a function of
    the bit index rather than a cone.
    """
    data = ff.connections.get("d")
    if not data:
        return None
    resolved = model.resolve(data[0])
    if resolved[0] != "net":
        return None
    driver = model.driver(resolved[1])
    if driver is None or driver[1] != "combout":
        return None
    try:
        keys, table, _, _, _ = model._lut_inputs(driver[0])  # noqa: SLF001
    except (UnsupportedCell, ConeTooWide):
        return None
    if not keys:
        return None
    function = TruthTable(keys, table).restricted()
    own = []
    for key in function.inputs:
        peeled = model.peel(key)[0]
        if _split(peeled)[0] == bank_name:
            own.append(peeled)
    if len(own) != 1:
        return None
    return own[0]


def register_bank_rotations(model):
    """Rotations read off the next-state fan-in of a register bank.

    For every bank wide enough to be a cipher word, ask which bit of the bank
    each bit's next-state cell reads *directly* (not through the cone: through
    the cell's own pins).  When that is exactly one bit for every position and
    the resulting index map is a rotation, the rotation is in the wiring even
    though no vector in the netlist carries it -- which is how Quartus spells
    ``y <= ROL(y, 2) ^ something``.
    """
    results = []
    for name, bank in sorted(_register_banks(model).items()):
        indices = sorted(bank)
        # The bank has to be bits 0..n-1 of its vector, not merely contiguous:
        # position i of the list below is read as bank bit i, and a bank whose
        # lowest bit is missing would shift every index by a constant and turn
        # straight wiring into a rotation.  Refuse rather than offset-correct --
        # the offset is not recoverable from a partial bank.
        if indices != list(range(len(indices))):
            continue
        sources = []
        for index in indices:
            source = _bank_self_source(model, bank[index], name)
            if source is None:
                sources = None
                break
            sources.append(source)
        if not sources:
            continue
        entry = vector_rotation(sources, width=vector_width(model.netlist, name))
        if entry is None:
            continue
        entry = dict(entry)
        entry["destination"] = "register bank {} next-state fan-in".format(name)
        entry["read_from"] = "register-bank next-state cells"
        entry["all_inverted"] = False
        results.append(entry)
    return results


# ---------------------------------------------------------------------------
# maps read off the next-state cone support
# ---------------------------------------------------------------------------


def _affine_in(table, position):
    """True when ``f = x_position XOR g(rest)``.

    The difference between "this register takes that bit, XORed with a round
    key" and "this register takes that bit when a select says so".  Both are
    links -- the index map is the same either way -- but only the first one is
    unconditional, so which it is goes in the finding instead of being averaged
    away.
    """
    step = 1 << position
    for index in range(len(table.values)):
        if index & step:
            continue
        if table.values[index] == table.values[index | step]:
            return False
    return True


def _bank_next_state_links(model, bank, vectors, cut):
    """``{source vector: {bank index: (source position, side nets, affine)}}``.

    A bank index appears under a vector only when the next-state function reads
    **exactly one** bit of that vector and genuinely depends on it: the cone is
    evaluated over the cut and reduced to its functional support first, so a
    dead LUT input cannot manufacture a link.
    """
    positions = {
        name: {key: index for index, key in enumerate(keys)}
        for name, keys in vectors.items()
    }
    links = {}
    for index in sorted(bank):
        data = bank[index].connections.get("d")
        if not data:
            continue
        try:
            resolved = model.resolve(data[0])
            if resolved[0] != "net":
                continue
            support = model.cut_support(resolved[1], cut, expand_root=True)
            if len(support) > MAX_CONE_SOURCES:
                continue
            order = tuple(sorted(support))
            table = model.cone(resolved[1], order=order).restricted()
        except (UnsupportedCell, ConeTooWide):
            continue
        live = list(table.inputs)
        seen = {}
        for key in live:
            name = _split(key)[0]
            if name in positions and key in positions[name]:
                seen.setdefault(name, []).append(key)
        for name, keys in seen.items():
            if len(keys) != 1:
                continue
            side = tuple(sorted(key for key in live if key != keys[0]))
            links.setdefault(name, {})[index] = (
                positions[name][keys[0]],
                side,
                _affine_in(table, live.index(keys[0])),
            )
    return links


def _map_entry(source, width, links):
    """Classify one bank's links to one source vector, or return ``None``."""
    sources = [entry[0] for entry in links.values()]
    if len(links) < MIN_PERMUTATION_WIDTH or len(set(sources)) != len(sources):
        return None
    if len(links) == width:
        permutation = [links[index][0] for index in range(width)]
        entry = classify_permutation(permutation)
        if entry["kind"] == "identity":
            return None
        entry["permutation"] = permutation
        entry["matches"] = match_permutation(permutation)
    else:
        if len(links) < MIN_ROTATION_COVERAGE * width:
            return None
        amounts = {
            (entry[0] - index) % width for index, entry in links.items()
        }
        if len(amounts) != 1:
            return None
        amount = amounts.pop()
        # 0 is straight wiring; 1 and -1 are the shift-chain shape, and a bank
        # missing exactly its head is a shift register, not a rotation.
        if amount in (0, 1, width - 1):
            return None
        entry = {
            "kind": "rotation",
            "rotation": amount,
            "rotate_left_by": (width - amount) % width,
            "matches": [],
        }
    shared = None
    for _, side, _ in links.values():
        shared = set(side) if shared is None else (shared & set(side))
    entry.update(
        {
            "source": source,
            "width": width,
            "bits_observed": len(links),
            "complete": len(links) == width,
            "link_form": "xor"
            if all(link[2] for link in links.values())
            else "gated",
            "shared_side_inputs": sorted(shared or ()),
            "max_side_inputs": max(len(side) for _, side, _ in links.values()),
            "read_from": "next-state cone support",
            "evidence_tier": "cone-support",
        }
    )
    return entry


def cone_support_maps(model, wiring=None, rejections=None):
    """Bit maps into register banks that survive one cell per link.

    *wiring* is the wiring-tier result (:func:`find_permutations`); it is
    computed when not supplied, and any map it already carries is left to it.
    *rejections* is an optional list; the banks that were refused, and why, are
    appended to it, which is what the negative-control tests read.
    """
    netlist = model.netlist
    vectors = vector_bits(netlist)
    cut = frozenset(key for keys in vectors.values() for key in keys)
    claimed = {
        (entry["destination"], entry["source"])
        for entry in (find_permutations(model) if wiring is None else wiring)
    }
    # ... and what the next-state fan-in reader already gets off the cell pins,
    # which is the same statement about the same bank with less inference.
    for entry in register_bank_rotations(model):
        destination = entry["destination"]
        suffix = " next-state fan-in"
        if destination.endswith(suffix):
            destination = destination[: -len(suffix)]
        claimed.add((destination, entry["source"]))
    results = []
    for bank_name, bank in sorted(_register_banks(model).items()):
        width = len(bank)
        # Position i of the map is read as bank bit i, so a bank that is not
        # bits 0..n-1 of its vector would shift every index by a constant.
        if sorted(bank) != list(range(width)):
            continue
        destination = "register bank {}".format(bank_name)
        candidates = []
        for source, links in sorted(
            _bank_next_state_links(model, bank, vectors, cut).items()
        ):
            if len(vectors[source]) != width:
                continue
            entry = _map_entry(source, width, links)
            if entry is not None:
                candidates.append(entry)
        if not candidates:
            continue
        if len(candidates) > 1:
            if rejections is not None:
                rejections.append(
                    {
                        "destination": destination,
                        "sources": [entry["source"] for entry in candidates],
                        "reason": (
                            "{} different sources each explain this bank's next "
                            "state, so the bank chooses between them: that is a "
                            "multiplexer, not a permutation layer".format(
                                len(candidates)
                            )
                        ),
                    }
                )
            continue
        entry = candidates[0]
        if (destination, entry["source"]) in claimed:
            if rejections is not None:
                rejections.append(
                    {
                        "destination": destination,
                        "sources": [entry["source"]],
                        "reason": (
                            "this map is already reported off the wires or off the "
                            "next-state cell pins, which needs less inference and "
                            "is the stronger claim"
                        ),
                    }
                )
            continue
        entry["destination"] = destination
        results.append(entry)
    return results


def rotation_amounts(permutations):
    """The non-zero rotation amounts among *permutations*, with their widths."""
    amounts = {}
    for entry in permutations:
        if entry.get("kind") != "rotation":
            continue
        amounts.setdefault(entry["width"], set()).add(entry["rotation"])
    return {width: sorted(values) for width, values in sorted(amounts.items())}
