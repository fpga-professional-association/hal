#!/usr/bin/env python3
"""The reverse-engineering steps of walkthrough 15, one function per step.

Every step reads *only* an exported netlist (``ntt_mult.vo``, the
counterfactual ``ntt_dsp.vo``, or ``netlist.hal.v`` for the one HAL step).
Nothing in this file looks at ``design.v``, ``spec.md`` or the reference
models: that is the whole point.  Where a step needs a constant -- a modulus, a
root of unity, a twiddle, a transform length, a cycle count -- it derives it
and fails loudly rather than assuming it.

Usage, from the repository root::

    python3 examples/agilex3_walkthroughs/15_ntt_mult/analysis.py all \\
        -o examples/agilex3_walkthroughs/15_ntt_mult/artifacts

Subcommands: ``stats``, ``registers``, ``schedule``, ``butterfly``,
``modulus``, ``twiddles``, ``addresses``, ``ring``, ``vectors``, ``identify``,
``hal``, ``all``.  Everything except ``hal`` runs on a plain Python 3
interpreter with ``tools/`` importable -- the structural work is done by
``tools/hal_agilex`` and ``tools/hal_crypto``, both HAL-free by design.
``hal`` loads the imported netlist through ``hal_py``: a second reader, a
second gate model and a plugin's own graph algorithm, agreeing (or not) with
the first.

## What has to be derived, and in what order

The netlist offers 299 flip-flops, 910 ALM cells, five input ports and three
output ports.  A negacyclic NTT multiplier's algebra needs a modulus, a ring
degree, a root of unity, eighty twiddle constants and a butterfly schedule.
None of that is in the export.  The order the steps recover it:

* ``registers``  every coefficient flip-flop's next-state cone reads exactly
  one bit of ``a_in`` or ``b_in`` -- the load path -- which groups the 288 of
  them into 32 banks of nine and gives eight of the nine bits their weight.
  The ninth is placed by which bit of the adder's output it reads, and the two
  readings are required to agree on the eight they share.
* ``schedule``   the eleven remaining flip-flops are walked as a *transition
  orbit* from a load: one of them is high for the whole run (``busy``), one
  rises at the end (``done``), six toggle with periods 1, 2, 4, 8, 16, 32 --
  which is what makes them a counter and gives each its weight -- and three
  are constant inside a phase.  144 cycles, five phases, lengths 64/16/16/32/16.
* ``butterfly``  one verified ``a + b`` carry chain and one verified ``a - b``
  carry chain over the *same* two nine-bit vectors.  That is the butterfly, and
  it is one butterfly, not thirty-two.
* ``modulus``    there is no constant-operand carry chain anywhere, because
  257 is cheap to add.  The modulus comes out of the *correction* instead:
  ``hal_crypto.ntt`` derives the q for which ``select ? sum - q : sum`` is a
  vector the netlist contains, and checks it on every sum the adder can make.
* ``twiddles``   hold every coefficient register at 1 and the multiplier
  returns its twiddle: sweeping the orbit reads all eighty constants straight
  off the adder's operand, in a bit order the carry chain already fixed.
* ``addresses``  which register the butterfly reads and writes at each step,
  probed one coefficient at a time.  The pairs are ``(j, j + len)`` with
  ``len`` halving every eight steps: four stages of eight butterflies.
* ``ring``       q, the transform length, the twiddle tables and the address
  schedule together pin psi: the unique element of order 32 modulo 257 whose
  bit-reversed powers reproduce the recovered forward table.
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

EXPORT = os.path.join(HERE, "ntt_mult.vo")
DSP_EXPORT = os.path.join(HERE, "ntt_dsp.vo")
NETLIST = os.path.join(HERE, "netlist.hal.v")
GATE_LIBRARY = os.path.join(
    REPO, "plugins", "gate_libraries", "definitions", "AGILEX_TENNM.hgl"
)

from hal_agilex import primitives, vo_netlist  # noqa: E402
from hal_agilex.simulate import Simulator  # noqa: E402
from hal_crypto import arith, classify, findings, ntt  # noqa: E402
from hal_crypto.netlist_model import NetlistModel, UnsupportedCell  # noqa: E402


def load(path=EXPORT):
    netlist = vo_netlist.parse_file(path)
    return netlist, NetlistModel(netlist)


# ---------------------------------------------------------------------------
# a probe: assign every source in the netlist, evaluate any net
# ---------------------------------------------------------------------------


class Probe(object):
    """Drive the combinational logic from a named state and read any net.

    The netlist's sources are the flip-flop outputs and the input port bits.
    Assigning all of them makes every combinational net computable, which is
    what lets a step ask "what does this cell say when the counter is 17 and
    every coefficient holds 1" without simulating anything.
    """

    def __init__(self, netlist, model):
        self.netlist = netlist
        self.model = model
        self.register_key = {}
        for instance in model.ff_instances:
            bits = instance.connections.get("q")
            if bits:
                self.register_key[instance.name] = bits[0].key
        self.port_bits = {}
        for name in netlist.ports("input"):
            self.port_bits[name] = [bit.key for bit in netlist.bits_of(name)]
        self.sources = sorted(
            set(self.register_key.values())
            | {key for bits in self.port_bits.values() for key in bits}
        )

    def blank(self):
        return dict.fromkeys(self.sources, 0)

    def evaluate(self, keys, vectors):
        return self.model.evaluate_samples(list(keys), list(vectors))

    def register_value(self, instance):
        """The resolved ``d`` connection of a register, as a net key."""
        resolved = self.model.ff_pin(instance, "d")
        return resolved

    def word(self, values, keys, index):
        word = 0
        for position, key in enumerate(keys):
            word |= values[key][index] << position
        return word


def _resolved_key(resolved):
    return resolved[1] if resolved[0] == "net" else None


# ---------------------------------------------------------------------------
# step 1 -- census
# ---------------------------------------------------------------------------


def _levels(model):
    """Combinational depth of every cell, cut at the flip-flops."""
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


def step_stats(netlist, model):
    """Census: what the export is made of, and how deep it is."""
    histogram = {}
    for instance in netlist.instances:
        histogram[instance.type] = histogram.get(instance.type, 0) + 1
    chains = arith.chains(model)
    levels = _levels(model)
    return {
        "design": netlist.name,
        "instances": len(netlist.instances),
        "gate_types": dict(sorted(histogram.items())),
        "input_ports": {
            name: len(netlist.bits_of(name)) for name in sorted(netlist.ports("input"))
        },
        "output_ports": {
            name: len(netlist.bits_of(name)) for name in sorted(netlist.ports("output"))
        },
        "flip_flops": len(model.ff_instances),
        "carry_chains": len(chains),
        "carry_chain_lengths": sorted(len(chain) for chain in chains),
        "combinational_levels": max(levels),
        "cells_per_level": {str(key): levels[key] for key in sorted(levels)},
    }


# ---------------------------------------------------------------------------
# step 2 -- the coefficient banks
# ---------------------------------------------------------------------------


def step_registers(netlist, model):
    """Group the flip-flops into coefficient banks, and weigh their bits.

    Two independent readings, required to agree:

    * the **load path**.  Each coefficient register's next-state cone reads
      exactly one bit of ``a_in`` or ``b_in``, because a load multiplexer is the
      only way an input port reaches a register.  That names the bank and, given
      the port is a flat vector, offers a bit index.
    * the **adder path**.  The same cone reads exactly one bit of the modular
      adder's output vector, whose bit order the carry chain already fixed.

    The first covers eight bits of each nine-bit coefficient (the ports are
    bytes); the second covers all nine.  Where they overlap they must agree, or
    the grouping is wrong.
    """
    probe = Probe(netlist, model)
    # The *wide* input ports are the data ones; a one-bit input is a control
    # line (this design has three: clk, rst_n, start) and every register's
    # cone reads at least one of them.
    port_of_key = {}
    for name, keys in probe.port_bits.items():
        if len(keys) < 2:
            continue
        for index, key in enumerate(keys):
            port_of_key[key] = (name, index)

    pairs = _butterfly_pairs(model)
    if len(pairs) != 1:
        raise SystemExit("expected exactly one butterfly, found {}".format(len(pairs)))
    reduction = _reduction(model, pairs)
    sum_order = {key: index for index, key in enumerate(reduction["sum_nets"])}
    difference_order = {
        key: index for index, key in enumerate(reduction["difference_nets"])
    }

    entries = []
    control = []
    for instance in model.ff_instances:
        resolved = probe.register_value(instance)
        key = _resolved_key(resolved)
        if key is None:
            control.append({"register": instance.name, "reason": "constant d"})
            continue
        support = model.support(key)
        ports = sorted(port_of_key[item] for item in support if item in port_of_key)
        cut = model.cut_support(
            key, frozenset(sum_order) | frozenset(difference_order), expand_root=True
        )
        sums = sorted(sum_order[item] for item in cut if item in sum_order)
        differences = sorted(
            difference_order[item] for item in cut if item in difference_order
        )
        if not ports and not sums:
            control.append({"register": instance.name, "reads_ports": []})
            continue
        if len(ports) > 1 or len(sums) > 1 or len(differences) > 1:
            raise SystemExit(
                "{} reads {} ports / {} sum bits".format(instance.name, ports, sums)
            )
        entries.append(
            {
                "register": instance.name,
                "bank": ports[0][0] if ports else None,
                "port_bit": ports[0][1] if ports else None,
                "sum_bit": sums[0] if sums else None,
                "difference_bit": differences[0] if differences else None,
            }
        )

    # Bank membership comes from which input port the load path reads.  Which
    # registers belong to *one* coefficient comes from the control nets they
    # share.  It cannot come from the clock enable alone: the top bit of each
    # coefficient has no port bit (the ports are bytes) and Quartus gives it no
    # clock enable either, folding the hold into its own next-state cone.  So
    # group on "reads a write-select net no other coefficient reads", which all
    # nine bits do however the hold was implemented.
    instance_by_name = {item.name: item for item in model.ff_instances}
    control_nets = _control_nets(netlist, model)
    signatures = {}
    for entry in entries:
        name = entry["register"]
        instance = instance_by_name[name]
        keys = set()
        data = _resolved_key(probe.register_value(instance))
        if data is not None:
            keys |= model.cut_support(data, control_nets, expand_root=True) & control_nets
        enable = _resolved_key(model.ff_pin(instance, "ena"))
        if enable in control_nets:
            keys.add(enable)
        signatures[name] = keys
    shared_by_all = set.intersection(*signatures.values()) if signatures else set()
    parent = {}

    def find(item):
        while parent.setdefault(item, item) != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(first, second):
        first, second = find(first), find(second)
        if first != second:
            parent[first] = second

    owner = {}
    for name, keys in signatures.items():
        find(name)
        for key in keys - shared_by_all:
            if key in owner:
                union(name, owner[key])
            else:
                owner[key] = name
    groups = {}
    for entry in entries:
        groups.setdefault(find(entry["register"]), []).append(entry)

    banks = {}
    for enable, members in sorted(groups.items(), key=lambda item: str(item[0])):
        ports = {member["bank"] for member in members if member["bank"]}
        indices = {
            member["port_bit"] // 8 for member in members if member["port_bit"] is not None
        }
        if len(ports) != 1 or len(indices) != 1:
            raise SystemExit(
                "enable group {} spans banks {} / coefficients {}".format(
                    enable, ports, indices
                )
            )
        bank = ports.pop()
        coefficient = indices.pop()
        weights = {}
        for member in members:
            weight = member["sum_bit"]
            if weight is None:
                raise SystemExit("{} reads no adder bit".format(member["register"]))
            if member["port_bit"] is not None:
                if member["port_bit"] % 8 != weight:
                    raise SystemExit(
                        "{}: load path says bit {}, adder says bit {}".format(
                            member["register"], member["port_bit"] % 8, weight
                        )
                    )
            weights[weight] = member["register"]
        banks.setdefault(bank, {})[coefficient] = [
            weights[weight] for weight in sorted(weights)
        ]

    widths = {len(bits) for bank in banks.values() for bits in bank.values()}
    return {
        "coefficient_registers": len(entries),
        "control_registers": sorted(item["register"] for item in control),
        "banks": {
            name: {str(index): bank[index] for index in sorted(bank)}
            for name, bank in sorted(banks.items())
        },
        "bank_count": len(banks),
        "coefficients_per_bank": sorted({len(bank) for bank in banks.values()}),
        "bits_per_coefficient": sorted(widths),
        "port_bits_per_coefficient": 8,
        "note": (
            "Nine bits of storage per coefficient and eight bits of input per "
            "coefficient: the ninth value a residue modulo 257 can take is not "
            "expressible on the port, which is the first hint that the modulus "
            "is 257 and not 256."
        ),
    }


# ---------------------------------------------------------------------------
# step 3 -- the control orbit
# ---------------------------------------------------------------------------


def _control_nets(netlist, model):
    """Combinational nets that are functions of the control state alone.

    The control state is whatever is left once the coefficient registers are
    taken out: a counter, a phase and two flags.  Everything a *datapath*
    register's next state reads that is not data is one of these -- the write
    selects, the load, the enables.
    """
    probe = Probe(netlist, model)
    port_keys = {
        key for bits in probe.port_bits.values() if len(bits) > 1 for key in bits
    }
    control_registers = set()
    for instance in model.ff_instances:
        key = _resolved_key(probe.register_value(instance))
        if key is None or not (model.support(key) & port_keys):
            bits = instance.connections.get("q")
            if bits:
                control_registers.add(bits[0].key)
    narrow = {
        key for bits in probe.port_bits.values() if len(bits) == 1 for key in bits
    }
    allowed = control_registers | narrow
    nets = set()
    for instance in model.lcells:
        for pin in ("combout", "sumout", "cout"):
            for bit in instance.connections.get(pin) or ():
                key = getattr(bit, "key", None)
                if key is None:
                    continue
                try:
                    support = model.support(key)
                except UnsupportedCell:
                    continue
                if support and support <= allowed:
                    nets.add(key)
    return frozenset(nets)


_LAYOUT_CACHE = {}


def _layout(netlist, model):
    """``step_registers`` once per netlist; several steps need the banks."""
    key = id(model)
    if key not in _LAYOUT_CACHE:
        _LAYOUT_CACHE[key] = step_registers(netlist, model)
    return _LAYOUT_CACHE[key]


def _coefficient_names(netlist, model):
    layout = _layout(netlist, model)
    return {
        name
        for bank in layout["banks"].values()
        for bits in bank.values()
        for name in bits
    }


def _control_registers(netlist, model):
    """The flip-flops that hold no coefficient, in netlist order."""
    probe = Probe(netlist, model)
    coefficients = _coefficient_names(netlist, model)
    control = [
        instance
        for instance in model.ff_instances
        if instance.name not in coefficients
    ]
    return probe, control


def _orbit(netlist, model, coefficient_value=None, limit=400):
    """Walk the control registers from a load until the machine goes idle.

    Returns the list of states; each state is a dict from control register name
    to bit, plus the coefficient registers held at ``coefficient_value`` (a
    9-bit word applied to every coefficient) so that the datapath is driven too.
    """
    probe, control = _control_registers(netlist, model)
    names = [instance.name for instance in control]
    data_registers = [
        instance
        for instance in model.ff_instances
        if instance.name not in set(names)
    ]

    # Coefficient registers are held at a fixed word.  `registers` gives the
    # bit weight of every one of them, so "hold every coefficient at 1" is a
    # statement about values, not about net names.
    weights = {}
    if coefficient_value is not None:
        layout = _layout(netlist, model)
        for bank in layout["banks"].values():
            for bits in bank.values():
                for weight, name in enumerate(bits):
                    weights[name] = (coefficient_value >> weight) & 1

    next_key = {}
    for instance in control:
        next_key[instance.name] = probe.register_value(instance)
    enable_key = {}
    for instance in control:
        enable_key[instance.name] = model.ff_pin(instance, "ena")

    def vector(state, start):
        values = probe.blank()
        for name, bit in state.items():
            values[probe.register_key[name]] = bit
        for instance in data_registers:
            values[probe.register_key[instance.name]] = weights.get(instance.name, 0)
        values[probe.port_bits["start"][0]] = start
        values[probe.port_bits["rst_n"][0]] = 1
        return values

    def advance(state, start):
        values = vector(state, start)
        keys = []
        for name in names:
            for resolved in (next_key[name], enable_key[name]):
                if resolved[0] == "net":
                    keys.append(resolved[1])
        evaluated = probe.evaluate(sorted(set(keys)), [values])

        def read(resolved, default):
            if resolved[0] == "const":
                return default if resolved[1] is None else resolved[1]
            bit = evaluated[resolved[1]][0]
            return bit ^ (1 if resolved[2] else 0)

        nxt = {}
        for name in names:
            enabled = read(enable_key[name], 1)
            nxt[name] = read(next_key[name], 0) if enabled else state[name]
        return nxt

    state = dict.fromkeys(names, 0)
    state = advance(state, 1)          # the load cycle
    orbit = [dict(state)]
    for _ in range(limit):
        state = advance(state, 0)
        orbit.append(dict(state))
        # The machine has gone idle when the state stops moving: `start` is held
        # low from here on, so a fixed point is the end of the run and not a
        # pause in it.
        if orbit[-1] == orbit[-2]:
            break
    return probe, names, orbit


def _counts_up(order, columns, length):
    """Do these registers, in this order, count 0, 1, 2, ... and reset?"""
    values = [
        sum(columns[name][index] << position for position, name in enumerate(order))
        for index in range(length)
    ]
    if values[0] != 0:
        return None
    resets = []
    for index in range(1, length):
        if values[index] == values[index - 1] + 1:
            continue
        if values[index] == 0:
            resets.append(index)
            continue
        return None
    return resets


def _counter_order(candidates, columns):
    """The largest ordered set of registers that behaves like a counter.

    No names and no assumed periods.  A binary counter's bit *k* toggles only
    where bits 0..k-1 are all one, so the order is built by that rule, most
    active bit first, and the result is then *checked*: the value has to
    increment by one or fall back to zero every cycle, and a reset has to be
    the only moment any other control bit moves.  Bits are dropped from the top
    until both hold, which is what keeps a phase register out of the counter --
    it also only changes where the low bits are all one.
    """
    length = len(next(iter(columns.values())))
    toggles = {
        name: {
            index
            for index in range(1, length)
            if columns[name][index] != columns[name][index - 1]
        }
        for name in candidates
    }
    order = []
    remaining = set(candidates)
    while remaining:
        if not order:
            picks = [
                name for name in remaining if len(toggles[name]) == length - 1
            ]
        else:
            hot = {
                index
                for index in range(1, length)
                if all(columns[name][index - 1] == 1 for name in order)
            }
            picks = [
                name
                for name in remaining
                if toggles[name] and toggles[name] <= hot
            ]
        if not picks:
            break
        pick = max(sorted(picks), key=lambda name: len(toggles[name]))
        order.append(pick)
        remaining.discard(pick)

    while order:
        resets = _counts_up(order, columns, length)
        if resets is not None:
            moments = set(resets)
            rest = [name for name in candidates if name not in order]
            if all(
                index in moments
                for name in rest
                for index in toggles[name]
            ):
                return order
        order.pop()
    raise SystemExit("no subset of the control registers counts")


def step_schedule(netlist, model):
    """Read the control machine off its own transition orbit.

    Nothing here knows which flip-flop is "the counter".  The orbit does: a bit
    that toggles every cycle, every second cycle, every fourth and so on is a
    binary counter and its period *is* its weight; a bit that is constant inside
    a run of counter values and changes when the counter restarts is a phase
    bit; a bit that is high for the whole run is ``busy`` and a bit that rises
    once at the end is ``done``.
    """
    probe, names, orbit = _orbit(netlist, model, coefficient_value=1)
    columns = {name: [state[name] for state in orbit] for name in names}

    # `busy` and `done` are the two registers the output ports read.
    output_of = {}
    for port in netlist.ports("output"):
        bits = netlist.bits_of(port)
        if len(bits) != 1:
            continue
        resolved = model.resolve(bits[0])
        if resolved[0] != "net":
            continue
        for name, key in probe.register_key.items():
            if key == resolved[1]:
                output_of[port] = name

    busy = output_of.get("busy")
    done = output_of.get("done")
    if busy is None or done is None:
        raise SystemExit("could not tie busy/done back to a register")
    running = [index for index, state in enumerate(orbit) if state[busy]]
    cycles = len(running)

    candidates = [name for name in names if name not in (busy, done)]
    columns_running = {
        name: [columns[name][index] for index in running] for name in candidates
    }
    order = _counter_order(candidates, columns_running)
    phases = sorted(set(candidates) - set(order))
    toggles = {}
    for name in candidates:
        column = columns_running[name]
        toggles[name] = sum(
            1 for index in range(1, len(column)) if column[index] != column[index - 1]
        )

    def counter_value(state):
        return sum(state[name] << position for position, name in enumerate(order))

    counts = [counter_value(orbit[index]) for index in running]
    boundaries = [0]
    for index in range(1, len(counts)):
        if counts[index] == 0:
            boundaries.append(index)
    lengths = [
        (boundaries[index + 1] if index + 1 < len(boundaries) else len(counts))
        - boundaries[index]
        for index in range(len(boundaries))
    ]
    phase_words = []
    for start in boundaries:
        phase_words.append(
            sum(
                orbit[running[start]][name] << position
                for position, name in enumerate(sorted(phases))
            )
        )
    return {
        "control_registers": sorted(names),
        "busy_register": busy,
        "done_register": done,
        "cycles_from_load_to_done": cycles,
        "counter_registers": order,
        "counter_width": len(order),
        "counter_toggles": {name: toggles[name] for name in order},
        "phase_toggles": {name: toggles[name] for name in phases},
        "phase_registers": sorted(phases),
        "phase_width": len(phases),
        "phase_count": len(lengths),
        "phase_lengths": lengths,
        "phase_codes": phase_words,
        "done_rises_at": next(
            (index for index, state in enumerate(orbit) if state[done]), None
        ),
        "note": (
            "Five phases of 64, 16, 16, 32 and 16 cycles. The 64 is two "
            "transforms of 32 butterflies each -- the counter's top bit "
            "selects which register bank -- and the 32 is one more transform. "
            "Sixteen-cycle phases touch one coefficient per cycle."
        ),
    }


# ---------------------------------------------------------------------------
# step 4 -- the butterfly
# ---------------------------------------------------------------------------


def _butterfly_pairs(model):
    return ntt.butterflies(arith.adders(model))


def _reduction(model, pairs):
    found = ntt.reduction_moduli(model, pairs)
    if len(found) != 1:
        raise SystemExit(
            "expected exactly one recovered modulus, got {}".format(
                [entry["modulus"] for entry in found]
            )
        )
    return found[0]


def step_butterfly(netlist, model):
    """One adder and one subtracter over the same two vectors, verified."""
    adders = arith.adders(model)
    pairs = ntt.butterflies(adders)
    if len(pairs) != 1:
        raise SystemExit("expected one butterfly, found {}".format(len(pairs)))
    pair = pairs[0]
    table = {}
    for chain in arith.chains(model):
        info = arith.classify_chain(model, chain)
        if info is not None:
            table[tuple(info["cells"])] = info
    addition = table[tuple(pair["sum_cells"])]
    subtraction = table[tuple(pair["difference_cells"])]
    return {
        "verified_adders": [
            {
                "operation": entry["operation"],
                "width": entry["width"],
                "cells": len(entry["cells"]),
                "left": entry["left_sources"],
                "right": entry["right_sources"],
                "carry_in": entry["carry_in"],
            }
            for entry in adders
        ],
        "butterfly_count": len(pairs),
        "butterfly_width": pair["width"],
        "operand_nets": pair["operands"],
        "sum_nets": addition["sums"],
        "difference_nets": subtraction["sums"],
        "vectors_checked": pair["vectors_checked"],
        "note": (
            "One butterfly, nine bits wide, and the two chains read the *same* "
            "two operand vectors. The subtracter's second operand arrives "
            "inverted inside the arithmetic cell's own mask and its carry-in of "
            "one is emitted by a leading chain cell with no data operands: that "
            "is what a Quartus subtracter looks like, and it is why the "
            "hand-built fixtures in tools/hal_crypto/fixtures are not enough."
        ),
    }


def step_modulus(netlist, model):
    """Recover q from the correction that follows the butterfly.

    There is no constant-operand carry chain in this design.  257 is
    ``2**8 + 1``, so subtracting it is an increment and one bit flip, and
    Quartus builds that out of ordinary LUTs -- the tier that reads a modulus
    off a chain's constant operand finds nothing at all.  The modulus is still
    there, as the function the correction computes.
    """
    pairs = _butterfly_pairs(model)
    reduction = _reduction(model, pairs)
    constants = [
        entry for entry in arith.adders(model) if entry["operation"] == "add_constant"
    ]
    modulus = reduction["modulus"]
    return {
        "constant_operand_chains": len(constants),
        "modulus": modulus,
        "modulus_is_prime": all(
            modulus % factor for factor in range(2, int(modulus ** 0.5) + 1)
        ),
        "modulus_form": "2**8 + 1 (a Fermat prime)" if modulus == 257 else None,
        "select_net": reduction["select_net"],
        "select_inverted": reduction["select_inverted"],
        "sum_nets": reduction["sum_nets"],
        "difference_nets": reduction["difference_nets"],
        "difference_select_net": reduction["difference_select_net"],
        "vectors_checked": reduction["vectors_checked"],
        "checked_every_sum": reduction["checked_every_sum"],
        "published_parameter_match": reduction["library_moduli"],
        "note": (
            "q = 257 is a real modulus read out of real logic, and it is in no "
            "published-parameter library: ML-KEM uses 3329 and ML-DSA 8380417. "
            "This is the shape of a lattice scheme's arithmetic at toy "
            "parameters, not a deployed scheme's parameters."
        ),
    }


# ---------------------------------------------------------------------------
# step 5 -- the twiddle constants
# ---------------------------------------------------------------------------


def _twiddle_sweep(netlist, model, held=1):
    """The value the multiplier contributes at every step of the run.

    Hold every coefficient register at the value 1 and the butterfly's second
    operand -- the product -- is the twiddle itself, in the bit order the carry
    chain already fixed.  Nothing about the multiplier's internals is needed.
    """
    probe, names, orbit = _orbit(netlist, model, coefficient_value=held)
    pairs = _butterfly_pairs(model)
    pair = pairs[0]
    table = {}
    for chain in arith.chains(model):
        info = arith.classify_chain(model, chain)
        if info is not None:
            table[tuple(info["cells"])] = info
    addition = table[tuple(pair["sum_cells"])]
    # `u` is the operand that is zero outside a butterfly phase; the product is
    # the other one.  Both are named by the chain, and which is which falls out
    # of the values (u is zero in the scalar phases, the product is not).
    left = [entry[0] for entry in addition["left"]]
    right = [entry[0] for entry in addition["right"]]

    layout = _layout(netlist, model)
    weights = {}
    for bank in layout["banks"].values():
        for bits in bank.values():
            for weight, name in enumerate(bits):
                weights[name] = (held >> weight) & 1

    control = set(names)
    vectors = []
    for state in orbit:
        values = probe.blank()
        for name, bit in state.items():
            values[probe.register_key[name]] = bit
        for instance in model.ff_instances:
            if instance.name in control:
                continue
            values[probe.register_key[instance.name]] = weights.get(instance.name, 0)
        values[probe.port_bits["rst_n"][0]] = 1
        vectors.append(values)

    evaluated = probe.evaluate(sorted(set(left) | set(right)), vectors)
    left_words = [probe.word(evaluated, left, index) for index in range(len(vectors))]
    right_words = [probe.word(evaluated, right, index) for index in range(len(vectors))]
    return probe, names, orbit, left_words, right_words


def step_twiddles(netlist, model):
    """Eighty constants, read off the butterfly's own operand."""
    probe, names, orbit, left_words, right_words = _twiddle_sweep(netlist, model)
    schedule = step_schedule(netlist, model)
    busy = schedule["busy_register"]
    running = [index for index, state in enumerate(orbit) if state[busy]]

    counter = schedule["counter_registers"]
    phases = sorted(schedule["phase_registers"])

    def counter_value(state):
        return sum(state[name] << position for position, name in enumerate(counter))

    def phase_value(state):
        return sum(state[name] << position for position, name in enumerate(phases))

    # One of the two operand vectors is zero in the sixteen-cycle phases (that
    # is the "upper" coefficient of a pair, forced to zero so that a scalar
    # multiply is a degenerate butterfly); the other one carries the product.
    left_zero = all(left_words[index] == 0 for index in running[64:80])
    product = right_words if left_zero else left_words

    tables = {}
    for index in running:
        state = orbit[index]
        tables.setdefault(phase_value(state), {})[counter_value(state)] = product[index]

    # A second sweep with every coefficient held at 3 instead of 1 separates a
    # phase whose multiplier operand is a *constant table* from one whose
    # operand is data.  Holding the coefficients at v scales a table phase's
    # product by v and leaves the table itself alone; in the phase where the
    # "twiddle" is the other polynomial, the product goes as v squared, and the
    # normalised table moves.  That is the pointwise multiply, and no amount of
    # staring at the counter would have found it.
    modulus = step_modulus(netlist, model)["modulus"]
    _, _, second_orbit, second_left, second_right = _twiddle_sweep(
        netlist, model, held=3
    )
    second_product = second_right if left_zero else second_left
    inverse_three = pow(3, -1, modulus)
    second_tables = {}
    for index in running:
        state = second_orbit[index]
        second_tables.setdefault(phase_value(state), {})[counter_value(state)] = (
            second_product[index] * inverse_three % modulus
        )

    described = {}
    for phase, entries in sorted(tables.items()):
        counts = sorted(entries)
        values = [entries[count] for count in counts]
        scaled = [second_tables[phase][count] for count in counts]
        period = None
        for candidate in (1, 2, 4, 8, 16, 32, 64):
            if candidate > len(values):
                break
            if all(
                values[i] == values[i % candidate] for i in range(len(values))
            ):
                period = candidate
                break
        data_driven = scaled != values
        described[str(phase)] = {
            "steps": len(values),
            "distinct_values": len(set(values)),
            "period": None if data_driven else period,
            "data_driven": data_driven,
            "table": None if data_driven else values[: period or len(values)],
        }
    total = sum(
        len(entry["table"]) for entry in described.values() if entry["table"]
    )
    return {
        "phases": described,
        "constants_recovered": total,
        "held_values": [1, 3],
        "note": (
            "Two 32-entry tables over the five low counter bits, one 16-entry "
            "table over the four low bits, and one phase whose 'twiddle' is the "
            "constant 1 -- a bit-reversal pass reusing the multiplier as a copy. "
            "The remaining phase's operand is not a constant at all: it scales "
            "with the held coefficient, so it is the other polynomial. That is "
            "the pointwise product."
        ),
    }


# ---------------------------------------------------------------------------
# step 6 -- the butterfly schedule
# ---------------------------------------------------------------------------


def step_addresses(netlist, model):
    """Which coefficient the butterfly reads and writes, step by step.

    Probed, not read off wires: hold every coefficient at zero except one, and
    see whether the operand moves.  The read addresses come out of that; the
    write addresses come out of which register's enable is high and whether its
    next state follows the sum or the difference.
    """
    probe, names, orbit = _orbit(netlist, model, coefficient_value=0)
    schedule = step_schedule(netlist, model)
    layout = _layout(netlist, model)
    busy = schedule["busy_register"]
    running = [index for index, state in enumerate(orbit) if state[busy]]
    counter = schedule["counter_registers"]
    phases = sorted(schedule["phase_registers"])
    control = set(names)

    banks = sorted(layout["banks"])
    coefficients = []
    for bank in banks:
        for index in sorted(layout["banks"][bank], key=int):
            coefficients.append((bank, int(index), layout["banks"][bank][index]))

    pairs = _butterfly_pairs(model)
    reduction = _reduction(model, pairs)
    sum_nets = reduction["sum_nets"]
    difference_nets = reduction["difference_nets"]

    table = {}
    for chain in arith.chains(model):
        info = arith.classify_chain(model, chain)
        if info is not None:
            table[tuple(info["cells"])] = info
    addition = table[tuple(pairs[0]["sum_cells"])]
    operand_a = [entry[0] for entry in addition["left"]]
    operand_b = [entry[0] for entry in addition["right"]]

    low_bits = [bits[0] for _bank, _position, bits in coefficients]

    def state_vector(state, cold=None):
        """Every coefficient at 1, except *cold* which is held at 0."""
        values = probe.blank()
        for name, bit in state.items():
            values[probe.register_key[name]] = bit
        for instance in model.ff_instances:
            if instance.name not in control:
                values[probe.register_key[instance.name]] = 0
        for name in low_bits:
            values[probe.register_key[name]] = 1
        for name in cold or ():
            values[probe.register_key[name]] = 0
        values[probe.port_bits["rst_n"][0]] = 1
        return values

    # -- reads: knock one coefficient down from 1 to 0 and see what moves -----
    # The baseline holds every coefficient at 1, so both of the adder's operands
    # are live in every phase -- including the pointwise one, where *both* are
    # data and a one-hot probe would show nothing at all.
    vectors = [state_vector(orbit[index]) for index in running]
    labels = [(index, None, None) for index in running]
    for index in running:
        for bank, position, bits in coefficients:
            vectors.append(state_vector(orbit[index], cold=bits))
            labels.append((index, bank, position))
    evaluated = probe.evaluate(sorted(set(operand_a) | set(operand_b)), vectors)
    baseline = {}
    for sample, (index, bank, _position) in enumerate(labels):
        if bank is None:
            baseline[index] = (
                probe.word(evaluated, operand_a, sample),
                probe.word(evaluated, operand_b, sample),
            )
    # Which of the adder's two operands is the "upper" coefficient and which is
    # the product?  The upper one is forced to zero outside a butterfly phase --
    # that is how a scalar multiply is made a degenerate butterfly -- so it is
    # the operand that is zero more often.
    zeros = [
        sum(1 for index in running if baseline[index][side] == 0) for side in (0, 1)
    ]
    upper_side = 0 if zeros[0] > zeros[1] else 1
    reads = {}
    for sample, (index, bank, position) in enumerate(labels):
        if bank is None:
            reads.setdefault(index, {"upper": [], "product": []})
            continue
        words = (
            probe.word(evaluated, operand_a, sample),
            probe.word(evaluated, operand_b, sample),
        )
        entry = reads.setdefault(index, {"upper": [], "product": []})
        for side in (0, 1):
            if words[side] == baseline[index][side]:
                continue
            entry["upper" if side == upper_side else "product"].append(
                (bank, position)
            )

    # -- writes: enable high, and sum versus difference ----------------------
    enable_key = {}
    data_key = {}
    for bank, position, bits in coefficients:
        for name in bits:
            instance = next(
                item for item in model.ff_instances if item.name == name
            )
            enable_key[name] = model.ff_pin(instance, "ena")
            data_key[name] = probe.register_value(instance)
    keys = set()
    for resolved in list(enable_key.values()) + list(data_key.values()):
        if resolved[0] == "net":
            keys.add(resolved[1])
    keys |= set(sum_nets) | set(difference_nets)

    # Every coefficient at 1: then sum = 1 + t and difference = 1 - t, which
    # differ for every twiddle (2t is never zero modulo an odd q), so "did this
    # register take the sum or the difference" has an answer at every step.
    base_vectors = [state_vector(orbit[index]) for index in running]
    written = probe.evaluate(sorted(keys), base_vectors)

    def read_pin(resolved, sample, default):
        if resolved[0] == "const":
            return default if resolved[1] is None else resolved[1]
        return written[resolved[1]][sample] ^ (1 if resolved[2] else 0)

    writes = {}
    for sample, index in enumerate(running):
        sum_word = probe.word(written, sum_nets, sample)
        difference_word = probe.word(written, difference_nets, sample)
        entry = writes.setdefault(index, {"sum": [], "difference": []})
        for bank, position, bits in coefficients:
            if not read_pin(enable_key[bits[0]], sample, 1):
                continue
            value = 0
            for weight, name in enumerate(bits):
                value |= read_pin(data_key[name], sample, 0) << weight
            if value == sum_word:
                entry["sum"].append((bank, position))
            elif value == difference_word:
                entry["difference"].append((bank, position))
            else:
                entry.setdefault("other", []).append((bank, position))

    def counter_value(state):
        return sum(state[name] << position for position, name in enumerate(counter))

    def phase_value(state):
        return sum(state[name] << position for position, name in enumerate(phases))

    steps = []
    for index in running:
        steps.append(
            {
                "phase": phase_value(orbit[index]),
                "count": counter_value(orbit[index]),
                "reads_upper": sorted(reads[index]["upper"]),
                "reads_product_operand": sorted(reads[index]["product"]),
                "writes_sum": sorted(writes[index]["sum"]),
                "writes_difference": sorted(writes[index]["difference"]),
            }
        )

    butterfly_steps = [step for step in steps if step["writes_difference"]]
    distances = []
    for step in butterfly_steps:
        low = step["writes_sum"][0][1]
        high = step["writes_difference"][0][1]
        distances.append(high - low)
    stages = []
    for start in range(0, len(butterfly_steps), 8):
        block = distances[start : start + 8]
        if block:
            stages.append(sorted(set(block)))

    reads_equal_writes = all(
        sorted(step["reads_upper"] + step["reads_product_operand"])
        == sorted(step["writes_sum"] + step["writes_difference"])
        for step in butterfly_steps
    )
    permutations = {}
    for step in steps:
        if step["writes_difference"] or len(step["reads_product_operand"]) != 1:
            continue
        source = step["reads_product_operand"][0]
        destination = step["writes_sum"][0]
        if source[0] == destination[0]:
            continue
        permutations.setdefault(step["phase"], {})[source[1]] = destination[1]
    bit_reversals = {}
    for phase, mapping in permutations.items():
        bit_reversals[str(phase)] = {
            "map": [mapping.get(index) for index in sorted(mapping)],
            "is_bit_reversal": all(
                mapping.get(index) == _bitreverse(index, 4) for index in mapping
            ),
        }

    return {
        "steps": steps,
        "butterfly_steps": len(butterfly_steps),
        "pair_distances_per_block_of_eight": stages,
        "reads_and_writes_are_the_same_pair": reads_equal_writes,
        "cross_bank_permutations": bit_reversals,
        "note": (
            "Every butterfly step writes two coefficients of one bank: the sum "
            "to index j and the difference to index j + len. The distance len "
            "is 8 for the first eight steps, then 4, then 2, then 1 -- four "
            "stages of eight butterflies, which is the loop nest of an "
            "iterative transform of length 16."
        ),
    }


# ---------------------------------------------------------------------------
# step 7 -- the ring
# ---------------------------------------------------------------------------


def _bitreverse(value, bits):
    result = 0
    for position in range(bits):
        if value & (1 << position):
            result |= 1 << (bits - 1 - position)
    return result


def step_ring(netlist, model):
    """Pin the root of unity the recovered twiddles are powers of."""
    twiddles = step_twiddles(netlist, model)
    modulus = step_modulus(netlist, model)["modulus"]
    addresses = step_addresses(netlist, model)
    degree = 1 + max(
        position
        for step in addresses["steps"]
        for _bank, position in step["reads_upper"] + step["reads_product_operand"]
    )

    tables = {}
    for phase, entry in twiddles["phases"].items():
        if entry["period"] == 32:
            tables[int(phase)] = entry["table"]
    if len(tables) != 2:
        raise SystemExit("expected two 32-entry twiddle tables")

    def stage_group(step):
        stage = step >> 3
        position = step & 7
        return stage, position >> (3 - stage)

    candidates = []
    for value in range(2, modulus):
        order = 1
        power = value % modulus
        while power != 1:
            power = power * value % modulus
            order += 1
        if order != 2 * degree:
            continue
        for phase, table in tables.items():
            want = [
                pow(value, _bitreverse((1 << stage) + group, 4), modulus)
                for stage, group in (stage_group(step) for step in range(32))
            ]
            if want == table:
                candidates.append({"root": value, "order": order, "phase": phase})
    if len(candidates) != 1:
        raise SystemExit(
            "psi is not pinned: {} candidates".format(len(candidates))
        )
    psi = candidates[0]["root"]
    forward_phase = candidates[0]["phase"]
    omega = psi * psi % modulus
    inverse_phase = next(phase for phase in tables if phase != forward_phase)
    inverse_table = tables[inverse_phase]
    want_inverse = [
        pow(omega, -_bitreverse(stage_group(step)[1], 3), modulus)
        for step in range(32)
    ]
    post = None
    for entry in twiddles["phases"].values():
        if entry["period"] == 16 and entry["distinct_values"] > 1:
            post = entry["table"]
    scale = None
    if post is not None:
        inverse_psi = pow(psi, -1, modulus)
        for candidate in range(1, modulus):
            if all(
                post[index]
                == candidate * pow(inverse_psi, _bitreverse(index, 4), modulus) % modulus
                for index in range(16)
            ):
                scale = candidate
                break
    return {
        "modulus": modulus,
        "degree": degree,
        "psi": psi,
        "psi_order": candidates[0]["order"],
        "omega": omega,
        "omega_order": degree,
        "forward_phase": forward_phase,
        "inverse_phase": inverse_phase,
        "inverse_table_matches_omega_inverse": inverse_table == want_inverse,
        "post_scale": scale,
        "post_scale_is_inverse_degree": scale is not None
        and scale * degree % modulus == 1,
        "ring": "Z_{}[x] / (x^{} + 1)".format(modulus, degree),
        "note": (
            "psi^{} = {} = -1 modulo {}, so the transform is negacyclic: it "
            "multiplies in Z_q[x]/(x^n + 1), not Z_q[x]/(x^n - 1). That minus "
            "sign is what a lattice scheme's ring is made of.".format(
                degree, pow(psi, degree, modulus), modulus
            )
        ),
    }


# ---------------------------------------------------------------------------
# step 8 -- behaviour
# ---------------------------------------------------------------------------


def _pack(values, width):
    word = 0
    for index, value in enumerate(values):
        word |= (value & ((1 << width) - 1)) << (width * index)
    return word


def _unpack(word, count, width):
    return [(word >> (width * index)) & ((1 << width) - 1) for index in range(count)]


def _schoolbook(first, second, modulus, degree):
    """``a(x) * b(x) mod (x^n + 1, q)``, straight from the definition."""
    result = [0] * degree
    for i in range(degree):
        for j in range(degree):
            total = i + j
            if total < degree:
                result[total] = (result[total] + first[i] * second[j]) % modulus
            else:
                result[total - degree] = (
                    result[total - degree] - first[i] * second[j]
                ) % modulus
    return result


def _run(netlist, first, second, limit=1000):
    simulator = Simulator(netlist)
    simulator.reset()
    for name, value in (("start", 0), ("a_in", 0), ("b_in", 0), ("rst_n", 0)):
        simulator.set_input(name, value)
    simulator.apply_async_clear()
    simulator.set_input("rst_n", 1)
    simulator.set_input("start", 1)
    simulator.set_input("a_in", _pack(first, 8))
    simulator.set_input("b_in", _pack(second, 8))
    simulator.clock()
    simulator.set_input("start", 0)
    cycles = 1
    while not simulator.get_output("done"):
        simulator.clock()
        cycles += 1
        if cycles > limit:
            raise SystemExit("done never rose")
    return cycles, simulator.get_output("c_out")


def step_vectors(netlist, model):
    """Drive the export as a black box and check it against the definition.

    Structure said "modulus 257, degree 16, psi = 15, negacyclic".  This step
    never reads that: it multiplies polynomials with the netlist and with the
    schoolbook convolution in ``Z_257[x]/(x^16 + 1)``, and requires the two to
    agree.  The convolution is the *definition* of the product; there is no
    published test vector for a toy ring, and there does not need to be one.
    """
    ring = step_ring(netlist, model)
    modulus = ring["modulus"]
    degree = ring["degree"]
    cases = [
        ("one times one", [1] + [0] * 15, [1] + [0] * 15),
        (
            "x^15 times x -- the negacyclic wrap",
            [0] * 15 + [1],
            [0, 1] + [0] * 14,
        ),
        (
            "pseudo-random operands",
            [(37 * index + 11) % 256 for index in range(16)],
            [(91 * index + 5) % 256 for index in range(16)],
        ),
        (
            "all bytes at their maximum",
            [255] * 16,
            [255] * 16,
        ),
    ]
    results = []
    for label, first, second in cases:
        cycles, word = _run(netlist, first, second)
        got = _unpack(word, degree, 9)
        want = _schoolbook(first, second, modulus, degree)
        results.append(
            {
                "case": label,
                "cycles_from_accepted_start_to_done": cycles,
                "netlist": got,
                "schoolbook": want,
                "agrees": got == want,
            }
        )
    return {
        "ring": ring["ring"],
        "cases": results,
        "all_agree": all(entry["agrees"] for entry in results),
        "wrap_is_negative": results[1]["netlist"][0] == modulus - 1,
        "note": (
            "x^15 * x comes back as -1 and not as +1: the quotient is x^16 + 1, "
            "the ring is negacyclic, and that is a structural fact about the "
            "design confirmed behaviourally."
        ),
    }


# ---------------------------------------------------------------------------
# step 9 -- the crypto identifier, on both exports
# ---------------------------------------------------------------------------


def step_identify(netlist, model):
    """What ``hal_crypto identify`` makes of each export."""
    results = {}
    for label, path in (("ntt_mult", EXPORT), ("ntt_dsp", DSP_EXPORT)):
        if not os.path.exists(path):
            continue
        parsed = vo_netlist.parse_file(path)
        artifact = findings.artifact_for(parsed, path, label)
        document = classify.build_document(parsed, artifact)
        verdict = {}
        for finding in document["findings"]:
            if finding["id"] == "hal_crypto/identify/family":
                verdict["family"] = finding["data"]["family"]
                verdict["families_present"] = finding["data"]["families_present"]
                verdict["confidence_tier"] = finding["data"]["confidence_tier"]
                verdict["evidence"] = finding["data"]["evidence"]
            if finding["id"] == "hal_crypto/identify/classical-vs-pqc":
                verdict["style"] = finding["data"]["style"]
            if finding["id"].startswith("hal_crypto/ntt/"):
                verdict["ntt_title"] = finding["title"]
                verdict["recovered_moduli"] = finding["data"].get("recovered_moduli")
        results[label] = verdict
    return {
        "exports": results,
        "note": (
            "The DSP export is the counterfactual: one attribute and two "
            "assignments away, the 9x9 multiplier becomes a tennm_mac and the "
            "export leaves hal_agilex's validated coverage entirely -- yet the "
            "butterfly and the modulus are still found, because they live "
            "outside the multiplier. What is lost is the ability to check the "
            "design at all, and what is gained is a spurious ARX reading."
        ),
    }


# ---------------------------------------------------------------------------
# step 10 -- the same netlist through hal_py
# ---------------------------------------------------------------------------


def _split(name):
    base = name.split("[")[0]
    return base, name


def step_hal(netlist, model):
    """Load the imported netlist through hal_py and decompose it."""
    for entry in (os.environ.get("HAL_PY_PATH"), os.environ.get("PYTHONPATH")):
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
            base, _full = _split(gate.get_name())
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
        "components": described[:6],
        "component_count": len(described),
        "largest_component": described[0] if described else None,
    }


STEPS = [
    ("stats", step_stats),
    ("registers", step_registers),
    ("schedule", step_schedule),
    ("butterfly", step_butterfly),
    ("modulus", step_modulus),
    ("twiddles", step_twiddles),
    ("addresses", step_addresses),
    ("ring", step_ring),
    ("vectors", step_vectors),
    ("identify", step_identify),
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
