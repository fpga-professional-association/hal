"""Extract substitution boxes from LUT cones and match them against the library.

The extraction is a clustering problem before it is a cryptographic one: an
S-box in a netlist is *n* combinational outputs that read the same *n* sources
and, taken together, permute them.  So the pass groups combinational nets by
the set of registers and inputs their cone depends on, keeps the groups that
form a bijection of that set onto itself, and only then goes looking for the
table in :mod:`hal_crypto.known`.

Two filters keep the output honest:

* **bijective** -- an *n*-to-*n* map that collides is not a substitution box,
  and reporting it as one would make every wide adder slice a "candidate";
* **not affine over GF(2)** -- a linear or affine map is a permutation network
  or a key XOR, both of which have their own passes.  Every published S-box has
  algebraic degree at least two, so nothing real is lost and the false-positive
  rate drops to near nothing.

A group that survives both filters and matches nothing in the library is still
reported.  "There is an unrecognised 4-bit S-box here" is a real finding: it is
what an S-box the library has never seen looks like, and hiding it would make
the pass useless against anything but a textbook cipher.

The *bit order* of the extracted box is the pass's own choice (sources and
outputs sorted by net name), which is why an exact table match is the exception
and a bit-permutation match is the norm.  The tier is carried through to the
finding text; see :func:`hal_crypto.boolfunc.match_sbox`.
"""

import itertools

from . import boolfunc, known
from .netlist_model import ConeTooWide, UnsupportedCell

__all__ = [
    "MIN_SBOX_BITS",
    "MAX_SBOX_BITS",
    "MAX_GROUP_COMBINATIONS",
    "candidate_groups",
    "extract_sboxes",
    "match_library",
]

MIN_SBOX_BITS = 3
MAX_SBOX_BITS = boolfunc.MAX_SBOX_BITS

#: A cone is only evaluated when it reads at most this many nets.  An S-box
#: output bit reads at most eight, so the four bits of slack cover a cone that
#: fans in wider than it functionally depends on; beyond that the enumeration
#: is paid for on every adder sum bit in the design and buys nothing.  Cones
#: that exceed it are *counted and reported*, not silently dropped -- their
#: functional support could still be small, and pretending otherwise would turn
#: a coverage limit into a clean negative.
MAX_EXTRACTION_SOURCES = 12

#: How many *n*-subsets of an oversized output group the pass will try before
#: giving up on it.  A group with far more outputs than inputs is a fan-out
#: cluster, not an S-box, and enumerating it is not worth the time.
MAX_GROUP_COMBINATIONS = 200


def _combinational_nets(model):
    """``(nets, skipped)``: the usable cones, and how many were too wide."""
    results = {}
    skipped = 0
    for instance in model.lcells:
        for pin in ("combout", "sumout", "cout"):
            bits = instance.connections.get(pin)
            if not bits:
                continue
            key = getattr(bits[0], "key", None)
            if key is None or key in results:
                continue
            try:
                support = model.support(key)
            except UnsupportedCell:
                continue
            if len(support) < MIN_SBOX_BITS:
                continue
            if len(support) > MAX_EXTRACTION_SOURCES:
                skipped += 1
                continue
            try:
                table = model.cone(key).restricted()
            except (UnsupportedCell, ConeTooWide):
                continue
            if table.arity < MIN_SBOX_BITS or table.arity > MAX_SBOX_BITS:
                continue
            results[key] = table
    return results, skipped


def candidate_groups(model):
    """Groups of ``n`` nets that together read exactly ``n`` sources.

    The candidate supports are the supports the netlist actually contains, and
    the pool for each one is found through a source-to-net index rather than by
    rescanning every net: on a large design the naive version is quadratic in
    the number of combinational nets, and an S-box pass that takes minutes on a
    real netlist is a pass nobody runs.
    """
    nets, skipped = _combinational_nets(model)
    by_source = {}
    for key, table in nets.items():
        for name in table.inputs:
            by_source.setdefault(name, set()).add(key)
    by_support = {}
    for key, table in nets.items():
        by_support.setdefault(frozenset(table.inputs), []).append(key)

    groups = []
    for support in sorted(by_support, key=lambda item: (len(item), sorted(item))):
        bits = len(support)
        if bits < MIN_SBOX_BITS or bits > MAX_SBOX_BITS:
            continue
        reachable = set()
        for name in support:
            reachable |= by_source.get(name, set())
        pool = sorted(key for key in reachable if set(nets[key].inputs) <= support)
        if len(pool) < bits:
            continue
        if len(pool) == bits:
            groups.append((sorted(support), pool))
            continue
        combinations = list(itertools.combinations(pool, bits))
        if len(combinations) > MAX_GROUP_COMBINATIONS:
            continue
        for combination in combinations:
            union = set()
            for key in combination:
                union.update(nets[key].inputs)
            if union == set(support):
                groups.append((sorted(support), list(combination)))
    return groups, nets, skipped


def extract_sboxes(model):
    """Bijective, non-affine ``n``-to-``n`` maps found in the combinational logic.

    Each result is a dict with the source nets, the output nets, the
    :class:`~hal_crypto.boolfunc.Sbox`, and the reason a rejected group was
    rejected (rejections are returned too, under ``rejected``).
    """
    groups, nets, skipped = candidate_groups(model)
    accepted = []
    rejected = []
    seen = set()
    if skipped:
        rejected.append(
            {
                "sources": [],
                "outputs": [],
                "reason": (
                    "{} combinational net(s) read more than {} sources, so their "
                    "cones were not enumerated; an S-box hidden behind that much "
                    "fan-in would not have been found".format(
                        skipped, MAX_EXTRACTION_SOURCES
                    )
                ),
            }
        )
    for sources, outputs in groups:
        signature = (tuple(sources), tuple(outputs))
        if signature in seen:
            continue
        seen.add(signature)
        bits = len(sources)
        table = []
        for value in range(1 << bits):
            assignment = {
                name: (value >> position) & 1 for position, name in enumerate(sources)
            }
            word = 0
            for position, key in enumerate(outputs):
                word |= nets[key].evaluate(assignment) << position
            table.append(word)
        sbox = boolfunc.Sbox(bits, table)
        if not sbox.is_bijective():
            rejected.append(
                {
                    "sources": list(sources),
                    "outputs": list(outputs),
                    "reason": "the {}-bit map collides, so it is not a substitution".format(
                        bits
                    ),
                }
            )
            continue
        if sbox.is_affine():
            rejected.append(
                {
                    "sources": list(sources),
                    "outputs": list(outputs),
                    "reason": (
                        "the {}-bit map is affine over GF(2); a linear layer or a "
                        "constant XOR, not a substitution box".format(bits)
                    ),
                }
            )
            continue
        accepted.append(
            {
                "sources": list(sources),
                "outputs": list(outputs),
                "bits": bits,
                "sbox": sbox,
                "table": list(sbox.table),
                "algebraic_degree": sbox.algebraic_degree(),
                "differential_uniformity": sbox.differential_uniformity(),
            }
        )
    return accepted, rejected


def match_library(sbox):
    """Every library entry *sbox* matches, best tier first.

    A candidate can match more than one entry -- the DES rows are all 4-bit
    bijections and several are bit-permutation equivalent to each other -- so
    the caller gets the whole list and the finding names all of them rather
    than picking a winner it cannot justify.
    """
    order = {"exact": 0, "xor_constant": 1, "bit_permutation": 2}
    matches = []
    for name, entry in known.sbox_entries(bits=sbox.bits):
        result = boolfunc.match_sbox(
            sbox, entry["sbox"], name=name, reference_note=entry["reference"]
        )
        if result is None:
            continue
        record = result.as_dict()
        record["family"] = entry["family"]
        matches.append(record)
    matches.sort(key=lambda record: (order[record["tier"]], record["name"]))
    return matches


def identify(model):
    """The full S-box pass: extraction plus library matching."""
    accepted, rejected = extract_sboxes(model)
    results = []
    for entry in accepted:
        matches = match_library(entry["sbox"])
        record = {
            key: value for key, value in entry.items() if key != "sbox"
        }
        record["matches"] = matches
        record["search_note"] = boolfunc.permutation_search_note(entry["bits"])
        record["families"] = sorted({match["family"] for match in matches})
        results.append(record)
    return {"sboxes": results, "rejected": rejected}
