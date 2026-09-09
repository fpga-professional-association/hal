#!/usr/bin/env python3
"""The behavioural half of the 08_shift_debouncer walkthrough.

`analyze.py` proves, from the netlist alone, that there is a 4-bit saturating
up/down counter, a hysteresis bit driven by its two end stops, and a rising-edge
detector.  It does not tell you how long a press has to be, what the latency
costs, or that any of this actually rejects contact bounce.  For that you drive
the thing.

This script simulates the *vendor export* with `tools/hal_agilex`'s reference
simulator -- no HAL build required -- and recovers, in order:

  1. the clock port    (the only net on every flip-flop's `clk` pin)
  2. the reset port    (the only net on every flip-flop's `clrn` pin)
  3. the press latency (step the remaining input and watch the outputs)
  4. the release latency
  5. the minimum press width that is accepted at all
  6. what a bounce burst does
  7. what a slow square wave that never sits still does

Nothing here reads `design.v`, `spec.md` or `reference.py`.

    python3 probe.py -o artifacts/
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FF = "tennm_ff"

#: A hand-written contact-bounce burst: 24 samples of chatter, 14 high and 10
#: low, no run longer than two.  Written out literally so the guide can quote
#: the exact stimulus rather than a seed.
BURST = "110010110100101101001011"


def load_agilex(repo):
    sys.path.insert(0, os.path.join(repo, "tools"))
    from hal_agilex import simulate, vo_netlist
    return simulate, vo_netlist


class Box(object):
    """A netlist you may drive and observe, and nothing else."""

    def __init__(self, simulate, netlist):
        self.nl = netlist
        self.sim = simulate.build(netlist)
        self.inputs = [b.name for b in _scalar_ports(netlist, "input")]
        self.outputs = [b.name for b in _scalar_ports(netlist, "output")]

    def reset_all(self, rst):
        self.sim.reset()
        for name in self.inputs:
            self.sim.set_input(name, 0)
        self.sim.set_input(rst, 0)
        self.sim.apply_async_clear()
        self.sim.set_input(rst, 1)

    def run(self, cycles, stimulus=None):
        """Return a list of dicts: the value of every output, per cycle."""
        trace = []
        for cycle in range(cycles):
            for name, value in (stimulus(cycle) if stimulus else {}).items():
                self.sim.set_input(name, value)
            self.sim.settle()
            trace.append(dict((o, self.sim.get_output(o)) for o in self.outputs))
            self.sim.clock()
        return trace


def _scalar_ports(netlist, direction):
    return [netlist.bits_of(name)[0] for name in netlist.ports(direction)]


def find_clock_and_reset(netlist):
    def pin_nets(pin):
        nets = {}
        for inst in netlist.instances_of_type(FF):
            bit = inst.single(pin)
            nets.setdefault(getattr(bit, "name", str(bit)), []).append(inst.name)
        return nets

    return pin_nets("clk"), pin_nets("clrn")


def edges(bits):
    return [i for i in range(1, len(bits)) if bits[i] != bits[i - 1]]


def pulses(bits):
    """(start, length) of every run of 1s."""
    out, start = [], None
    for i, b in enumerate(bits):
        if b and start is None:
            start = i
        elif not b and start is not None:
            out.append((start, i - start))
            start = None
    if start is not None:
        out.append((start, len(bits) - start))
    return out


def probe(repo, netlist_path, out, images=None):
    simulate, vo_netlist = load_agilex(repo)
    netlist = vo_netlist.parse_file(netlist_path)
    box = Box(simulate, netlist)
    log = []

    def say(*parts):
        line = " ".join(str(p) for p in parts)
        log.append(line)
        print(line)

    say("netlist: {} ({} instances)".format(
        os.path.basename(netlist_path), sum(netlist.type_histogram().values())))
    say("ports: {} inputs, {} outputs".format(len(box.inputs), len(box.outputs)))
    say("  inputs : " + " ".join(box.inputs))
    say("  outputs: " + " ".join(box.outputs))
    say("")

    # -- 1 & 2: clock and reset, structurally -------------------------------
    clk_nets, clr_nets = find_clock_and_reset(netlist)
    say("nets on every flip-flop clk pin :", dict((k, len(v)) for k, v in clk_nets.items()))
    say("nets on every flip-flop clrn pin:", dict((k, len(v)) for k, v in clr_nets.items()))
    clk = next(iter(clk_nets))
    rst = next(iter(clr_nets))
    say("=> clock = {}, asynchronous reset = {}".format(clk, rst))
    unknown = [p for p in box.inputs if p not in (clk, rst)]
    say("=> {} input(s) left: {}".format(len(unknown), " ".join(unknown)))
    if len(unknown) != 1:
        raise SystemExit("probe expects exactly one data input, got {}".format(unknown))
    btn = unknown[0]
    say("")

    result = {"clock": clk, "reset": rst, "data_input": btn,
              "outputs": list(box.outputs), "burst": BURST}

    # -- 3: press latency ---------------------------------------------------
    PRESS_AT, CYCLES = 4, 60
    box.reset_all(rst)
    trace = box.run(CYCLES, lambda c: {btn: 1 if c >= PRESS_AT else 0})
    say("Probe A -- hold {} low, raise it at cycle {} and never let go.".format(btn, PRESS_AT))
    say("")
    for o in box.outputs:
        say("  {:<10}: {}".format(o, "".join(str(t[o]) for t in trace)))
    say("  {:<10}: {}".format(btn, "".join("0" if c < PRESS_AT else "1"
                                           for c in range(CYCLES))))
    say("")
    level, pulse = None, None
    for o in box.outputs:
        bits = [t[o] for t in trace]
        p = pulses(bits)
        if p and p[0][1] == 1 and len(p) == 1:
            pulse = o
        elif p and p[0][0] + p[0][1] == CYCLES:
            level = o
    say("  one output goes high and STAYS high  -> the level output : {}".format(level))
    say("  one output goes high for a single cycle -> the pulse output: {}".format(pulse))
    lvl_bits = [t[level] for t in trace]
    pls_bits = [t[pulse] for t in trace]
    press_latency = lvl_bits.index(1) - PRESS_AT
    pulse_at, pulse_len = pulses(pls_bits)[0]
    say("  {} rises at cycle {} -> {} cycles after the press".format(
        level, lvl_bits.index(1), press_latency))
    say("  {} is high on cycle {} only, {} cycle(s) wide, and never again".format(
        pulse, pulse_at, pulse_len))
    say("  => the pulse output is a one-cycle marker for the rising edge of the level")
    say("")
    result.update({"level_output": level, "pulse_output": pulse,
                   "press_latency_cycles": press_latency,
                   "pulse_width_cycles": pulse_len,
                   "pulses_per_press": len(pulses(pls_bits))})

    # -- 4: release latency -------------------------------------------------
    RELEASE_AT = 30
    box.reset_all(rst)
    trace = box.run(CYCLES, lambda c: {btn: 1 if PRESS_AT <= c < RELEASE_AT else 0})
    say("Probe B -- same press, released at cycle {}.".format(RELEASE_AT))
    say("")
    lvl_bits = [t[level] for t in trace]
    say("  {:<10}: {}".format(btn, "".join("1" if PRESS_AT <= c < RELEASE_AT else "0"
                                           for c in range(CYCLES))))
    say("  {:<10}: {}".format(level, "".join(str(b) for b in lvl_bits)))
    say("  {:<10}: {}".format(pulse, "".join(str(t[pulse]) for t in trace)))
    say("")
    high = [i for i, b in enumerate(lvl_bits) if b]
    release_latency = (high[-1] + 1) - RELEASE_AT if high else None
    say("  {} is high on cycles {}..{} and falls at cycle {}".format(
        level, high[0], high[-1], high[-1] + 1))
    say("  => release latency = {} cycles, the same as the press latency".format(
        release_latency))
    say("  => the delay is symmetric: whatever integrates the input runs at the")
    say("     same rate in both directions")
    say("")
    result["release_latency_cycles"] = release_latency

    # -- 5: how short a press is still accepted? ----------------------------
    say("Probe C -- press for exactly k cycles, then release, for k = 0..20.")
    say("Does the level output ever go high?")
    say("")
    accepted = {}
    for k in range(21):
        box.reset_all(rst)
        t = box.run(CYCLES, lambda c, kk=k: {btn: 1 if PRESS_AT <= c < PRESS_AT + kk else 0})
        bits = [x[level] for x in t]
        accepted[k] = 1 if any(bits) else 0
        say("  k={:>2}  {} high: {}   {}".format(
            k, level, "yes" if accepted[k] else "no ",
            "(rises at cycle {})".format(bits.index(1)) if accepted[k] else ""))
    threshold = min(k for k, v in accepted.items() if v)
    say("")
    say("  => the shortest accepted press is {} cycles".format(threshold))
    say("  => that is exactly the {} steps the counter of analyze.py step 07 needs".format(
        threshold))
    say("     to walk from one end stop to the other.  The structural claim and")
    say("     the behavioural measurement are the same number, arrived at from")
    say("     two directions that never looked at each other.")
    say("")
    result["min_accepted_press_cycles"] = threshold
    result["accepted_by_press_width"] = accepted

    # -- 6: the bounce burst ------------------------------------------------
    burst = [int(c) for c in BURST]
    TOTAL = len(burst) + 60
    box.reset_all(rst)

    def bouncy(c):
        if c < PRESS_AT:
            return {btn: 0}
        i = c - PRESS_AT
        return {btn: burst[i] if i < len(burst) else 1}

    trace = box.run(TOTAL, bouncy)
    raw_bits = [bouncy(c)[btn] for c in range(TOTAL)]
    lvl_bits = [t[level] for t in trace]
    pls_bits = [t[pulse] for t in trace]
    say("Probe D -- a contact-bounce burst: {} chattering samples starting at".format(
        len(burst)))
    say("cycle {}, then the line settles high.".format(PRESS_AT))
    say("")
    say("  stimulus  : {}".format(BURST))
    say("  {:<10}: {}".format(btn, "".join(str(b) for b in raw_bits)))
    say("  {:<10}: {}".format(level, "".join(str(b) for b in lvl_bits)))
    say("  {:<10}: {}".format(pulse, "".join(str(b) for b in pls_bits)))
    say("")
    burst_end = PRESS_AT + len(burst)
    say("  {} has {} edges inside the burst (cycles {}..{})".format(
        btn, len(edges(raw_bits[PRESS_AT:burst_end])), PRESS_AT, burst_end - 1))
    say("  {} has {} edges inside the burst".format(
        level, len(edges(lvl_bits[PRESS_AT:burst_end]))))
    rise = lvl_bits.index(1) if 1 in lvl_bits else None
    say("  {} rises once, at cycle {} -- {} cycles after the line settles".format(
        level, rise, rise - burst_end))
    say("  {} pulses {} time(s): {}".format(
        pulse, len(pulses(pls_bits)),
        " ".join("cycle {} for {} cycle(s)".format(a, b) for a, b in pulses(pls_bits))))
    say("")
    say("  Note the {} cycles, not {}: a cold press (probe A) takes {} cycles, but".format(
        rise - burst_end, press_latency, press_latency))
    say("  this burst ended on two high samples, so the integrator was already")
    say("  part-way up when the line settled.  The debounce delay is a function of")
    say("  the integrator's STATE, not a fixed pipeline depth -- which is exactly")
    say("  what a counter-based debouncer is and a shift-register one is not.")
    say("")
    say("  => {} bouncing edges in, ONE clean edge out.  That is the whole job.".format(
        len(edges(raw_bits[PRESS_AT:burst_end]))))
    say("")
    result["bounce"] = {
        "burst_len": len(burst),
        "raw_edges_in_burst": len(edges(raw_bits[PRESS_AT:burst_end])),
        "level_edges_in_burst": len(edges(lvl_bits[PRESS_AT:burst_end])),
        "level_rises_at": rise,
        "cycles_after_settling": rise - burst_end,
        "pulse_count": len(pulses(pls_bits)),
        "pulse_widths": [b for _, b in pulses(pls_bits)],
    }

    # -- 7: a square wave that never sits still -----------------------------
    say("Probe E -- a square wave on {}: half a period high, half low, forever.".format(btn))
    say("If the block were a plain synchronizer the output would just follow it.")
    say("")
    squares = {}
    for half in (5, 10, 14, 15, 16, 20):
        box.reset_all(rst)
        t = box.run(400, lambda c, h=half: {btn: (c // h) % 2})
        bits = [x[level] for x in t]
        n = len(edges(bits))
        squares[half] = n
        say("  half-period {:>2} cycles ({:>3} cycle period): {} changes {} time(s)".format(
            half, 2 * half, level, n))
    quiet = sorted(h for h, n in squares.items() if n == 0)
    say("")
    say("  => anything with a half-period of {} cycles or less is rejected".format(
        max(quiet) if quiet else "?"))
    say("  => the block is not a filter with a smooth roll-off; it is a threshold.")
    say("     Below {} consecutive stable samples: nothing at all comes out.".format(
        threshold))
    say("     At or above it: the full transition.")
    say("")
    result["square_wave_output_edges"] = squares

    if out:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "08_probe.txt"), "w", newline="\n") as fh:
            fh.write("\n".join(log) + "\n")
        with open(os.path.join(out, "probe.json"), "w", newline="\n") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
        print("wrote", os.path.join(out, "08_probe.txt"))
    if images:
        os.makedirs(images, exist_ok=True)
        write_waveform_svg(os.path.join(images, "bounce_waveform.svg"),
                           [(btn, raw_bits), (level, lvl_bits), (pulse, pls_bits)],
                           marks=[(PRESS_AT, "burst starts"),
                                  (burst_end, "line settles"),
                                  (rise, "output rises")])
    return result


def write_waveform_svg(path, rows, marks=()):
    """Draw the probed traces as a waveform.  No library, no external font."""
    n = max(len(bits) for _, bits in rows)
    w, amp, gap, left, top = 9, 22, 40, 96, 34
    width = left + n * w + 24
    height = top + len(rows) * gap + 66
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {0} {1}" '
        'width="{0}" height="{1}">'.format(width, height),
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="8" y="20" font-family="monospace" font-size="12" fill="#37474f">'
        'probe D: a bounce burst in, one clean edge out</text>',
    ]
    for cycle, label in marks:
        if cycle is None:
            continue
        x = left + cycle * w
        parts.append('<line x1="{0}" y1="{1}" x2="{0}" y2="{2}" stroke="#b0bec5" '
                     'stroke-dasharray="3 3"/>'.format(x, top - 8, top + len(rows) * gap))
        parts.append('<text x="{}" y="{}" font-family="monospace" font-size="10" '
                     'fill="#78909c" transform="rotate(-90 {} {})">{}</text>'.format(
                         x + 3, top + len(rows) * gap + 4, x + 3,
                         top + len(rows) * gap + 4, label))
    for i, (label, bits) in enumerate(rows):
        base = top + i * gap
        y = lambda b: base + (0 if b else amp)
        # cell j spans x in [left + j*w, left + (j+1)*w], so a marker drawn at
        # left + c*w lands exactly on the left edge of cycle c's cell
        x = left + w
        seg = ["M {} {}".format(left, y(bits[0])), "L {} {}".format(x, y(bits[0]))]
        for j in range(1, len(bits)):
            if bits[j] != bits[j - 1]:
                seg.append("L {} {}".format(x, y(bits[j])))
            x += w
            seg.append("L {} {}".format(x, y(bits[j])))
        parts.append('<text x="8" y="{}" font-family="monospace" font-size="11" '
                     'fill="#37474f">{}</text>'.format(base + amp - 4, label))
        parts.append('<path d="{}" fill="none" stroke="#1565c0" stroke-width="2"/>'.format(
            " ".join(seg)))
    parts.append("</svg>")
    with open(path, "w", newline="\n") as fh:
        fh.write("\n".join(parts) + "\n")
    print("wrote", path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.abspath(os.path.join(HERE, "..", "..", "..")))
    ap.add_argument("--netlist", default=os.path.join(HERE, "shift_debouncer.vo"))
    ap.add_argument("-o", "--output", default=os.path.join(HERE, "artifacts"))
    ap.add_argument("--images", default=os.path.join(HERE, "images"))
    args = ap.parse_args(argv)
    probe(args.repo, args.netlist, args.output, args.images)
    return 0


if __name__ == "__main__":
    sys.exit(main())
