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

"Read the same *n* sources" has to mean *between them*, not *each*.  Every
output bit of an AES or PRESENT S-box reads every input bit, so keying the
search on the support of one net finds those; a Keccak or Ascon chi row does
not, because ``y_i = x_i ^ (~x_{i+1} & x_{i+2})`` reads **three** of five, and a
five-lane row is then five cones that cover five sources without any one cone
covering them all.  Candidate supports therefore come from two places: the
support of some single net, and the source set of a connected component of the
net/source graph.  The second is what finds chi -- see
:func:`cluster_supports`.
"""

import itertools
import math

from . import boolfunc, known
from .netlist_model import ConeTooWide, UnsupportedCell

__all__ = [
    "MIN_SBOX_BITS",
    "MAX_SBOX_BITS",
    "MAX_GROUP_COMBINATIONS",
    "candidate_groups",
    "cluster_supports",
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


def cluster_supports(nets, by_source):
    """Candidate source sets grown from one cone by tightest coupling.

    A candidate support has to be a set of sources some group of cones covers.
    The cheap way to name one is "the support of a single cone", and that is
    what :func:`candidate_groups` tries first -- but it only ever names a set
    every member of the group reads in full.  A substitution whose output bits
    read a *subset* each is invisible to it: Keccak chi is the published case,
    with five outputs over five sources and three sources per output, and the
    same shape appears in Ascon and in any other chi-like row map.

    So grow one instead.  Seed the set with a cone's support and repeatedly add
    the neighbouring cone that brings the **fewest new sources**, until the
    cones lying wholly inside the set are at least as many as the sources, or
    the set would grow past :data:`MAX_SBOX_BITS`.

    Growing by fewest-new-sources rather than by any neighbour is the whole
    trick, and it is the same observation the rest of this package keeps
    making: a substitution layer is cones that share their sources tightly,
    while the multiplexer layer on top of it is cones that each drag in a
    control net and one data bit of their own.  Following every neighbour
    merges the two and loses both -- on a real Keccak export it turns forty
    five-source clusters into one cluster of everything.  Following the
    cheapest one walks along the substitution and stops at the multiplexers.

    The growth is deterministic (ties broken by net name), bounded by
    ``MAX_SBOX_BITS`` steps per seed, and produces *candidates* only: whether a
    set really carries an ``n``-to-``n`` map is still decided by
    :func:`_groups_for_support` and the bijectivity and affineness filters.
    Returned sorted by ``(size, sorted names)``.
    """
    results = set()
    for seed in sorted(nets):
        support = set(nets[seed].inputs)
        for _ in range(MAX_SBOX_BITS):
            inside = [
                key
                for key in _reachable(support, by_source)
                if set(nets[key].inputs) <= support
            ]
            if len(inside) >= len(support):
                if MIN_SBOX_BITS <= len(support) <= MAX_SBOX_BITS:
                    results.add(frozenset(support))
                break
            best = None
            for key in _reachable(support, by_source):
                extra = set(nets[key].inputs) - support
                if not extra or len(support) + len(extra) > MAX_SBOX_BITS:
                    continue
                if best is None or (len(extra), key) < (len(best[0]), best[1]):
                    best = (extra, key)
            if best is None:
                break
            support |= best[0]
    return sorted(results, key=lambda item: (len(item), sorted(item)))


def _reachable(support, by_source):
    """Every cone that reads at least one source in *support*, sorted."""
    keys = set()
    for name in support:
        keys |= by_source.get(name, set())
    return sorted(keys)


def _groups_for_support(support, nets, by_source):
    """Every ``len(support)``-subset of the cones inside *support* that covers it."""
    bits = len(support)
    if bits < MIN_SBOX_BITS or bits > MAX_SBOX_BITS:
        return []
    reachable = set()
    for name in support:
        reachable |= by_source.get(name, set())
    pool = sorted(key for key in reachable if set(nets[key].inputs) <= support)
    if len(pool) < bits:
        return []
    if len(pool) == bits:
        return [(sorted(support), pool)]
    # Count before building.  `math.comb` of the pool size is the same guard
    # the list length was, but it does not have to allocate the thing it is
    # refusing first: a datapath with a wide bank of same-support next-state
    # cells (walkthrough 15's coefficient registers are 144 of them) makes
    # C(pool, bits) large enough to exhaust memory on the way to the check.
    if math.comb(len(pool), bits) > MAX_GROUP_COMBINATIONS:
        return []
    combinations = itertools.combinations(pool, bits)
    groups = []
    for combination in combinations:
        union = set()
        for key in combination:
            union.update(nets[key].inputs)
        if union == set(support):
            groups.append((sorted(support), list(combination)))
    return groups


def candidate_groups(model):
    """Groups of ``n`` nets that together read exactly ``n`` sources.

    The candidate supports are the supports the netlist actually contains plus
    the source sets of :func:`cluster_supports`, and the pool for each one is
    found through a source-to-net index rather than by rescanning every net: on
    a large design the naive version is quadratic in the number of
    combinational nets, and an S-box pass that takes minutes on a real netlist
    is a pass nobody runs.

    Single-net supports are tried first and in their own order, so adding the
    cluster supports can only *append* groups: a netlist whose S-boxes were
    already found reports them in the same order as before.
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
        groups.extend(_groups_for_support(support, nets, by_source))
    for support in cluster_supports(nets, by_source):
        if support in by_support:
            continue
        groups.extend(_groups_for_support(support, nets, by_source))
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
