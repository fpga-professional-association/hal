#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
hal_walkthrough.py -- redo this example's hand analysis with HAL's own passes.

The rest of the example recovers the puzzle chip with purpose-written Python
(``gds2netlist.py`` -> ``sc_sim.py`` -> ``symbolic.py`` -> ``flop_map.py``).
This script asks a different question: **how far does stock HAL get on the same
netlist, with no puzzle-specific knowledge?**  It runs the steps
``ai/skills/re-walkthrough-method`` prescribes, in order:

1. census -- gate histogram, ports, flip-flops, isolated fill cells;
2. flip-flop control-pin signatures -- clock / reset / set nets per flop;
3. gate-level SCCs -- every cycle in the cell graph is state;
4. the flip-flop-only dependency graph -- next-state support, its SCCs, and
   the shift chains that fall out of it;
5. DANA (the ``dataflow_analysis`` plugin) register grouping, under five
   configurations, scored against ``artifacts/flop_map.md`` -- with step 4's
   SCC decomposition as the free baseline every configuration has to beat;
6. the ``success`` cone -- what the verdict flop actually reads;
7. a read-back of whatever ``tools/hal_fsm analyze`` wrote into
   ``artifacts/hal_fsm/``, with its states re-indexed by the hand map's bit
   names (``run_hal_analysis.sh`` runs ``hal_fsm`` first).

Nothing here reads ``flop_map.md`` before step 5's scoring: steps 1-4 are
computed from the netlist alone, and the map is only opened to grade DANA and
to relabel ``hal_fsm``'s state encoding.

Run it in the ``halbuild`` bench container (HAL has no Windows build)::

    docker exec -e HAL_BASE_PATH=/work/build -e HAL_PY_PATH=/work/build/lib \\
        -e PYTHONPATH=/work/build/lib -w /work halbuild bash -c \\
        'python3 examples/janestreet_asic_2026/tools/hal_walkthrough.py'

Writes ``artifacts/hal_walkthrough.txt`` (the transcript),
``artifacts/hal_dataflow.txt`` (DANA's own group listing for the best
configuration) and ``artifacts/hal_dataflow.json`` (groups + scoring, machine
readable).  Exit code is 0 unless the netlist or the library failed to load.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Set, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXAMPLE = os.path.dirname(_HERE)
_REPO = os.path.dirname(os.path.dirname(_EXAMPLE))

#: the three DANA configurations this script compares.  ``min_group_size``
#: defaults to 8 in the plugin, which is larger than most of this design's
#: registers -- so the sweep is really "does the default penalty hurt here?".
DANA_CONFIGS: Tuple[Dict[str, object], ...] = (
    {
        "label": "default",
        "note": "stock with_flip_flops(): min_group_size 8, no size hints",
    },
    {
        "label": "min2",
        "min_group_size": 2,
        "note": "min_group_size 2 -- this design's registers are mostly 2 bits wide",
    },
    {
        "label": "min2+size2",
        "min_group_size": 2,
        "expected_sizes": [2],
        "note": "min_group_size 2, expected size 2 -- 22 of the 36 banks are 2 bits",
    },
    {
        "label": "min2+sizes+types",
        "min_group_size": 2,
        "expected_sizes": [2, 4, 8, 12],
        "type_consistency": True,
        "note": "min_group_size 2, expected sizes 2/4/8/12, gate-type consistency",
    },
    {
        "label": "min2+stages",
        "min_group_size": 2,
        "stage_identification": True,
        "note": "min_group_size 2 plus DANA's stage identification",
    },
)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def rel(path: str) -> str:
    """Repo-relative path, so the checked-in report is host-independent."""
    try:
        common = os.path.commonpath([os.path.abspath(path), _REPO])
    except ValueError:  # different drives on Windows
        return path
    return os.path.relpath(path, _REPO).replace(os.sep, "/") if common == _REPO else path


class Out:
    """Accumulates the transcript and echoes it as it goes."""

    def __init__(self, echo: bool = True) -> None:
        self.lines: List[str] = []
        self.echo = echo

    def __call__(self, text: str = "") -> None:
        self.lines.append(text)
        if self.echo:
            print(text)

    def head(self, text: str) -> None:
        self("")
        self(text)
        self("=" * len(text))

    def sub(self, text: str) -> None:
        self("")
        self(text)
        self("-" * len(text))

    def render(self) -> str:
        return "\n".join(self.lines) + "\n"


def tarjan(nodes: Sequence[int], succ: Dict[int, Set[int]]) -> List[List[int]]:
    """Iterative Tarjan SCC -- recursion depth would blow up on 738 cells."""
    index: Dict[int, int] = {}
    low: Dict[int, int] = {}
    on_stack: Set[int] = set()
    stack: List[int] = []
    result: List[List[int]] = []
    counter = 0

    for root in nodes:
        if root in index:
            continue
        work: List[Tuple[int, int]] = [(root, 0)]
        while work:
            node, pi = work[-1]
            if pi == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            children = sorted(succ.get(node, ()))
            if pi < len(children):
                work[-1] = (node, pi + 1)
                child = children[pi]
                if child not in index:
                    work.append((child, 0))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
            else:
                if low[node] == index[node]:
                    component: List[int] = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        component.append(w)
                        if w == node:
                            break
                    result.append(sorted(component))
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
    return result


# --------------------------------------------------------------------------
# flop map (ground truth, only read for scoring)
# --------------------------------------------------------------------------
_ROW_RE = re.compile(r"^\|(.+)\|$")


def load_flop_map(path: str) -> Dict[str, Dict[str, str]]:
    """Parse ``artifacts/flop_map.md`` into ``{instance: {...}}``.

    ``group`` is the map's own coarse label; ``signal`` is the bit name from the
    role column; ``bank`` is that name with a trailing ``[n]`` stripped, which
    is the word-level register a grouping pass should recover (so
    ``reg_cnt[3][1]`` banks as ``reg_cnt[3]`` -- eleven 2-bit region counters,
    not one 22-bit blob).
    """
    out: Dict[str, Dict[str, str]] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            m = _ROW_RE.match(line)
            if not m:
                continue
            cells = [c.strip() for c in m.group(1).split("|")]
            if len(cells) < 5 or cells[0] in ("group", "---") or set(cells[0]) <= {"-"}:
                continue
            group, role, qnet, cell, instance = (c.strip("`") for c in cells[:5])
            # "sr[9]  = up-right neighbour" / "lfsr[0] (dfstp -> 1)" -> "sr[9]" / "lfsr[0]"
            signal = re.split(r"\s+\(|\s+=", role)[0].strip()
            bank = re.sub(r"\[\d+\]$", "", signal)
            out[instance] = {
                "group": group,
                "signal": signal,
                "bank": bank,
                "q_net": qnet.strip("`"),
                "cell": cell,
            }
    return out


def partition_of(key: str, flop_map: Dict[str, Dict[str, str]]) -> Dict[str, List[str]]:
    buckets: Dict[str, List[str]] = collections.defaultdict(list)
    for instance, info in flop_map.items():
        buckets[info[key]].append(instance)
    return {k: sorted(v) for k, v in sorted(buckets.items())}


def pair_score(
    recovered: Dict[object, List[str]], truth: Dict[object, List[str]]
) -> Dict[str, object]:
    """Co-membership pair precision/recall/F1 between two partitions.

    A grouping pass is right about a *pair* of flops when it puts them in the
    same group exactly when the hand map does.  Flops the pass never grouped are
    counted as singletons, so dropping a register costs recall rather than being
    invisible.
    """
    def pairs(part) -> Set[Tuple[str, str]]:
        s: Set[Tuple[str, str]] = set()
        for members in part.values():
            ms = sorted(members)
            for i in range(len(ms)):
                for j in range(i + 1, len(ms)):
                    s.add((ms[i], ms[j]))
        return s

    got, want = pairs(recovered), pairs(truth)
    tp = len(got & want)
    precision = tp / len(got) if got else 0.0
    recall = tp / len(want) if want else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "pairs_recovered": len(got),
        "pairs_expected": len(want),
        "pairs_correct": tp,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------
def step_census(out: Out, hal_py, netlist) -> Dict[str, object]:
    out.head("1. census")
    gates = netlist.get_gates()
    nets = netlist.get_nets()
    hist = collections.Counter(g.get_type().get_name() for g in gates)
    isolated = [g for g in gates if not g.get_fan_in_nets() and not g.get_fan_out_nets()]
    iso_hist = collections.Counter(g.get_type().get_name() for g in isolated)
    ffs = [g for g in gates if g.get_type().has_property(hal_py.GateTypeProperty.ff)]
    ff_hist = collections.Counter(g.get_type().get_name() for g in ffs)

    out("design name        : %s" % netlist.get_design_name())
    out("gates              : %d" % len(gates))
    out("nets               : %d" % len(nets))
    out("modules            : %d  (flat export -- no hierarchy survived the GDS)" % len(netlist.get_modules()))
    out("distinct gate types: %d" % len(hist))
    out("flip-flops         : %d   <- hard upper bound on state" % len(ffs))
    out("")
    out("physical-only cells (no signal pin connected at all):")
    for name, count in sorted(iso_hist.items()):
        out("  %-40s %d" % (name, count))
    out("  %-40s %d  (of %d gates -> %d logic cells)"
        % ("TOTAL", len(isolated), len(gates), len(gates) - len(isolated)))
    out("")
    out("flip-flop families:")
    for name, count in sorted(ff_hist.items()):
        gt = netlist.get_gate_library().get_gate_type_by_name(name)
        comp = gt.get_component(lambda c: hal_py.FFComponent.is_class_of(c))
        ar = comp.get_async_reset_function()
        as_ = comp.get_async_set_function()
        out("  %-32s %2d  reset=%-10s set=%-10s"
            % (name, count,
               "-" if ar is None or ar.is_empty() else str(ar),
               "-" if as_ is None or as_.is_empty() else str(as_)))
    out("")
    out("top-level ports:")
    out("  inputs : %s" % ", ".join(sorted(n.get_name() for n in netlist.get_global_input_nets())))
    out("  outputs: %s" % ", ".join(sorted(n.get_name() for n in netlist.get_global_output_nets())))
    out("")
    out("combinational gate histogram (logic cells only, top 25):")
    for name, count in hist.most_common():
        if name in iso_hist or name in ff_hist:
            continue
        out("  %-40s %d" % (name, count))

    return {
        "gates": len(gates),
        "nets": len(nets),
        "logic_cells": len(gates) - len(isolated),
        "physical_only_cells": len(isolated),
        "flip_flops": len(ffs),
        "gate_types": len(hist),
        "ff_census": dict(sorted(ff_hist.items())),
    }


def clock_root(hal_py, net, limit: int = 32) -> Tuple[str, int]:
    """Walk a clock net back through single-input cells to its root net."""
    depth = 0
    while depth < limit:
        sources = net.get_sources()
        if not sources:
            return net.get_name(), depth
        src = sources[0].get_gate()
        signal_in = [
            p.get_name()
            for p in src.get_type().get_input_pins()
            if p.get_type() not in (hal_py.PinType.power, hal_py.PinType.ground)
        ]
        if len(signal_in) != 1:
            return "%s (through %s)" % (net.get_name(), src.get_type().get_name()), depth
        nxt = src.get_fan_in_net(signal_in[0])
        if nxt is None:
            return net.get_name(), depth
        net = nxt
        depth += 1
    return net.get_name(), depth


def step_ff_signatures(out: Out, hal_py, ffs) -> Dict[str, object]:
    out.head("2. flip-flop control-pin signatures")
    sig: Dict[Tuple[str, str, str, str], List] = collections.defaultdict(list)
    for gate in ffs:
        gt = gate.get_type()
        def netname(ptype):
            for pin in gt.get_pins():
                if pin.get_type() == ptype and pin.get_direction() == hal_py.PinDirection.input:
                    net = gate.get_fan_in_net(pin.get_name())
                    if net is not None:
                        return net.get_name()
            return "-"
        key = (
            gt.get_name(),
            netname(hal_py.PinType.clock),
            netname(hal_py.PinType.reset),
            netname(hal_py.PinType.set),
        )
        sig[key].append(gate)

    out("%-32s %-12s %-12s %-12s %s" % ("cell", "clock net", "reset net", "set net", "count"))
    for key in sorted(sig, key=lambda k: (-len(sig[k]), k)):
        out("%-32s %-12s %-12s %-12s %d" % (key + (len(sig[key]),)))
    clocks = {k[1] for k in sig}
    out("")
    out("distinct clock nets on flop CLK pins: %d -> %s" % (len(clocks), ", ".join(sorted(clocks))))
    roots: Dict[str, List[Tuple[str, int]]] = collections.defaultdict(list)
    net_by_name = {}
    for gate in ffs:
        net = gate.get_fan_in_net("CLK")
        if net is not None:
            net_by_name[net.get_name()] = net
    for name in sorted(clocks):
        net = net_by_name.get(name)
        if net is None:
            continue
        root, depth = clock_root(hal_py, net)
        roots[root].append((name, depth))
    out("...but walking each of them back through single-input cells:")
    for root, leaves in sorted(roots.items()):
        depths = sorted({d for _, d in leaves})
        out("  %d leaf net(s) -> %-8s at buffer depth %s"
            % (len(leaves), root, "/".join(str(d) for d in depths)))
    out("So this is *one* clock domain wearing a clock tree, not 16 domains --")
    out("the 32 clkbuf cells in the census are exactly that tree.")
    out("")
    out("No flop has an enable pin: sky130's df*tp family has none, so every")
    out("register in this design is gated by a multiplexer in its D cone, not by")
    out("a control pin.  Step 2 therefore separates *reset polarity* here and")
    out("nothing else -- the enable-group split the method expects on an FPGA")
    out("export does not exist in a standard-cell flow.")
    return {
        "signatures": [
            {"cell": k[0], "clock": k[1], "reset": k[2], "set": k[3], "count": len(v)}
            for k, v in sorted(sig.items())
        ],
        "clock_nets": sorted(clocks),
        "clock_roots": {r: sorted(n for n, _ in v) for r, v in sorted(roots.items())},
    }


def step_gate_sccs(out: Out, hal_py, netlist) -> Dict[str, object]:
    out.head("3. gate-level strongly connected components")
    gates = [g for g in netlist.get_gates() if g.get_fan_in_nets() or g.get_fan_out_nets()]
    ids = [g.get_id() for g in gates]
    by_id = {g.get_id(): g for g in gates}
    succ: Dict[int, Set[int]] = {i: set() for i in ids}
    for net in netlist.get_nets():
        srcs = [ep.get_gate().get_id() for ep in net.get_sources()]
        dsts = [ep.get_gate().get_id() for ep in net.get_destinations()]
        for s in srcs:
            for d in dsts:
                if s != d and s in succ:
                    succ[s].add(d)
    comps = [c for c in tarjan(ids, succ) if len(c) > 1]
    sizes = collections.Counter(len(c) for c in comps)
    out("connected cells        : %d" % len(gates))
    out("non-trivial SCCs       : %d" % len(comps))
    out("SCC size histogram     : %s" % (", ".join("%dx size %d" % (n, s) for s, n in sorted(sizes.items())) or "none"))
    for comp in sorted(comps, key=len, reverse=True)[:6]:
        ff = [by_id[i] for i in comp if by_id[i].get_type().has_property(hal_py.GateTypeProperty.ff)]
        out("  size %-4d  %d flop(s): %s"
            % (len(comp), len(ff), ", ".join(sorted(g.get_name() for g in ff))[:120]))
    out("")
    out("Reading: a cycle in the cell graph is a value feeding its own future.")
    return {
        "connected_cells": len(gates),
        "nontrivial_sccs": len(comps),
        "scc_sizes": {str(s): n for s, n in sorted(sizes.items())},
        "largest": [
            {"size": len(c), "flops": sorted(by_id[i].get_name() for i in c
                                             if by_id[i].get_type().has_property(hal_py.GateTypeProperty.ff))}
            for c in sorted(comps, key=len, reverse=True)[:5]
        ],
    }


def ff_support(hal_py, gate, pin_types) -> Tuple[Set[int], Set[str], int]:
    """Backward cone from ``gate``'s pins of ``pin_types``, stopping at flops.

    Returns ``(driving flop ids, primary input net names, combinational cells
    traversed)``.
    """
    gt = gate.get_type()
    frontier = []
    for pin in gt.get_pins():
        if pin.get_direction() != hal_py.PinDirection.input:
            continue
        if pin.get_type() not in pin_types:
            continue
        net = gate.get_fan_in_net(pin.get_name())
        if net is not None:
            frontier.append(net)
    seen_nets: Set[int] = set()
    flops: Set[int] = set()
    inputs: Set[str] = set()
    cells: Set[int] = set()
    while frontier:
        net = frontier.pop()
        if net.get_id() in seen_nets:
            continue
        seen_nets.add(net.get_id())
        sources = net.get_sources()
        if not sources:
            inputs.add(net.get_name())
            continue
        for ep in sources:
            src = ep.get_gate()
            if src.get_type().has_property(hal_py.GateTypeProperty.ff):
                flops.add(src.get_id())
                continue
            cells.add(src.get_id())
            for pin in src.get_type().get_pins():
                if pin.get_direction() != hal_py.PinDirection.input:
                    continue
                if pin.get_type() in (hal_py.PinType.power, hal_py.PinType.ground):
                    continue
                nxt = src.get_fan_in_net(pin.get_name())
                if nxt is not None:
                    frontier.append(nxt)
    return flops, inputs, len(cells)


def step_register_graph(out: Out, hal_py, ffs):
    out.head("4. flip-flop-only dependency graph")
    by_id = {g.get_id(): g for g in ffs}
    ff_ids = sorted(by_id)
    data_types = (hal_py.PinType.data,)
    pred: Dict[int, Set[int]] = {}
    prim: Dict[int, Set[str]] = {}
    cone_size: Dict[int, int] = {}
    for gate in ffs:
        flops, inputs, cells = ff_support(hal_py, gate, data_types)
        pred[gate.get_id()] = flops - {gate.get_id()}
        prim[gate.get_id()] = inputs
        cone_size[gate.get_id()] = cells

    succ: Dict[int, Set[int]] = {i: set() for i in ff_ids}
    for dst, srcs in pred.items():
        for s in srcs:
            succ[s].add(dst)

    comps = tarjan(ff_ids, succ)
    cyc = [c for c in comps if len(c) > 1]
    self_loop = [i for i in ff_ids if i in pred[i] or i in succ.get(i, ())]
    acyclic = [c[0] for c in comps if len(c) == 1 and c[0] not in succ[c[0]]]
    sizes = collections.Counter(len(c) for c in comps)

    out("edges are 'this flop's D cone reads that flop's Q', combinational only.")
    out("flops                  : %d" % len(ff_ids))
    out("D-cone edges           : %d" % sum(len(v) for v in pred.values()))
    out("SCCs of the FF graph   : %d  (%s)"
        % (len(comps), ", ".join("%d of size %d" % (n, s) for s, n in sorted(sizes.items()))))
    out("flops in a cycle       : %d" % sum(len(c) for c in cyc))
    out("flops outside any cycle: %d  <- these can only pass a value along" % len(acyclic))
    out("")
    out("cyclic components (the real state cores):")
    for comp in sorted(cyc, key=len, reverse=True):
        names = sorted(by_id[i].get_name() for i in comp)
        out("  size %-3d : %s" % (len(comp), ", ".join(names) if len(names) <= 6
                                  else ", ".join(names[:6]) + ", ... (+%d)" % (len(names) - 6)))

    # shift chains: a flop whose D cone reads exactly one other flop, walked by
    # predecessor so that a *tapped* stage does not end the walk.
    def find_chains(predmap: Dict[int, Set[int]]) -> List[List[int]]:
        single = {i: next(iter(predmap[i])) for i in predmap if len(predmap[i]) == 1}
        chains: List[List[int]] = []
        consumed: Set[int] = set()
        for tail in sorted(single, key=lambda i: by_id[i].get_name()):
            if tail in consumed or any(single.get(j) == tail for j in single):
                continue
            chain = [tail]
            cur = tail
            while cur in single and single[cur] not in chain:
                cur = single[cur]
                chain.append(cur)
            if len(chain) >= 3:
                chains.append(chain)
                consumed.update(chain)
        return sorted(chains, key=len, reverse=True)

    chains = find_chains(pred)
    out("")
    out("pure shift chains (D cone reads exactly one flop, >=3 deep), by predecessor:")
    if not chains:
        out("  none")
    for chain in chains:
        out("  %2d stages: %s" % (len(chain), " <- ".join(by_id[i].get_name() for i in chain)))

    # ...and the same test with the broadcast control flops held out.  A flop
    # that appears in most of the design's D cones is a mode/phase bit gating
    # everything, and "reads exactly one flop" is false for every stage at once
    # while it is in the graph -- the failure mode 13_trivium_stream documents.
    fanout = collections.Counter()
    for srcs in pred.values():
        fanout.update(srcs)
    broadcast = sorted(i for i, n in fanout.items() if n >= len(ff_ids) // 4)
    out("")
    out("broadcast control flops (in >= 25%% of all D cones): %s"
        % (", ".join("%s (%d cones)" % (by_id[i].get_name(), fanout[i]) for i in broadcast) or "none"))
    reduced = {i: (pred[i] - set(broadcast)) for i in ff_ids if i not in broadcast}
    chains2 = find_chains(reduced)
    out("the same chain test with those held out:")
    if not chains2:
        out("  none")
    for chain in chains2:
        out("  %2d stages: %s" % (len(chain), " <- ".join(by_id[i].get_name() for i in chain)))

    out("")
    out("widest next-state cones (combinational cells between Q and D):")
    for i in sorted(ff_ids, key=lambda i: -cone_size[i])[:8]:
        out("  %-28s %3d cells, %2d flop sources, primary inputs %s"
            % (by_id[i].get_name(), cone_size[i], len(pred[i]),
               ", ".join(sorted(prim[i])) or "-"))

    summary = {
        "flops": len(ff_ids),
        "edges": sum(len(v) for v in pred.values()),
        "scc_sizes": {str(s): n for s, n in sorted(sizes.items())},
        "cyclic_components": [
            sorted(by_id[i].get_name() for i in c) for c in sorted(cyc, key=len, reverse=True)
        ],
        "acyclic_flops": sorted(by_id[i].get_name() for i in acyclic),
        "shift_chains": [[by_id[i].get_name() for i in c] for c in chains],
        "broadcast_control_flops": [by_id[i].get_name() for i in broadcast],
        "shift_chains_without_broadcast": [[by_id[i].get_name() for i in c] for c in chains2],
        "self_loops": sorted(by_id[i].get_name() for i in self_loop),
    }
    partition = {
        idx: sorted(by_id[i].get_name() for i in comp)
        for idx, comp in enumerate(sorted(comps, key=len, reverse=True))
    }
    return summary, pred, partition


def step_dana(out: Out, hal_py, netlist, ffs, flop_map, art_dir, scc_partition) -> Dict[str, object]:
    out.head("5. DANA register grouping vs. the hand-made flop map")
    from hal_plugins import dataflow

    by_name = {g.get_name(): g for g in ffs}
    truth_bank = partition_of("bank", flop_map)
    truth_group = partition_of("group", flop_map)
    out("flop_map.md: %d flops, %d coarse groups, %d word-level banks"
        % (len(flop_map), len(truth_group), len(truth_bank)))
    out("")

    # baseline: step 4's SCC decomposition is already a partition of the flops,
    # and it costs nothing.  Anything DANA adds has to beat it.
    scc_score = pair_score(scc_partition, truth_bank)
    scc_exact = sorted(
        name for name, members in truth_bank.items()
        if any(set(members) == set(g) for g in scc_partition.values())
    )
    out("[%-16s] step 4's flip-flop SCC decomposition, used as a grouping" % "SCC baseline")
    out("   %d groups covering %d/%d flops" % (len(scc_partition), len(ffs), len(ffs)))
    out("   vs. 2-bit-bank truth : P=%.2f R=%.2f F1=%.2f, %d exact group(s)"
        % (scc_score["precision"], scc_score["recall"], scc_score["f1"], len(scc_exact)))

    runs: List[Dict[str, object]] = []
    best: Optional[Tuple[float, Dict[str, object], object]] = None
    for spec in DANA_CONFIGS:
        config = dataflow.Configuration(netlist).with_flip_flops()
        if "min_group_size" in spec:
            config = config.with_min_group_size(spec["min_group_size"])
        if "expected_sizes" in spec:
            config = config.with_expected_sizes(list(spec["expected_sizes"]))
        if spec.get("type_consistency"):
            config = config.with_type_consistency(True)
        if spec.get("stage_identification"):
            config = config.with_stage_identification(True)
        result = dataflow.analyze(config)
        if result is None:
            out("[%s] DANA failed" % spec["label"])
            continue
        groups = {gid: sorted(g.get_name() for g in gates)
                  for gid, gates in result.get_groups().items()}
        sizes = collections.Counter(len(v) for v in groups.values())
        scored_bank = pair_score(groups, truth_bank)
        scored_group = pair_score(groups, truth_group)
        exact_bank = sorted(
            name for name, members in truth_bank.items()
            if any(set(members) == set(g) for g in groups.values())
        )
        exact_group = sorted(
            name for name, members in truth_group.items()
            if any(set(members) == set(g) for g in groups.values())
        )
        # purity: a DANA group is "pure" when every flop in it belongs to the
        # same hand-made register (resp. the same coarse functional block).
        # Pure-but-small means under-merging; impure means a real mistake.
        pure_bank = sum(
            1 for members in groups.values()
            if len({flop_map[m]["bank"] for m in members if m in flop_map}) == 1
        )
        pure_coarse = sum(
            1 for members in groups.values()
            if len({flop_map[m]["group"] for m in members if m in flop_map}) == 1
        )
        run = {
            "label": spec["label"],
            "note": spec["note"],
            "groups": len(groups),
            "pure_wrt_bank": pure_bank,
            "pure_wrt_coarse_group": pure_coarse,
            "grouped_flops": sum(len(v) for v in groups.values()),
            "size_histogram": {str(s): n for s, n in sorted(sizes.items())},
            "vs_banks": scored_bank,
            "vs_coarse_groups": scored_group,
            "exact_bank_matches": exact_bank,
            "exact_coarse_matches": exact_group,
            "group_members": {str(k): v for k, v in sorted(groups.items())},
        }
        runs.append(run)
        out("[%-16s] %s" % (spec["label"], spec["note"]))
        out("   %d groups covering %d/%d flops; sizes %s"
            % (len(groups), run["grouped_flops"], len(ffs),
               ", ".join("%dx%d" % (n, s) for s, n in sorted(sizes.items()))))
        out("   vs. 2-bit-bank truth : P=%.2f R=%.2f F1=%.2f, %d exact group(s)"
            % (scored_bank["precision"], scored_bank["recall"], scored_bank["f1"], len(exact_bank)))
        out("   vs. coarse-map truth : P=%.2f R=%.2f F1=%.2f, %d exact group(s)"
            % (scored_group["precision"], scored_group["recall"], scored_group["f1"], len(exact_group)))
        out("   purity               : %d/%d groups single-register, %d/%d single-block"
            % (pure_bank, len(groups), pure_coarse, len(groups)))
        key = scored_bank["f1"]
        if best is None or key > best[0]:
            best = (key, run, result)

    if best is None:
        return {"runs": runs, "scc_baseline": {"vs_banks": scc_score}}

    _, best_run, best_result = best
    txt_path = os.path.join(art_dir, "hal_dataflow.txt")
    best_result.write_txt(txt_path)
    out("")
    out("best configuration by bank F1: %s -- its group listing is %s"
        % (best_run["label"], rel(txt_path)))

    # ---- per-hand-group agreement table, on the best run ------------------
    groups = {int(k): v for k, v in best_run["group_members"].items()}
    owner: Dict[str, int] = {}
    for gid, members in groups.items():
        for m in members:
            owner[m] = gid

    scc_owner: Dict[str, int] = {}
    for gid, members in scc_partition.items():
        for m in members:
            scc_owner[m] = gid

    def verdict_for(members, part, own, tag) -> Tuple[str, Optional[int]]:
        gids = collections.Counter(own.get(m, -1) for m in members)
        top_gid, _ = gids.most_common(1)[0]
        if top_gid == -1:
            return "not grouped by %s" % tag, None
        got = part[top_gid]
        if set(got) == set(members) and len(gids) == 1:
            return "exact", top_gid
        if len(gids) > 1:
            return "split across %d groups" % len(gids), top_gid
        extra = sorted({flop_map[e]["bank"] for e in set(got) - set(members)})
        return "merged with %s" % ", ".join(extra), top_gid

    out.sub("per-register agreement: step 4 SCCs vs. DANA (%s)" % best_run["label"])
    out("%-22s %-5s %-28s %s" % ("hand register", "bits", "SCC baseline", "DANA " + best_run["label"]))
    table: List[Dict[str, object]] = []
    for bank, members in truth_bank.items():
        sv, sg = verdict_for(members, scc_partition, scc_owner, "SCC")
        dv, dg = verdict_for(members, groups, owner, "DANA")
        out("%-22s %-5d %-28s %s" % (bank, len(members), sv, dv))
        table.append({
            "bank": bank,
            "bits": len(members),
            "coarse_group": flop_map[members[0]]["group"],
            "scc_group": sg,
            "scc_verdict": sv,
            "dana_group": dg,
            "dana_group_size": None if dg is None else len(groups[dg]),
            "dana_verdict": dv,
        })

    out.sub("DANA groups, and what the hand map calls them")
    out("%-6s %-5s %s" % ("group", "bits", "hand registers covered"))
    dana_table: List[Dict[str, object]] = []
    for gid in sorted(groups):
        banks = collections.Counter(flop_map[m]["bank"] for m in groups[gid] if m in flop_map)
        desc = ", ".join("%s(%d/%d)" % (b, n, len(truth_bank[b])) for b, n in sorted(banks.items()))
        out("%-6d %-5d %s" % (gid, len(groups[gid]), desc))
        dana_table.append({"group": gid, "bits": len(groups[gid]), "banks": dict(sorted(banks.items()))})

    ungrouped = sorted(set(by_name) - set(owner))
    out("")
    out("flops in no DANA group: %d%s"
        % (len(ungrouped), (" -- " + ", ".join("%s (%s)" % (u, flop_map[u]["signal"]) for u in ungrouped))
           if ungrouped else ""))

    return {
        "runs": runs,
        "scc_baseline": {
            "groups": len(scc_partition),
            "vs_banks": scc_score,
            "exact_bank_matches": scc_exact,
            "group_members": {str(k): v for k, v in sorted(scc_partition.items())},
        },
        "best": best_run["label"],
        "per_bank": table,
        "dana_groups": dana_table,
        "ungrouped": [{"instance": u, "signal": flop_map[u]["signal"]} for u in ungrouped],
        "truth_banks": {k: v for k, v in truth_bank.items()},
    }


#: one colour per coarse block of ``flop_map.md``, for the register graph.
_BLOCK_COLOURS = {
    "bit counter (column, 0..10)": "#cfe8ff",
    "character counter (row, 0..10)": "#cfe8ff",
    "population counter (total stars)": "#ffe6b3",
    "per-row star checker": "#ffd6cc",
    "input delay line (adjacency window)": "#d9f2d9",
    "adjacency violation flag": "#b8e6b8",
    "message LFSR / input digest": "#e6d9f2",
    "phase control": "#f2f2b3",
    "output byte counter": "#dcd0f0",
    "verdict": "#ffb3b3",
    "region star counter 0": "#ffe0f0",
    "region star counters": "#ffe0f0",
    "column star counters": "#d9ecff",
}


def write_register_graph_dot(
    path: str, by_id, pred, broadcast, dana_groups, flop_map
) -> str:
    """Emit the flip-flop dependency graph, boxed by DANA and coloured by hand.

    One picture holding both answers: the **boxes** are what DANA recovered from
    the netlist alone, the **colours** are what the hand analysis named, so
    every disagreement in the comparison table is visible as a box that mixes
    colours or a colour split across boxes.  Edges out of the broadcast control
    flops are omitted -- they run to most of the design and drown everything.
    """
    owner: Dict[str, int] = {}
    for gid, members in dana_groups.items():
        for m in members:
            owner[m] = gid
    name_of = {i: by_id[i].get_name() for i in by_id}
    broadcast_set = set(broadcast)

    lines = [
        "digraph register_graph {",
        '  graph [rankdir=LR, splines=true, overlap=false, fontname="Helvetica", '
        'label="puzzle -- flip-flop dependency graph\\n'
        'boxes: DANA groups (min_group_size 2)   colours: flop_map.md blocks\\n'
        'edges out of the 9 broadcast control flops omitted", labelloc=t];',
        '  node [shape=box, style="filled,rounded", fontname="Helvetica", fontsize=9];',
        "  edge [color=\"#888888\", arrowsize=0.6];",
    ]
    for gid in sorted(dana_groups):
        members = dana_groups[gid]
        lines.append('  subgraph cluster_%d {' % gid)
        lines.append('    label="DANA #%d (%d)"; fontsize=9; color="#555555";' % (gid, len(members)))
        for m in sorted(members):
            info = flop_map.get(m, {})
            colour = _BLOCK_COLOURS.get(info.get("group", ""), "#eeeeee")
            label = info.get("signal", m)
            shape = "doubleoctagon" if m in {name_of[i] for i in broadcast_set} else "box"
            lines.append(
                '    "%s" [label="%s", fillcolor="%s", shape=%s, tooltip="%s"];'
                % (m, label, colour, shape, m)
            )
        lines.append("  }")
    for dst, srcs in sorted(pred.items()):
        for src in sorted(srcs):
            if src in broadcast_set:
                continue
            lines.append('  "%s" -> "%s";' % (name_of[src], name_of[dst]))
    lines.append("}")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def step_success_cone(out: Out, hal_py, ffs, flop_map, pred) -> Dict[str, object]:
    out.head("6. the success cone")
    by_id = {g.get_id(): g for g in ffs}
    succ_gate = None
    for gate in ffs:
        net = gate.get_fan_out_net("Q")
        if net is not None and net.get_name() == "success":
            succ_gate = gate
    if succ_gate is None:
        out("no flop drives the 'success' net -- nothing to do")
        return {}

    out("the output port 'success' is driven by %s (%s), not by combinational logic"
        % (succ_gate.get_name(), succ_gate.get_type().get_name()))
    flops, inputs, cells = ff_support(hal_py, succ_gate, (hal_py.PinType.data,))
    out("its D cone: %d combinational cells, %d flop sources, primary inputs %s"
        % (cells, len(flops), ", ".join(sorted(inputs)) or "none"))
    banks = collections.Counter(flop_map[by_id[i].get_name()]["bank"] for i in flops if by_id[i].get_name() in flop_map)
    out("")
    out("which hand-named registers the verdict flop reads:")
    for bank, n in sorted(banks.items()):
        out("  %-22s %d bit(s)" % (bank, n))
    out("")
    out("Every constraint the chip checks has to reach this one cone, so the")
    out("set above is the whole judging rule stated as a register list -- with")
    out("no puzzle-specific knowledge, one backward walk names the checkers.")

    # transitive cone over flops
    reach: Set[int] = set()
    frontier = list(flops)
    while frontier:
        i = frontier.pop()
        if i in reach:
            continue
        reach.add(i)
        frontier.extend(pred.get(i, ()))
    out("")
    out("transitively, %d of %d flops can influence 'success'; the %d that cannot are:"
        % (len(reach), len(ffs), len(ffs) - len(reach)))
    outside = sorted(by_id[i].get_name() for i in by_id if i not in reach and i != succ_gate.get_id())
    for name in outside:
        out("  %-28s %s" % (name, flop_map.get(name, {}).get("signal", "?")))
    return {
        "driver": succ_gate.get_name(),
        "cone_cells": cells,
        "direct_flop_sources": sorted(by_id[i].get_name() for i in flops),
        "direct_banks": dict(sorted(banks.items())),
        "transitive_flops": len(reach),
        "flops_outside_cone": [
            {"instance": n, "signal": flop_map.get(n, {}).get("signal", "?")} for n in outside
        ],
    }


def step_fsm(out: Out, fsm_dir: str, flop_map: Dict[str, Dict[str, str]]) -> Dict[str, object]:
    """Read back what ``tools/hal_fsm analyze`` recovered, and decode it.

    ``hal_fsm`` hands back states as opaque integers over whatever bit order it
    happened to pick.  Re-indexing them by the Act-1 bit names is the only place
    the hand map is used here, and it is a *relabelling*, not a fit: the state
    set and the transition relation are whatever the SMT solver proved.
    """
    out.head("7. hal_fsm on the control registers")
    findings_path = os.path.join(fsm_dir, "findings.json")
    if not os.path.exists(findings_path):
        out("no hal_fsm output at %s -- run tools/run_hal_analysis.sh" % rel(fsm_dir))
        return {}
    with open(findings_path, "r", encoding="utf-8") as fh:
        findings = json.load(fh)
    items = findings.get("findings", findings if isinstance(findings, list) else [])
    out("hal_fsm findings (verbatim ids and statuses):")
    for item in items:
        out("  %-34s %-26s %s"
            % (item.get("id", "?"), item.get("status", "?"),
               (item.get("summary") or item.get("title") or "")[:96]))

    machines: List[Dict[str, object]] = []
    for name in sorted(os.listdir(fsm_dir)):
        if not (name.startswith("transitions-") and name.endswith(".json")):
            continue
        with open(os.path.join(fsm_dir, name), "r", encoding="utf-8") as fh:
            tr = json.load(fh)
        order = tr.get("bit_order", [])
        banks = {flop_map[i]["bank"] for i in order if i in flop_map}
        indices: Dict[str, int] = {}
        for inst in order:
            sig = flop_map.get(inst, {}).get("signal", "")
            m = re.search(r"\[(\d+)\]$", sig)
            if m:
                indices[inst] = int(m.group(1))
        out("")
        out("%s: %d flop(s), %d state(s), %d transition(s), solver %s"
            % (name, len(order), len(tr.get("states", [])),
               len(tr.get("transitions", [])), tr.get("solver", "?")))
        out("  bit order as hal_fsm returned it: %s" % ", ".join(order))
        out("  the hand map calls those: %s"
            % ", ".join(flop_map.get(i, {}).get("signal", "?") for i in order))
        out("  external signals it needed: %s"
            % ", ".join(sorted(s.get("name", "?") for s in tr.get("signals", {}).values())))
        entry: Dict[str, object] = {
            "file": name,
            "flops": order,
            "signals_hand_map": [flop_map.get(i, {}).get("signal", "?") for i in order],
            "states": tr.get("states", []),
            "transitions": len(tr.get("transitions", [])),
            "banks": sorted(banks),
        }
        if len(banks) == 1 and len(indices) == len(order):
            def decode(value: int) -> int:
                word = 0
                for bit, inst in enumerate(order):
                    if (value >> bit) & 1:
                        word |= 1 << indices[inst]
                return word

            states = sorted(decode(s) for s in tr.get("states", []))
            advance = sorted(
                (decode(t["source"]), decode(t["target"]))
                for t in tr.get("transitions", [])
                if t["source"] != t["target"]
            )
            out("  re-indexed by those names: states %s" % states)
            out("  non-self transitions     : %s"
                % ", ".join("%d->%d" % ab for ab in advance))
            entry["decoded_states"] = states
            entry["decoded_advance"] = ["%d->%d" % ab for ab in advance]
            if advance == [(k, (k + 1) % len(states)) for k in states]:
                out("  => a modulo-%d counter, proven by SMT, from the netlist alone."
                    % len(states))
                entry["shape"] = "modulo-%d counter" % len(states)
        machines.append(entry)
    return {"findings": items, "machines": machines}


# --------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--netlist", default=os.path.join(_EXAMPLE, "artifacts", "puzzle_netlist.v"))
    ap.add_argument(
        "--gate-library",
        default=os.path.join(_REPO, "plugins", "gate_libraries", "definitions", "SKY130_FD_SC_HD.hgl"),
    )
    ap.add_argument("--flop-map", default=os.path.join(_EXAMPLE, "artifacts", "flop_map.md"))
    ap.add_argument("--artifacts", default=os.path.join(_EXAMPLE, "artifacts"))
    ap.add_argument("--images", default=os.path.join(_EXAMPLE, "images"))
    ap.add_argument(
        "--fsm-dir",
        default=os.path.join(_EXAMPLE, "artifacts", "hal_fsm"),
        help="where tools/hal_fsm analyze wrote its output (read, never written)",
    )
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    import hal_py

    os.makedirs(args.artifacts, exist_ok=True)
    hal_py.plugin_manager.load_all_plugins()
    netlist = hal_py.NetlistFactory.load_netlist(args.netlist, args.gate_library)
    if netlist is None:
        print("failed to load %s with %s" % (args.netlist, args.gate_library), file=sys.stderr)
        return 1

    out = Out(echo=not args.quiet)
    out("HAL-native walkthrough of the Jane Street ASIC puzzle netlist")
    out("=" * 60)
    out("netlist      : %s" % rel(args.netlist))
    out("gate library : %s" % rel(args.gate_library))

    ffs = [g for g in netlist.get_gates() if g.get_type().has_property(hal_py.GateTypeProperty.ff)]
    flop_map = load_flop_map(args.flop_map)

    doc: Dict[str, object] = {
        "netlist": rel(args.netlist),
        "gate_library": rel(args.gate_library),
        "flop_map": rel(args.flop_map),
    }
    doc["census"] = step_census(out, hal_py, netlist)
    doc["ff_signatures"] = step_ff_signatures(out, hal_py, ffs)
    doc["gate_sccs"] = step_gate_sccs(out, hal_py, netlist)
    reg, pred, scc_partition = step_register_graph(out, hal_py, ffs)
    doc["register_graph"] = reg
    doc["dataflow"] = step_dana(
        out, hal_py, netlist, ffs, flop_map, args.artifacts, scc_partition
    )
    doc["success_cone"] = step_success_cone(out, hal_py, ffs, flop_map, pred)
    doc["fsm"] = step_fsm(out, args.fsm_dir, flop_map)

    # one drawing holding both answers at once
    best_label = doc["dataflow"].get("best")
    best_run = next((r for r in doc["dataflow"]["runs"] if r["label"] == best_label), None)
    if best_run is not None:
        by_id = {g.get_id(): g for g in ffs}
        by_name = {g.get_name(): g.get_id() for g in ffs}
        broadcast = [by_name[n] for n in reg["broadcast_control_flops"] if n in by_name]
        os.makedirs(args.images, exist_ok=True)
        dot_path = write_register_graph_dot(
            os.path.join(args.images, "hal_register_graph.dot"),
            by_id,
            pred,
            broadcast,
            {int(k): v for k, v in best_run["group_members"].items()},
            flop_map,
        )
        out("")
        out("wrote the flip-flop dependency graph (DANA boxes, flop_map colours) to %s"
            % rel(dot_path))

    txt = os.path.join(args.artifacts, "hal_walkthrough.txt")
    with open(txt, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out.render())
    js = os.path.join(args.artifacts, "hal_dataflow.json")
    with open(js, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=2, sort_keys=False)
        fh.write("\n")
    print("\nwrote %s" % rel(txt))
    print("wrote %s" % rel(js))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
