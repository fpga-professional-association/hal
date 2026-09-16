#!/usr/bin/env python3
"""The reverse-engineering steps of walkthrough 13, one function per step.

Every step reads *only* the exported netlist (``trivium_stream.vo``, or
``netlist.hal.v`` for the one HAL step).  Nothing in this file looks at
``design.v``, ``spec.md`` or the reference models: that is the whole point.
Where a step needs a constant -- a tap position, a warm-up length, a load
pattern -- it derives it and fails loudly rather than assuming it.

Usage, from the repository root::

    python3 examples/agilex3_walkthroughs/13_trivium_stream/analysis.py all \\
        -o examples/agilex3_walkthroughs/13_trivium_stream/artifacts

Subcommands: ``stats``, ``registers``, ``chains``, ``feedback``, ``output``,
``load``, ``warmup``, ``keystream``, ``hal``, ``all``.  Everything except
``hal`` runs on a plain Python 3 interpreter with ``tools/`` importable -- the
structural work is done by ``tools/hal_agilex`` and ``tools/hal_crypto``, both
of which are HAL-free by design.  ``hal`` loads the imported netlist through
``hal_py`` and is the *independent* confirmation: a second reader, a second
gate model and a plugin's own graph algorithm, agreeing (or not) with the first.

Two numbering schemes are in play and the code never mixes them silently.  The
netlist calls its flip-flops ``s[0] .. s[287]``; the Trivium specification
numbers the same bits ``s1 .. s288``.  Anything named ``*_spec`` in an output
document is 1-based; everything else is the netlist's own indexing.
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

EXPORT = os.path.join(HERE, "trivium_stream.vo")
NETLIST = os.path.join(HERE, "netlist.hal.v")
GATE_LIBRARY = os.path.join(
    REPO, "plugins", "gate_libraries", "definitions", "AGILEX_TENNM.hgl"
)

from hal_agilex import vo_netlist  # noqa: E402
from hal_agilex.simulate import Simulator  # noqa: E402
from hal_crypto import boolfunc, shiftreg  # noqa: E402
from hal_crypto.netlist_model import ConeTooWide, NetlistModel, UnsupportedCell  # noqa: E402

#: The three published eSTREAM/ECRYPT Trivium vectors this walkthrough uses.
#: Literature, not netlist: they are the *independent* behavioural check, and
#: nothing structural in this file depends on them.
TEST_VECTORS = [
    ("Set 1, #0", "80000000000000000000", "00000000000000000000", "38EB86FF730D7A9C"),
    ("Set 2, #0", "00000000000000000000", "00000000000000000000", "FBE0BF265859051B"),
    ("Set 3, #0", "00010203040506070809", "00000000000000000000", "D2A8740BBA6FD906"),
]

#: The vector the guide drives the netlist with: key and IV are both zero, so
#: the byte-to-bit conventions below cannot influence the result at all.
HEADLINE_VECTOR = 1


def port_word(hexadecimal):
    """The 80-bit port value for a published 20-hex key or IV string.

    The published file prints byte strings; a port is a bit vector, and the two
    conventions have to be stated.  ``K1`` is the most significant bit of the
    **last** printed byte and ``K80`` the least significant bit of the first, so
    the port word is the printed string read back to front, bit by bit.  This
    function is the whole of that convention; ``spec.md`` states it in prose and
    ``check.py`` requires all three vectors to come back byte for byte, which is
    what makes it a derivation rather than a guess.
    """
    word = 0
    position = 0
    for byte in reversed(bytes.fromhex(hexadecimal)):
        for offset in range(8):
            if (byte >> (7 - offset)) & 1:
                word |= 1 << position
            position += 1
    return word


def keystream_hex(bits):
    """Published keystream bytes from a list of ``z1, z2, ...``.

    ``z1`` is the least significant bit of the first byte.
    """
    out = bytearray()
    for start in range(0, len(bits) - 7, 8):
        value = 0
        for offset in range(8):
            value |= bits[start + offset] << offset
        out.append(value)
    return bytes(out).hex().upper()


def load(path=EXPORT):
    """Parse the export and build the shared cone/evaluation model."""
    netlist = vo_netlist.parse_file(path)
    return netlist, NetlistModel(netlist)


def _split(key):
    if key.endswith("]") and "[" in key:
        name, _, index = key[:-1].partition("[")
        try:
            return name, int(index)
        except ValueError:
            return key, None
    return key, None


def _cofactor(table, assignment):
    """*table* with every named input held at its value, then restricted."""
    for name, value in assignment.items():
        if name in table.inputs:
            table = table.cofactor(table.inputs.index(name), value)
    return table.restricted()


def _d_cone(model, ff):
    bits = ff.connections.get("d")
    if not bits:
        return None
    try:
        return model.cone_of_bit(bits[0]).restricted()
    except (UnsupportedCell, ConeTooWide):
        return None


# ---------------------------------------------------------------------------
# step 1 -- first contact
# ---------------------------------------------------------------------------


def step_stats(netlist, model):
    """What is in the box: primitive census and the boundary."""
    return {
        "design_name": netlist.name,
        "instances": len(netlist.instances),
        "gate_types": dict(sorted(netlist.type_histogram().items())),
        "inputs": [
            {"name": name, "width": netlist.width(name)}
            for name in netlist.ports("input")
        ],
        "outputs": [
            {"name": name, "width": netlist.width(name)}
            for name in netlist.ports("output")
        ],
        "declared_vectors": {
            name: netlist.width(name)
            for name, entry in sorted(netlist.declarations.items())
            if entry[1] is not None
        },
    }


# ---------------------------------------------------------------------------
# step 2 -- the register banks
# ---------------------------------------------------------------------------


def _control(model, ff, pin):
    bits = ff.connections.get(pin)
    if not bits:
        return None
    resolved = model.resolve(bits[0])
    if resolved[0] == "const":
        return str(resolved[1])
    return model.peel(resolved[1])[0]


def step_registers(netlist, model):
    """Group the flip-flops by (clock, async clear, enable).

    The cheapest structural clustering there is.  Here it says almost nothing --
    301 registers, one bucket -- and that *is* the finding: this design has no
    enables, so the usual "registers that start and stop together" split has no
    purchase and the structure has to come from the wiring instead.
    """
    buckets = {}
    banks = {}
    for ff in model.ff_instances:
        key = (
            _control(model, ff, "clk"),
            _control(model, ff, "clrn"),
            _control(model, ff, "ena"),
        )
        buckets.setdefault(key, []).append(ff.name)
        bits = ff.connections.get("q")
        if bits:
            name, index = _split(getattr(bits[0], "key", ""))
            banks.setdefault(name, set()).add(index)

    return {
        "flip_flops": len(model.ff_instances),
        "enable_groups": [
            {"clk": key[0], "clrn": key[1], "ena": key[2], "size": len(names)}
            for key, names in sorted(buckets.items(), key=lambda kv: -len(kv[1]))
        ],
        "q_vectors": {
            name: len(indices) for name, indices in sorted(banks.items())
        },
    }


# ---------------------------------------------------------------------------
# step 3 -- the chains, and the multiplexer that hides them
# ---------------------------------------------------------------------------


def step_chains(netlist, model):
    """Shift chains, and what it takes to see them.

    Read with nothing held, **not one** of the 301 registers is driven by
    exactly one other register: every stage's next state is a load multiplexer.
    So the pass first ranks the external nets by how many next-state functions
    read them nonlinearly, holds the winner, and looks again.  Both numbers are
    reported here, because "there are three shift registers" and "there are
    three shift registers while ``start`` is low" are different claims.
    """
    updates = shiftreg.register_updates(model)
    plain_links = sorted(
        name for name, update in updates.items() if update.single_register_link()
    )
    candidates = shiftreg.mode_candidates(updates)
    structures = shiftreg.find_shift_structures(model)
    described = []
    for entry in sorted(structures, key=lambda item: item["chain"]):
        described.append(
            {
                "chain": entry["chain"],
                "head": entry["head"],
                "tail": entry["tail"],
                "length": entry["length"],
                "kind": entry["kind"],
                "coupled": entry["coupled"],
                "coupled_chains": entry.get("coupled_chains"),
                "mode": entry["mode"],
                "autonomous": entry.get("autonomous"),
                "own_taps": entry.get("taps"),
                "feedback_anf": entry.get("feedback_anf"),
                "feedback_degree": entry.get("feedback_degree"),
            }
        )
    return {
        "registers": len(updates),
        "plain_shift_links_without_holding_anything": len(plain_links),
        "mode_candidates": candidates,
        "chains": len(described),
        "total_stages": sum(entry["length"] for entry in described),
        "segments": described,
    }


# ---------------------------------------------------------------------------
# step 4 -- the feedback functions, in the cipher's own numbering
# ---------------------------------------------------------------------------


def _base_of(head):
    """A chain head named ``s[i]`` starts at netlist index *i*."""
    name, index = _split(head)
    if index is None:
        raise SystemExit("chain head {!r} is not a vector bit".format(head))
    return name, index


def step_feedback(netlist, model):
    """Each segment's head cone, enumerated and written out as an equation.

    The cone of a segment head is five register bits wide, so it is enumerated
    exhaustively -- 32 rows -- and the algebraic normal form that comes back is
    the feedback, not a fit.  Exactly one term of each is a *product*, and that
    product is the entire difference between this design and walkthrough 05's
    LFSR.
    """
    structures = {
        entry["chain"]: entry
        for entry in shiftreg.find_shift_structures(model)
    }
    if not structures:
        raise SystemExit("no shift structure found; step 3 must run first")
    bases = {}
    for number, entry in structures.items():
        _name, base = _base_of(entry["head"])
        bases[number] = base

    segments = []
    for number in sorted(structures):
        entry = structures[number]
        table = None
        head_ff = next(
            ff for ff in model.ff_instances if ff.name == entry["head"]
        )
        table = _cofactor(
            _d_cone(model, head_ff), {entry["mode"]["net"]: entry["mode"]["value"]}
        )
        linear = []
        products = []
        for term in table.anf():
            if len(term) == 1:
                linear.append(term[0])
            elif len(term) >= 2:
                products.append(list(term))
        segments.append(
            {
                "chain": number,
                "head": entry["head"],
                "base": bases[number],
                "length": entry["length"],
                "sources": list(table.inputs),
                "sources_spec": sorted(
                    _split(key)[1] + 1 for key in table.inputs
                ),
                "linear_terms": sorted(linear),
                "product_terms": products,
                "algebraic_degree": table.algebraic_degree(),
                "constant_term": 1 if () in table.anf() else 0,
                "equation_spec": _equation(table),
            }
        )
    widths = [entry["length"] for entry in segments]
    return {
        "segments": segments,
        "segment_lengths": widths,
        "state_bits": sum(widths),
        "and_terms": sum(len(entry["product_terms"]) for entry in segments),
        "degrees": sorted({entry["algebraic_degree"] for entry in segments}),
    }


def _equation(table):
    """The ANF written in the cipher's 1-based numbering."""
    pieces = []
    for term in table.anf():
        if not term:
            pieces.append("1")
            continue
        names = ["s{}".format(_split(key)[1] + 1) for key in sorted(term)]
        pieces.append(" * ".join(names) if len(names) > 1 else names[0])
    return " + ".join(pieces)


# ---------------------------------------------------------------------------
# step 5 -- the output function
# ---------------------------------------------------------------------------


def step_output(netlist, model):
    """The cell that drives ``ks``, and what it has to do with the feedbacks.

    Six state bits, one ALM, a pure XOR.  The interesting part is *which* six:
    they are exactly the linear taps the three feedback functions share -- each
    segment's own output pair -- and none of the AND terms.  A generator whose
    output function is the linear part of its feedback, with the nonlinearity
    left out, is the Trivium construction in one sentence.
    """
    bits = netlist.bits_of("ks")
    if len(bits) != 1:
        raise SystemExit("ks is not a one-bit port")
    table = model.cone_of_bit(bits[0]).restricted()
    affine = table.linear_terms()
    sources = sorted(_split(key)[1] for key in table.inputs)

    feedback = step_feedback(netlist, model)
    linear_union = set()
    product_union = set()
    for entry in feedback["segments"]:
        linear_union.update(_split(key)[1] for key in entry["linear_terms"])
        for term in entry["product_terms"]:
            product_union.update(_split(key)[1] for key in term)

    return {
        "cell_inputs": list(table.inputs),
        "arity": table.arity,
        "is_pure_xor": affine is not None and len(affine[1]) == table.arity,
        "constant": affine[0] if affine else None,
        "sources": sources,
        "sources_spec": [index + 1 for index in sources],
        "also_a_feedback_tap": sorted(set(sources) & linear_union),
        "not_a_feedback_tap": sorted(set(sources) - linear_union),
        "reads_any_and_term_input": sorted(set(sources) & product_union),
    }


# ---------------------------------------------------------------------------
# step 6 -- the load pattern
# ---------------------------------------------------------------------------


def step_load(netlist, model):
    """What each state bit is set to when the load multiplexer selects load.

    The select is found, not assumed: for one ordinary stage the pass enumerates
    every assignment of the cone's control inputs and keeps the one under which
    the next state stops depending on the stage before it.  That assignment is
    "load", and applying it to all 288 stages reads the initial state straight
    out of the wiring.
    """
    stages = sorted(
        (ff for ff in model.ff_instances if _split(ff.name)[0] == "s"),
        key=lambda ff: _split(ff.name)[1],
    )
    if not stages:
        raise SystemExit("no s[...] flip-flops in the export")

    mode = _find_load_mode(model, stages)
    pattern = []
    for ff in stages:
        index = _split(ff.name)[1]
        table = _cofactor(_d_cone(model, ff), mode)
        if table.arity == 0:
            pattern.append((index, "constant", table.values[0], None))
            continue
        if table.arity == 1 and table.linear_terms() == (0, list(table.inputs)):
            name, bit = _split(table.inputs[0])
            pattern.append((index, name, None, bit))
            continue
        raise SystemExit(
            "stage {} does not reduce to a constant or a port bit under {}: {}".format(
                index, mode, boolfunc.anf_string(table)
            )
        )

    from_port = {}
    constants = {0: [], 1: []}
    for index, kind, value, bit in pattern:
        if kind == "constant":
            constants[value].append(index)
        else:
            from_port.setdefault(kind, []).append((index, bit))

    fields = []
    for name, pairs in sorted(from_port.items()):
        pairs.sort()
        offsets = {index - bit for index, bit in pairs}
        fields.append(
            {
                "port": name,
                "bits": len(pairs),
                "state_range": [pairs[0][0], pairs[-1][0]],
                "port_range": [min(bit for _i, bit in pairs), max(bit for _i, bit in pairs)],
                "identity_offset": sorted(offsets)[0] if len(offsets) == 1 else None,
            }
        )
    return {
        "load_mode": mode,
        "stages": len(stages),
        "fields": fields,
        "ones_at": constants[1],
        "zero_bits": len(constants[0]),
        "zero_runs": _runs(constants[0]),
        "ones_at_spec": [index + 1 for index in constants[1]],
    }


def _find_load_mode(model, stages):
    """The control assignment under which a stage stops reading its predecessor."""
    for ff in stages:
        index = _split(ff.name)[1]
        table = _d_cone(model, ff)
        if table is None:
            continue
        shift_source = "s[{}]".format(index - 1)
        if shift_source not in table.inputs:
            continue  # a segment head: its cone reads the feedback, not one stage
        controls = [
            key
            for key in table.inputs
            if key != shift_source and _split(key)[0] not in ("key", "iv")
        ]
        if not controls or len(controls) > 4:
            continue
        for pattern in range(1 << len(controls)):
            assignment = {
                name: (pattern >> position) & 1
                for position, name in enumerate(controls)
            }
            reduced = _cofactor(table, assignment)
            if shift_source not in reduced.inputs:
                return assignment
    raise SystemExit("no assignment of the control nets selects a load")


def _runs(indices):
    runs = []
    for index in sorted(indices):
        if runs and runs[-1][1] + 1 == index:
            runs[-1][1] = index
        else:
            runs.append([index, index])
    return [{"from": start, "to": end, "length": end - start + 1} for start, end in runs]


# ---------------------------------------------------------------------------
# step 7 -- the warm-up length
# ---------------------------------------------------------------------------


def step_warmup(netlist, model):
    """Two terminal-count cells, two moduli, one product.

    A cell whose cone reads nothing but the bits of one register vector and is
    true for exactly one of them is a terminal-count test, and the value it
    fires on plus one is that counter's modulus.  There are two of them here --
    six bits and five bits -- because 1152 needs eleven and an ALM inside the
    validated coverage reads six.
    """
    counters = []
    for instance in model.lcells:
        bits = instance.connections.get("combout")
        if not bits:
            continue
        try:
            table = model.cone_of_bit(bits[0]).restricted()
        except (UnsupportedCell, ConeTooWide):
            continue
        if table.arity < 2:
            continue
        vectors = {_split(key)[0] for key in table.inputs}
        indices = [_split(key)[1] for key in table.inputs]
        if len(vectors) != 1 or None in indices:
            continue
        vector = sorted(vectors)[0]
        width = sum(
            1
            for ff in model.ff_instances
            if _split(ff.name)[0] == vector and _split(ff.name)[1] is not None
        )
        if table.arity != width:
            continue
        hits = [index for index, value in enumerate(table.values) if value]
        if len(hits) != 1:
            continue
        value = 0
        for position, key in enumerate(table.inputs):
            if (hits[0] >> position) & 1:
                value |= 1 << _split(key)[1]
        counters.append(
            {
                "cell": instance.name,
                "counter": vector,
                "width": width,
                "fires_on": value,
                "modulus": value + 1,
            }
        )
    counters.sort(key=lambda entry: -entry["width"])
    product = 1
    for entry in counters:
        product *= entry["modulus"]
    state_bits = sum(
        1
        for ff in model.ff_instances
        if _split(ff.name)[0] == "s" and _split(ff.name)[1] is not None
    )
    return {
        "terminal_count_cells": counters,
        "moduli": [entry["modulus"] for entry in counters],
        "warmup_steps": product,
        "state_bits": state_bits,
        "steps_per_state_bit": (
            product // state_bits if state_bits and product % state_bits == 0 else None
        ),
    }


# ---------------------------------------------------------------------------
# step 8 -- drive the black box
# ---------------------------------------------------------------------------


def _run_vector(netlist, key, iv, bits=64, limit=4000):
    """Load key/iv, clock until ``ks_valid``, then collect *bits* of keystream."""
    simulator = Simulator(netlist)
    simulator.reset()
    for name, value in (("start", 0), ("key", 0), ("iv", 0), ("rst_n", 0)):
        simulator.set_input(name, value)
    simulator.apply_async_clear()
    simulator.set_input("rst_n", 1)

    simulator.set_input("start", 1)
    simulator.set_input("key", key)
    simulator.set_input("iv", iv)
    simulator.clock()
    simulator.set_input("start", 0)
    simulator.set_input("key", 0)
    simulator.set_input("iv", 0)

    cycles = 1
    busy = 0
    while not simulator.get_output("ks_valid"):
        busy += simulator.get_output("busy")
        simulator.clock()
        cycles += 1
        if cycles > limit:
            raise SystemExit("ks_valid never rose within {} cycles".format(limit))

    stream = []
    for _ in range(bits):
        stream.append(simulator.get_output("ks"))
        simulator.clock()
    return cycles, busy, stream


def step_keystream(netlist, model):
    """Run the netlist itself on the published eSTREAM vectors.

    Structure said "three coupled NLFSRs, these taps, 1152 warm-up steps".  This
    step never reads that: it drives the exported netlist as a black box with
    vectors out of the literature and looks at what comes out.  Two independent
    methods agreeing is the evidence; either one alone is a hypothesis.
    """
    results = []
    for label, key_hex, iv_hex, expected in TEST_VECTORS:
        cycles, busy, stream = _run_vector(
            netlist, port_word(key_hex), port_word(iv_hex)
        )
        got = keystream_hex(stream)
        results.append(
            {
                "vector": label,
                "key": key_hex,
                "iv": iv_hex,
                "cycles_from_accepted_start_to_valid": cycles,
                "warmup_steps": busy,
                "keystream": got,
                "published": expected,
                "matches_published_vector": got == expected,
            }
        )
    return {
        "vectors": results,
        "all_match": all(entry["matches_published_vector"] for entry in results),
        "warmup_steps": sorted({entry["warmup_steps"] for entry in results}),
        "headline": results[HEADLINE_VECTOR],
    }


# ---------------------------------------------------------------------------
# step 9 -- the same netlist, read by HAL
# ---------------------------------------------------------------------------


def step_hal(netlist, model):
    """Load ``netlist.hal.v`` through hal_py and re-derive the census + SCCs.

    A second reader and a second gate model.  The SCC decomposition is the
    interesting disagreement: it sees **one** machine, because the three
    segments feed each other in a ring and everything in a ring is mutually
    reachable.  Only the chain walk of step 3 can cut that one component into
    93 + 84 + 111.  Both are right; they answer different questions.
    """
    for entry in os.environ.get("HAL_PY_PATH", "").split(os.pathsep):
        if entry and entry not in sys.path:
            sys.path.insert(0, entry)
    import hal_py

    hal_py.plugin_manager.load_all_plugins()
    loaded = hal_py.NetlistFactory.load_netlist(NETLIST, GATE_LIBRARY)
    if loaded is None:
        raise SystemExit("could not load {}".format(NETLIST))

    from hal_agilex import hal_adapter

    report = hal_adapter.elaborate(hal_py, loaded)

    histogram = {}
    for gate in loaded.get_gates():
        name = gate.get_type().get_name()
        histogram[name] = histogram.get(name, 0) + 1

    graph_algorithm = __import__(
        "hal_plugins.graph_algorithm", fromlist=["graph_algorithm"]
    )
    graph = graph_algorithm.NetlistGraph.from_netlist(loaded)
    if graph is None:
        raise SystemExit("NetlistGraph.from_netlist returned None")
    components = graph_algorithm.get_connected_components(graph, True, 2)
    if components is None:
        raise SystemExit("graph_algorithm.get_connected_components failed")

    described = []
    for vertices in components:
        gates = graph.get_gates_from_vertices(list(vertices))
        types = {}
        banks = {}
        for gate in gates:
            key = gate.get_type().get_name()
            types[key] = types.get(key, 0) + 1
            if key != "tennm_ff":
                continue
            base, _index = _split(gate.get_name())
            banks[base] = banks.get(base, 0) + 1
        described.append(
            {
                "size": len(gates),
                "gate_types": dict(sorted(types.items())),
                "registers": dict(sorted(banks.items())),
            }
        )
    described.sort(key=lambda entry: -entry["size"])

    return {
        "gate_library": loaded.get_gate_library().get_name(),
        "gates": len(loaded.get_gates()),
        "nets": len(loaded.get_nets()),
        "modules": len(loaded.get_modules()),
        "gate_types": dict(sorted(histogram.items())),
        "elaborated": report["elaborated"],
        "refused": report["refused"],
        "vertices": graph.get_num_vertices(),
        "edges": graph.get_num_edges(),
        "components": described,
        "largest_component": described[0] if described else None,
    }


STEPS = [
    ("stats", step_stats),
    ("registers", step_registers),
    ("chains", step_chains),
    ("feedback", step_feedback),
    ("output", step_output),
    ("load", step_load),
    ("warmup", step_warmup),
    ("keystream", step_keystream),
]

HAL_STEPS = [("hal", step_hal)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "step",
        choices=[name for name, _ in STEPS + HAL_STEPS] + ["all"],
        help="which reverse-engineering step to run",
    )
    parser.add_argument("-o", "--output", help="directory to write step_<name>.json into")
    parser.add_argument(
        "--with-hal",
        action="store_true",
        help="with 'all', also run the hal_py step (needs a built HAL)",
    )
    parser.add_argument("--export", default=EXPORT)
    args = parser.parse_args(argv)

    netlist, model = load(args.export)

    if args.step == "all":
        chosen = list(STEPS) + (HAL_STEPS if args.with_hal else [])
    else:
        chosen = [item for item in STEPS + HAL_STEPS if item[0] == args.step]

    for name, function in chosen:
        result = function(netlist, model)
        text = json.dumps(result, indent=2, sort_keys=True)
        print("== {}".format(name))
        print(text)
        print()
        if args.output:
            if not os.path.isdir(args.output):
                os.makedirs(args.output)
            path = os.path.join(args.output, "step_{}.json".format(name))
            with open(path, "w", newline="\n") as handle:
                handle.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
