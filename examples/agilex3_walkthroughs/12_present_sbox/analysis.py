#!/usr/bin/env python3
"""Recover the cipher in 12_present_sbox from the exported netlist alone.

Nothing here reads ``design.v`` or ``spec.md``.  It reads ``present_sbox.vo``
with ``tools/hal_agilex`` (the validated Agilex primitive semantics) and
``tools/hal_crypto`` (the cone/S-box machinery), and derives, in this order:

1. the primitive census and the flip-flop control-pin banks;
2. every 4-bit bijective non-affine cone group -- the substitution layer;
3. which flip-flop each substitution output drives -- the bit permutation;
4. how many rounds the counter runs;
5. the block cipher's own answer to the published PRESENT-80 test vectors,
   obtained by driving the netlist, not by trusting any of the above.

Both 3 and 5 are independent of the library match in 2: step 3 compares
functions, step 5 compares 64-bit words out of a simulator.

    python examples/agilex3_walkthroughs/12_present_sbox/analysis.py [--json PATH]

No HAL build and no Quartus needed -- ``tools/hal_agilex`` and
``tools/hal_crypto`` are plain standard library.
"""

import argparse
import itertools
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools"))

from hal_agilex import simulate, vo_netlist          # noqa: E402
from hal_crypto import netlist_model                 # noqa: E402
from hal_crypto import sbox as sbox_pass             # noqa: E402

DEFAULT_VO = os.path.join(HERE, "present_sbox.vo")

#: The published PRESENT S-box (CHES 2007, table 1).  Used twice: as the table
#: the recovered one is compared against, and -- once that comparison has held
#: -- as the labelling that turns four anonymous output signals into bits 0..3
#: of a nibble, which is what the permutation is expressed over.
PRESENT_SBOX = [0xC, 0x5, 0x6, 0xB, 0x9, 0x0, 0xA, 0xD,
                0x3, 0xE, 0xF, 0x8, 0x4, 0x7, 0x1, 0x2]

#: The published pLayer, for comparison only; the recovery never uses it.
PRESENT_PLAYER = [63 if i == 63 else (16 * i) % 63 for i in range(64)]

#: PRESENT-80 test vectors from the same paper: (plaintext, key, ciphertext).
TEST_VECTORS = [
    (0x0000000000000000, 0x00000000000000000000, 0x5579C1387B228445),
    (0x0000000000000000, 0xFFFFFFFFFFFFFFFFFFFF, 0xE72C46C0F5945049),
    (0xFFFFFFFFFFFFFFFF, 0x00000000000000000000, 0xA112FFC72F68417B),
    (0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFFFFFF, 0x3333DCD3213210D2),
]


# --------------------------------------------------------------------------
# small helpers over net keys.  A key is either "name" or "name[index]".

def bank_of(key):
    return key[:key.index("[")] if "[" in key else key


def index_of(key):
    return int(key[key.index("[") + 1:-1]) if "[" in key else None


def load(path=DEFAULT_VO):
    netlist = vo_netlist.parse_file(path)
    return netlist, netlist_model.NetlistModel(netlist)


# --------------------------------------------------------------------------
# 1. census and register banks

def census(netlist, model):
    types = {}
    for instance in netlist.instances:
        types[instance.type] = types.get(instance.type, 0) + 1
    return {
        "gate_types": types,
        "gates": len(netlist.instances),
        "inputs": list(netlist.ports("input")),
        "outputs": list(netlist.ports("output")),
    }


def register_banks(model):
    """Flip-flops grouped by (clock, clear, enable) net -- no names involved."""
    banks = {}
    for ff in model.ff_instances:
        signature = []
        for pin in ("clk", "clrn", "ena"):
            bits = ff.connections.get(pin)
            if not bits:
                signature.append(None)
                continue
            resolved = model.resolve(bits[0])
            signature.append(resolved[1] if resolved[0] == "net"
                             else "const%s" % resolved[1])
        banks.setdefault(tuple(signature), []).append(ff.name)
    return [{"clock": key[0], "clear": key[1], "enable": key[2],
             "size": len(names), "members": sorted(names)}
            for key, names in sorted(banks.items(), key=lambda item: -len(item[1]))]


# --------------------------------------------------------------------------
# 2. the substitution layer

def substitutions(model):
    """Every 4-bit bijective non-affine cone group, with its library match."""
    result = sbox_pass.identify(model)
    entries = []
    for record in result["sboxes"]:
        entries.append({
            "sources": list(record["sources"]),
            "outputs": list(record["outputs"]),
            "bits": record["bits"],
            "table": list(record["table"]),
            "algebraic_degree": record["algebraic_degree"],
            "differential_uniformity": record["differential_uniformity"],
            "matches": [{"name": m["name"], "tier": m["tier"]} for m in record["matches"]],
            "reads": sorted({bank_of(name) for name in record["sources"]}),
        })
    entries.sort(key=lambda entry: (entry["reads"], entry["sources"]))
    return entries


# --------------------------------------------------------------------------
# 3. the permutation layer

def _state_bank(entries):
    """The register bank the widest set of substitution groups reads."""
    counts = {}
    for entry in entries:
        for name in entry["reads"]:
            counts[name] = counts.get(name, 0) + 1
    return max(counts, key=lambda name: counts[name])


def _destination_functions(model, bank):
    """For each flip-flop of *bank*: the four bank bits its D reads, and the
    16-entry function of them, with every other source held at zero."""
    functions = {}
    for ff in model.ff_instances:
        q = ff.connections.get("q")
        d = ff.connections.get("d")
        if not q or not d:
            continue
        target = model.resolve(q[0])
        if target[0] != "net" or bank_of(target[1]) != bank:
            continue
        data = model.resolve(d[0])
        if data[0] != "net":
            continue
        cone = model.cone(data[1])
        table = cone.truth_table()
        reads = sorted(index_of(key) for key in cone.order if bank_of(key) == bank)
        if len(reads) != 4:
            continue
        base = {name: 0 for name in cone.order}
        values = []
        for value in range(16):
            assignment = dict(base)
            for position, index in enumerate(reads):
                assignment["%s[%d]" % (bank, index)] = (value >> position) & 1
            values.append(table.evaluate(assignment) ^ (1 if data[2] else 0))
        functions[index_of(target[1])] = (tuple(reads), tuple(values))
    return functions


def permutation(model, entries):
    """Recover ``P``: substitution-layer bit i drives state bit ``P[i]``.

    Two steps, both functional:

    * every flip-flop's D is enumerated over the four state bits it reads
      (everything else held at zero), and matched -- up to complement, since
      the round key XOR may invert it -- against the four output cones of the
      group that reads the same four bits.  That says *which* flip-flop each
      substitution output drives, with no reliance on net names;
    * the four outputs of a group are then labelled 0..3 by the unique
      relabelling that makes the group's table the PRESENT S-box.  Without a
      published table to match, four output signals have no intrinsic bit
      order and the permutation could only be given up to a per-nibble
      relabelling; the labelling step is where the recovery becomes
      conditional on the match found in step 2.
    """
    bank = _state_bank(entries)
    destinations = _destination_functions(model, bank)
    groups = [entry for entry in entries if entry["reads"] == [bank]]

    mapping = {}
    notes = []
    for entry in groups:
        reads = tuple(sorted(index_of(name) for name in entry["sources"]))
        outputs = {}
        for key in entry["outputs"]:
            cone = model.cone(key).truth_table()
            outputs[key] = tuple(
                cone.evaluate({"%s[%d]" % (bank, index): (value >> position) & 1
                               for position, index in enumerate(reads)})
                for value in range(16)
            )

        driven = {}
        for key, values in outputs.items():
            complement = tuple(1 - bit for bit in values)
            hits = [index for index, (source, table) in destinations.items()
                    if source == reads and table in (values, complement)]
            if len(hits) != 1:
                notes.append("%s drives %d flip-flops, not 1" % (key, len(hits)))
                continue
            driven[key] = hits[0]
        if len(driven) != 4:
            continue

        labellings = []
        for order in itertools.permutations(entry["outputs"]):
            table = [sum(outputs[key][value] << position
                         for position, key in enumerate(order))
                     for value in range(16)]
            if table == PRESENT_SBOX:
                labellings.append(order)
        if len(labellings) != 1:
            notes.append("%s: %d labellings make the table PRESENT, not 1"
                         % (list(reads), len(labellings)))
            continue
        for position, key in enumerate(labellings[0]):
            mapping[reads[0] + position] = driven[key]

    return {
        "bank": bank,
        "width": len(destinations),
        "permutation": [mapping.get(index) for index in range(64)],
        "complete": len(mapping) == 64,
        "matches_published_player": [mapping.get(i) for i in range(64)] == PRESENT_PLAYER,
        "notes": notes,
    }


# --------------------------------------------------------------------------
# 4. the key schedule

def key_schedule(model, entries, state_bank):
    """The second register bank's next-state wiring, bit by bit.

    Every flip-flop of the bank is classified by *which registers* its D cone
    reads, which is enough to separate the three things a key schedule does:
    a bit that reads exactly one bit of its own bank is a rotation link (and
    the offset is the rotation amount); bits that read four is a substitution;
    bits that read one bank bit plus counter bits are where the round counter
    is injected.
    """
    banks = {}
    for ff in model.ff_instances:
        q = ff.connections.get("q")
        if not q:
            continue
        resolved = model.resolve(q[0])
        if resolved[0] == "net" and index_of(resolved[1]) is not None:
            banks.setdefault(bank_of(resolved[1]), []).append(ff)
    candidates = {name: ffs for name, ffs in banks.items()
                  if name != state_bank and len(ffs) >= 8}
    if not candidates:
        return {}
    key_bank = max(candidates, key=lambda name: len(candidates[name]))
    counter_banks = {name for name, ffs in banks.items()
                     if name not in (state_bank, key_bank)}

    rotations = {}
    substituted = []
    injected = []
    unclassified = []
    for ff in candidates[key_bank]:
        target = index_of(model.resolve(ff.connections["q"][0])[1])
        data = model.resolve(ff.connections["d"][0])
        if data[0] != "net":
            unclassified.append(target)
            continue
        own, counted = [], []
        for source in model.cone(data[1]).order:
            if bank_of(source) == key_bank:
                own.append(index_of(source))
            elif bank_of(source) in counter_banks:
                counted.append(source)
        if len(own) == 1 and not counted:
            rotations[target] = (target - own[0]) % len(candidates[key_bank])
        elif len(own) == 1 and counted:
            injected.append(target)
        elif len(own) == 4:
            substituted.append(target)
        else:
            unclassified.append(target)

    amounts = sorted(set(rotations.values()))
    return {
        "bank": key_bank,
        "width": len(candidates[key_bank]),
        "rotation_links": len(rotations),
        "rotate_left_by": amounts[0] if len(amounts) == 1 else None,
        "rotation_amounts": amounts,
        "substituted_bits": sorted(substituted),
        "counter_injected_bits": sorted(injected),
        "counter_banks": sorted(counter_banks),
        "unclassified_bits": sorted(unclassified),
    }


# --------------------------------------------------------------------------
# 5. the round counter, and 6. the netlist's own answers

def _drive(netlist, plaintext, key, limit=200):
    """Load one block and clock until the design stops; returns the trace."""
    simulator = simulate.Simulator(netlist)
    simulator.reset()
    for name in ("start", "plaintext", "key_in"):
        simulator.set_input(name, 0)
    simulator.set_input("rst_n", 0)
    simulator.apply_async_clear()
    simulator.set_input("rst_n", 1)
    simulator.set_input("plaintext", plaintext)
    simulator.set_input("key_in", key)
    simulator.set_input("start", 1)
    simulator.clock()
    # the payload ports go quiet again, so nothing below can be read off them
    simulator.set_input("start", 0)
    simulator.set_input("plaintext", 0)
    simulator.set_input("key_in", 0)

    counter = []
    cycles = 0
    while simulator.get_output("busy") and cycles < limit:
        counter.append(simulator.get_output("round"))
        simulator.clock()
        cycles += 1
    return {
        "ciphertext": simulator.get_output("ciphertext"),
        "cycles": cycles,
        "counter_trace": counter,
        "done": simulator.get_output("done"),
    }


def round_count(netlist):
    run = _drive(netlist, 0, 0)
    return {
        "cycles_busy": run["cycles"],
        "counter_first": run["counter_trace"][0] if run["counter_trace"] else None,
        "counter_last": run["counter_trace"][-1] if run["counter_trace"] else None,
        "counter_monotone": run["counter_trace"] == list(
            range(1, len(run["counter_trace"]) + 1)),
        "done_after": run["done"],
    }


def test_vectors(netlist):
    results = []
    for plaintext, key, expected in TEST_VECTORS:
        run = _drive(netlist, plaintext, key)
        results.append({
            "plaintext": "%016X" % plaintext,
            "key": "%020X" % key,
            "expected": "%016X" % expected,
            "netlist": "%016X" % run["ciphertext"],
            "agrees": run["ciphertext"] == expected,
            "round_cycles": run["cycles"],
        })
    return results


# --------------------------------------------------------------------------

def analyse(path=DEFAULT_VO):
    netlist, model = load(path)
    entries = substitutions(model)
    layer = permutation(model, entries)
    return {
        "netlist": os.path.basename(path),
        "census": census(netlist, model),
        "register_banks": [
            {key: value for key, value in bank.items() if key != "members"}
            for bank in register_banks(model)
        ],
        "substitutions": entries,
        "permutation": layer,
        "key_schedule": key_schedule(model, entries, layer["bank"]),
        "rounds": round_count(netlist),
        "test_vectors": test_vectors(netlist),
    }


def report(result, stream=sys.stdout):
    write = stream.write
    census_ = result["census"]
    write("netlist: %s\n" % result["netlist"])
    write("  %d instances: %s\n" % (
        census_["gates"],
        ", ".join("%s x%d" % (name, count)
                  for name, count in sorted(census_["gate_types"].items()))))
    write("  inputs %s / outputs %s\n" % (census_["inputs"], census_["outputs"]))
    write("register banks (clock, clear, enable):\n")
    for bank in result["register_banks"]:
        write("  %3d flip-flops  clk=%s clrn=%s ena=%s\n"
              % (bank["size"], bank["clock"], bank["clear"], bank["enable"]))

    write("substitution layer: %d groups\n" % len(result["substitutions"]))
    tiers = {}
    for entry in result["substitutions"]:
        tier = entry["matches"][0]["tier"] if entry["matches"] else "no match"
        tiers[tier] = tiers.get(tier, 0) + 1
    for tier, count in sorted(tiers.items()):
        write("  %2d group(s) matched at tier %s\n" % (count, tier))
    first = result["substitutions"][0]
    write("  table: %s (degree %d, differential uniformity %d)\n"
          % ("".join("%X" % value for value in first["table"]),
             first["algebraic_degree"], first["differential_uniformity"]))

    perm = result["permutation"]
    write("permutation layer over register bank '%s' (%d bits): %s\n"
          % (perm["bank"], perm["width"],
             "complete" if perm["complete"] else "INCOMPLETE"))
    write("  equals the published PRESENT pLayer: %s\n"
          % perm["matches_published_player"])

    schedule = result["key_schedule"]
    if schedule:
        write("key schedule over register bank '%s' (%d bits):\n"
              % (schedule["bank"], schedule["width"]))
        write("  %d bits are a plain rotation, left by %s\n"
              % (schedule["rotation_links"], schedule["rotate_left_by"]))
        write("  substituted bits %s, counter injected into bits %s\n"
              % (schedule["substituted_bits"], schedule["counter_injected_bits"]))
        if schedule["unclassified_bits"]:
            write("  unclassified: %s\n" % schedule["unclassified_bits"])

    rounds = result["rounds"]
    write("rounds: busy for %s cycles, counter %s..%s, monotone %s\n"
          % (rounds["cycles_busy"], rounds["counter_first"],
             rounds["counter_last"], rounds["counter_monotone"]))

    write("published PRESENT-80 test vectors:\n")
    for vector in result["test_vectors"]:
        write("  %s / %s -> %s  %s\n"
              % (vector["plaintext"], vector["key"], vector["netlist"],
                 "agrees" if vector["agrees"] else
                 "DISAGREES (expected %s)" % vector["expected"]))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("netlist", nargs="?", default=DEFAULT_VO)
    parser.add_argument("--json", help="also write the whole result here")
    args = parser.parse_args(argv)

    result = analyse(args.netlist)
    report(result)
    if args.json:
        directory = os.path.dirname(os.path.abspath(args.json))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(args.json, "w") as handle:
            json.dump(result, handle, indent=2, sort_keys=True)
            handle.write("\n")
    ok = (result["permutation"]["complete"]
          and all(vector["agrees"] for vector in result["test_vectors"]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
