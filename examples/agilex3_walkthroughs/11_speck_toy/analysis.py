#!/usr/bin/env python3
"""The reverse-engineering steps of walkthrough 11, one function per step.

Every step reads *only* the exported netlist (``speck_toy.vo``, or
``netlist.hal.v`` for the one HAL step).  Nothing in this file looks at
``design.v``, ``spec.md`` or the reference models: that is the whole point.
Where a step needs a constant -- a rotation amount, a round count -- it derives
it and fails loudly rather than assuming it.

Usage, from the repository root::

    python3 examples/agilex3_walkthroughs/11_speck_toy/analysis.py all \\
        -o examples/agilex3_walkthroughs/11_speck_toy/artifacts

Subcommands: ``stats``, ``registers``, ``chains``, ``banks``, ``xor``,
``encrypt``, ``round``, ``hal``, ``all``.  Everything except ``hal`` runs on a
plain Python 3 interpreter with ``tools/`` importable -- the structural work is
done by ``tools/hal_agilex`` and ``tools/hal_crypto``, both of which are
HAL-free by design.  ``hal`` loads the imported netlist through ``hal_py`` and
is the *independent* confirmation: a second reader, a second gate model and a
plugin's own graph algorithm, agreeing (or not) with the first.
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

EXPORT = os.path.join(HERE, "speck_toy.vo")
NETLIST = os.path.join(HERE, "netlist.hal.v")
GATE_LIBRARY = os.path.join(
    REPO, "plugins", "gate_libraries", "definitions", "AGILEX_TENNM.hgl"
)

from hal_agilex import vo_netlist  # noqa: E402
from hal_agilex.simulate import Simulator  # noqa: E402
from hal_crypto import arith, arx, permutation  # noqa: E402
from hal_crypto.boolfunc import TruthTable  # noqa: E402
from hal_crypto.netlist_model import ConeTooWide, NetlistModel, UnsupportedCell  # noqa: E402


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

    The cheapest structural clustering there is, and it needs no analysis at
    all: registers that start, stop and clear together are candidates for being
    one architectural object.  It is necessary, never sufficient -- everything
    on one enable is one bucket whatever it means.
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
            if index is not None:
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
# step 3 -- the carry chains, and the first rotation
# ---------------------------------------------------------------------------


def step_chains(netlist, model):
    """Every dedicated ``cout -> cin`` chain, verified as arithmetic.

    A carry chain is unambiguous -- the connection is dedicated silicon, not a
    heuristic -- and ``hal_crypto.arith`` does not stop at the pattern: it
    evaluates each chain against ``a + b + cin`` before calling it an adder.
    The interesting part for this design is *which register bit lands on which
    slice*: that ordering is the rotation, and it is the only place the
    rotation exists.
    """
    entries = []
    for entry in arith.adders(model):
        operands = {}
        for side, label in (("left_sources", "first"), ("right_sources", "second")):
            sources = entry.get(side)
            if not sources:
                continue
            names = {_split(key)[0] for key in sources}
            width = None
            if len(names) == 1:
                width = permutation.vector_width(netlist, next(iter(names)))
            operands[label] = {
                "sources": sources,
                "vector": sorted(names)[0] if len(names) == 1 else None,
                "declared_width": width,
                "rotation": permutation.vector_rotation(sources, width=width),
            }
        entries.append(
            {
                "cells": entry["cells"],
                "operation": entry["operation"],
                "verified_bits": entry["width"],
                "carry_in": entry["carry_in"],
                "checked_vectors": entry["checked"],
                "exhaustive": entry["exhaustive"],
                "operands": operands,
            }
        )
    entries.sort(key=lambda item: item["cells"][0])
    return {"chains": len(arith.chains(model)), "adders": entries}


# ---------------------------------------------------------------------------
# step 4 -- the second rotation, in the next-state fan-in
# ---------------------------------------------------------------------------


def step_banks(netlist, model):
    """Which single bit of its own bank each register bit's next state reads.

    The second rotation of an ARX round never reaches an adder, so step 3
    cannot see it.  It is still in the wiring: bit *i* of the bank is XORed
    with bit *(i - beta) mod w* of the same bank, so bit *i*'s next-state cell
    reads exactly one bank bit and the index map is the rotation.
    """
    return {"rotations": permutation.register_bank_rotations(model)}


# ---------------------------------------------------------------------------
# step 5 -- the XOR layer, which is not made of XOR cells
# ---------------------------------------------------------------------------


def step_xor(netlist, model):
    """The XOR layer, after undoing the multiplexer it was packed with.

    Not one cell in this export is a standalone XOR: every one of them also
    carries the load multiplexer, because ``load ? port : (a ^ b)`` is four
    inputs and an ALM takes six.  Holding the select input at the value that
    selects the round result recovers the XOR exactly.
    """
    cells = arx.xor_nets(model)
    standalone = [entry for entry in cells if not entry["conditional"]]
    conditional = [entry for entry in cells if entry["conditional"]]
    cofactors = {}
    for entry in conditional:
        key = "{}={}".format(entry["cofactor"]["net"], entry["cofactor"]["value"])
        cofactors[key] = cofactors.get(key, 0) + 1
    arities = {}
    for entry in cells:
        arities[entry["arity"]] = arities.get(entry["arity"], 0) + 1
    return {
        "xor_cells": len(cells),
        "standalone": len(standalone),
        "conditional": len(conditional),
        "cofactors": dict(sorted(cofactors.items(), key=lambda kv: -kv[1])),
        "arities": dict(sorted(arities.items())),
        "examples": [
            {
                "cell": entry["cell"],
                "inputs": entry["inputs"],
                "cofactor": entry.get("cofactor"),
            }
            for entry in cells[:4]
        ],
    }


# ---------------------------------------------------------------------------
# step 6 -- drive the black box
# ---------------------------------------------------------------------------

#: The one published Speck32/64 vector (Beaulieu et al. 2013, appendix C).
#: Nothing structural depends on it; it is the independent behavioural check.
TEST_KEY = (0x1918 << 48) | (0x1110 << 32) | (0x0908 << 16) | 0x0100
TEST_PLAINTEXT = (0x6574 << 16) | 0x694C


def _run_once(netlist, plaintext, key, limit=64):
    """Assert start, clock until ``done``, return the trace."""
    simulator = Simulator(netlist)
    simulator.reset()
    for name, value in (("start", 0), ("pt", 0), ("key", 0), ("rst_n", 0)):
        simulator.set_input(name, value)
    simulator.apply_async_clear()
    simulator.set_input("rst_n", 1)

    simulator.set_input("start", 1)
    simulator.set_input("pt", plaintext)
    simulator.set_input("key", key)
    simulator.clock()
    simulator.set_input("start", 0)
    simulator.set_input("pt", 0)
    simulator.set_input("key", 0)

    trace = []
    cycles = 0
    while cycles < limit:
        trace.append(
            {
                "cycle": cycles,
                "round_counter": simulator.get_output("rnd"),
                "x": simulator.get_output("x"),
                "y": simulator.get_output("y"),
                "busy": simulator.get_output("busy"),
                "done": simulator.get_output("done"),
            }
        )
        if simulator.get_output("done"):
            break
        simulator.clock()
        cycles += 1
    return cycles, trace, simulator.get_output("ct")


def step_encrypt(netlist, model):
    """Run the netlist itself on the published Speck32/64 test vector.

    Structure said "ARX round, alpha 7, beta 2, 22 rounds".  This step never
    reads that: it drives the exported netlist as a black box with the vector
    from the SPECK paper and looks at what comes out.  Two independent methods
    agreeing is the evidence; either one alone is a hypothesis.
    """
    cycles, trace, ciphertext = _run_once(netlist, TEST_PLAINTEXT, TEST_KEY)
    counters = [entry["round_counter"] for entry in trace if entry["busy"]]
    return {
        "plaintext": "0x{:08x}".format(TEST_PLAINTEXT),
        "key": "0x{:016x}".format(TEST_KEY),
        "ciphertext": "0x{:08x}".format(ciphertext),
        # one load edge, then one edge per round, then `done` is up
        "cycles_from_accepted_start_to_done": cycles + 1,
        "busy_cycles": len(counters),
        "round_counter_range": [min(counters), max(counters)] if counters else None,
        "rounds": max(counters) + 1 if counters else 0,
        "published_ciphertext": "0xa86842f2",
        "matches_published_vector": ciphertext == 0xA86842F2,
    }


# ---------------------------------------------------------------------------
# step 7 -- put the round function back together
# ---------------------------------------------------------------------------


def step_round(netlist, model):
    """Assemble the recovered round function from steps 3, 4, 5 and 6."""
    chains = step_chains(netlist, model)
    banks = step_banks(netlist, model)
    xors = step_xor(netlist, model)
    run = step_encrypt(netlist, model)

    add_rotations = sorted(
        {
            operand["rotation"]["rotation"]
            for entry in chains["adders"]
            for operand in entry["operands"].values()
            if operand["rotation"]
        }
    )
    xor_rotations = sorted(
        {entry["rotate_left_by"] for entry in banks["rotations"]}
    )
    widths = {entry["width"] for entry in banks["rotations"]}
    for entry in chains["adders"]:
        for operand in entry["operands"].values():
            if operand["rotation"]:
                widths.add(operand["rotation"]["width"])
    widths = sorted(widths)

    if len(add_rotations) != 1 or len(xor_rotations) != 1 or len(widths) != 1:
        raise SystemExit(
            "the round is not uniform: add rotations {}, xor rotations {}, "
            "widths {}".format(add_rotations, xor_rotations, widths)
        )

    alpha, beta, word = add_rotations[0], xor_rotations[0], widths[0]
    return {
        "word_bits": word,
        "alpha_right_rotation": alpha,
        "beta_left_rotation": beta,
        "adders": len(chains["adders"]),
        "xor_cells": xors["xor_cells"],
        "standalone_xor_cells": xors["standalone"],
        "rounds": run["rounds"],
        "round_function": (
            "x <- (ROR(x, {a}) + y) mod 2^{w}  XOR  k ; "
            "y <- ROL(y, {b}) XOR x".format(a=alpha, b=beta, w=word)
        ),
        "key_schedule": (
            "l <- (k + ROR(l0, {a})) mod 2^{w}  XOR  i ; "
            "k <- ROL(k, {b}) XOR l".format(a=alpha, b=beta, w=word)
        ),
        "matches_published_vector": run["matches_published_vector"],
    }


# ---------------------------------------------------------------------------
# step 8 -- the same netlist, read by HAL
# ---------------------------------------------------------------------------


def step_hal(netlist, model):
    """Load ``netlist.hal.v`` through hal_py and re-derive the census + SCCs.

    A second reader and a second gate model.  If the import rewrite or the
    gate library disagreed with ``hal_agilex``'s own reader, the counts here
    would not match step 1 -- and the SCC decomposition comes from the
    ``graph_algorithm`` plugin, not from anything in this file.
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
            base, index = _split(gate.get_name())
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
    ("banks", step_banks),
    ("xor", step_xor),
    ("encrypt", step_encrypt),
    ("round", step_round),
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
