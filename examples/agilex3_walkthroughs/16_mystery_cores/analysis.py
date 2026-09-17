#!/usr/bin/env python3
"""Walkthrough 16 -- the blind identification pass, and the reveal that scores it.

Two halves, and the separation between them is the walkthrough:

``blind``
    the six steps of ``spec.md`` section 3, run identically on each of the five
    anonymized exports in ``cores/``.  **No function reachable from ``blind``
    opens a path under ``ground_truth/``** -- ``check.py`` asserts that by
    reading this file's own source, which is crude and exactly the point.  The
    only thing a blind step is allowed to know about a core is the letter in its
    filename.
``reveal``
    reads ``ground_truth/MANIFEST.json``, compares each blind call and each
    ``hal_crypto identify`` verdict against the answer key, and writes the score
    table the guide quotes.

Usage, from the repository root::

    python3 examples/agilex3_walkthroughs/16_mystery_cores/analysis.py all \\
        -o examples/agilex3_walkthroughs/16_mystery_cores/artifacts

Subcommands: ``blind``, ``reveal``, ``all``.  Everything runs on a plain
Python 3 interpreter with ``tools/`` importable: the structural work is
``tools/hal_agilex`` and ``tools/hal_crypto``, both HAL-free by design.  There
is no ``hal`` step -- walkthroughs 11 and 13 already establish that a second
reader and a second graph algorithm agree with this one, and re-establishing it
five times would be repetition rather than evidence.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
TOOLS = os.path.join(REPO, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from hal_agilex import primitives, vo_netlist  # noqa: E402
from hal_crypto import arith, classify, findings as crypto_findings  # noqa: E402
from hal_crypto import shiftreg  # noqa: E402
from hal_crypto.netlist_model import (  # noqa: E402
    ConeTooWide,
    NetlistModel,
    UnsupportedCell,
)
from hal_findings import model as findings_model  # noqa: E402
from hal_findings.adapters.common import utc_now  # noqa: E402

#: The five cores, in the order the guide walks them.  The letters were fixed by
#: shuffling and carry no information -- see spec.md section 2.
CORES = ["core_a", "core_b", "core_c", "core_d", "core_e"]

CORES_DIR = os.path.join(HERE, "cores")
GROUND_TRUTH = os.path.join(HERE, "ground_truth")

PRODUCER = {
    "name": "agilex3_walkthroughs/16_mystery_cores",
    "version": "1.0.0",
}

# --- the thresholds spec.md section 4 fixes ---------------------------------

#: R2: how many disjoint five-register parity cells a sponge round has to show.
MIN_PARITY_CELLS = 8
#: R2/R4: the parity cells a sponge's theta layer is built from read this many.
PARITY_WIDTH = 5
#: R4: the narrowest carry chain that counts as a word-level adder.
MIN_ADDER_WIDTH = 8
#: R4: an ARX round ripples a carry, so it is deep.
MIN_ARX_DEPTH = 10
#: R5: the shortest feedback register that counts as a keystream generator.
MIN_STREAM_STAGES = 32
#: R3: how many instances of a substitution make a substitution *layer*.
MIN_SBOX_INSTANCES = 4
#: R3: the narrowest substitution worth calling an S-box.
MIN_SBOX_BITS = 4

_CONFIDENCE_ORDER = ["low", "medium", "high"]


def core_export(core):
    """The one file a blind step is allowed to open."""
    if core not in CORES:
        raise SystemExit("unknown core {!r}".format(core))
    return os.path.join(CORES_DIR, core + ".anon.hal.v")


def load(core):
    """Parse a blinded export and build the shared cone/evaluation model."""
    netlist = vo_netlist.parse_file(core_export(core))
    return netlist, NetlistModel(netlist)


# ---------------------------------------------------------------------------
# step 1 -- census
# ---------------------------------------------------------------------------


def step_census(netlist, model):
    """What is in the box, and what arithmetic it is wired for.

    The gate histogram and the port list are free.  The carry chains are not:
    a chain is the one object a vendor export contains unambiguously (a
    dedicated ``cout -> cin`` wire needs no heuristic), and ``arith.adders``
    goes one step further and *evaluates* each chain, so the difference between
    ``a + b`` and ``a + 1`` is measured rather than assumed.  That difference is
    what rule R4 turns on, and it is the only thing separating core_b's
    watchdog from core_d's round function at this level of detail.
    """
    histogram = {}
    for instance in netlist.instances:
        histogram[instance.type] = histogram.get(instance.type, 0) + 1

    chains = arith.chains(model)
    adders = []
    for entry in arith.adders(model):
        left = entry.get("left_sources") or []
        right = entry.get("right_sources") or []
        adders.append(
            {
                "operation": entry["operation"],
                "width": entry["width"],
                "left_sources": len(left),
                "right_sources": len(right),
                "left_registers": sum(
                    1 for key in left if model.register_of(key) is not None
                ),
                "right_registers": sum(
                    1 for key in right if model.register_of(key) is not None
                ),
                "constant": entry.get("constant"),
                "addend": entry.get("addend"),
                "exhaustive": entry.get("exhaustive"),
            }
        )
    adders.sort(key=lambda item: (item["operation"], -item["width"]))

    return {
        "design": netlist.name,
        "instances": len(netlist.instances),
        "gate_types": dict(sorted(histogram.items())),
        "flip_flops": len(model.ff_instances),
        "input_ports": {
            name: len(netlist.bits_of(name)) for name in sorted(netlist.ports("input"))
        },
        "output_ports": {
            name: len(netlist.bits_of(name)) for name in sorted(netlist.ports("output"))
        },
        "carry_chains": len(chains),
        "carry_chain_lengths": sorted((len(chain) for chain in chains), reverse=True),
        "adders": adders,
        # An adder over two operand *vectors*: both sides carry signals, neither
        # is a constant.  A counter is `a + 1` and lands in `constant_adders`
        # instead, which is the whole of the difference rule R4 turns on.
        "two_operand_adders": sorted(
            (
                entry["width"]
                for entry in adders
                if entry["operation"] == "add"
                and entry["left_sources"] >= MIN_ADDER_WIDTH
                and entry["right_sources"] >= 1
            ),
            reverse=True,
        ),
        "constant_adders": sorted(
            (
                entry["addend"]
                for entry in adders
                if entry["operation"] == "add_constant"
            )
        ),
        "subtracters": sorted(
            (entry["width"] for entry in adders if entry["operation"] == "subtract"),
            reverse=True,
        ),
    }


# ---------------------------------------------------------------------------
# step 2 -- shape
# ---------------------------------------------------------------------------


def _levels(model):
    """Combinational depth of every cell, with the feedback cut at the flops.

    The same computation ``hal_viz dag`` levels a drawing with, done here so
    that the number is available without a HAL build.  ``hal_viz`` counts the
    register-output rank as level 0, so its level count is this plus one.
    """
    depth = {}

    def level(key, stack):
        if key in depth:
            return depth[key]
        if model.is_source(key):
            depth[key] = 0
            return 0
        if key in stack:
            raise UnsupportedCell("combinational loop at {}".format(key))
        driver = model.driver(key)
        if driver is None:
            depth[key] = 0
            return 0
        instance, _pin = driver
        try:
            keys, _, _, _, _ = model._lut_inputs(instance)  # noqa: SLF001
        except UnsupportedCell:
            depth[key] = 0
            return 0
        best = 0
        for child in keys:
            best = max(best, level(child, stack | {key}))
        carry = instance.connections.get("cin")
        if carry:
            resolved = model.resolve(instance.single("cin"))
            if resolved[0] == "net":
                best = max(best, level(resolved[1], stack | {key}))
        depth[key] = best + 1
        return depth[key]

    histogram = {}
    for instance in model.lcells:
        best = 0
        for pin in primitives.LCELL_OUTPUT_PINS:
            for bit in instance.connections.get(pin) or ():
                key = getattr(bit, "key", None)
                if key is None:
                    continue
                best = max(best, level(key, frozenset()))
        histogram[best] = histogram.get(best, 0) + 1
    return histogram


def step_shape(netlist, model):
    """The column profile: how deep the logic between two registers is.

    Walkthroughs 11, 12 and 13 measured 18, 3 and 3 levels for an ARX round, an
    SPN round and a bit-serial stream cipher, and 15 measured 43 for a modular
    multiplier.  Deep and narrow versus wide and shallow is the cheapest
    discriminator in the whole method, and it costs one pass over the graph.
    """
    histogram = _levels(model)
    widest = max(histogram.items(), key=lambda item: (item[1], -item[0]))
    return {
        "combinational_levels": max(histogram),
        "dag_levels": max(histogram) + 1,
        "cells": sum(histogram.values()),
        "cells_per_level": {str(key): histogram[key] for key in sorted(histogram)},
        "widest_level": widest[0],
        "widest_level_cells": widest[1],
    }


# ---------------------------------------------------------------------------
# step 3 -- the state, partitioned
# ---------------------------------------------------------------------------


def _register_graph(model):
    """``{flip-flop: {flip-flops its next state reads}}``.

    Built from the *support* of the ``d`` cone, not from its truth table.  That
    matters: a cone 33 flip-flops wide cannot be enumerated and
    ``hal_crypto.shiftreg`` rightly refuses it, but its support is still exact
    and still tells you which registers feed which.  Half the state of core_a
    and most of core_e would be invisible here otherwise.
    """
    graph = {}
    for ff in model.ff_instances:
        bits = ff.connections.get("d")
        sources = set()
        if bits:
            try:
                support = model.support_of_bit(bits[0])
            except UnsupportedCell:
                support = frozenset()
            for key in support:
                other = model.register_of(key)
                if other is not None and other.name != ff.name:
                    sources.add(other.name)
        graph[ff.name] = sources
    return graph


def _external_only(model, ff):
    """True when this flip-flop's next state reads no register at all."""
    bits = ff.connections.get("d")
    if not bits:
        return False
    try:
        support = model.support_of_bit(bits[0])
    except UnsupportedCell:
        return False
    if not support:
        return False
    return all(model.register_of(key) is None for key in support)


def _components(graph):
    """Strongly connected components of *graph*, iteratively (the state is big)."""
    index, low, stack, on_stack, out = {}, {}, [], set(), []
    counter = [0]

    def strong(root):
        work = [(root, iter(sorted(graph.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, following = work[-1]
            descended = False
            for nxt in following:
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(graph.get(nxt, ())))))
                    descended = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if descended:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    top = stack.pop()
                    on_stack.discard(top)
                    component.append(top)
                    if top == node:
                        break
                out.append(sorted(component))

    for node in sorted(graph):
        if node not in index:
            strong(node)
    return out


def _single_predecessor_chains(graph):
    """Longest run of registers each of which reads exactly one other register.

    ``hal_crypto.shiftreg`` stops walking a chain whose stage has two
    successors, which is correct for its purpose and wrong for this one: a
    shift register whose stages are also *tapped* -- core_b's receive register
    feeds a capture register off every stage -- then disappears entirely.  Here
    the walk follows the predecessor instead, which fan-out cannot break.
    """
    single = {
        name: next(iter(sources))
        for name, sources in graph.items()
        if len(sources) == 1
    }
    successors = {}
    for name, source in single.items():
        successors.setdefault(source, []).append(name)

    best = []
    for start in sorted(graph):
        if start in single:
            continue  # not a head: something shifts into it
        # depth-first longest path through the single-predecessor forest
        stack = [(start, [start])]
        while stack:
            node, path = stack.pop()
            following = sorted(successors.get(node, ()))
            if not following:
                if len(path) > len(best):
                    best = path
                continue
            for nxt in following:
                if nxt in path:
                    if len(path) > len(best):
                        best = path
                    continue
                stack.append((nxt, path + [nxt]))
    return single, best


def step_state(netlist, model):
    """The register dependency graph, its components, and any shift structure.

    Three readings of the same graph, because they answer different questions
    and walkthrough 13 showed they can disagree without either being wrong:
    the components say how many machines there are and which drives which, the
    single-predecessor walk says whether any of them is a shift register, and
    ``hal_crypto.shiftreg`` says whether such a register closes onto itself with
    a feedback function -- possibly only once an external net is held.
    """
    graph = _register_graph(model)
    components = _components(graph)
    sizes = {}
    membership = {}
    for position, component in enumerate(components):
        sizes[position] = len(component)
        for name in component:
            membership[name] = position

    couplings = set()
    for name, sources in graph.items():
        for source in sources:
            if membership[source] != membership[name]:
                couplings.add((membership[source], membership[name]))
    cross = sorted(
        (
            {"from": sizes[a], "to": sizes[b]}
            for a, b in couplings
            if sizes[a] > 1 and sizes[b] > 1
        ),
        key=lambda entry: (-entry["from"], -entry["to"]),
    )

    single, longest = _single_predecessor_chains(graph)
    head_reads_input = False
    if longest:
        head = next(
            (ff for ff in model.ff_instances if ff.name == longest[0]), None
        )
        head_reads_input = bool(head is not None and _external_only(model, head))

    structures = []
    for entry in sorted(shiftreg.find_shift_structures(model), key=lambda e: e["chain"]):
        structures.append(
            {
                "chain": entry["chain"],
                "length": entry["length"],
                "kind": entry["kind"],
                "coupled": entry.get("coupled"),
                "coupled_chains": entry.get("coupled_chains"),
                "autonomous": entry.get("autonomous"),
                "mode": entry["mode"],
                "feedback_degree": entry.get("feedback_degree"),
                "feedback_anf": entry.get("feedback_anf"),
                "polynomial": entry.get("polynomial"),
            }
        )

    return {
        "registers": len(graph),
        "component_sizes": sorted(
            (len(component) for component in components if len(component) > 1),
            reverse=True,
        ),
        "isolated_registers": sum(1 for component in components if len(component) == 1),
        "one_way_couplings": cross,
        "single_predecessor_registers": len(single),
        "longest_single_predecessor_chain": len(longest),
        "chain_head_reads_only_inputs": head_reads_input,
        "shift_structures": structures,
        "autonomous_feedback_stages": sorted(
            (
                entry["length"]
                for entry in structures
                if entry["kind"] in ("lfsr", "nlfsr") and entry.get("autonomous")
            ),
            reverse=True,
        ),
    }


# ---------------------------------------------------------------------------
# step 4 -- nonlinearity
# ---------------------------------------------------------------------------


def step_nonlinearity(netlist, model):
    """How nonlinear the state update is, and whether a parity layer sits on it.

    Two censuses.  The first enumerates every next-state cone it can and reads
    the algebraic degree off the ANF; the cones it cannot enumerate are
    **counted, not ignored**, because "this register reads more of the state
    than can be enumerated" is itself the signature of a diffusion layer and is
    exactly what walkthrough 14 found on a Keccak permutation.

    The second looks for cells that are a pure XOR of *k* register outputs and
    nothing else.  Five is the interesting width: Keccak's theta computes the
    parity of each of five columns of five lanes, and walkthrough 14 recovered
    the whole 5 x 5 x 8 geometry starting from exactly that set of cells.  The
    disjointness test is the part that makes it a claim about a layer rather
    than about forty coincidences.
    """
    updates = shiftreg.register_updates(model)
    degrees = {}
    unreadable = 0
    linear_in_registers = 0
    for update in updates.values():
        if update.problem is not None or update.table is None:
            unreadable += 1
            continue
        terms = update.table.anf()
        degree = max((len(term) for term in terms), default=0)
        degrees[degree] = degrees.get(degree, 0) + 1
        if degree <= 1 and update.register_deps:
            linear_in_registers += 1

    widths = {}
    supports = {}
    for instance in model.lcells:
        for pin in primitives.LCELL_OUTPUT_PINS:
            for bit in instance.connections.get(pin) or ():
                key = getattr(bit, "key", None)
                if key is None:
                    continue
                try:
                    table = model.cone_of_bit(bit).restricted()
                except (UnsupportedCell, ConeTooWide):
                    continue
                if not table.inputs:
                    continue
                if any(model.register_of(src) is None for src in table.inputs):
                    continue
                terms = table.anf()
                if not terms or any(len(term) != 1 for term in terms):
                    continue
                width = len(terms)
                if width < 3:
                    continue
                widths[width] = widths.get(width, 0) + 1
                supports.setdefault(width, []).append(frozenset(table.inputs))

    disjoint = {}
    for width, sets in supports.items():
        claimed = set()
        count = 0
        for group in sets:
            if group & claimed:
                continue
            claimed |= group
            count += 1
        disjoint[width] = count

    return {
        "registers": len(updates),
        "next_state_degrees": {str(key): degrees[key] for key in sorted(degrees)},
        "unreadable_cones": unreadable,
        "registers_linear_in_registers": linear_in_registers,
        "max_readable_degree": max(degrees) if degrees else None,
        "pure_register_xor_cells": {str(key): widths[key] for key in sorted(widths)},
        "disjoint_register_xor_cells": {
            str(key): disjoint[key] for key in sorted(disjoint)
        },
        "parity_cells": disjoint.get(PARITY_WIDTH, 0),
    }


# ---------------------------------------------------------------------------
# step 5 -- what the tool says, recorded verbatim
# ---------------------------------------------------------------------------


def step_identify(netlist, model):
    """``hal_crypto identify``, in process, with the parts rules R1/R3 read.

    Recorded as *evidence given to the method*, not as the method's answer:
    steps 1-4 never look at it and step 6 weighs it against them.
    """
    evidence = classify.run_passes(netlist)
    decision = classify.verdict(evidence)

    sboxes = []
    for entry in evidence["sbox"]["sboxes"]:
        tiers = sorted({match["tier"] for match in entry["matches"]})
        sboxes.append(
            {
                "bits": entry.get("bits"),
                "instances": entry.get("instances", 1),
                "tiers": tiers,
                "names": sorted({match["name"] for match in entry["matches"]}),
            }
        )

    return {
        "family": decision["family"],
        "families_present": decision["families_present"],
        "style": decision["style"],
        "confidence": decision["confidence"],
        "evidence": decision["evidence"],
        "sboxes": sboxes,
        "ntt_verdict": evidence["ntt"]["verdict"],
        "ntt_butterflies": evidence["ntt"]["butterfly_count"],
        "recovered_moduli": sorted(evidence["ntt"].get("recovered_moduli") or []),
        "named_moduli": sorted(evidence["ntt"].get("named_moduli") or []),
        "arx_verdict": evidence["arx"]["verdict"],
        "arx_rotations": len(evidence["arx"].get("rotations") or []),
    }


# ---------------------------------------------------------------------------
# step 6 -- the call
# ---------------------------------------------------------------------------


def _rule_r1(census, shape, state, nonlinear, identify):
    """Lattice-style ring arithmetic.

    Both halves are reported when both hold, because they are independent: one
    is what the tool concluded, the other is a shape read off the chain census
    without asking it.  Core E is the case where the shape alone would also have
    fired **R4** -- a butterfly is an add and a subtract over two operands
    behind deep logic, which is an ARX round's census exactly -- so the order of
    the table, not the evidence, is what keeps a lattice kernel from being
    called a block cipher.  That is a real dependency and it is written down.
    """
    reasons = []
    if identify["family"] == "lattice-ntt":
        reasons.append(
            "hal_crypto identify reports lattice-ntt ({} butterfly/-ies, "
            "moduli {})".format(
                identify["ntt_butterflies"],
                identify["recovered_moduli"] or "none recovered",
            )
        )
    shared = sorted(set(census["subtracters"]) & set(census["two_operand_adders"]))
    if shared:
        reasons.append(
            "an add and a subtract of the same width ({}) each over two "
            "non-constant operand vectors".format(shared[0])
        )
    return "; ".join(reasons) if reasons else None


def _rule_r2(census, shape, state, nonlinear, identify):
    """A sponge permutation."""
    if census["carry_chains"]:
        return None
    if nonlinear["parity_cells"] < MIN_PARITY_CELLS:
        return None
    return (
        "no carry chain anywhere and {} pairwise-disjoint cells, each a pure XOR "
        "of exactly {} flip-flop outputs".format(
            nonlinear["parity_cells"], PARITY_WIDTH
        )
    )


def _rule_r3(census, shape, state, nonlinear, identify):
    """A substitution-permutation network."""
    for entry in identify["sboxes"]:
        wide_enough = (entry["bits"] or 0) >= MIN_SBOX_BITS
        many_enough = entry["instances"] >= MIN_SBOX_INSTANCES
        matched = any(tier in ("exact", "xor_constant") for tier in entry["tiers"])
        if wide_enough and many_enough and matched:
            return "{} instances of a {}-bit substitution, matched at tier {}".format(
                entry["instances"], entry["bits"], "/".join(entry["tiers"])
            )
    return None


def _rule_r4(census, shape, state, nonlinear, identify):
    """An add-rotate-xor round."""
    widths = [
        width for width in census["two_operand_adders"] if width >= MIN_ADDER_WIDTH
    ]
    repeated = sorted(
        {width for width in widths if widths.count(width) >= 2}, reverse=True
    )
    if not repeated:
        return None
    if shape["combinational_levels"] < MIN_ARX_DEPTH:
        return None
    return (
        "{} carry chains adding two register operands, {} bits wide, behind {} "
        "levels of combinational logic".format(
            widths.count(repeated[0]), repeated[0], shape["combinational_levels"]
        )
    )


def _rule_r5(census, shape, state, nonlinear, identify):
    """An autonomous feedback shift register."""
    qualifying = [
        entry
        for entry in state["shift_structures"]
        if entry["kind"] in ("lfsr", "nlfsr")
        and entry.get("autonomous")
        and entry["length"] >= MIN_STREAM_STAGES
        and (entry.get("feedback_degree") or 0) >= 1
    ]
    if not qualifying:
        return None
    mode = qualifying[0]["mode"]
    return "{} autonomous feedback register(s) of {} stages{}".format(
        len(qualifying),
        " + ".join(str(entry["length"]) for entry in qualifying),
        ""
        if mode is None
        else ", visible only while {} is held at {}".format(mode["net"], mode["value"]),
    )


#: ``(rule id, predicate, family, style)`` -- spec.md section 4, in order.
RULES = [
    ("R1-lattice", _rule_r1, "lattice-ntt", "pqc-style"),
    ("R2-sponge", _rule_r2, "sponge", "undetermined"),
    ("R3-spn", _rule_r3, "spn", "classical-style"),
    ("R4-arx", _rule_r4, "arx", "classical-style"),
    ("R5-stream", _rule_r5, "lfsr-stream", "classical-style"),
]


def step_call(census, shape, state, nonlinear, identify):
    """The blind verdict: the first rule that fires, and how sure of it to be.

    Confidence is the second fixed rule of spec.md section 4, and it has one
    clamp worth stating out loud: when the structural rule leaned on
    ``hal_crypto``'s own verdict, the call may not come out *more* confident
    than the tool it quoted.  Core E is where that bites -- the tool says
    ``medium`` because the modulus it recovered is in no published-parameter
    library, and a method that repeated the tool's answer with more confidence
    than the tool had would be manufacturing certainty.
    """
    fired = []
    for rule_id, predicate, family, style in RULES:
        reason = predicate(census, shape, state, nonlinear, identify)
        if reason is not None:
            fired.append({"rule": rule_id, "family": family, "style": style,
                          "reason": reason})

    if fired:
        chosen = fired[0]
        family = chosen["family"]
        style = chosen["style"]
        agrees = identify["family"] == family
        confidence = "high" if agrees else "medium"
        if agrees:
            # never more certain than the tool the rule quoted
            if _CONFIDENCE_ORDER.index(identify["confidence"]) < _CONFIDENCE_ORDER.index(
                confidence
            ):
                confidence = identify["confidence"]
    else:
        chosen = None
        family = "none-detected"
        # The same word `hal_crypto` uses, and for the same reason: nothing was
        # found, so there is nothing to place on the classical/PQC axis.  A
        # design with no cryptography in it has no position on that axis, which
        # is not the same as having one nobody could determine -- but the report
        # says so in the family field, and inventing a second vocabulary for it
        # would only make two verdicts that mean the same thing look different.
        style = "undetermined"
        # R6 with *no* cryptographic evidence anywhere is a strong negative; a
        # refused cone or an unexplained feedback register would weaken it.
        clean = (
            identify["family"] == "none-detected"
            and not identify["sboxes"]
            and not state["autonomous_feedback_stages"]
            and not nonlinear["parity_cells"]
        )
        confidence = "high" if clean else "medium"

    # A sponge is not placed on the classical/PQC axis at all, so it carries no
    # confidence number for the style -- walkthrough 14's point, kept here.
    style_confidence = None if style == "undetermined" else confidence

    return {
        "family": family,
        "style": style,
        "confidence": confidence,
        "style_confidence": style_confidence,
        "rules_fired": [entry["rule"] for entry in fired],
        "deciding_rule": chosen["rule"] if chosen else "R6-none",
        "reason": chosen["reason"] if chosen else "no rule fired",
        "all_reasons": fired,
        "agrees_with_identify": identify["family"] == family,
        "identify_family": identify["family"],
        "identify_style": identify["style"],
        "identify_confidence": identify["confidence"],
    }


BLIND_STEPS = [
    ("census", step_census),
    ("shape", step_shape),
    ("state", step_state),
    ("nonlinearity", step_nonlinearity),
    ("identify", step_identify),
]


def blind(core):
    """Every blind step for one core, in order, as one dict."""
    netlist, model = load(core)
    steps = {}
    for name, function in BLIND_STEPS:
        steps[name] = function(netlist, model)
    steps["call"] = step_call(
        steps["census"],
        steps["shape"],
        steps["state"],
        steps["nonlinearity"],
        steps["identify"],
    )
    return steps


# ---------------------------------------------------------------------------
# the blind findings document
# ---------------------------------------------------------------------------

BLIND_METHOD = findings_model.method(
    "blind structural identification",
    "structural",
    False,
    description=(
        "The six steps of examples/agilex3_walkthroughs/16_mystery_cores/spec.md "
        "section 3 over an anonymized Quartus export, followed by the fixed rule "
        "table of section 4. No behavioural simulation and no reference model: "
        "a reference model is a hypothesis about what the design is, and there "
        "is nothing to compare one against until the reveal."
    ),
)

BLINDING_ASSUMPTION = findings_model.assumption(
    "blinding",
    "The analysed file is the anonymised import: module, port, net and instance "
    "names are meaningless and internal vector declarations have been split into "
    "unrelated scalars, so no claim here can rest on a name or on a word "
    "boundary the vendor tool happened to keep.",
    kind="naming",
)

RULE_TABLE_ASSUMPTION = findings_model.assumption(
    "fixed-rule-table",
    "The family is decided by the rule table in spec.md section 4, fixed before "
    "these exports were analysed and calibrated on walkthroughs 11-15. A rule "
    "that fires is a structural observation, never an identification of an "
    "algorithm.",
    kind="tool",
)


def blind_document(core, steps, generated_at=None):
    """One findings document per core: the call, plus the steps behind it."""
    path = core_export(core)
    netlist = vo_netlist.parse_file(path)
    artifact = crypto_findings.artifact_for(netlist, path, artifact_id=core)
    call = steps["call"]
    scope = findings_model.scope([core], description="the whole blinded export")

    summary = (
        "Blind call: family {family}, style {style}, confidence {confidence}. "
        "Deciding rule {rule} -- {reason}. hal_crypto identify said {tool} "
        "({tool_confidence})."
    ).format(
        family=call["family"],
        style=call["style"],
        confidence=call["confidence"],
        rule=call["deciding_rule"],
        reason=call["reason"],
        tool=call["identify_family"],
        tool_confidence=call["identify_confidence"],
    )

    items = [
        findings_model.finding(
            "walkthrough16/{}/call".format(core),
            "Blind identification of {}: {}".format(core, call["family"]),
            findings_model.STATUS_HEURISTIC,
            BLIND_METHOD,
            scope,
            summary=summary,
            confidence=call["confidence"],
            assumptions=[BLINDING_ASSUMPTION, RULE_TABLE_ASSUMPTION],
            data={
                "call": call,
                "census": steps["census"],
                "shape": steps["shape"],
                "state": steps["state"],
                "nonlinearity": steps["nonlinearity"],
                "identify": steps["identify"],
            },
            tags=["blind", "crypto-identification", call["family"]],
        )
    ]
    return findings_model.document(
        PRODUCER,
        [artifact],
        {
            "entry_point": "16_mystery_cores/analysis.py:blind",
            "plugin": {"name": "agilex3_walkthroughs", "version": "1.0.0"},
        },
        items,
        generated_at=generated_at or utc_now(),
        notes=[
            "Produced without reading anything under ground_truth/. The letter "
            "in the artifact id is the only thing this document knows about the "
            "design it describes.",
        ],
    )


# ---------------------------------------------------------------------------
# the reveal
# ---------------------------------------------------------------------------


def manifest():
    with open(os.path.join(GROUND_TRUTH, "MANIFEST.json")) as handle:
        return json.load(handle)


def identify_named(core):
    """``hal_crypto identify`` on the *named* export -- the blinding control.

    Reveal only.  Running the same command on the export with its RTL names and
    vector declarations intact, and on the blinded one, measures what the
    blinding cost: the difference is a property of the passes, not of the
    designs, and until this walkthrough it had never been measured on more than
    one netlist at a time.
    """
    path = os.path.join(GROUND_TRUTH, "exports", core + ".vo")
    netlist = vo_netlist.parse_file(path)
    decision = classify.verdict(classify.run_passes(netlist))
    return {
        "family": decision["family"],
        "style": decision["style"],
        "confidence": decision["confidence"],
        "families_present": decision["families_present"],
    }


#: ``fact id -> predicate over the blind steps``.  The prose of each fact lives
#: in ground_truth/MANIFEST.json; what counts as having recovered it lives here,
#: and it may read only the blind steps.  A fact with no predicate is one the
#: blind pass does not attempt (spec.md section 5) and scores as not recovered.
FACT_CHECKS = {
    "a-state-200": lambda s: 200 in s["state"]["component_sizes"],
    "a-no-arithmetic": lambda s: s["census"]["carry_chains"] == 0,
    "a-parity-layer": lambda s: s["nonlinearity"]["parity_cells"] == 40,
    "a-nonlinear-layer": lambda s: s["nonlinearity"]["unreadable_cones"] >= 200,
    "b-not-cryptographic": lambda s: s["call"]["family"] == "none-detected",
    "b-absorbing-chain": lambda s: (
        s["state"]["longest_single_predecessor_chain"] >= 8
        and s["state"]["chain_head_reads_only_inputs"]
    ),
    "b-counter-chain": lambda s: s["census"]["constant_adders"] == [1]
    and s["census"]["carry_chain_lengths"][:1] == [16],
    "c-three-segments": lambda s: sorted(s["state"]["autonomous_feedback_stages"])
    == [84, 93, 111],
    "c-coupled": lambda s: all(
        entry.get("coupled") for entry in s["state"]["shift_structures"]
    )
    and bool(s["state"]["shift_structures"]),
    "c-nonlinear-feedback": lambda s: all(
        (entry.get("feedback_degree") or 0) == 2
        for entry in s["state"]["shift_structures"]
    )
    and bool(s["state"]["shift_structures"]),
    "c-held-mode": lambda s: any(
        entry["mode"] is not None for entry in s["state"]["shift_structures"]
    ),
    "d-two-adders": lambda s: s["census"]["two_operand_adders"].count(
        max(s["census"]["two_operand_adders"] or [0])
    )
    == 2,
    "d-key-schedule-split": lambda s: any(
        entry["from"] == 64 and entry["to"] == 32
        for entry in s["state"]["one_way_couplings"]
    ),
    "d-depth": lambda s: s["shape"]["combinational_levels"] >= MIN_ARX_DEPTH,
    "e-butterfly": lambda s: s["identify"]["ntt_butterflies"] >= 1,
    "e-modulus": lambda s: 257 in s["identify"]["recovered_moduli"],
}


def score(blind_steps, truth):
    """Score every blind call and every tool verdict against the answer key."""
    rows = []
    for core in CORES:
        steps = blind_steps[core]
        call = steps["call"]
        entry = truth["cores"][core]
        facts = []
        for fact in entry["key_facts"]:
            predicate = FACT_CHECKS.get(fact["id"])
            recovered = bool(predicate(steps)) if predicate else False
            facts.append(
                {
                    "id": fact["id"],
                    "statement": fact["statement"],
                    "attempted": predicate is not None,
                    "recovered": recovered,
                }
            )
        recovered = sum(1 for fact in facts if fact["recovered"])

        family_ok = call["family"] == entry["family"]
        style_ok = call["style"] == entry["style"]
        rows.append(
            {
                "core": core,
                "truth_family": entry["family"],
                "truth_style": entry["style"],
                "truth_design": entry["design"],
                "blind_family": call["family"],
                "blind_style": call["style"],
                "blind_confidence": call["confidence"],
                "deciding_rule": call["deciding_rule"],
                "tool_family": call["identify_family"],
                "tool_style": call["identify_style"],
                "tool_confidence": call["identify_confidence"],
                "blind_outcome": _outcome(family_ok, style_ok, entry, call["family"]),
                "tool_outcome": _outcome(
                    call["identify_family"] == entry["family"],
                    call["identify_style"] == entry["style"],
                    entry,
                    call["identify_family"],
                ),
                "facts_recovered": recovered,
                "facts_total": len(facts),
                "facts": facts,
            }
        )

    delta = []
    for core in CORES:
        named = identify_named(core)
        blinded = blind_steps[core]["identify"]
        delta.append(
            {
                "core": core,
                "named_family": named["family"],
                "named_style": named["style"],
                "named_confidence": named["confidence"],
                "blinded_family": blinded["family"],
                "blinded_style": blinded["style"],
                "blinded_confidence": blinded["confidence"],
                "changed": named["family"] != blinded["family"],
            }
        )

    return {
        "cores": rows,
        "blinding_delta": delta,
        "blinding_losses": [
            entry["core"] for entry in delta if entry["changed"]
        ],
        "tool_correct_named": sum(
            1
            for entry, row in zip(delta, rows)
            if entry["named_family"] == row["truth_family"]
            and entry["named_style"] == row["truth_style"]
        ),
        "blind_correct": sum(1 for row in rows if row["blind_outcome"] == "correct"),
        "tool_correct": sum(1 for row in rows if row["tool_outcome"] == "correct"),
        "total": len(rows),
        "blind_outcomes": _tally(row["blind_outcome"] for row in rows),
        "tool_outcomes": _tally(row["tool_outcome"] for row in rows),
        "facts_recovered": sum(row["facts_recovered"] for row in rows),
        "facts_total": sum(row["facts_total"] for row in rows),
        "rules_never_exercised": sorted(
            {rule_id for rule_id, _, _, _ in RULES}
            - {row["deciding_rule"] for row in rows}
        ),
    }


def _outcome(family_ok, style_ok, entry, claimed):
    """spec.md section 6: correct / partial / wrong / missed."""
    if family_ok and style_ok:
        return "correct"
    if family_ok:
        return "partial"
    if claimed == "none-detected" and entry["cryptographic"]:
        return "missed"
    if not entry["cryptographic"]:
        return "missed"  # a family claimed on the decoy: a false positive
    return "wrong"


def _tally(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------


def _write(directory, name, payload):
    if not os.path.isdir(directory):
        os.makedirs(directory)
    text = json.dumps(payload, indent=2, sort_keys=True)
    with open(os.path.join(directory, name), "w", newline="\n") as handle:
        handle.write(text + "\n")
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("step", choices=["blind", "reveal", "all"])
    parser.add_argument("--core", choices=CORES, help="with 'blind', one core only")
    parser.add_argument("-o", "--output", help="directory to write the documents into")
    args = parser.parse_args(argv)

    blind_steps = {}
    chosen = [args.core] if args.core else CORES

    if args.step in ("blind", "all"):
        for core in chosen:
            steps = blind(core)
            blind_steps[core] = steps
            print("== {} -- blind".format(core))
            print(json.dumps(steps["call"], indent=2, sort_keys=True))
            print()
            if args.output:
                _write(args.output, "step_{}.json".format(core), steps)
                _write(
                    args.output,
                    "blind_{}.findings.json".format(core),
                    blind_document(core, steps),
                )

    if args.step in ("reveal", "all"):
        for core in CORES:
            if core not in blind_steps:
                blind_steps[core] = blind(core)
        table = score(blind_steps, manifest())
        print("== reveal -- the score table")
        print(json.dumps(table, indent=2, sort_keys=True))
        if args.output:
            _write(args.output, "score.json", table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
