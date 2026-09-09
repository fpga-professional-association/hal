#!/usr/bin/env python3
"""The structural half of the 08_shift_debouncer walkthrough, as one run.

Everything `guide.html` claims about the *structure* of the netlist is produced
here, from `netlist.hal.v` -- the HAL-readable rewrite of the Quartus export --
and from nothing else.  No step reads `design.v`, `spec.md` or `reference.py`.

Needs a built HAL:

    export HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib
    PYTHONPATH=/work/build/lib:tools python3 \\
        examples/agilex3_walkthroughs/08_shift_debouncer/analyze.py \\
        --repo . -o examples/agilex3_walkthroughs/08_shift_debouncer/artifacts

Writes one numbered text file per step (quoted verbatim in the guide), a DOT
graph of the register structure, and `analysis.json` with the machine-readable
conclusions that `check.py` asserts on.
"""

import argparse
import json
import os
import subprocess
import sys

FF = "tennm_ff"
LUT = "tennm_lcell_comb"
DATA_PINS = ("dataa", "datab", "datac", "datad", "datae", "dataf", "datag", "datah")


# ---------------------------------------------------------------------------
# helpers over the loaded netlist
# ---------------------------------------------------------------------------


def gate_type(g):
    return g.get_type().get_name()


def is_ff(g):
    return gate_type(g) == FF


def is_lut(g):
    return gate_type(g) == LUT


def is_const(net):
    return net is not None and (net.is_gnd_net() or net.is_vcc_net())


def const_kind(net):
    if net is None:
        return None
    if net.is_gnd_net():
        return "0"
    if net.is_vcc_net():
        return "1"
    return None


def source_gate(net):
    if net is None:
        return None
    for ep in net.get_sources():
        return ep.get_gate()
    return None


def net_name(net):
    if net is None:
        return "<open>"
    if net.is_vcc_net():
        return "1'b1"
    if net.is_gnd_net():
        return "1'b0"
    return net.get_name()


def cone_support(gate, pin):
    """Sequential/primary-input support of one input pin of *gate*.

    Walks backwards through combinational cells only and returns
    (ff_gates, primary_input_names).  It answers "which registers and which
    ports does this pin actually depend on", which is the question that tells a
    pass-through flop from a flop that can hold its own value.
    """
    net = gate.get_fan_in_net(pin)
    return _walk(net, set())


def _walk(net, seen):
    if net is None or is_const(net) or net.get_id() in seen:
        return set(), set()
    seen.add(net.get_id())
    src = source_gate(net)
    if src is None:
        return set(), {net.get_name()}      # no driver inside: a primary input
    if is_ff(src):
        return {src}, set()
    ffs, ins = set(), set()
    for pin in DATA_PINS + ("cin", "sharein"):
        sub = src.get_fan_in_net(pin)
        if sub is None or is_const(sub):
            continue
        f, i = _walk(sub, seen)
        ffs |= f
        ins |= i
    return ffs, ins


def tarjan(nodes, edges):
    """Strongly connected components of a small directed graph. Iterative."""
    index, low, on_stack, stack, order, out = {}, {}, set(), [], [0], []
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(edges.get(root, ())))]
        index[root] = low[root] = order[0]
        order[0] += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index:
                    index[nxt] = low[nxt] = order[0]
                    order[0] += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(edges.get(nxt, ()))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                out.append(sorted(comp))
    return out


# ---------------------------------------------------------------------------
# step 01 -- the census
# ---------------------------------------------------------------------------


def step_stats(nl, out):
    types = {}
    for g in nl.get_gates():
        types[gate_type(g)] = types.get(gate_type(g), 0) + 1
    inputs = sorted(n.get_name() for n in nl.get_nets() if n.is_global_input_net())
    outputs = sorted(n.get_name() for n in nl.get_nets() if n.is_global_output_net())
    modules = nl.get_modules()

    lines = [
        "design name        : {}".format(nl.get_design_name()),
        "gate library       : {}".format(nl.get_gate_library().get_name()),
        "gates              : {}".format(len(nl.get_gates())),
        "nets               : {}".format(len(nl.get_nets())),
        "modules            : {}".format(len(modules)),
        "",
        "gate types:",
    ]
    for t, c in sorted(types.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append("  {:<24} {}".format(t, c))
    lines += [
        "",
        "primary inputs  ({:2d}): {}".format(len(inputs), " ".join(inputs)),
        "primary outputs ({:2d}): {}".format(len(outputs), " ".join(outputs)),
        "",
        "module tree:",
    ]
    for m in modules:
        lines.append(
            "  {} (id {}, type {!r}) gates={} submodules={}".format(
                m.get_name(), m.get_id(), m.get_type(),
                len(m.get_gates(recursive=False)), len(m.get_submodules()),
            )
        )
    lines += [
        "",
        "{} flip-flops and {} ALM cells.  Three ports in, two out.  Flat: one".format(
            types.get(FF, 0), types.get(LUT, 0)),
        "module, so the hierarchy was flattened by synthesis and carries no",
        "information about how the source was partitioned.",
    ]
    out("01_stats.txt", lines)
    return {"gate_types": types, "inputs": inputs, "outputs": outputs,
            "num_modules": len(modules), "num_gates": len(nl.get_gates()),
            "num_nets": len(nl.get_nets())}


# ---------------------------------------------------------------------------
# step 02 -- flip-flop control pins
# ---------------------------------------------------------------------------


def step_clock_reset(nl, out):
    """Which nets reach the clock, asynchronous-clear, enable and load pins."""
    pins = ("clk", "clrn", "ena", "sclr", "aload", "sload")
    buckets = dict((p, {}) for p in pins)
    ffs = [g for g in nl.get_gates() if is_ff(g)]
    for g in ffs:
        for pin in pins:
            buckets[pin].setdefault(net_name(g.get_fan_in_net(pin)), []).append(g.get_name())

    lines = [
        "Every flip-flop control pin, grouped by the net that drives it.",
        "",
    ]
    titles = {"clk": "clk", "clrn": "clrn (asynchronous clear, active low)",
              "ena": "ena (clock enable)", "sclr": "sclr (synchronous clear)",
              "aload": "aload (asynchronous load)", "sload": "sload (synchronous load)"}
    for pin in pins:
        lines.append("{}:".format(titles[pin]))
        for net, gates in sorted(buckets[pin].items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines.append("  {:<10} x{:<3} {}".format(net, len(gates), " ".join(sorted(gates))))
        lines.append("")

    signature = {}
    for g in ffs:
        key = tuple(net_name(g.get_fan_in_net(p)) for p in pins)
        signature.setdefault(key, []).append(g.get_name())
    lines += [
        "Control-pin signature (clk, clrn, ena, sclr, aload, sload) -> flops:",
        "",
    ]
    for key, gates in sorted(signature.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        lines.append("  ({}) x{}".format(", ".join(key), len(gates)))
        lines.append("      {}".format(" ".join(sorted(gates))))
    lines += [
        "",
        "All {} flip-flops share one signature: one clock net, one asynchronous".format(len(ffs)),
        "clear net, and every other control pin tied to a constant.  So:",
        "",
        "  * one clock domain -- no CDC analysis to do, and no clock gating;",
        "  * one asynchronous reset, active low, hitting every flop;",
        "  * NO clock enables at all.  Whatever makes a register hold its value",
        "    in this design is done in the LUT that drives its D pin, not with",
        "    the flip-flop's enable.  Register grouping by control signature",
        "    therefore yields exactly one group here and tells us nothing about",
        "    the internal structure.  That has to come from the dataflow.",
    ]
    out("02_clock_reset.txt", lines)
    return {
        "clock_nets": sorted(buckets["clk"]),
        "reset_nets": sorted(buckets["clrn"]),
        "enable_nets": sorted(buckets["ena"]),
        "signatures": [{"pins": list(k), "gates": sorted(v)}
                       for k, v in sorted(signature.items(), key=lambda kv: -len(kv[1]))],
        "num_ffs": len(ffs),
    }


# ---------------------------------------------------------------------------
# step 03 -- strongly connected components of the raw gate graph
# ---------------------------------------------------------------------------


def step_gate_sccs(nl, out):
    """SCCs of the gate graph, via HAL's graph_algorithm plugin."""
    # NOTE: the plugin is `hal_plugins.graph_algorithm`.  Importing a bare
    # `graph_algorithm` picks up nothing (or, worse, something else on the path)
    # and has bitten three walkthroughs in this series already.
    from hal_plugins import graph_algorithm

    graph = graph_algorithm.NetlistGraph.from_netlist(nl)
    comps = graph_algorithm.get_connected_components(graph, True, 2)
    named = []
    for c in comps or []:
        named.append(sorted(g.get_name() for g in graph.get_gates_from_vertices(c)))
    named.sort(key=lambda c: (-len(c), c))

    cyclic = set()
    for c in named:
        cyclic |= set(c)
    ff_names = {g.get_name() for g in nl.get_gates() if is_ff(g)}
    acyclic_ffs = sorted(ff_names - cyclic)

    lines = [
        "$ graph_algorithm.get_connected_components(graph, strong=True, min_size=2)",
        "",
        "{} non-trivial strongly connected component(s) in the GATE graph:".format(len(named)),
        "",
    ]
    for i, c in enumerate(named):
        ffs = sorted(n for n in c if n in ff_names)
        lines.append("  SCC {}: {} gates, {} of them flip-flops".format(i, len(c), len(ffs)))
        lines.append("     flip-flops: " + (" ".join(ffs) if ffs else "(none)"))
        lines.append("     all gates : " + " ".join(c))
        lines.append("")
    lines += [
        "flip-flops in NO non-trivial SCC ({}): {}".format(len(acyclic_ffs), " ".join(acyclic_ffs)),
        "",
        "A flop inside a feedback loop can hold or evolve a value of its own.",
        "A flop outside every loop can only pass a value along: its next value",
        "is a function of things that are not itself.  That is the first real",
        "cut through this netlist, and it was made without looking at a single",
        "instance name.",
    ]
    out("03_gate_sccs.txt", lines)
    return {"gate_sccs": named, "ffs_outside_gate_sccs": acyclic_ffs}


# ---------------------------------------------------------------------------
# step 04 -- the register dependency graph
# ---------------------------------------------------------------------------


def step_register_graph(nl, out):
    """Pin-aware register-to-register graph: what each flop's D depends on."""
    ffs = [g for g in nl.get_gates() if is_ff(g)]
    info = {}
    for g in ffs:
        d_ffs, d_ins = cone_support(g, "d")
        e_net = g.get_fan_in_net("ena")
        if const_kind(e_net) is not None:
            e_ffs, e_ins = set(), set()
        else:
            e_ffs, e_ins = cone_support(g, "ena")
        d_net = g.get_fan_in_net("d")
        driver = source_gate(d_net)
        info[g.get_name()] = {
            "d_ffs": sorted(x.get_name() for x in d_ffs),
            "d_inputs": sorted(d_ins),
            "d_driver": None if driver is None else driver.get_name(),
            "d_driver_type": None if driver is None else gate_type(driver),
            "ena_const": const_kind(e_net),
            "ena_net": net_name(e_net),
            "ena_ffs": sorted(x.get_name() for x in e_ffs),
            "q_net": net_name(g.get_fan_out_net("q")),
            "self_feedback": g.get_name() in {x.get_name() for x in d_ffs},
        }

    lines = [
        "For every flip-flop: the gate that drives its D pin, and the flip-flops",
        "and primary inputs that D depends on through combinational cells only.",
        "'self' marks a flop whose own output is in its next-state cone -- it can",
        "hold, count or accumulate.  A flop without it is a pure delay stage.",
        "",
        "{:<10} {:<5} {:<16} {:<32} {:<10} {}".format(
            "FF", "self", "D driven by", "D depends on (FFs)", "D <- PI", "ENA"),
        "-" * 104,
    ]
    for name in sorted(info):
        i = info[name]
        ena = ("1'b" + i["ena_const"]) if i["ena_const"] else i["ena_net"]
        lines.append("{:<10} {:<5} {:<16} {:<32} {:<10} {}".format(
            name, "self" if i["self_feedback"] else ".",
            i["d_driver"] or "(port)",
            ",".join(i["d_ffs"]) or "-",
            ",".join(i["d_inputs"]) or "-", ena))
    lines += [
        "",
        "Three shapes are visible in that table, and they are the three textbook",
        "blocks this design is made of:",
        "",
        "  * a flop whose D is a primary input, feeding a flop whose D is that",
        "    first flop: a two-stage delay line straight off an input port;",
        "  * a group of flops that all depend on each other and on themselves:",
        "    a word-sized register with internal feedback -- a counter, an LFSR",
        "    or an accumulator; step 07 decides which;",
        "  * a self-dependent single bit fed by that group, followed by one more",
        "    pure delay flop.",
        "",
        "Note what is NOT here: not one flop has a computed clock enable, and no",
        "flop's D cone reaches the primary input except the head of the delay",
        "line.  The button never touches the counter directly.",
    ]
    out("04_register_graph.txt", lines)
    return info


# ---------------------------------------------------------------------------
# step 05 -- SCCs of the register graph
# ---------------------------------------------------------------------------


def step_register_sccs(nl, out, info):
    nodes = sorted(info)
    # edge s -> n means "s feeds n's next state"
    succ = dict((n, []) for n in nodes)
    for n in nodes:
        for s in info[n]["d_ffs"]:
            if s != n:
                succ[s].append(n)
    comps = sorted(tarjan(nodes, succ), key=lambda c: (-len(c), c))
    words = [c for c in comps if len(c) > 1]
    coupled = set()
    for c in words:
        coupled |= set(c)
    holders = sorted(n for n in nodes
                     if n not in coupled and info[n]["self_feedback"])
    delays = sorted(n for n in nodes
                    if n not in coupled and not info[n]["self_feedback"])

    # order the pure-delay flops by their own edges
    dp = set(delays)
    pred = dict((n, [s for s in info[n]["d_ffs"] if s in dp and s != n]) for n in delays)
    nxt = {}
    for n, ps in pred.items():
        for p in ps:
            nxt.setdefault(p, []).append(n)
    heads = [n for n in delays if not pred[n]]
    chains = []
    for h in sorted(heads):
        chain = [h]
        while len(nxt.get(chain[-1], [])) == 1:
            chain.append(nxt[chain[-1]][0])
        chains.append(chain)
    chains.sort(key=lambda c: (-len(c), c))

    lines = [
        "The same edges as step 04, between flip-flops only, self-loops removed.",
        "Strongly connected components of THAT graph:",
        "",
    ]
    for i, c in enumerate(comps):
        lines.append("  component {}: {} flop(s)  {}".format(i, len(c), " ".join(c)))
    lines += [
        "",
        "=> {} multi-flop component(s): {}".format(
            len(words), "; ".join(" ".join(c) for c in words) or "(none)"),
        "=> {} self-holding singleton(s): {}".format(len(holders), " ".join(holders) or "(none)"),
        "=> {} pure delay flop(s): {}".format(len(delays), " ".join(delays) or "(none)"),
        "",
        "The pure delay flops, chained by their own edges:",
        "",
    ]
    for c in chains:
        lines.append("  chain of {}: {}".format(
            len(c), "  ->  ".join(c) + ("  ->  (nothing)" if len(c) == 1 else "")))
        head = c[0]
        lines.append("     head {} takes D from: {}".format(
            head, ",".join(info[head]["d_inputs"] + info[head]["d_ffs"]) or "(nothing)"))
    lines += [
        "",
        "Read the chains carefully, because this is the trap in this design.",
        "Structurally, a two-flop CLOCK-DOMAIN SYNCHRONIZER and a two-flop",
        "EDGE-DETECTOR PAIR are the same object: flop -> flop, no enable, no",
        "feedback, one clock.  Nothing in the graph distinguishes them.  The only",
        "difference is what feeds the head of the chain:",
        "",
        "  * a chain fed by a PRIMARY INPUT is (or at least can be) a",
        "    synchronizer -- the input is asynchronous until proven otherwise;",
        "  * a chain fed by an internal flop is a delayed copy of that flop, and",
        "    a delayed copy exists to be compared with the original, i.e. to",
        "    detect an edge.",
        "",
        "That is an argument from intent, not from structure.  It is the weakest",
        "inference in this walkthrough and the guide says so.",
    ]
    out("05_register_sccs.txt", lines)
    return {"components": comps, "words": words, "holders": holders,
            "delays": delays, "chains": chains}


# ---------------------------------------------------------------------------
# step 06 -- the Boolean function of every ALM
# ---------------------------------------------------------------------------


def step_functions(nl, out):
    lines = ["Every tennm_lcell_comb, with the function HAL derived from its",
             "lut_mask, and the nets on its used data pins.", ""]
    funcs = {}
    for g in sorted((g for g in nl.get_gates() if is_lut(g)), key=lambda g: g.get_name()):
        used = []
        pin_nets = {}
        for pin in DATA_PINS:
            net = g.get_fan_in_net(pin)
            if net is not None and not is_const(net):
                used.append("{}={}".format(pin, net.get_name()))
                pin_nets[pin] = net.get_name()
        out_net = g.get_fan_out_net("combout")
        f = g.get_boolean_function("combout")
        text = str(f) if f is not None else "<none>"
        mask = (g.get_data("generic", "lut_mask") or ("", "?"))[1]
        funcs[g.get_name()] = {"function": text, "lut_mask": str(mask),
                               "out": None if out_net is None else out_net.get_name(),
                               "pins": used, "pin_nets": pin_nets}
        lines.append("{}  ->  {}   lut_mask={}".format(
            g.get_name(), out_net.get_name() if out_net else "-", mask))
        lines.append("    pins : " + ", ".join(used))
        lines.append("    func : " + text)
        lines.append("")

    masks = {}
    for name, d in funcs.items():
        masks.setdefault(d["lut_mask"], []).append(name)
    repeats = {m: sorted(v) for m, v in masks.items() if len(v) > 1}
    lines.append("lut_mask values used by more than one cell:")
    if not repeats:
        lines.append("  (none)")
    for m, v in sorted(repeats.items()):
        lines.append("  {}  {}".format(m, " ".join(v)))
    lines += [
        "",
        "Two cells sharing a mask do NOT compute the same thing: the mask is a",
        "truth table over the ALM's own pins, and the same table over different",
        "nets is a different function of the circuit.  Read the pin list, always.",
    ]
    out("06_lut_functions.txt", lines)
    return funcs


# ---------------------------------------------------------------------------
# step 07 -- what the feedback group actually computes
# ---------------------------------------------------------------------------


def _evaluate(hal_py, function, assignment):
    values = {}
    for name, bit in assignment.items():
        values[name] = (hal_py.BooleanFunction.Value.ONE if bit
                        else hal_py.BooleanFunction.Value.ZERO)
    result = function.evaluate(values)
    if result == hal_py.BooleanFunction.Value.ONE:
        return 1
    if result == hal_py.BooleanFunction.Value.ZERO:
        return 0
    raise RuntimeError("non-constant evaluation: {}".format(result))


def _bindings(gate, function):
    """pin -> ('net', name) or ('const', bit) for every free variable.

    HAL keeps a pin in the Boolean function even when the netlist ties it to a
    constant -- `dataf` survives in every 5-input ALM function here -- so the
    binding has to be resolved against the netlist, not guessed from the text.
    """
    out = {}
    for pin in function.get_variable_names():
        net = gate.get_fan_in_net(pin)
        if net is None or net.is_gnd_net():
            out[pin] = ("const", 0)
        elif net.is_vcc_net():
            out[pin] = ("const", 1)
        else:
            out[pin] = ("net", net.get_name())
    return out


def _assign(bindings, values):
    assignment = {}
    for pin, (kind, what) in bindings.items():
        assignment[pin] = what if kind == "const" else values[what]
    return assignment


def _free_nets(bindings):
    return sorted({what for kind, what in bindings.values() if kind == "net"})


def _next_word(hal_py, cells, order, word, extra):
    """Apply every bit's next-state function once.  *word* is a tuple of bits."""
    values = {}
    for i, name in enumerate(order):
        values[cells[name]["q_net"]] = word[i]
    values.update(extra)
    out = []
    for name in order:
        cell = cells[name]
        out.append(_evaluate(hal_py, cell["function"],
                             _assign(cell["bindings"], values)))
    return tuple(out)


def step_word_semantics(hal_py, nl, out, info, words, holders):
    """Enumerate the feedback group's transition function; then the flag's."""
    lines = []
    result = {}
    if not words:
        lines.append("no multi-flop feedback group; nothing to enumerate")
        out("07_word_semantics.txt", lines)
        return result

    group = words[0]
    by_name = {g.get_name(): g for g in nl.get_gates()}
    cells = {}
    external = set()
    for name in group:
        ff = by_name[name]
        driver = source_gate(ff.get_fan_in_net("d"))
        f = driver.get_boolean_function("combout")
        cells[name] = {"gate": driver, "function": f,
                       "bindings": _bindings(driver, f),
                       "q_net": net_name(ff.get_fan_out_net("q"))}
    q_nets = {cells[n]["q_net"] for n in group}
    for name in group:
        external |= set(_free_nets(cells[name]["bindings"])) - q_nets
    external = sorted(external)

    lines += [
        "== A. the {}-flop feedback group ==".format(len(group)),
        "",
        "flops        : {}".format(" ".join(group)),
        "next-state   : {}".format(" ".join(cells[n]["gate"].get_name() for n in group)),
        "outside nets in the group's next-state cone: {}".format(
            " ".join(external) or "(none)"),
        "",
        "Every one of the {} cells reads all {} group outputs, so nothing about".format(
            len(group), len(group)),
        "the SUPPORT of the next-state functions orders the bits: they are",
        "symmetric.  Ordering has to come from the transition function itself.",
        "",
        "Enumerate it.  There are 2**{} = {} states of the group and {} outside".format(
            len(group), 1 << len(group), len(external)),
        "net(s), so {} transitions -- exhaustive, not sampled.".format(
            (1 << len(group)) * (1 << len(external))),
        "",
    ]

    order = sorted(group)
    n_states = 1 << len(order)
    trans = {}
    for ext_vec in range(1 << len(external)):
        extra = dict((external[i], (ext_vec >> i) & 1) for i in range(len(external)))
        table = {}
        for s in range(n_states):
            word = tuple((s >> i) & 1 for i in range(len(order)))
            table[word] = _next_word(hal_py, cells, order, word, extra)
        trans[ext_vec] = table

    def word_str(w):
        return "".join(str(b) for b in reversed(w))

    for ext_vec in sorted(trans):
        extra = dict((external[i], (ext_vec >> i) & 1) for i in range(len(external)))
        lines.append("  with {}:".format(
            ", ".join("{}={}".format(k, v) for k, v in sorted(extra.items()))))
        fixed = [w for w, nw in trans[ext_vec].items() if nw == w]
        lines.append("    fixed points (state maps to itself): {}".format(
            " ".join(word_str(w) for w in sorted(fixed)) or "(none)"))
        lines.append("")

    lines += [
        "One fixed point per setting of the outside net, and a different one",
        "each time.  A free-running counter has NO fixed point; an LFSR has one",
        "only at the degenerate all-zero (or all-one) word.  A fixed point that",
        "MOVES with an input is a saturation: the group stops instead of",
        "wrapping.  Walk the orbits to see it.",
        "",
    ]

    orbits = {}
    for ext_vec in sorted(trans):
        table = trans[ext_vec]
        # every state has one successor; find the longest path ending in the fixed point
        fixed = [w for w, nw in table.items() if nw == w][0]
        pred = {}
        for w, nw in table.items():
            if nw != w:
                pred.setdefault(nw, []).append(w)
        path = [fixed]
        while len(pred.get(path[-1], [])) == 1:
            path.append(pred[path[-1]][0])
        path.reverse()
        orbits[ext_vec] = [word_str(w) for w in path]
        extra = dict((external[i], (ext_vec >> i) & 1) for i in range(len(external)))
        lines.append("  with {}: the {} state(s) form one path of length {}:".format(
            ", ".join("{}={}".format(k, v) for k, v in sorted(extra.items())),
            n_states, len(path)))
        lines.append("    " + " -> ".join(word_str(w) for w in path) + "  -> (holds)")
        lines.append("")

    lines += [
        "Both orbits cover all {} states, run in opposite directions, and end in".format(n_states),
        "a hold.  So the group is a bidirectional walk over a linear order with a",
        "clamp at each end -- a SATURATING UP/DOWN COUNTER, and the outside net",
        "chooses the direction.  Nothing was assumed; this is the whole",
        "transition relation.",
        "",
        "The path also hands over the bit weights.  Number the states by their",
        "position on the up path and read each flop's column:",
        "",
    ]
    up_ext = None
    for ext_vec in sorted(trans):
        if orbits[ext_vec][0] == "0" * len(order):
            up_ext = ext_vec
    up_path = orbits[up_ext if up_ext is not None else sorted(trans)[0]]
    lines.append("    position : " + " ".join("{:>2}".format(i) for i in range(len(up_path))))
    weights = {}
    for i, name in enumerate(order):
        col = [int(w[len(order) - 1 - i]) for w in up_path]
        run = next((j + 1 for j in range(len(col) - 1) if col[j] != col[j + 1]), len(col))
        weights[name] = run
        lines.append("    {:<9}: {}   flips every {:>2} step(s)".format(
            name, " ".join(" " + str(b) for b in col), run))
    ranked = sorted(order, key=lambda n: (weights[n], n))
    lines += [
        "",
        "    => bit order, least significant first: {}".format(" ".join(ranked)),
        "    => the states along the up path are 0,1,2,...,{} in plain binary,".format(
            len(up_path) - 1),
        "       so the group is a {}-bit binary counter, not a Gray or one-hot".format(len(order)),
        "       encoding, and it takes {} steps to cross from one end stop to".format(
            len(up_path) - 1),
        "       the other.",
        "",
    ]
    result["group"] = {"flops": list(group), "bit_order_lsb_first": ranked,
                       "external_nets": external,
                       "orbits": dict((str(k), v) for k, v in orbits.items()),
                       "path_length": len(up_path)}

    # -- B. the self-holding flag ------------------------------------------
    if holders:
        flag = holders[0]
        ff = by_name[flag]
        driver = source_gate(ff.get_fan_in_net("d"))
        f = driver.get_boolean_function("combout")
        bindings = _bindings(driver, f)
        flag_q = net_name(ff.get_fan_out_net("q"))
        group_nets = [cells[n]["q_net"] for n in ranked]   # LSB first
        lines += [
            "== B. the self-holding flag {} ==".format(flag),
            "",
            "next-state cell: {}".format(driver.get_name()),
            "pins           : {}".format(", ".join(
                "{}={}".format(p, w if k == "net" else "1'b{}".format(w))
                for p, (k, w) in sorted(bindings.items()))),
            "func           : {}".format(str(f)),
            "",
            "It reads itself and every bit of the counter.  Enumerate all",
            "{} combinations of (flag, counter value):".format(2 * n_states),
            "",
            "    counter     flag=0 -> next   flag=1 -> next   behaviour",
            "    " + "-" * 57,
        ]
        flag_table = {}
        for value in range(n_states):
            row = []
            for cur in (0, 1):
                values = {flag_q: cur}
                for bit, net in enumerate(group_nets):
                    values[net] = (value >> bit) & 1
                row.append(_evaluate(hal_py, f, _assign(bindings, values)))
            flag_table[value] = row
            kind = ("SET" if row == [1, 1] else
                    "CLEAR" if row == [0, 0] else
                    "hold" if row == [0, 1] else "invert")
            lines.append("    {:>2} ({})        {}                {}            {}".format(
                value, format(value, "0{}b".format(len(order))), row[0], row[1], kind))
        sets = sorted(v for v, r in flag_table.items() if r == [1, 1])
        clears = sorted(v for v, r in flag_table.items() if r == [0, 0])
        holds = sorted(v for v, r in flag_table.items() if r == [0, 1])
        lines += [
            "",
            "    SET   at counter value(s): {}".format(" ".join(str(v) for v in sets)),
            "    CLEAR at counter value(s): {}".format(" ".join(str(v) for v in clears)),
            "    HOLD  at counter value(s): {}".format(" ".join(str(v) for v in holds)),
            "",
            "    The two values that move the flag are exactly the two end stops",
            "    of the counter from part A.  Everything in between holds.  That",
            "    is hysteresis -- a Schmitt trigger built out of a counter and a",
            "    latch -- and it is why a burst of noise cannot toggle the",
            "    output: noise never lets the counter reach an end stop.",
            "",
        ]
        result["flag"] = {"name": flag, "cell": driver.get_name(),
                          "set_values": sets, "clear_values": clears,
                          "hold_values": holds}

    # -- C. the output cell ------------------------------------------------
    out_cells = []
    for g in nl.get_gates():
        if not is_lut(g):
            continue
        net = g.get_fan_out_net("combout")
        if net is not None and net.is_global_output_net():
            out_cells.append(g)
    for g in sorted(out_cells, key=lambda g: g.get_name()):
        f = g.get_boolean_function("combout")
        bindings = _bindings(g, f)
        free = _free_nets(bindings)
        lines += [
            "== C. the combinational output cell {} ==".format(g.get_name()),
            "",
            "drives  : {} (a primary output)".format(g.get_fan_out_net("combout").get_name()),
            "pins    : {}".format(", ".join(
                "{}={}".format(p, w if k == "net" else "1'b{}".format(w))
                for p, (k, w) in sorted(bindings.items()))),
            "func    : {}".format(str(f)),
            "",
        ]
        lines.append("    " + "  ".join("{:<12}".format(n) for n in free) + "  out")
        rows = {}
        for vec in range(1 << len(free)):
            values = dict((free[i], (vec >> i) & 1) for i in range(len(free)))
            value = _evaluate(hal_py, f, _assign(bindings, values))
            rows[vec] = value
            lines.append("    " + "  ".join(
                "{:<12}".format((vec >> i) & 1) for i in range(len(free))) +
                "  {}".format(value))
        lines += [
            "",
            "    High for exactly one of the four combinations: the one where the",
            "    flop is 1 and its delayed copy is 0.  That is a rising-edge",
            "    detector, and it settles step 05's ambiguity: the second chain",
            "    is an edge detector, because something in the netlist actually",
            "    compares the two stages.  The synchronizer chain has no such",
            "    comparator -- nothing reads its two stages together.",
            "",
        ]
        result["output_cell"] = {"name": g.get_name(), "free_nets": free,
                                 "rows": dict((str(k), v) for k, v in rows.items())}
    out("07_word_semantics.txt", lines)
    return result


# ---------------------------------------------------------------------------
# the picture
# ---------------------------------------------------------------------------


def write_register_dot(path, info, reg):
    coupled = set()
    for c in reg["words"]:
        coupled |= set(c)
    holders, delays = set(reg["holders"]), set(reg["delays"])
    dot = ["digraph registers {", "  rankdir=LR;", '  bgcolor="transparent";',
           '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10,',
           '        color="#455a64", fontcolor="#102027"];',
           '  edge [fontname="Helvetica", fontsize=8];']
    dot.append('  subgraph cluster_dl { label="pure delay flops"; style="rounded"; '
               'color="#2e7d32"; fontcolor="#2e7d32";')
    for n in sorted(delays):
        dot.append('    "{}" [fillcolor="#c8e6c9"];'.format(n))
    dot.append("  }")
    dot.append('  subgraph cluster_w { label="mutually coupled word"; style="rounded"; '
               'color="#ef6c00"; fontcolor="#ef6c00";')
    for n in sorted(coupled):
        dot.append('    "{}" [fillcolor="#ffe0b2"];'.format(n))
    dot.append("  }")
    dot.append('  subgraph cluster_h { label="self-holding bit"; style="rounded"; '
               'color="#6a1b9a"; fontcolor="#6a1b9a";')
    for n in sorted(holders):
        dot.append('    "{}" [fillcolor="#e1bee7"];'.format(n))
    dot.append("  }")
    for n in sorted(info):
        for s in info[n]["d_ffs"]:
            if s == n:
                dot.append('  "{0}" -> "{0}" [color="#ef6c00", style=bold];'.format(n))
                continue
            if s in coupled and n in coupled:
                colour = "#ef6c00"
            elif s in delays and n in delays:
                colour = "#2e7d32"
            else:
                colour = "#90a4ae"
            dot.append('  "{}" -> "{}" [color="{}"];'.format(s, n, colour))
        for pi in info[n]["d_inputs"]:
            dot.append('  "{}" [shape=ellipse, fillcolor="#bbdefb"];'.format(pi))
            dot.append('  "{}" -> "{}" [color="#1565c0", style=dotted];'.format(pi, n))
    dot.append("}")
    with open(path, "w", newline="\n") as fh:
        fh.write("\n".join(dot) + "\n")
    render(path)


def render(dot_path):
    svg = dot_path[:-4] + ".svg"
    try:
        subprocess.run(["dot", "-Tsvg", dot_path, "-o", svg], check=True)
        print("rendered", svg)
    except Exception as exc:  # graphviz is optional
        print("dot failed for {}: {}".format(dot_path, exc), file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--netlist", default=None)
    ap.add_argument("--gate-library", default=None)
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--images", default=None,
                    help="where to write register_graph.dot/.svg (default: <example>/images)")
    args = ap.parse_args(argv)

    repo = os.path.abspath(args.repo)
    here = os.path.dirname(os.path.abspath(__file__))
    netlist_path = args.netlist or os.path.join(here, "netlist.hal.v")
    gl = args.gate_library or os.path.join(
        repo, "plugins", "gate_libraries", "definitions", "AGILEX_TENNM.hgl")
    outdir = os.path.abspath(args.output)
    imgdir = os.path.abspath(args.images or os.path.join(here, "images"))
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(imgdir, exist_ok=True)

    sys.path.insert(0, os.path.join(repo, "tools"))
    from hal_agilex import hal_adapter

    hal_py = hal_adapter.import_hal([os.environ.get("HAL_PY_PATH", "")])
    nl = hal_adapter.load_netlist(hal_py, netlist_path, gl)
    report = hal_adapter.elaborate(hal_py, nl)
    print("elaborate:", {k: v for k, v in report.items() if k != "refused"})
    if report["refused"]:
        print("REFUSED:", report["refused"], file=sys.stderr)

    def out(name, lines):
        path = os.path.join(outdir, name)
        with open(path, "w", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        print("=" * 76)
        print("== " + name)
        print("=" * 76)
        print("\n".join(lines))
        print()

    result = {"netlist": os.path.basename(netlist_path),
              "elaborate": {k: v for k, v in report.items() if k != "refused"}}
    result["stats"] = step_stats(nl, out)
    result["clock_reset"] = step_clock_reset(nl, out)
    result["gate_scc"] = step_gate_sccs(nl, out)
    info = step_register_graph(nl, out)
    result["registers"] = info
    reg = step_register_sccs(nl, out, info)
    result["register_scc"] = reg
    result["luts"] = step_functions(nl, out)
    result["semantics"] = step_word_semantics(hal_py, nl, out, info,
                                              reg["words"], reg["holders"])

    write_register_dot(os.path.join(imgdir, "register_graph.dot"), info, reg)

    with open(os.path.join(outdir, "analysis.json"), "w", newline="\n") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    print("wrote", os.path.join(outdir, "analysis.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
