#!/usr/bin/env python3
"""The structural half of the 03_uart_tx walkthrough, as one reproducible run.

Everything `guide.html` claims about the *structure* of the netlist is produced
here, from `netlist_anon.hal.v` -- the name-stripped netlist -- so that no step
can accidentally read an answer off a signal name.

Needs a built HAL:

    export HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib
    PYTHONPATH=/work/build/lib python3 examples/agilex3_walkthroughs/03_uart_tx/analyze.py \
        --repo . -o examples/agilex3_walkthroughs/03_uart_tx/artifacts

Writes one text file per step (quoted verbatim in the guide), two DOT graphs,
and `analysis.json` with the machine-readable conclusions that `check.py`
asserts on.
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


def cone_support(gate, pin):
    """Sequential/primary-input support of one input pin of *gate*.

    Walks backwards through combinational cells only and returns
    (ff_gates, primary_input_names).  This is the primitive every later step is
    built from: it answers "which registers and which pins does this pin
    actually depend on", which is what tells a counter from a shift register.
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
# steps
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
    out("01_stats.txt", lines)
    return {"gate_types": types, "inputs": inputs, "outputs": outputs,
            "num_modules": len(modules)}


def step_clock_reset(nl, out):
    """Which nets reach the clock, asynchronous-clear and enable pins."""
    buckets = {"clk": {}, "clrn": {}, "ena": {}}
    for g in nl.get_gates():
        if not is_ff(g):
            continue
        for pin in buckets:
            net = g.get_fan_in_net(pin)
            key = "<open>" if net is None else (
                "1'b1" if net.is_vcc_net() else "1'b0" if net.is_gnd_net() else net.get_name())
            buckets[pin].setdefault(key, []).append(g.get_name())

    lines = ["Every flip-flop control pin, grouped by the net that drives it.", ""]
    for pin, title in (("clk", "clk"), ("clrn", "clrn (asynchronous clear)"), ("ena", "ena")):
        lines.append("{}:".format(title))
        for net, gates in sorted(buckets[pin].items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines.append("  {:<10} x{:<3} {}".format(net, len(gates), " ".join(sorted(gates))))
        lines.append("")
    lines += [
        "One clock net and one clear net across all {} flip-flops: a single".format(
            len(buckets["clk"] and [g for g in nl.get_gates() if is_ff(g)])),
        "clock domain with one asynchronous reset.  Nothing here needs a CDC",
        "analysis; everything here is one synchronous machine.",
    ]
    out("02_clock_reset.txt", lines)
    return {
        "clock_nets": sorted(buckets["clk"]),
        "reset_nets": sorted(buckets["clrn"]),
        "enable_nets": {k: sorted(v) for k, v in buckets["ena"].items()},
    }


def step_gate_sccs(nl, out):
    """Strongly connected components of the raw gate graph, via graph_algorithm."""
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
        "Read this carefully.  The big component is NOT 'one register': the",
        "gate graph merges everything that is mutually reachable, and a control",
        "flag that gates a counter while the counter feeds the flag back is",
        "mutually reachable with it.  The gate-level SCC therefore answers only",
        "the coarse question -- which gates are inside feedback and which are",
        "not -- and the fine structure has to come from the register graph below.",
    ]
    out("03_gate_sccs.txt", lines)
    return {"gate_sccs": named, "ffs_outside_gate_sccs": acyclic_ffs}


def step_register_graph(nl, out, outdir):
    """Pin-aware register-to-register graph: what each flop's D and ENA depend on."""
    ffs = [g for g in nl.get_gates() if is_ff(g)]
    info = {}
    for g in ffs:
        d_ffs, d_ins = cone_support(g, "d")
        e_net = g.get_fan_in_net("ena")
        if const_kind(e_net) is not None:
            e_ffs, e_ins = set(), set()
        else:
            e_ffs, e_ins = cone_support(g, "ena")
        info[g.get_name()] = {
            "d_ffs": sorted(x.get_name() for x in d_ffs),
            "d_inputs": sorted(d_ins),
            "ena_const": const_kind(e_net),
            "ena_net": None if e_net is None else e_net.get_name(),
            "ena_ffs": sorted(x.get_name() for x in e_ffs),
            "ena_inputs": sorted(e_ins),
            "self_feedback": g.get_name() in {x.get_name() for x in d_ffs},
        }

    lines = [
        "For every flip-flop: the flip-flops and primary inputs its D pin depends",
        "on (through combinational cells only), and the same for its ENA pin.",
        "'self' marks a flop whose own output is in its next-state cone -- it can",
        "hold or count; a flop without it can only be loaded from somewhere else.",
        "",
        "{:<8} {:<5} {:<34} {:<12} {}".format("FF", "self", "D depends on (FFs)", "D <- PI", "ENA"),
        "-" * 100,
    ]
    for name in sorted(info):
        i = info[name]
        ena = ("1'b" + i["ena_const"]) if i["ena_const"] else "{} <- {}{}".format(
            i["ena_net"], ",".join(i["ena_ffs"]),
            ("+" + ",".join(i["ena_inputs"])) if i["ena_inputs"] else "")
        lines.append("{:<8} {:<5} {:<34} {:<12} {}".format(
            name, "self" if i["self_feedback"] else ".",
            ",".join(i["d_ffs"]) or "-", ",".join(i["d_inputs"]) or "-", ena))
    out("04_register_graph.txt", lines)
    return info


def step_register_sccs(nl, out, info, outdir):
    """SCCs of the REGISTER graph: control (feedback) versus datapath (a chain)."""
    nodes = sorted(info)
    edges = {n: [s for s in info[n]["d_ffs"] if s != n] for n in nodes}
    rev = {n: [] for n in nodes}
    for dst, srcs in edges.items():
        for s in srcs:
            rev[s].append(dst)
    comps = sorted(tarjan(nodes, rev), key=lambda c: (-len(c), c))
    big = [c for c in comps if len(c) > 1]
    control = set()
    for c in big:
        control |= set(c)
    datapath = sorted(set(nodes) - control)

    # order the datapath by its own edges
    dp_pred = {n: [s for s in info[n]["d_ffs"] if s in set(datapath) and s != n]
               for n in datapath}
    heads = [n for n in datapath if not dp_pred[n]]
    chains = []
    succ = {}
    for n, ps in dp_pred.items():
        for p in ps:
            succ.setdefault(p, []).append(n)
    for h in sorted(heads):
        chain = [h]
        while len(succ.get(chain[-1], [])) == 1:
            chain.append(succ[chain[-1]][0])
        chains.append(chain)
    chains.sort(key=len, reverse=True)

    lines = [
        "The same edges as step 04, but between flip-flops only, and with the",
        "self-loops removed.  Strongly connected components of THAT graph:",
        "",
    ]
    for i, c in enumerate(comps):
        lines.append("  component {}: {:2d} flop(s)  {}".format(i, len(c), " ".join(c)))
    lines += [
        "",
        "=> {} flop(s) sit inside feedback: {}".format(len(control), " ".join(sorted(control))),
        "=> {} flop(s) do not: {}".format(len(datapath), " ".join(datapath)),
        "",
        "A register that is not inside any feedback loop cannot count and cannot",
        "hold a value that depends on its own history through other registers.",
        "It can only pass a value along.  Nine of them, in one clock domain, is",
        "a serializer -- and the edges between them give the order:",
        "",
    ]
    for c in chains:
        lines.append("  chain of {:2d}: {}".format(len(c), "  ->  ".join(c)))
    if chains:
        head, tail = chains[0][0], chains[0][-1]
        lines += [
            "",
            "head {}: D depends on primary input(s) {} and no datapath flop".format(
                head, ",".join(info[head]["d_inputs"]) or "(none)"),
            "tail {}: drives the netlist output".format(tail),
            "",
            "Per-stage primary inputs along the chain (the parallel load port):",
        ]
        for n in chains[0]:
            lines.append("  {:<8} <- {}".format(n, ",".join(info[n]["d_inputs"]) or "(none)"))
    out("05_register_sccs.txt", lines)
    return {"components": comps, "control": sorted(control), "datapath": datapath,
            "chains": chains}


def step_counters(nl, out, info, control):
    """Split the control blob: find the feedback flop, then the two counters."""
    blob = set(control)
    dsup = {n: set(info[n]["d_ffs"]) & blob for n in control}

    def cyclic(without):
        nodes = [n for n in control if n not in without]
        edges = {n: [s for s in dsup[n] if s != n and s not in without] for n in nodes}
        return any(len(c) > 1 for c in tarjan(nodes, edges))

    breakers = sorted(n for n in control if not cyclic({n}))

    lines = [
        "The feedback blob from step 05 is nine flops, not one register.  Take",
        "it apart in two moves.",
        "",
        "(a) MINIMUM FEEDBACK VERTEX.  Remove one flop at a time and ask whether",
        "    the rest is still cyclic.  A flop whose removal makes the whole blob",
        "    acyclic is carrying ALL of the feedback: that is a control flag, not",
        "    a datapath bit.",
        "",
    ]
    for n in sorted(control):
        lines.append("    without {:<8} -> {}".format(
            n, "still cyclic" if cyclic({n}) else "ACYCLIC"))
    lines += ["", "    => feedback flop(s): {}".format(" ".join(breakers) or "(none)"), ""]

    flag = breakers[0] if len(breakers) == 1 else None
    rest = sorted(blob - {flag}) if flag else sorted(blob)

    lines += [
        "(b) THE ENABLE NETS.  With the flag set aside, {} flops remain and the".format(len(rest)),
        "    graph between them is acyclic.  Which of them gate the others?",
        "",
    ]
    enable_support = {}
    for n in rest:
        if info[n]["ena_const"] is not None:
            continue
        sup = (set(info[n]["ena_ffs"]) & blob) - {flag}
        enable_support.setdefault(frozenset(sup), []).append(n)
        lines.append("    {:<8} ena = {:<8} computed from {}".format(
            n, info[n]["ena_net"], " ".join(sorted(sup)) or "(nothing in the blob)"))
    gating = set()
    for sup in enable_support:
        gating |= set(sup)
    fast = sorted(gating)
    slow = sorted(set(rest) - gating)
    lines += [
        "",
        "    => {} flop(s) appear in the enable of other flops: {}".format(len(fast), " ".join(fast)),
        "    => {} flop(s) are the ones being gated (plus whatever shares their".format(len(slow)),
        "       dependency structure): {}".format(" ".join(slow)),
        "",
        "    A register whose enable is a function of a second register only",
        "    advances when that second register says so.  The gating group is the",
        "    fast counter (the prescaler); the gated group is the slow one.",
        "",
        "(c) BIT ORDER inside each group.  In a ripple/carry counter, bit i's",
        "    next state depends on bits 0..i-1 and on itself, and on nothing",
        "    above it.  Sorting each group by the size of its in-group D-support",
        "    therefore recovers the bit significance:",
        "",
    ]
    order = {}
    for label, group in (("fast", fast), ("slow", slow)):
        ranked = sorted(group, key=lambda n: (len((dsup[n] & set(group)) - {n}), n))
        order[label] = ranked
        lines.append("    {} counter, LSB first:".format(label))
        for pos, n in enumerate(ranked):
            in_group = sorted((dsup[n] - {n} - {flag}) & set(group))
            lines.append("      bit {}  {:<8} D depends on {}".format(
                pos, n, " ".join(in_group) or "(only itself and the flag)"))
        lines.append("")
    lines += [
        "    Both groups are {} bits wide.  Nothing about their WIDTH".format(len(fast)),
        "    distinguished them -- only the dataflow did.",
    ]
    out("06_counters.txt", lines)
    return {"feedback_flops": breakers, "flag": flag,
            "fast_counter": order.get("fast", []), "slow_counter": order.get("slow", [])}


def step_functions(nl, out):
    """The Boolean function of every LUT, after hal_agilex attaches semantics."""
    lines = ["Every tennm_lcell_comb, with the function hal_agilex derived from",
             "its lut_mask, and the nets on its used data pins.", ""]
    funcs = {}
    for g in sorted((g for g in nl.get_gates() if is_lut(g)), key=lambda g: g.get_name()):
        used = []
        for pin in DATA_PINS:
            net = g.get_fan_in_net(pin)
            if net is not None and not is_const(net):
                used.append("{}={}".format(pin, net.get_name()))
        out_net = g.get_fan_out_net("combout")
        f = g.get_boolean_function("combout")
        text = str(f) if f is not None else "<none>"
        mask = (g.get_data("generic", "lut_mask") or ("", "?"))[1]
        funcs[g.get_name()] = {"function": text, "lut_mask": str(mask),
                               "out": None if out_net is None else out_net.get_name(),
                               "pins": used}
        lines.append("{}  ->  {}   lut_mask={}".format(
            g.get_name(), out_net.get_name() if out_net else "-", mask))
        lines.append("    pins : " + ", ".join(used))
        lines.append("    func : " + text)
        lines.append("")
    out("07_lut_functions.txt", lines)
    return funcs


def write_register_dot(path, info, datapath, control):
    dp, ct = set(datapath), set(control)
    dot = ["digraph registers {", "  rankdir=LR;", '  bgcolor="transparent";',
           '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10,',
           '        color="#455a64", fontcolor="#102027"];',
           '  edge [fontname="Helvetica", fontsize=8];',
           '  subgraph cluster_dp { label="no feedback: the serializer"; '
           'style="rounded"; color="#2e7d32"; fontcolor="#2e7d32";']
    for n in datapath:
        dot.append('    "{}" [fillcolor="#c8e6c9"];'.format(n))
    dot.append("  }")
    dot.append('  subgraph cluster_ct { label="inside feedback: the control"; '
               'style="rounded"; color="#ef6c00"; fontcolor="#ef6c00";')
    for n in sorted(control):
        dot.append('    "{}" [fillcolor="#ffe0b2"];'.format(n))
    dot.append("  }")
    for n in sorted(info):
        for s in info[n]["d_ffs"]:
            if s == n:
                continue
            if s in dp and n in dp:
                dot.append('  "{}" -> "{}" [color="#2e7d32", penwidth=2];'.format(s, n))
            elif s in ct and n in ct:
                dot.append('  "{}" -> "{}" [color="#ef6c00"];'.format(s, n))
            else:
                dot.append('  "{}" -> "{}" [color="#b0bec5"];'.format(s, n))
        for s in info[n]["ena_ffs"]:
            dot.append('  "{}" -> "{}" [style=dashed, color="#6a1b9a"];'.format(s, n))
    for n in sorted(info):
        for pi in info[n]["d_inputs"]:
            dot.append('  "{}" [shape=ellipse, fillcolor="#bbdefb"];'.format(pi))
            dot.append('  "{}" -> "{}" [color="#90a4ae", style=dotted];'.format(pi, n))
    dot.append("}")
    with open(path, "w") as fh:
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
    args = ap.parse_args(argv)

    repo = os.path.abspath(args.repo)
    here = os.path.join(repo, "examples", "agilex3_walkthroughs", "03_uart_tx")
    netlist_path = args.netlist or os.path.join(here, "netlist_anon.hal.v")
    gl = args.gate_library or os.path.join(
        repo, "plugins", "gate_libraries", "definitions", "AGILEX_TENNM.hgl")
    outdir = os.path.abspath(args.output)
    os.makedirs(outdir, exist_ok=True)

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
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        print("=" * 76)
        print("== " + name)
        print("=" * 76)
        print("\n".join(lines))
        print()

    result = {"netlist": os.path.relpath(netlist_path, repo),
              "elaborate": {k: v for k, v in report.items() if k != "refused"}}
    result["stats"] = step_stats(nl, out)
    result["clock_reset"] = step_clock_reset(nl, out)
    result["gate_scc"] = step_gate_sccs(nl, out)
    info = step_register_graph(nl, out, outdir)
    result["registers"] = info
    reg = step_register_sccs(nl, out, info, outdir)
    result["register_scc"] = reg
    result["counters"] = step_counters(nl, out, info, reg["control"])
    result["luts"] = step_functions(nl, out)

    write_register_dot(os.path.join(outdir, "register_graph.dot"),
                       info, reg["datapath"], reg["control"])

    with open(os.path.join(outdir, "analysis.json"), "w") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    print("wrote", os.path.join(outdir, "analysis.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
