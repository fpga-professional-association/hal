#!/usr/bin/env python3
"""The behavioural half of the 03_uart_tx walkthrough: probe the black box.

Structural analysis (`analyze.py`) tells you the netlist contains a nine-deep
shift chain, two four-bit counters and a flag.  It does not tell you what the
frame on the wire looks like, which port is the start request, or which data
port is bit 0.  For that you drive the thing.

This script simulates the *anonymised* netlist with `tools/hal_agilex`'s
reference simulator -- no HAL build required, no design names -- and recovers,
in order:

  1. the clock port      (the only net on every flip-flop's `clk` pin)
  2. the reset port      (the only net on every flip-flop's `clrn` pin)
  3. the request port    (the one remaining input that makes anything move)
  4. the bit period      (from the length of the output's plateaus)
  5. the frame length    (from the busy-like output, and from the trace)
  6. the frame layout    (start/stop bit values, and where each data port lands)

Nothing here reads `netlist.hal.v`, `design.v`, `spec.md` or the name map.

    python3 probe.py --netlist netlist_anon.hal.v -o artifacts/
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FF = "tennm_ff"


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

    def reset_all(self, clk, rst, values=None):
        self.sim.reset()
        for name in self.inputs:
            self.sim.set_input(name, 0)
        self.sim.set_input(rst, 0)
        self.sim.apply_async_clear()
        self.sim.set_input(rst, 1)
        for name, value in (values or {}).items():
            self.sim.set_input(name, value)

    def run(self, cycles, stimulus=None):
        """Return a list of dicts: the value of every output, per cycle."""
        trace = []
        for cycle in range(cycles):
            for name, value in (stimulus(cycle) if stimulus else {}).items():
                self.sim.set_input(name, value)
            self.sim.settle()
            trace.append({o: self.sim.get_output(o) for o in self.outputs})
            self.sim.clock()
        return trace


def _scalar_ports(netlist, direction):
    return [netlist.bits_of(name)[0] for name in netlist.ports(direction)]


# ---------------------------------------------------------------------------
# structural identification of clock and reset -- the only two things you can
# read off the netlist without simulating anything
# ---------------------------------------------------------------------------


def find_clock_and_reset(netlist):
    def pin_nets(pin):
        nets = {}
        for inst in netlist.instances_of_type(FF):
            bit = inst.single(pin)
            nets.setdefault(getattr(bit, "name", str(bit)), []).append(inst.name)
        return nets

    clk = pin_nets("clk")
    clr = pin_nets("clrn")
    return clk, clr


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


def plateaus(bits):
    """Run-length encode a bit trace."""
    runs, cur, n = [], bits[0], 0
    for b in bits:
        if b == cur:
            n += 1
        else:
            runs.append((cur, n))
            cur, n = b, 1
    runs.append((cur, n))
    return runs


def probe(repo, netlist_path, out):
    simulate, vo_netlist = load_agilex(repo)
    netlist = vo_netlist.parse_file(netlist_path)
    box = Box(simulate, netlist)
    log = []

    def say(*parts):
        line = " ".join(str(p) for p in parts)
        log.append(line)
        print(line)

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
    say("=> ONE clock domain: {} flip-flops, one clock net, one clear net".format(
        len(netlist.instances_of_type(FF))))
    say("")

    unknown = [p for p in box.inputs if p not in (clk, rst)]
    say("{} input(s) left to identify: {}".format(len(unknown), " ".join(unknown)))
    say("")

    # -- 3: which input makes anything happen at all? -----------------------
    CYCLES = 400
    say("Probe A -- hold everything at 0 after reset, then raise exactly one")
    say("input for one cycle, and see whether any output ever changes.")
    say("")
    movers = []
    for candidate in unknown:
        box.reset_all(clk, rst)
        trace = box.run(CYCLES, lambda c, p=candidate: {p: 1 if c == 2 else 0})
        changed = sorted({o for o in box.outputs
                          if len({t[o] for t in trace}) > 1})
        say("  {:<8} -> outputs that moved: {}".format(candidate, changed or "none"))
        if changed:
            movers.append(candidate)
    say("")
    if len(movers) != 1:
        say("!! expected exactly one 'request' input, got: {}".format(movers))
    request = movers[0]
    data_ports = [p for p in unknown if p != request]
    say("=> request/start input = {}".format(request))
    say("=> the other {} inputs are payload: {}".format(len(data_ports), " ".join(data_ports)))
    say("")

    # -- 4 & 5: bit period and frame length ---------------------------------
    box.reset_all(clk, rst)
    trace = box.run(CYCLES, lambda c: {request: 1 if c == 2 else 0})
    say("Probe B -- pulse the request with all payload inputs at 0.")
    for o in box.outputs:
        bits = [t[o] for t in trace]
        runs = plateaus(bits)
        say("  {}: {}".format(o, "".join(str(b) for b in bits[:200])))
        say("     run lengths: {}".format(
            " ".join("{}x{}".format(v, n) for v, n in runs[:12])))
    say("")

    # the output whose activity window is one long pulse is the status flag;
    # the other one carries the frame.
    status, serial = None, None
    for o in box.outputs:
        bits = [t[o] for t in trace]
        runs = [r for r in plateaus(bits) if r[1] > 1]
        if len([r for r in plateaus(bits) if True]) <= 4:
            status = o
        else:
            serial = o
    if status is None or serial is None:
        # fall back: the one with fewer transitions is the flag
        by_edges = sorted(box.outputs,
                          key=lambda o: len(plateaus([t[o] for t in trace])))
        status, serial = by_edges[0], by_edges[-1]
    say("=> status flag output = {} (few transitions, one long pulse)".format(status))
    say("=> serial output      = {} (the frame)".format(serial))
    say("")

    active = [i for i, t in enumerate(trace) if t[status]]
    busy_len = len(active)
    say("status high for {} cycles (cycles {}..{})".format(busy_len, active[0], active[-1]))

    # the all-zero payload frame is: start(0) 0*8 stop(1)  -> one long low run
    sbits = [t[serial] for t in trace]
    runs = plateaus(sbits)
    say("serial run lengths with payload 0: " +
        " ".join("{}x{}".format(v, n) for v, n in runs[:8]))
    low = [n for v, n in runs if v == 0 and n > 1]
    say("=> the low plateau is {} cycles long".format(low[0] if low else "?"))
    say("")

    # -- 6: where does each payload input land? -----------------------------
    say("Probe C -- raise exactly one payload input and see which slot of the")
    say("frame flips.  That gives the bit period, the slot count, the bit order")
    say("and the polarity of the framing bits, all at once.")
    say("")
    base = sbits
    slots = {}
    for p in data_ports:
        box.reset_all(clk, rst)
        t = box.run(CYCLES, lambda c, pp=p: {request: 1 if c == 2 else 0, pp: 1})
        bits = [x[serial] for x in t]
        diff = [i for i, (a, b) in enumerate(zip(base, bits)) if a != b]
        slots[p] = (diff[0], diff[-1], len(diff))
        say("  {:<8} changes the line on cycles {}..{}  ({} cycles)".format(
            p, diff[0], diff[-1], len(diff)))
    say("")

    widths = sorted({v[2] for v in slots.values()})
    starts = sorted(v[0] for v in slots.values())
    period = starts[1] - starts[0] if len(starts) > 1 else widths[0]
    say("=> every payload input owns exactly one contiguous window of {} cycle(s)".format(
        ", ".join(str(w) for w in widths)))
    say("=> consecutive windows are {} cycles apart -> the bit period is {}".format(
        period, period))
    order = [p for p, _ in sorted(slots.items(), key=lambda kv: kv[1][0])]
    say("=> payload order on the wire (first sent first): " + " ".join(order))
    say("")

    first_data = starts[0]
    frame_start = active[0]
    n_before = (first_data - frame_start) // period
    total_slots = busy_len // period
    say("first payload window starts {} cycles after the status flag rises".format(
        first_data - frame_start))
    say("=> {} framing slot(s) before the payload".format(n_before))
    say("=> the frame is {} slot(s) long ({} busy cycles / {} cycles per slot)".format(
        total_slots, busy_len, period))
    tail = busy_len - total_slots * period
    if tail:
        say("!! {} busy cycles is NOT a whole number of slots: {}*{} = {}, so the".format(
            busy_len, total_slots, period, total_slots * period))
        say("!! status flag stays high for {} extra cycle(s) after the last slot.".format(tail))
        say("!! Do not round this away -- it is a real property of the control logic")
        say("!! and any reconstruction has to reproduce it.")

    # sample the middle of every slot for a known payload
    def sample(payload_bits):
        box.reset_all(clk, rst)
        stim = {request: 0}
        t = box.run(CYCLES, lambda c: dict(
            [(request, 1 if c == 2 else 0)] +
            [(p, payload_bits.get(p, 0)) for p in data_ports]))
        b = [x[serial] for x in t]
        return [b[frame_start + s * period + period // 2] for s in range(total_slots)]

    all_zero = sample({})
    all_one = sample({p: 1 for p in data_ports})
    say("")
    say("slot values, payload all 0 : " + " ".join(str(x) for x in all_zero))
    say("slot values, payload all 1 : " + " ".join(str(x) for x in all_one))
    fixed = [i for i in range(total_slots) if all_zero[i] == all_one[i]]
    say("=> slots {} do not depend on the payload -> framing bits, values {}".format(
        fixed, [all_zero[i] for i in fixed]))
    say("=> {} payload slots between them -> {}N1-style frame, LSB first".format(
        total_slots - len(fixed), total_slots - len(fixed)))

    result = {
        "clock": clk,
        "reset": rst,
        "request": request,
        "status_output": status,
        "serial_output": serial,
        "payload_inputs_in_wire_order": order,
        "bit_period_cycles": period,
        "frame_slots": total_slots,
        "busy_cycles": busy_len,
        "framing_slots": {str(i): all_zero[i] for i in fixed},
        "slots_all_zero_payload": all_zero,
        "slots_all_one_payload": all_one,
    }
    if out:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "07_probe.txt"), "w") as fh:
            fh.write("\n".join(log) + "\n")
        with open(os.path.join(out, "probe.json"), "w") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
        # a small SVG of the recovered frame, drawn from the trace itself
        write_frame_svg(os.path.join(out, "07_frame.svg"),
                        [x for x in sbits[frame_start:frame_start + total_slots * period + period]],
                        period, frame_start, total_slots,
                        sample({p: 1 for p in order[::2]}), order)
    return result


def write_frame_svg(path, bits, period, frame_start, total_slots, sampled, order):
    """Draw the recovered frame as a waveform.  No library, no external font."""
    n = len(bits)
    w, h, top, amp = 12, 46, 10, 24
    width, height = n * w + 120, 190
    seg, x = [], 60
    y = lambda b: top + (0 if b else amp)
    seg.append("M {} {}".format(x, y(bits[0])))
    for i in range(1, n):
        if bits[i] != bits[i - 1]:
            seg.append("L {} {}".format(x, y(bits[i])))
        x += w
        seg.append("L {} {}".format(x, y(bits[i])))
    ticks = []
    for s in range(total_slots + 1):
        tx = 60 + s * period * w
        ticks.append('<line x1="{0}" y1="{1}" x2="{0}" y2="{2}" stroke="#b0bec5" '
                     'stroke-dasharray="3 3"/>'.format(tx, top - 6, top + amp + 34))
        if s < total_slots:
            label = "start" if s == 0 else ("stop" if s == total_slots - 1 else "d{}".format(s - 1))
            ticks.append('<text x="{}" y="{}" font-family="monospace" font-size="11" '
                         'fill="#455a64" text-anchor="middle">{}</text>'.format(
                             tx + period * w / 2, top + amp + 24, label))
    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {} {}" width="{}" height="{}">'.format(
            width, height, width, height),
        '<rect width="100%" height="100%" fill="none"/>',
        '<text x="6" y="{}" font-family="monospace" font-size="12" fill="#37474f">line</text>'.format(top + amp / 2 + 4),
        "".join(ticks),
        '<path d="{}" fill="none" stroke="#1565c0" stroke-width="2"/>'.format(" ".join(seg)),
        '<text x="60" y="{}" font-family="monospace" font-size="11" fill="#607d8b">'
        'recovered frame: {} slots x {} clock cycles, sampled mid-slot</text>'.format(
            top + amp + 52, total_slots, period),
        '<text x="60" y="{}" font-family="monospace" font-size="11" fill="#607d8b">'
        'payload order on the wire: {}</text>'.format(top + amp + 70, " ".join(order)),
        "</svg>",
    ]
    with open(path, "w") as fh:
        fh.write("\n".join(svg) + "\n")
    print("wrote", path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.abspath(os.path.join(HERE, "..", "..", "..")))
    ap.add_argument("--netlist", default=os.path.join(HERE, "netlist_anon.hal.v"))
    ap.add_argument("-o", "--output", default=None)
    args = ap.parse_args(argv)
    probe(args.repo, args.netlist, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
