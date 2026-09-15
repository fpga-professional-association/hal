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
"""

from . import known

__all__ = [
    "MIN_PERMUTATION_WIDTH",
    "vector_bits",
    "find_permutations",
    "classify_permutation",
    "match_permutation",
]

#: Narrower than this, "permutation" is not a useful word: any two-bit swap
#: qualifies and none of them is a cipher layer.
MIN_PERMUTATION_WIDTH = 4


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


def rotation_amounts(permutations):
    """The non-zero rotation amounts among *permutations*, with their widths."""
    amounts = {}
    for entry in permutations:
        if entry.get("kind") != "rotation":
            continue
        amounts.setdefault(entry["width"], set()).add(entry["rotation"])
    return {width: sorted(values) for width, values in sorted(amounts.items())}
