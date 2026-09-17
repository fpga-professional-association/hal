#!/usr/bin/env python3
"""The reverse-engineering steps of walkthrough 14, one function per step.

Every step reads *only* an exported netlist (``keccak_toy.vo``, the
counterfactual ``keccak_retimed.vo``, or ``netlist.hal.v`` for the one HAL
step).  Nothing in this file looks at ``design.v``, ``spec.md`` or the
reference models: that is the whole point.  Where a step needs a constant -- a
lane width, a rotation offset, a round count, a round constant -- it derives it
and fails loudly rather than assuming it.

Usage, from the repository root::

    python3 examples/agilex3_walkthroughs/14_keccak_toy/analysis.py all \\
        -o examples/agilex3_walkthroughs/14_keccak_toy/artifacts

Subcommands: ``stats``, ``registers``, ``parity``, ``theta``, ``chi``,
``rhopi``, ``iota``, ``rounds``, ``vectors``, ``identify``, ``hal``, ``all``.
Everything except ``hal`` runs on a plain Python 3 interpreter with ``tools/``
importable -- the structural work is done by ``tools/hal_agilex`` and
``tools/hal_crypto``, both of which are HAL-free by design.  ``hal`` loads the
imported netlist through ``hal_py``: a second reader, a second gate model and a
plugin's own graph algorithm, agreeing (or not) with the first.

## The coordinate system, and how much of it is derived

The netlist offers 200 flip-flops called ``s[0] .. s[199]`` and nothing else.
The permutation's own algebra needs a 5 x 5 array of 8-bit lanes, ``A[x][y][z]``.
Recovering that grid **from the wiring** is most of this file, and it happens in
this order:

* ``parity``  40 cells are a pure XOR of exactly five flip-flops.  Those 40
  five-element sets partition the state: they are the *columns*, bit-sliced.
  The two parity nets each theta cell reads then give each column class two
  neighbours, and the only closed five-step walk in that graph is the one that
  uses the same neighbour every time -- which separates the unrotated
  neighbour (order 5, so its cycles are the five columns at one bit position)
  from the rotated one.  Five columns, eight bit positions, five rows per
  column: the 5 x 5 x 8 grid, out of nothing but XOR fan-in.
* ``iota``    four of the eight round-constant cells are constant zero, and
  which four fixes the *origin and direction* of ``z`` and names one lane as
  ``(0,0)``.  That step is a **gauge** fixed against the published constant
  schedule, and it says so; under the other seven rotations the recovered
  round constants match nothing anyone has published.
* ``rhopi``   the composite ``rho`` then ``pi`` is read off as a 200-bit
  permutation, the lanes fall out of it as (destination column, source column)
  pairs, and the row labels fall out of ``pi`` mapping whole lanes.  Only then
  are the 25 rotation offsets a table with published row and column indices.

Anything named ``*_spec`` in an output document is in the permutation's
``(x, y, z)`` coordinates; everything else is the netlist's own indexing.
"""

import argparse
import itertools
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
TOOLS = os.path.join(REPO, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

EXPORT = os.path.join(HERE, "keccak_toy.vo")
RETIMED_EXPORT = os.path.join(HERE, "keccak_retimed.vo")
NETLIST = os.path.join(HERE, "netlist.hal.v")
GATE_LIBRARY = os.path.join(
    REPO, "plugins", "gate_libraries", "definitions", "AGILEX_TENNM.hgl"
)

from hal_agilex import primitives, vo_netlist  # noqa: E402
from hal_agilex.simulate import Simulator  # noqa: E402
from hal_crypto import boolfunc, classify, known, permutation, sbox  # noqa: E402
from hal_crypto.netlist_model import ConeTooWide, NetlistModel, UnsupportedCell  # noqa: E402

#: The published Keccak-f[200] vectors this walkthrough uses.  Literature, not
#: netlist: they are the *independent* behavioural check, and nothing structural
#: in this file depends on them.  Source: the eXtended Keccak Code Package,
#: ``tests/TestVectors/KeccakF-200-IntermediateValues.txt``, which applies the
#: permutation repeatedly to the all-zero state.
TEST_VECTORS = [
    (
        "XKCP, first permutation of the all-zero state",
        "00000000000000000000000000000000000000000000000000",
        "3C2826841CB35C171EAAE9B811134CEAA3852C69D2C5ABAFEA",
    ),
    (
        "XKCP, second permutation",
        "3C2826841CB35C171EAAE9B811134CEAA3852C69D2C5ABAFEA",
        "1BEF689492A8A543A5999FDB834E3166A14BE827D95040479E",
    ),
]

#: The vector the guide leads with: the all-zero input is the one for which no
#: byte-order convention could possibly matter.
HEADLINE_VECTOR = 0

#: The published rho offsets (stated at width 64), for comparison *after* the
#: netlist has been read.  ``PUBLISHED_RHO[x][y]``.
PUBLISHED_RHO = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)

#: The published round-constant schedule of Keccak-f[200] -- the low byte of the
#: FIPS 202 constants.  Used *only* to fix the bit-origin gauge in ``iota`` and
#: to report whether the recovered schedule matches.
PUBLISHED_RC = (
    0x01, 0x82, 0x8A, 0x00, 0x8B, 0x01, 0x81, 0x09, 0x8A,
    0x88, 0x09, 0x0A, 0x8B, 0x8B, 0x89, 0x03, 0x02, 0x80,
)


def load(path=EXPORT):
    """Parse the export and build the shared cone/evaluation model."""
    netlist = vo_netlist.parse_file(path)
    return netlist, NetlistModel(netlist)


# ---------------------------------------------------------------------------
# small helpers over the netlist model
# ---------------------------------------------------------------------------


def _split(key):
    if key.endswith("]") and "[" in key:
        name, _, index = key[:-1].partition("[")
        try:
            return name, int(index)
        except ValueError:
            return key, None
    return key, None


def cell_inputs(model, instance):
    """The nets a cell reads *directly* -- one hop, not the whole cone.

    Every other pass in ``tools/hal_crypto`` asks for the cone back to the
    flip-flops, which is the right question when the layer under study sits on
    a register bank.  Here it is not: theta, rho/pi and chi are three layers
    deep between registers, and the question "which theta net does this chi
    cell read" is exactly the one that recovers the rotation offsets.
    """
    keys = []
    for pin in primitives.LCELL_DATA_PINS:
        bits = instance.connections.get(pin)
        if not bits:
            continue
        resolved = model.resolve(bits[0])
        if resolved[0] == "net" and resolved[1] not in keys:
            keys.append(resolved[1])
    return keys


def cell_function(model, instance):
    """``(input keys, truth table)`` of one cell over its own inputs."""
    keys = cell_inputs(model, instance)
    if not keys:
        return [], None
    table = model.cone(instance.connections["combout"][0].key, order=keys)
    return keys, table.restricted()


def _combout_key(instance):
    bits = instance.connections.get("combout")
    if not bits:
        return None
    return getattr(bits[0], "key", None)


def register_bits(model, bank="s"):
    """``{index: q net key}`` for one register bank, by flip-flop name."""
    result = {}
    for ff in model.ff_instances:
        name, index = _split(ff.name)
        if name != bank or index is None:
            continue
        bits = ff.connections.get("q")
        if bits:
            result[index] = getattr(bits[0], "key", None)
    return result


def _d_driver(model, ff):
    """The cell driving a flip-flop's ``d`` pin."""
    bits = ff.connections.get("d")
    if not bits:
        return None
    resolved = model.resolve(bits[0])
    if resolved[0] != "net":
        return None
    driver = model.driver(resolved[1])
    return driver[0] if driver else None


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

    Unlike walkthrough 13 -- where all 301 registers ran every cycle and the
    grouping said nothing -- this design *idles*, and it idles through the
    flip-flop's ``ena`` pin rather than a third multiplexer input.  So the
    grouping immediately separates "the machine" (everything gated by one net)
    from the two flags that are safe to update unconditionally, and the gating
    net is the first thing worth naming.
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
            name, _index = _split(getattr(bits[0], "key", ""))
            banks.setdefault(name, set()).add(ff.name)

    return {
        "flip_flops": len(model.ff_instances),
        "enable_groups": [
            {
                "clk": key[0],
                "clrn": key[1],
                "ena": key[2],
                "size": len(names),
                "banks": sorted({_split(name)[0] for name in names}),
            }
            for key, names in sorted(buckets.items(), key=lambda kv: -len(kv[1]))
        ],
        "q_vectors": {
            name: len(names) for name, names in sorted(banks.items())
        },
    }


# ---------------------------------------------------------------------------
# step 3 -- theta, and the 5 x 5 x 8 grid that falls out of it
# ---------------------------------------------------------------------------


def parity_cells(model):
    """Cells whose cone is a pure XOR of exactly five flip-flop outputs.

    No name is consulted.  A cell qualifies when its cone, taken back to the
    registers, depends on five sources and is the XOR of all five -- which on
    this export selects exactly the column-parity layer and nothing else (the
    round-constant cells also read five registers and are emphatically not an
    XOR of them).
    """
    found = []
    for instance in model.lcells:
        key = _combout_key(instance)
        if key is None:
            continue
        try:
            support = model.support(key)
        except (UnsupportedCell, ConeTooWide):
            continue
        if len(support) != 5:
            continue
        if not all(model.register_of(name) is not None for name in support):
            continue
        try:
            table = model.cone(key).restricted()
        except (UnsupportedCell, ConeTooWide):
            continue
        if table.arity != 5 or not table.is_xor_of_all_inputs():
            continue
        found.append((instance.name, key, frozenset(support)))
    return sorted(found)


def _state_index(key):
    name, index = _split(key)
    return index if name == "s" else None


def grid(model):
    """The 5 x 5 x 8 geometry, derived from the parity and theta layers.

    Returns a dict with the column classes, the two neighbour maps, and the
    ``x``/``z`` coordinate of every column class -- ``x`` up to its origin and
    ``z`` up to origin *and* direction, both of which :func:`step_iota` pins.
    """
    cells = parity_cells(model)
    classes = []
    for _name, key, support in cells:
        members = sorted(_state_index(name) for name in support)
        if any(index is None for index in members):
            raise SystemExit("a parity cell reads a non-state register")
        classes.append((key, tuple(members)))
    classes.sort(key=lambda entry: entry[1])
    if len(classes) != 40:
        raise SystemExit("expected 40 column-parity cells, found {}".format(len(classes)))

    class_of_bit = {}
    for number, (_key, members) in enumerate(classes):
        for index in members:
            if index in class_of_bit:
                raise SystemExit("bit {} is in two parity classes".format(index))
            class_of_bit[index] = number
    parity_net_class = {key: number for number, (key, _m) in enumerate(classes)}

    # Each theta cell reads one flip-flop and two parity nets.  All the theta
    # cells of one class read the same pair of classes, so the pair is a
    # property of the class.
    neighbours = {}
    theta = {}
    for instance in model.lcells:
        keys = cell_inputs(model, instance)
        if len(keys) != 3:
            continue
        indices = [_state_index(key) for key in keys]
        state = [index for index in indices if index is not None]
        parities = [key for key in keys if key in parity_net_class]
        if len(state) != 1 or len(parities) != 2:
            continue
        _inputs, table = cell_function(model, instance)
        if table is None or table.arity != 3 or not table.is_xor_of_all_inputs():
            continue
        bit = state[0]
        theta[bit] = _combout_key(instance)
        pair = tuple(sorted(parity_net_class[key] for key in parities))
        current = neighbours.setdefault(class_of_bit[bit], pair)
        if current != pair:
            raise SystemExit(
                "class {} reads two different parity pairs".format(class_of_bit[bit])
            )
    if len(theta) != 200 or len(neighbours) != 40:
        raise SystemExit(
            "expected 200 theta cells over 40 classes, found {} over {}".format(
                len(theta), len(neighbours)
            )
        )

    # Separate the unrotated neighbour from the rotated one.  Walking five
    # steps and coming back is only possible with the unrotated one every time:
    # a walk with `a` unrotated and `b` rotated steps moves the column by b - a
    # (mod 5) and the bit by -b (mod 8), so returning needs b = 0 and a = 5.
    alpha = {}
    for number, pair in neighbours.items():
        closed = [
            path
            for path in itertools.product((0, 1), repeat=5)
            if _walk(number, path, neighbours) == number
        ]
        if len(closed) != 1:
            raise SystemExit(
                "class {} has {} closed five-step walks".format(number, len(closed))
            )
        alpha[number] = pair[closed[0][0]]
    beta = {
        number: (pair[0] if alpha[number] == pair[1] else pair[1])
        for number, pair in neighbours.items()
    }
    gamma = {number: alpha[beta[number]] for number in neighbours}

    columns = _cycles(gamma)      # constant x, eight bits each
    slices = _cycles(alpha)       # constant z, five columns each
    if sorted(len(cycle) for cycle in columns) != [8] * 5:
        raise SystemExit("the column map is not five cycles of eight")
    if sorted(len(cycle) for cycle in slices) != [5] * 8:
        raise SystemExit("the bit-slice map is not eight cycles of five")

    return {
        "classes": classes,
        "class_of_bit": class_of_bit,
        "parity_net_class": parity_net_class,
        "neighbours": neighbours,
        "alpha": alpha,
        "beta": beta,
        "gamma": gamma,
        "columns": columns,
        "slices": slices,
        "theta": theta,
    }


def _walk(start, path, neighbours):
    node = start
    for choice in path:
        node = neighbours[node][choice]
    return node


def _cycles(permutation):
    """The cycles of a permutation given as a dict, each as an ordered tuple."""
    seen = set()
    result = []
    for start in sorted(permutation):
        if start in seen:
            continue
        cycle = [start]
        seen.add(start)
        node = permutation[start]
        while node != start:
            cycle.append(node)
            seen.add(node)
            node = permutation[node]
        result.append(tuple(cycle))
    return result


def step_parity(netlist, model):
    """theta as five XOR parity planes, and the grid they reveal."""
    geometry = grid(model)
    return {
        "parity_cells": len(geometry["classes"]),
        "sources_per_cell": 5,
        "every_cell_is_a_pure_xor": True,
        "state_bits_partitioned": sum(len(m) for _k, m in geometry["classes"]),
        "column_classes": [list(members) for _key, members in geometry["classes"]],
        "unrotated_neighbour_map_order": 5,
        "columns": [list(cycle) for cycle in geometry["columns"]],
        "bit_slices": [list(cycle) for cycle in geometry["slices"]],
        "lane_width": len(geometry["columns"][0]),
        "columns_in_the_array": len(geometry["columns"]),
        "rows_per_column": len(geometry["classes"][0][1]),
        "grid": "{} x {} lanes of {} bits".format(
            len(geometry["columns"]),
            len(geometry["classes"][0][1]),
            len(geometry["columns"][0]),
        ),
    }


def step_theta(netlist, model):
    """Every theta cell, checked against the recovered neighbour structure."""
    geometry = grid(model)
    alpha, beta = geometry["alpha"], geometry["beta"]
    gamma = geometry["gamma"]
    arities = {}
    for bit, key in sorted(geometry["theta"].items()):
        arities[len(model.support(key))] = arities.get(len(model.support(key)), 0) + 1
    return {
        "theta_cells": len(geometry["theta"]),
        "inputs_per_cell": "1 flip-flop + 2 parity nets",
        "cone_width_histogram": {str(k): v for k, v in sorted(arities.items())},
        "unrotated_neighbour_is_a_five_cycle": all(
            _orbit_length(alpha, number) == 5 for number in alpha
        ),
        "rotated_neighbour_composed_with_it_is_an_eight_cycle": all(
            _orbit_length(gamma, number) == 8 for number in gamma
        ),
        "reading": (
            "each theta cell is s ^ C[column-1][same bit] ^ C[column+1][one bit "
            "lower]: the unrotated parity neighbour fixes the bit index and steps "
            "the column, the rotated one steps both"
        ),
        "beta_is_the_other_neighbour": all(
            beta[number] != alpha[number] for number in alpha
        ),
    }


def _orbit_length(permutation, start):
    node = permutation[start]
    length = 1
    while node != start:
        node = permutation[node]
        length += 1
    return length


# ---------------------------------------------------------------------------
# step 4 -- chi: the roles, the rows, and the 5-bit table
# ---------------------------------------------------------------------------


def chi_cells(model):
    """Cells reading exactly three theta nets, with their operand roles.

    ``chi = b0 ^ (~b1 & b2)`` has algebraic normal form ``b0 + b2 + b1*b2``, so
    the three operands are told apart by the ANF alone and no ordering
    convention is needed: ``b1`` is the variable that appears *only* in the
    product, ``b2`` in both the product and a linear term, ``b0`` only linear.
    """
    geometry = grid(model)
    theta_nets = {key: bit for bit, key in geometry["theta"].items()}
    found = {}
    for instance in model.lcells:
        keys = cell_inputs(model, instance)
        if len(keys) != 3 or not all(key in theta_nets for key in keys):
            continue
        _inputs, table = cell_function(model, instance)
        if table is None or table.arity != 3:
            continue
        terms = boolfunc.anf_terms(table)
        linear = {term[0] for term in terms if len(term) == 1}
        products = [term for term in terms if len(term) == 2]
        if len(products) != 1 or len(linear) != 2:
            continue
        if any(len(term) > 2 for term in terms):
            continue
        product = set(products[0])
        only_product = product - linear
        both = product & linear
        only_linear = linear - product
        if len(only_product) != 1 or len(both) != 1 or len(only_linear) != 1:
            continue
        roles = (
            theta_nets[next(iter(only_linear))],
            theta_nets[next(iter(only_product))],
            theta_nets[next(iter(both))],
        )
        found[_combout_key(instance)] = {
            "cell": instance.name,
            "b0": roles[0],
            "b1": roles[1],
            "b2": roles[2],
            "table": table,
        }
    return geometry, found


def chi_destinations(model, chi):
    """``{destination state bit: chi output net}``.

    A chi output does not reach a flip-flop directly: the load multiplexer sits
    between them.  Walking one cell forward is the whole of it, and doing so is
    also what proves the multiplexer is a multiplexer.
    """
    result = {}
    for ff in model.ff_instances:
        name, index = _split(ff.name)
        if name != "s" or index is None:
            continue
        driver = _d_driver(model, ff)
        if driver is None:
            continue
        for key in cell_inputs(model, driver):
            if key in chi:
                result[index] = key
                break
    return result


def step_chi(netlist, model):
    """The 5-bit row map, extracted, and what the S-box pass makes of it."""
    geometry, chi = chi_cells(model)
    destinations = chi_destinations(model, chi)
    if len(chi) != 200 or len(destinations) != 200:
        raise SystemExit(
            "expected 200 chi cells feeding 200 registers, found {} / {}".format(
                len(chi), len(destinations)
            )
        )

    # b1 is "the next lane along the row", so following b1 closes a cycle of
    # five: that is the row, and it is the only thing that groups lanes.
    source_to_output = {entry["b0"]: key for key, entry in chi.items()}
    output_to_dest = {key: bit for bit, key in destinations.items()}
    rows = []
    seen = set()
    for key in sorted(chi):
        if key in seen:
            continue
        cycle = [key]
        seen.add(key)
        node = source_to_output[chi[key]["b1"]]
        while node != key:
            cycle.append(node)
            seen.add(node)
            node = source_to_output[chi[node]["b1"]]
        rows.append(cycle)
    lengths = sorted(len(row) for row in rows)

    # One 5-bit substitution per row cycle, with the inputs in the cycle's own
    # order.  No net-name ordering is involved, which is why the table comes out
    # equal to the published one rather than a bit permutation of it.
    tables = set()
    for cycle in rows:
        order = [chi[key]["b0"] for key in cycle]
        table = []
        for value in range(32):
            assignment = {}
            for position, bit in enumerate(order):
                assignment[geometry["theta"][bit]] = (value >> position) & 1
            word = 0
            for position, key in enumerate(cycle):
                word |= chi[key]["table"].evaluate(assignment) << position
            table.append(word)
        tables.add(tuple(table))
    if len(tables) != 1:
        raise SystemExit("the {} rows are not all the same map".format(len(rows)))
    extracted = boolfunc.Sbox(5, list(next(iter(tables))))
    reference = known.SBOXES["keccak_chi_5"]["sbox"]
    match = boolfunc.match_sbox(extracted, reference, name="keccak_chi_5")

    pass_result = sbox.identify(model)
    return {
        "chi_cells": len(chi),
        "inputs_per_cell": 3,
        "row_cycles": len(rows),
        "row_cycle_lengths": lengths,
        "instances_of_the_same_5_bit_map": len(rows),
        "table": list(extracted.table),
        "table_hex": " ".join("{:02X}".format(value) for value in extracted.table),
        "algebraic_degree": extracted.algebraic_degree(),
        "differential_uniformity": extracted.differential_uniformity(),
        "is_bijective": extracted.is_bijective(),
        "is_affine": extracted.is_affine(),
        "library_match": match.as_dict() if match else None,
        "anf": boolfunc.anf_string(
            extracted.coordinate(0, names=["x0", "x1", "x2", "x3", "x4"])
        ),
        "sbox_pass_found": len(pass_result["sboxes"]),
        "sbox_pass_rejections": [entry["reason"] for entry in pass_result["rejected"]],
        "why_the_pass_cannot_see_it": (
            "the pass cuts cones at the flip-flops, and in this architecture chi "
            "reads the register bank through theta -- the widest chi cone here "
            "depends on {} of them".format(
                max(len(model.support(key)) for key in chi)
            )
        ),
        "chi_cone_width_at_the_registers": sorted(
            {len(model.support(key)) for key in chi}
        ),
        "destinations": len(destinations),
        "output_to_dest_is_a_bijection": len(set(output_to_dest.values())) == 200,
    }


# ---------------------------------------------------------------------------
# step 5 -- rho and pi: the layer that costs nothing
# ---------------------------------------------------------------------------


def coordinates(model):
    """A full ``(x, y, z)`` label for every state bit, derived from the wiring.

    The three coordinates come from three different places, and the docstring
    at the top of the file says which is which.  Returns the label map plus the
    lane grouping, the rotation offsets and the recovered ``pi``.
    """
    geometry, chi = chi_cells(model)
    destinations = chi_destinations(model, chi)
    class_of_bit = geometry["class_of_bit"]

    # rho o pi as a 200-bit permutation: destination state bit -> source bit.
    permutation = {}
    for bit, key in destinations.items():
        permutation[bit] = chi[key]["b0"]
    if sorted(permutation.values()) != sorted(permutation):
        raise SystemExit("the recovered rho/pi map is not a permutation")

    # x: alpha steps the column by -1, so the alpha orbit of any class is the
    # five columns in order.  The origin is fixed in `iota`; here every column
    # gets a provisional label and `iota` rotates them.
    alpha = geometry["alpha"]
    column_of_class = {}
    for number, cycle in enumerate(geometry["columns"]):
        for member in cycle:
            column_of_class[member] = number
    # Order the five columns along alpha, starting from column 0 of class 0.
    start = 0
    order = [column_of_class[start]]
    node = alpha[start]
    while column_of_class[node] != order[0]:
        order.append(column_of_class[node])
        node = alpha[node]
    if len(order) != 5:
        raise SystemExit("the column order is not five long")
    # alpha goes to column x-1, so walking it counts x downwards.
    x_of_column = {name: (-position) % 5 for position, name in enumerate(order)}

    # z: gamma steps the bit index by one within a column.  Direction and
    # origin are both a gauge until iota fixes them, so start with 0 at the
    # class each column cycle happens to begin with.
    gamma = geometry["gamma"]
    z_of_class = {}
    for cycle in geometry["columns"]:
        node = cycle[0]
        for step in range(len(cycle)):
            z_of_class[node] = step
            node = gamma[node]

    lanes = {}
    for bit in sorted(permutation):
        lanes.setdefault(
            (
                x_of_column[column_of_class[class_of_bit[bit]]],
                x_of_column[column_of_class[class_of_bit[permutation[bit]]]],
            ),
            [],
        ).append(bit)
    if sorted(len(members) for members in lanes.values()) != [8] * 25:
        raise SystemExit(
            "the (destination column, source column) grouping is not 25 lanes of 8"
        )

    # Rows: chi's b1 chain groups the *destination* lanes five at a time.
    lane_of_bit = {}
    for label, members in lanes.items():
        for bit in members:
            lane_of_bit[bit] = label
    source_to_output = {entry["b0"]: key for key, entry in chi.items()}
    dest_of_output = {key: bit for bit, key in destinations.items()}
    row_neighbour = {}
    for bit, key in destinations.items():
        following = dest_of_output[source_to_output[chi[key]["b1"]]]
        current = row_neighbour.setdefault(lane_of_bit[bit], lane_of_bit[following])
        if current != lane_of_bit[following]:
            raise SystemExit("a lane has two different row neighbours")
    rows = _cycles(row_neighbour)
    if sorted(len(cycle) for cycle in rows) != [5] * 5:
        raise SystemExit("the row grouping is not five cycles of five")

    # pi maps a whole lane to a whole lane, and the source lane of every
    # destination lane in column X lies in one and the same row -- which is
    # what labels the rows.  (The published pi says that row is X itself; this
    # derives the labelling and the guide then checks the published relation.)
    row_of_lane = {}
    for number, cycle in enumerate(rows):
        for label in cycle:
            row_of_lane[label] = number
    label_of_row = {}
    for label, members in lanes.items():
        source_lane = lane_of_bit[permutation[members[0]]]
        previous = label_of_row.setdefault(row_of_lane[source_lane], label[0])
        if previous != label[0]:
            raise SystemExit(
                "row {} is the source of two different destination columns".format(
                    row_of_lane[source_lane]
                )
            )
    if sorted(label_of_row.values()) != [0, 1, 2, 3, 4]:
        raise SystemExit("the row labels are not a permutation of 0..4")
    y_of_lane = {label: label_of_row[row_of_lane[label]] for label in lanes}

    return {
        "geometry": geometry,
        "chi": chi,
        "destinations": destinations,
        "permutation": permutation,
        "lanes": lanes,
        "lane_of_bit": lane_of_bit,
        "x_of_bit": {
            bit: x_of_column[column_of_class[class_of_bit[bit]]]
            for bit in permutation
        },
        "z_of_bit": {bit: z_of_class[class_of_bit[bit]] for bit in permutation},
        "y_of_lane": y_of_lane,
        "rows": rows,
    }


def step_rhopi(netlist, model, gauge=None):
    """The 25 rotation offsets and the lane transposition, read off the wiring."""
    labels = coordinates(model)
    gauge = gauge or iota_gauge(model, labels)
    rho_pi = labels["permutation"]
    lanes = labels["lanes"]
    z_of_bit = labels["z_of_bit"]
    lane_of_bit = labels["lane_of_bit"]

    offsets = {}
    lane_map = {}
    for label, members in sorted(lanes.items()):
        shifts = {
            (gauge["z"](z_of_bit[bit]) - gauge["z"](z_of_bit[rho_pi[bit]])) % 8
            for bit in members
        }
        if len(shifts) != 1:
            raise SystemExit(
                "lane {} does not rotate by a single amount: {}".format(label, shifts)
            )
        source_label = lane_of_bit[rho_pi[members[0]]]
        destination = (gauge["x"](label[0]), labels["y_of_lane"][label])
        source = (gauge["x"](source_label[0]), labels["y_of_lane"][source_label])
        offsets[source] = shifts.pop()
        lane_map[source] = destination

    recovered = [[offsets[(x, y)] for y in range(5)] for x in range(5)]
    published = [[value % 8 for value in row] for row in PUBLISHED_RHO]
    pi_holds = all(
        lane_map[(x, y)] == (y, (2 * x + 3 * y) % 5)
        for x in range(5)
        for y in range(5)
    )

    # What the two tiers of the permutation pass see on *this* export, so the
    # hand recovery above can be compared against them rather than asserted to
    # be the only way.  The wiring tier wants a net that is a differently
    # indexed copy of another net; the cone-support tier wants each destination
    # bit's next state to read exactly one source bit through one cell.
    wiring = permutation.find_permutations(model)
    cone = permutation.cone_support_maps(model, wiring=wiring)

    return {
        "cells_spent_on_rho_and_pi": 0,
        "nets_named_after_a_rotation": 0,
        "wiring_tier_layers": [
            entry["kind"] for entry in wiring if entry["kind"] != "identity"
        ],
        "cone_support_tier_maps": [
            {
                "kind": entry["kind"],
                "source": entry["source"],
                "destination": entry["destination"],
                "width": entry["width"],
                "bits_observed": entry["bits_observed"],
                "matches": [match["name"] for match in entry["matches"]],
            }
            for entry in cone
        ],
        "recovered_rho_mod_8_spec": recovered,
        "published_rho_mod_8": published,
        "rho_matches_published": recovered == published,
        "lanes_rotated_by_zero": sorted(
            [x, y] for x in range(5) for y in range(5) if recovered[x][y] == 0
        ),
        "recovered_pi_spec": {
            "{},{}".format(x, y): list(lane_map[(x, y)])
            for x in range(5)
            for y in range(5)
        },
        "pi_is_lane_x_y_to_y_2x_plus_3y": pi_holds,
        "how": (
            "the composite rho-then-pi is the 200-bit map from each chi cell's "
            "b0 operand to the flip-flop its output reaches; lanes are the "
            "(destination column, source column) pairs, rows are chi's own "
            "b1 chain, and the rotation amount is the bit-index difference "
            "across a lane, which is constant or the recovery fails"
        ),
        "and_what_the_tool_gives_you": (
            "on this export the wiring tier reports no layer at all, and the "
            "cone-support tier reports {}. Neither ever yields the 25 offsets "
            "or the lane map: a tier returns an index map over two vectors, and "
            "turning that into r[x][y] and (x,y) -> (y, 2x+3y) needs the lane "
            "geometry of the parity and chi steps".format(
                "none either, because chi sits between theta and the register"
                if not cone
                else "the whole {}-bit map, {} of {} links".format(
                    cone[0]["width"], cone[0]["bits_observed"], cone[0]["width"]
                )
            )
        ),
    }


# ---------------------------------------------------------------------------
# step 6 -- iota: the round constants, and the bit-origin gauge
# ---------------------------------------------------------------------------


def round_constant_cells(model):
    """Cells whose cone is exactly the round counter, feeding a state bit.

    They are found by what they read and where they go, not by name: five
    counter flip-flops in, and the only consumer is a next-state multiplexer of
    the state register.
    """
    counter = sorted(register_bits(model, "rnd"))
    counter_nets = {
        key for index, key in register_bits(model, "rnd").items()
    }
    if len(counter) != 5:
        raise SystemExit("expected a 5-bit round counter, found {}".format(len(counter)))
    consumers = {}
    for ff in model.ff_instances:
        name, index = _split(ff.name)
        if name != "s" or index is None:
            continue
        driver = _d_driver(model, ff)
        if driver is None:
            continue
        for key in cell_inputs(model, driver):
            consumers.setdefault(key, []).append(index)

    found = {}
    for instance in model.lcells:
        key = _combout_key(instance)
        if key is None or key not in consumers:
            continue
        try:
            support = model.support(key)
        except (UnsupportedCell, ConeTooWide):
            continue
        if not support <= counter_nets:
            continue
        table = (
            model.cone(key, order=sorted(counter_nets)).truth_table()
            if support
            else None
        )
        found[key] = {
            "cell": instance.name,
            "reaches": consumers[key],
            "table": table,
        }
    return found, sorted(counter_nets)


def iota_gauge(model, labels=None):
    """Fix the origin and direction of ``z``, and the origin of ``x``.

    ``iota`` touches exactly one lane, which is therefore lane ``(0,0)``; four
    of its eight bit positions are constant zero across all eighteen rounds,
    and the cyclic pattern of which four is asymmetric enough that exactly one
    (origin, direction) pair reproduces the published schedule.  That is a
    **gauge**, stated as one: the netlist fixes the pattern, the publication
    fixes which rotation of it to call bit zero.
    """
    labels = labels or coordinates(model)
    cells, counter = round_constant_cells(model)
    reached = sorted({bit for entry in cells.values() for bit in entry["reaches"]})
    lane = {labels["lane_of_bit"][bit] for bit in reached}
    if len(lane) != 1 or len(reached) != 8:
        raise SystemExit(
            "iota touches {} bits in {} lane(s)".format(len(reached), len(lane))
        )
    iota_lane = lane.pop()

    # Which of the lane's eight bit positions ever changes with the round.
    varying = set()
    for key, entry in cells.items():
        table = entry["table"]
        constant = table is None or table.restricted().arity == 0
        for bit in entry["reaches"]:
            if not constant:
                varying.add(labels["z_of_bit"][bit])
    published = {0, 1, 3, 7}
    candidates = []
    for direction in (1, -1):
        for origin in range(8):
            mapped = {(direction * (value - origin)) % 8 for value in varying}
            if mapped == published:
                candidates.append((direction, origin))
    if len(candidates) != 1:
        raise SystemExit(
            "the iota bit pattern fixes {} (direction, origin) pairs, not one; "
            "measured positions {}".format(len(candidates), sorted(varying))
        )
    direction, origin = candidates[0]
    x_origin = iota_lane[0]
    return {
        "z": lambda value: (direction * (value - origin)) % 8,
        "x": lambda value: (value - x_origin) % 5,
        "direction": direction,
        "origin": origin,
        "iota_lane": iota_lane,
        "varying_positions_before_the_gauge": sorted(varying),
    }


def step_iota(netlist, model):
    """Recover the eighteen round constants from the counter cones."""
    labels = coordinates(model)
    gauge = iota_gauge(model, labels)
    cells, counter = round_constant_cells(model)
    rounds = round_count(model)

    constants = []
    for value in range(rounds):
        assignment = {
            name: (value >> position) & 1 for position, name in enumerate(counter)
        }
        word = 0
        for entry in cells.values():
            table = entry["table"]
            bit = table.evaluate(assignment) if table is not None else 0
            for reached in entry["reaches"]:
                word |= bit << gauge["z"](labels["z_of_bit"][reached])
        constants.append(word)

    constant_cells = sum(
        1
        for entry in cells.values()
        if entry["table"] is None or entry["table"].restricted().arity == 0
    )
    return {
        "round_constant_cells": len(cells),
        "of_those_constant_zero": constant_cells,
        "iota_lane_spec": [gauge["x"](gauge["iota_lane"][0]), 0],
        "state_bits_touched": 8,
        "recovered_rc": constants,
        "recovered_rc_hex": " ".join("{:02X}".format(value) for value in constants),
        "published_rc_hex": " ".join("{:02X}".format(v) for v in PUBLISHED_RC),
        "matches_published": constants == list(PUBLISHED_RC[:rounds]),
        "nonzero_bit_positions_spec": sorted(
            {
                position
                for value in constants
                for position in range(8)
                if (value >> position) & 1
            }
        ),
        "rounds_with_no_iota_at_all": [
            index for index, value in enumerate(constants) if value == 0
        ],
        "bit_origin_gauge": {
            "measured_varying_positions": gauge["varying_positions_before_the_gauge"],
            "direction": gauge["direction"],
            "origin": gauge["origin"],
            "note": (
                "the netlist fixes which four of the eight positions iota ever "
                "touches; the publication fixes which rotation of that pattern is "
                "bit zero, and exactly one of the sixteen (direction, origin) "
                "choices reproduces it"
            ),
        },
    }


# ---------------------------------------------------------------------------
# step 7 -- the schedule: how many rounds, and what gates them
# ---------------------------------------------------------------------------


def round_count(model):
    """The round count, from the single terminal-count cell over the counter."""
    counter = register_bits(model, "rnd")
    counter_nets = sorted(counter.values())
    best = None
    for instance in model.lcells:
        key = _combout_key(instance)
        if key is None:
            continue
        try:
            support = model.support(key)
        except (UnsupportedCell, ConeTooWide):
            continue
        if set(support) != set(counter_nets):
            continue
        table = model.cone(key, order=counter_nets).restricted()
        if table.arity != len(counter_nets):
            continue
        ones = [
            value
            for value in range(1 << len(counter_nets))
            if table.evaluate(
                {
                    name: (value >> position) & 1
                    for position, name in enumerate(counter_nets)
                }
            )
        ]
        if len(ones) == 1:
            best = ones[0] + 1
    if best is None:
        raise SystemExit("no single-value terminal count over the round counter")
    return best


def step_rounds(netlist, model):
    """The counter, the terminal count, the enable and the two flags."""
    counter = register_bits(model, "rnd")
    rounds = round_count(model)
    groups = step_registers(netlist, model)["enable_groups"]
    gated = max(groups, key=lambda entry: entry["size"])
    return {
        "counter_width": len(counter),
        "rounds": rounds,
        "cycles_from_accepted_start_to_done": rounds + 1,
        "terminal_count_value": rounds - 1,
        "counter_reaches_its_terminal_count_once_per_permutation": True,
        "gated_registers": gated["size"],
        "gating_net": gated["ena"],
        "ungated_registers": sum(
            entry["size"] for entry in groups if entry["ena"] != gated["ena"]
        ),
        "reading": (
            "one counter, one five-input terminal test and one enable net: "
            "unlike walkthrough 13's 1152 there is no modulus to factor, because "
            "eighteen fits in a single ALM's six inputs"
        ),
    }


# ---------------------------------------------------------------------------
# step 8 -- drive the black box
# ---------------------------------------------------------------------------


def state_word(hexadecimal):
    """The 200-bit port value for a 25-byte state string.

    The sponge literature writes the state as a byte string, and a port is a
    bit vector.  This is the whole of that convention: byte *n* of the printed
    string is bits ``8n .. 8n+7`` of the port, least significant bit first.
    ``check.py`` requires the published vectors back byte for byte, which is
    what makes it a derivation rather than a guess.
    """
    word = 0
    for position, value in enumerate(bytes.fromhex(hexadecimal)):
        word |= value << (8 * position)
    return word


def state_hex(word):
    return bytes((word >> (8 * index)) & 0xFF for index in range(25)).hex().upper()


def _run_vector(netlist, word, limit=200):
    simulator = Simulator(netlist)
    simulator.reset()
    for name, value in (("start", 0), ("din", 0), ("rst_n", 0)):
        simulator.set_input(name, value)
    simulator.apply_async_clear()
    simulator.set_input("rst_n", 1)

    simulator.set_input("start", 1)
    simulator.set_input("din", word)
    simulator.clock()
    simulator.set_input("start", 0)
    simulator.set_input("din", 0)

    cycles = 1
    busy = 0
    while not simulator.get_output("done"):
        busy += simulator.get_output("busy")
        simulator.clock()
        cycles += 1
        if cycles > limit:
            raise SystemExit("done never rose within {} cycles".format(limit))
    return cycles, busy, simulator.get_output("dout")


def step_vectors(netlist, model):
    """Run the netlist itself on the published Keccak-f[200] vectors.

    Structure said "five parity planes, these rotation offsets, this 5-bit
    substitution, eighteen rounds".  This step never reads that: it drives the
    exported netlist as a black box with vectors out of the literature and looks
    at what comes out.  Two independent methods agreeing is the evidence; either
    one alone is a hypothesis.
    """
    results = []
    for label, input_hex, expected in TEST_VECTORS:
        cycles, busy, out = _run_vector(netlist, state_word(input_hex))
        got = state_hex(out)
        results.append(
            {
                "vector": label,
                "input": input_hex,
                "cycles_from_accepted_start_to_done": cycles,
                "rounds_run": busy,
                "output": got,
                "published": expected,
                "matches_published_vector": got == expected,
            }
        )
    return {
        "vectors": results,
        "all_match": all(entry["matches_published_vector"] for entry in results),
        "rounds_run": sorted({entry["rounds_run"] for entry in results}),
        "headline": results[HEADLINE_VECTOR],
    }


# ---------------------------------------------------------------------------
# step 9 -- what the crypto identifier makes of both exports
# ---------------------------------------------------------------------------


def _identify(path):
    parsed = vo_netlist.parse_file(path)
    evidence = classify.run_passes(parsed)
    decision = classify.verdict(evidence)
    boxes = evidence["sbox"]["sboxes"]
    matched = [entry for entry in boxes if entry["matches"]]
    names = sorted({m["name"] for entry in boxes for m in entry["matches"]})
    tiers = sorted({m["tier"] for entry in boxes for m in entry["matches"]})
    return {
        "export": os.path.basename(path),
        "instances": len(parsed.instances),
        "family": decision["family"],
        "families_present": decision["families_present"],
        "style": decision["style"],
        "style_rationale": decision["style_rationale"],
        "confidence": decision["confidence"],
        "sboxes_extracted": len(boxes),
        "sboxes_matched": len(matched),
        "matched_names": names,
        "match_tiers": tiers,
        "rejections": [entry["reason"] for entry in evidence["sbox"]["rejected"]],
        # The permutation pass has two tiers and they disagree here, which is
        # the point of step 5: the *wiring* tier needs a net that is a
        # differently-indexed copy of another net, and rho/pi is not a net at
        # all; the *cone-support* tier needs each destination bit's next state
        # to read exactly one source bit through one cell, which is true in one
        # of these two exports and false in the other.
        "wiring_permutation_layers": [
            entry["kind"]
            for entry in evidence["permutations"]
            if entry["kind"] != "identity"
        ],
        "cone_support_maps": [
            {
                "kind": entry["kind"],
                "source": entry["source"],
                "destination": entry["destination"],
                "width": entry["width"],
                "bits_observed": entry["bits_observed"],
                "matches": [match["name"] for match in entry["matches"]],
            }
            for entry in evidence.get("cone_permutations", ())
        ],
        "evidence": decision["evidence"][:4],
    }


def step_identify(netlist, model):
    """The same command on two exports of the same permutation.

    The only difference between them is where the register sits inside the
    round.  One is `none-detected`, the other is a sponge with forty chi
    instances, and neither answer is wrong: a structural negative is a statement
    about the netlist you were given.
    """
    canonical = _identify(EXPORT)
    retimed = _identify(RETIMED_EXPORT)
    return {
        "canonical": canonical,
        "retimed": retimed,
        "same_permutation": True,
        "style_is_undetermined_on_the_sponge": retimed["style"] == "undetermined",
        "reading": (
            "sponge is the one family the classifier refuses to place on the "
            "classical/PQC axis, because the same permutation is SHA-3 and is "
            "the extendable-output function inside ML-KEM, ML-DSA and SPHINCS+"
        ),
    }


# ---------------------------------------------------------------------------
# step 10 -- the same netlist, read by HAL
# ---------------------------------------------------------------------------


def step_hal(netlist, model):
    """Load ``netlist.hal.v`` through hal_py and re-derive the census + SCCs.

    A second reader and a second gate model.  The SCC decomposition sees **one**
    machine of 200 registers, because theta mixes every column into every other
    and chi mixes every row: after one round every state bit depends on most of
    the state, which is the diffusion property the permutation is designed for
    and is exactly why the component cannot be cut.  Only the parity and chi
    layers of steps 3 and 4 give it structure.
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
    ("parity", step_parity),
    ("theta", step_theta),
    ("chi", step_chi),
    ("rhopi", step_rhopi),
    ("iota", step_iota),
    ("rounds", step_rounds),
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
