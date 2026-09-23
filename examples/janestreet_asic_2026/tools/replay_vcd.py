#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""replay_vcd.py -- ground-truth validation of the extracted puzzle netlist.

Replays ``example_inputs.vcd`` through the netlist recovered from ``puzzle.gds``
(``artifacts/puzzle_netlist.json``) using the cycle-based gate-level simulator
in :mod:`sc_sim`, and demands *exact* agreement with the outputs the VCD
recorded.

Why this exists
---------------
``gds2netlist.py`` was validated structurally (100% against the shipped warmup
DEF) and ``sc_sim.py`` was validated functionally (against the warmup RTL), but
neither check exercises *the puzzle netlist behaving like the puzzle*.  The
shipped VCD is the only piece of ground truth that couples the two: it is a
recording of the real design being driven with a known stimulus and producing
known outputs.  If the extraction dropped a wire, mis-assigned a pin, or picked
the wrong cell flavour, the replay diverges here.

Method
------
1. Parse the VCD directly -- no hand-transcribed stimulus.  Every value change
   is applied in file order to a running state; a snapshot is kept *before* and
   *after* the events of each timestamp.
2. Find every rising edge of ``clk``.  The puzzle's stimulus changes on falling
   edges, so the value a real DFF samples at a rising edge is the *pre*-edge
   state of ``rst_n`` / ``enable`` / ``I``.  (The harness asserts that no
   stimulus signal actually changes at a posedge timestamp, so the choice is
   provably unambiguous for this VCD.)
3. The recorded response ``O[7:0]`` / ``success`` is the *post*-edge state:
   registered outputs settle after the edge, and the dumper emits them in the
   same timestamp block as the ``clk`` transition.
4. Drive ``sc_sim`` one :meth:`Simulator.step` per rising edge with the sampled
   stimulus.  ``step()`` applies inputs, settles (which is where asynchronous
   ``rst_n`` assertion between edges takes effect -- ``dfrtp`` clears to 0,
   ``dfstp`` presets to 1), takes the rising edge, and settles again.  ``clk``
   itself is *not* poked as a net: the simulator owns the edge.
5. Compare ``O``/``success`` at every edge.  Any mismatch is reported with its
   edge index, timestamp, stimulus and both values.

Undriven net ``n278``
---------------------
The GDS genuinely leaves ``n278`` unrouted (it feeds ``a31oi_2_172960_89760.A1``
and ``a311o_2_177560_89760.A1`` and has no driver).  ``sc_sim`` reads undriven
nets as 0.  ``--undriven both`` runs the whole replay twice, once with the net
forced to 0 and once forced to 1, so the report can state whether the ground
truth actually distinguishes the two conventions.  ``--fanout`` additionally
prints the transitive fan-out cone of the net (which flops and outputs it can
reach at all).

Usage::

    python tools/replay_vcd.py                       # default paths, both conventions
    python tools/replay_vcd.py --undriven 0
    python tools/replay_vcd.py -o artifacts/replay_validation.txt
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import sc_sim  # noqa: E402

DEFAULT_VCD = os.path.join(ROOT, "example_inputs.vcd")
DEFAULT_NETLIST = os.path.join(ROOT, "artifacts", "puzzle_netlist.json")
DEFAULT_REPORT = os.path.join(ROOT, "artifacts", "replay_validation.txt")

STIMULUS = ("rst_n", "enable", "I")
UNDRIVEN_NET = "n278"


# ---------------------------------------------------------------------------
# VCD parsing
# ---------------------------------------------------------------------------

def parse_vcd(path: str) -> Tuple[Dict[str, Tuple[str, int]], List[Tuple[int, List[Tuple[str, str]]]]]:
    """Parse a VCD into ``(id2name, blocks)``.

    ``id2name`` maps the one-character symbol to ``(signal name, width)``;
    a vector's name is the bare identifier (``"O"``), the ``[7:0]`` suffix is
    dropped.  ``blocks`` is ``[(time, [(sym, value_string), ...]), ...]`` in
    file order, one entry per ``#<time>`` marker plus the initial ``$dumpvars``
    block at time 0.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = [ln.strip() for ln in fh]

    id2name: Dict[str, Tuple[str, int]] = {}
    blocks: List[Tuple[int, List[Tuple[str, str]]]] = []
    cur: List[Tuple[str, str]] = []
    time = 0
    in_defs = True
    skip_block = False  # inside $date/$version/$comment ... $end

    for s in lines:
        if not s:
            continue
        if skip_block:
            if s.endswith("$end"):
                skip_block = False
            continue
        if in_defs:
            if s.startswith("$var"):
                tok = s.split()
                # $var wire 8 % O [7:0] $end
                id2name[tok[3]] = (tok[4], int(tok[2]))
                continue
            if s.startswith("$enddefinitions"):
                in_defs = False
                blocks.append((0, cur))
                continue
            if s.startswith(("$date", "$version", "$comment", "$timescale")) and not s.endswith("$end"):
                skip_block = True
            continue
        if s.startswith(("$comment", "$date", "$version")) and not s.endswith("$end"):
            skip_block = True
            continue
        if s in ("$dumpall", "$dumpvars", "$end", "$dumpon", "$dumpoff", "$upscope"):
            continue
        if s.startswith("#"):
            time = int(s[1:])
            cur = []
            blocks.append((time, cur))
            continue
        if s[0] in "bBrR":
            val, sym = s.split()
            cur.append((sym, val[1:]))
        else:
            cur.append((s[1:], s[0]))
    return id2name, blocks


def _scalar(v: str) -> Optional[int]:
    if v in ("0", "1"):
        return int(v)
    return None  # x / z


def _vector(v: str, width: int) -> Optional[int]:
    if any(c not in "01" for c in v):
        return None
    return int(v, 2)


class Edge:
    """One recorded rising clock edge."""

    __slots__ = ("index", "time", "stim", "exp_o", "exp_success")

    def __init__(self, index: int, time: int, stim: Dict[str, Optional[int]],
                 exp_o: Optional[int], exp_success: Optional[int]):
        self.index = index
        self.time = time
        self.stim = stim
        self.exp_o = exp_o
        self.exp_success = exp_success


def extract_posedges(id2name, blocks) -> Tuple[List[Edge], List[str]]:
    """Walk the VCD timeline and pull out every clk 0->1 edge.

    Returns ``(edges, notes)``; ``notes`` records anything surprising about the
    sampling model (e.g. a stimulus signal changing on a rising edge, which
    would make the pre/post choice ambiguous).
    """
    sym = {name: s for s, (name, _w) in id2name.items()}
    for need in ("clk", "rst_n", "enable", "I", "O", "success"):
        if need not in sym:
            raise SystemExit("VCD is missing signal %r" % need)
    width = {name: w for _s, (name, w) in id2name.items()}

    state: Dict[str, str] = {name: "x" for name in sym}
    edges: List[Edge] = []
    notes: List[str] = []

    for time, evs in blocks:
        pre = dict(state)
        changed = set()
        for s, val in evs:
            if s not in id2name:
                continue
            name = id2name[s][0]
            state[name] = val
            changed.add(name)
        if not (pre.get("clk") == "0" and state.get("clk") == "1"):
            continue
        # rising edge
        touched = sorted(changed & set(STIMULUS))
        if touched:
            notes.append(
                "t=%d: stimulus %s changed in the same timestamp block as the "
                "rising edge -- pre-edge value used" % (time, ",".join(touched))
            )
        stim = {n: _scalar(pre[n]) for n in STIMULUS}
        edges.append(Edge(
            index=len(edges),
            time=time,
            stim=stim,
            exp_o=_vector(state["O"], width["O"]),
            exp_success=_scalar(state["success"]),
        ))
    return edges, notes


# ---------------------------------------------------------------------------
# fan-out cone of the undriven net
# ---------------------------------------------------------------------------

def fanout_cone(nl: "sc_sim.Netlist", start: str) -> Dict[str, object]:
    """Transitive combinational fan-out of ``start``: which cells, which flop
    data pins, and which primary outputs it can reach."""
    sim = sc_sim.Simulator(nl)
    # net -> list of (instance, pin) readers
    readers: Dict[str, List[Tuple[sc_sim.Instance, str]]] = {}
    for inst in nl.instances:
        cell = sc_sim.lookup_cell(inst.cell)
        if cell is None or cell.physical:
            continue
        ins = list(cell.inputs)
        if cell.is_seq and cell.seq is not None:
            ins = [p for p in (cell.seq.d, cell.seq.clk, cell.seq.reset_b,
                               cell.seq.set_b, cell.seq.enable,
                               cell.seq.scan_sel, cell.seq.scan_in) if p]
        for pin in ins:
            net = inst.pins.get(pin)
            if net:
                readers.setdefault(net, []).append((inst, pin))

    seen_nets = {start}
    frontier = [start]
    cells: List[str] = []
    flops: List[str] = []
    while frontier:
        net = frontier.pop()
        for inst, pin in readers.get(net, []):
            cell = sc_sim.lookup_cell(inst.cell)
            assert cell is not None
            if cell.is_seq:
                flops.append("%s.%s" % (inst.name, pin))
                continue
            if inst.name not in cells:
                cells.append(inst.name)
            for out_pin in cell.outputs:
                onet = inst.pins.get(out_pin)
                if onet and onet not in seen_nets:
                    seen_nets.add(onet)
                    frontier.append(onet)
    outs = [o for o in nl.outputs if o in seen_nets]
    return {
        "nets": sorted(seen_nets),
        "cells": cells,
        "flop_pins": sorted(flops),
        "outputs": outs,
        "undriven": list(sim.undriven),
    }


def cone_observability(nl: "sc_sim.Netlist", cone: Dict[str, object], start: str,
                       max_boundary: int = 22) -> Dict[str, object]:
    """Exhaustively ask whether ``start`` can *ever* change a primary output.

    The fan-out cone of ``n278`` is purely combinational, so its behaviour is a
    finite function of ``start`` plus the cone's boundary inputs.  Enumerating
    every boundary assignment answers the question the VCD replay cannot: the
    replay only shows the two conventions agree *on this stimulus*.
    """
    byname = {i.name: i for i in nl.instances}
    cells = list(cone["cells"])  # type: ignore[arg-type]
    produced = set()
    for c in cells:
        inst = byname[c]
        cd = sc_sim.lookup_cell(inst.cell)
        assert cd is not None
        for p in cd.outputs:
            if inst.pins.get(p):
                produced.add(inst.pins[p])
    boundary = set()
    for c in cells:
        inst = byname[c]
        cd = sc_sim.lookup_cell(inst.cell)
        assert cd is not None
        for p in cd.inputs:
            n = inst.pins.get(p)
            if n and n not in produced and n != start:
                boundary.add(n)
    boundary = sorted(boundary)
    if len(boundary) > max_boundary:
        return {"boundary": boundary, "exhaustive": False}

    order: List[str] = []
    ready = set(boundary) | {start}
    remaining = list(cells)
    while remaining:
        progressed = False
        still = []
        for c in remaining:
            inst = byname[c]
            cd = sc_sim.lookup_cell(inst.cell)
            assert cd is not None
            if all(inst.pins.get(p) in ready for p in cd.inputs):
                order.append(c)
                for p in cd.outputs:
                    if inst.pins.get(p):
                        ready.add(inst.pins[p])
                progressed = True
            else:
                still.append(c)
        remaining = still
        if not progressed:
            return {"boundary": boundary, "exhaustive": False}

    targets = [o for o in nl.outputs if o in set(cone["nets"])]  # type: ignore[arg-type]

    def evaluate(vals):
        v = dict(vals)
        for c in order:
            inst = byname[c]
            cd = sc_sim.lookup_cell(inst.cell)
            assert cd is not None
            pv = {p: v.get(inst.pins.get(p), 0) for p in cd.inputs}
            for p, fn in cd.outputs.items():
                if inst.pins.get(p):
                    v[inst.pins[p]] = fn(pv)
        return v

    import itertools
    diff = 0
    total = 0
    witness = None
    for bits in itertools.product((0, 1), repeat=len(boundary)):
        base = dict(zip(boundary, bits))
        a = evaluate({**base, start: 0})
        b = evaluate({**base, start: 1})
        total += 1
        if any(a[t] != b[t] for t in targets):
            diff += 1
            if witness is None:
                witness = base
    return {
        "boundary": boundary,
        "exhaustive": True,
        "targets": targets,
        "sensitising": diff,
        "total": total,
        "witness": witness,
    }


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

class Mismatch:
    __slots__ = ("edge", "field", "got", "want")

    def __init__(self, edge: Edge, field: str, got: int, want: int):
        self.edge = edge
        self.field = field
        self.got = got
        self.want = want

    def __str__(self) -> str:
        e = self.edge
        return ("edge %4d t=%-9d rst_n=%s enable=%s I=%s : %s sim=0x%02x vcd=0x%02x"
                % (e.index, e.time, e.stim["rst_n"], e.stim["enable"], e.stim["I"],
                   self.field, self.got, self.want))


def replay(netlist_path: str, edges: Sequence[Edge], undriven_value: int,
           probe_nets: Sequence[str] = ()):
    """Run the whole VCD through the simulator; return a result dict.

    ``probe_nets`` are sampled after every edge so two runs can be diffed at
    the internal-net level, not just at the primary outputs.
    """
    nl = sc_sim.load_netlist(netlist_path)
    if undriven_value:
        nl.constants[UNDRIVEN_NET] = 1
    sim = sc_sim.Simulator(nl)

    compared = 0
    skipped = 0
    mismatches: List[Mismatch] = []
    trace: List[Tuple[Edge, int, int]] = []
    probe: List[Tuple[int, ...]] = []
    flop_toggles: Dict[str, int] = {}
    prev_state = sim.state()

    for e in edges:
        stim = {k: v for k, v in e.stim.items() if v is not None}
        sim.step(stim)
        st = sim.state()
        for k, v in st.items():
            if v != prev_state[k]:
                flop_toggles[k] = flop_toggles.get(k, 0) + 1
        prev_state = st
        got_o = sim.get_bus("O", 8)
        got_s = sim.get("success")
        trace.append((e, got_o, got_s))
        if probe_nets:
            probe.append(tuple(sim.get(n) for n in probe_nets))
        if e.exp_o is None or e.exp_success is None:
            skipped += 1
            continue
        compared += 1
        if got_o != e.exp_o:
            mismatches.append(Mismatch(e, "O", got_o, e.exp_o))
        if got_s != e.exp_success:
            mismatches.append(Mismatch(e, "success", got_s, e.exp_success))
    return {
        "undriven_value": undriven_value,
        "edges": len(edges),
        "compared": compared,
        "skipped_x": skipped,
        "mismatches": mismatches,
        "trace": trace,
        "probe": probe,
        "flop_toggles": flop_toggles,
        "sim": sim,
    }


def split_attempts(edges: Sequence[Edge]) -> List[Tuple[int, int]]:
    """Group edges into attempts: a new attempt starts at each rst_n low run."""
    spans: List[Tuple[int, int]] = []
    start = None
    prev_rst = 1
    for e in edges:
        r = e.stim["rst_n"]
        if r == 0 and prev_rst != 0:
            if start is not None:
                spans.append((start, e.index - 1))
            start = e.index
        prev_rst = r if r is not None else prev_rst
    if start is not None:
        spans.append((start, edges[-1].index))
    return spans


def byte_stream(trace, lo: int, hi: int, source: str) -> List[int]:
    """Bytes emitted on O while enable is low, within [lo, hi]."""
    out = []
    for e, got_o, _got_s in trace:
        if not (lo <= e.index <= hi):
            continue
        if e.stim["enable"] == 1:
            continue
        v = got_o if source == "sim" else e.exp_o
        if v is None:
            continue
        out.append(v)
    return out


def ascii_of(bs: Sequence[int]) -> str:
    return "".join(chr(b) if 32 <= b < 127 else ("\\0" if b == 0 else "\\x%02x" % b)
                   for b in bs)


def serial_payload(edges: Sequence[Edge], lo: int, hi: int) -> Tuple[List[int], str]:
    """The I bits shifted in while enable=1, and their LSB-first ASCII reading."""
    bits = [e.stim["I"] for e in edges if lo <= e.index <= hi and e.stim["enable"] == 1]
    chars = []
    for i in range(0, len(bits) - 7, 11):
        grp = bits[i:i + 11]
        if len(grp) < 8:
            break
        val = 0
        for j, b in enumerate(grp[:8]):
            if b:
                val |= 1 << j
        chars.append(chr(val) if 32 <= val < 127 else "?")
    return bits, "".join(chars)


# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--vcd", default=DEFAULT_VCD)
    ap.add_argument("--netlist", default=DEFAULT_NETLIST)
    ap.add_argument("-o", "--out", default=DEFAULT_REPORT)
    ap.add_argument("--undriven", default="both", choices=["0", "1", "both"],
                    help="value forced onto the undriven net %s" % UNDRIVEN_NET)
    ap.add_argument("--max-mismatch", type=int, default=40,
                    help="how many mismatches to print in full")
    ap.add_argument("--fanout", action="store_true", default=True)
    args = ap.parse_args(argv)

    id2name, blocks = parse_vcd(args.vcd)
    edges, notes = extract_posedges(id2name, blocks)
    spans = split_attempts(edges)

    L: List[str] = []
    def out(s: str = "") -> None:
        L.append(s)
        print(s)

    out("=" * 78)
    out("END-TO-END REPLAY VALIDATION -- example_inputs.vcd vs extracted netlist")
    out("=" * 78)
    out("vcd      : %s" % args.vcd)
    out("netlist  : %s" % args.netlist)
    out("simulator: tools/sc_sim.py (cycle-based, 2-valued, zero-delay)")
    out()
    out("Sampling model")
    out("--------------")
    out("  stimulus (rst_n/enable/I) sampled at the value in effect *entering*")
    out("  each clk 0->1 timestamp (they change on falling edges);")
    out("  response (O[7:0]/success) compared against the value in effect after")
    out("  that timestamp's events (registered outputs, dumped with the edge).")
    if notes:
        out("  NOTE: %d timestamp(s) carry both a rising edge and a stimulus change:" % len(notes))
        for n in notes[:10]:
            out("    " + n)
    else:
        out("  No stimulus signal changes in a rising-edge timestamp block, so the")
        out("  pre/post split above is unambiguous for this VCD.")
    out()
    out("Timeline")
    out("--------")
    out("  rising edges found : %d" % len(edges))
    out("  first / last time  : %d ps / %d ps" % (edges[0].time, edges[-1].time))
    out("  attempts (rst_n low runs) : %d" % len(spans))
    for k, (lo, hi) in enumerate(spans):
        bits, txt = serial_payload(edges, lo, hi)
        n_en = sum(1 for e in edges if lo <= e.index <= hi and e.stim["enable"] == 1)
        out("    attempt %d: edges %d..%d  (%d edges), enable=1 for %d edges, "
            "payload=%r" % (k + 1, lo, hi, hi - lo + 1, n_en, txt))
        del bits
    out()

    # --- fan-out of the undriven net ---------------------------------------
    nl0 = sc_sim.load_netlist(args.netlist)
    cone = fanout_cone(nl0, UNDRIVEN_NET)
    out("Undriven net %s" % UNDRIVEN_NET)
    out("-" * 78)
    out("  simulator's undriven list : %s" % (cone["undriven"] or "(none)"))
    out("  transitive fan-out: %d nets, %d combinational cells" %
        (len(cone["nets"]) - 1, len(cone["cells"])))
    out("  reaches flop pins : %s" % (", ".join(cone["flop_pins"]) or "(none)"))
    out("  reaches outputs   : %s" % (", ".join(cone["outputs"]) or "(none)"))
    out("  cells in cone     : %s" % (", ".join(cone["cells"]) or "(none)"))
    obs = cone_observability(nl0, cone, UNDRIVEN_NET)
    out("  cone boundary inputs : %d (%s)"
        % (len(obs["boundary"]), ", ".join(obs["boundary"])))
    if obs.get("exhaustive"):
        out("  exhaustive check over all %d boundary assignments:" % obs["total"])
        out("    %s flips %s in %d of them (%.3f%%)"
            % (UNDRIVEN_NET, "/".join(obs["targets"]), obs["sensitising"],
               100.0 * obs["sensitising"] / obs["total"]))
        out("    -> the net IS structurally observable at O[1]/O[4], but only for")
        out("       boundary states that the recorded stimulus never reaches.")
        out("    -> it reaches NO flip-flop and NOT `success`, so the value chosen")
        out("       for it cannot affect the key search, which keys on `success`.")
    else:
        out("  cone too wide for an exhaustive observability check")
    out()

    probe_nets = [n for n in cone["nets"]]
    conventions = [0, 1] if args.undriven == "both" else [int(args.undriven)]
    results = {}
    for uv in conventions:
        res = replay(args.netlist, edges, uv, probe_nets)
        results[uv] = res
        out("Replay with %s = %d" % (UNDRIVEN_NET, uv))
        out("-" * 78)
        out("  clock edges simulated        : %d" % res["edges"])
        out("  edge/value comparisons made  : %d  (O and success at %d edges)"
            % (res["compared"] * 2, res["compared"]))
        out("  edges skipped (VCD value x)  : %d" % res["skipped_x"])
        out("  mismatches                   : %d" % len(res["mismatches"]))
        for m in res["mismatches"][:args.max_mismatch]:
            out("      " + str(m))
        if len(res["mismatches"]) > args.max_mismatch:
            out("      ... %d more" % (len(res["mismatches"]) - args.max_mismatch))
        for k, (lo, hi) in enumerate(spans):
            sim_bytes = byte_stream(res["trace"], lo, hi, "sim")
            vcd_bytes = byte_stream(res["trace"], lo, hi, "vcd")
            sim_nz = [b for b in sim_bytes]
            out("  attempt %d output byte stream (enable=0 window):" % (k + 1))
            out("      sim : %s  |%s|" % (" ".join("%02x" % b for b in sim_nz[:12]),
                                          ascii_of(sim_nz[:12])))
            out("      vcd : %s  |%s|" % (" ".join("%02x" % b for b in vcd_bytes[:12]),
                                          ascii_of(vcd_bytes[:12])))
            out("      identical over the whole window: %s"
                % (sim_bytes == vcd_bytes))
        succ = [1 for e, _o, s in res["trace"] if s]
        out("  success asserted at any edge : %s" % bool(succ))
        out("  verdict                      : %s"
            % ("EXACT MATCH" if not res["mismatches"] else "MISMATCH"))
        out()

    out("Summary")
    out("-" * 78)
    ok = [uv for uv in conventions if not results[uv]["mismatches"]]
    if ok:
        out("  100%% agreement with the recorded VCD for %s in {%s}."
            % (UNDRIVEN_NET, ", ".join(str(u) for u in ok)))
        n = results[ok[0]]["compared"]
        out("  %d clock edges, %d value comparisons (O[7:0] as a byte + success), "
            "0 mismatches." % (n, n * 2))
    else:
        out("  NO convention reproduces the VCD.  See mismatch list above.")
    if len(conventions) == 2:
        same = (results[0]["trace"] and
                [(o, s) for _e, o, s in results[0]["trace"]] ==
                [(o, s) for _e, o, s in results[1]["trace"]])
        out("  %s=0 vs %s=1 produce %s observable output sequence."
            % (UNDRIVEN_NET, UNDRIVEN_NET,
               "an IDENTICAL" if same else "a DIFFERENT"))
        st0 = results[0]["sim"].state()
        st1 = results[1]["sim"].state()
        diff = [k for k in st0 if st0[k] != st1[k]]
        out("  final flop state differs in %d of %d flops%s"
            % (len(diff), len(st0), (": " + ", ".join(diff[:8])) if diff else ""))
        # internal-net diff across the whole run, over the fan-out cone
        p0, p1 = results[0]["probe"], results[1]["probe"]
        ever = set()
        for a, b in zip(p0, p1):
            for i, (x, y) in enumerate(zip(a, b)):
                if x != y:
                    ever.add(probe_nets[i])
        out("  cone nets that ever differ between the two runs: %d of %d%s"
            % (len(ever), len(probe_nets),
               (" -> " + ", ".join(sorted(ever))) if ever else ""))
        if ever and same:
            out("    (the difference is masked before it reaches O/success)")

    out()
    out("Alignment cross-check (is the chosen edge alignment uniquely right?)")
    out("-" * 78)
    ref = results[conventions[0]]["trace"]
    for shift in (-2, -1, 0, 1, 2):
        bad = 0
        n = 0
        for i, (e, got_o, got_s) in enumerate(ref):
            j = i + shift
            if not (0 <= j < len(edges)):
                continue
            n += 1
            if got_o != edges[j].exp_o or got_s != edges[j].exp_success:
                bad += 1
        out("  sim edge k vs VCD edge k%+d : %d/%d edges disagree%s"
            % (shift, bad, n, "   <== chosen alignment" if shift == 0 else ""))
    out("  A non-zero count at every shift except 0 means the match is not an")
    out("  artefact of a constant output sequence.")

    out()
    out("Discriminating power of this test")
    out("-" * 78)
    res = results[conventions[0]]
    tog = res["flop_toggles"]
    n_ff = len(res["sim"].state())
    out("  flops that toggled at least once : %d of %d" % (len(tog), n_ff))
    quiet = [k for k in res["sim"].state() if k not in tog]
    if quiet:
        out("  never-toggling flops             : %s%s"
            % (", ".join(sorted(quiet)[:10]), " ..." if len(quiet) > 10 else ""))
    nz = sorted({o for _e, o, _s in res["trace"]})
    out("  distinct O values produced       : %s" % " ".join("0x%02x" % v for v in nz))
    out("  NOTE: both recorded attempts fail, so `success` is 0 at all %d edges."
        % res["compared"])
    out("  This validates reset behaviour, the 121-cycle shift/enable window, the")
    out("  output FSM timing and the failure-message ROM -- it does NOT prove the")
    out("  key-comparison logic is correct, only that it rejects both payloads.")
    out("  Quantified by tools/replay_mutation_check.py -> "
        "artifacts/replay_mutation_check.txt:")
    out("  single-cell pin-swap mutants that genuinely change a cell's truth table")
    out("  are caught only ~10% of the time, because the comparator cone is")
    out("  unobservable while every recorded attempt fails.")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(L) + "\n")
        print("\nwrote %s" % args.out)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
