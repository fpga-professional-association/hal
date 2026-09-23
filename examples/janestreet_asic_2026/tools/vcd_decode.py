#!/usr/bin/env python3
"""Decode example_inputs.vcd from the Jane Street ASIC puzzle.

Parses the VCD by hand (it is tiny), samples every signal on each rising edge
of clk, and prints:
  * the header metadata (date / version / comment -- the authors hid hints there)
  * a per-rising-edge table of rst_n, enable, I, O, success
  * the serial input bitstream grouped by enable window, with ASCII/LSB-first
    and MSB-first interpretations
  * the O byte stream decoded as ASCII

Usage:
    python tools/vcd_decode.py [path/to/example_inputs.vcd] [-o artifacts/vcd_decode.txt]
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_VCD = os.path.join(ROOT, "example_inputs.vcd")


def parse_vcd(path):
    """Return (header_lines, id2name, events) where events is [(time, id, value)]."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = [ln.rstrip("\n") for ln in fh]

    id2name = {}
    header = []
    events = []
    time = 0
    in_defs = True

    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        i += 1
        if not s:
            continue
        if in_defs:
            header.append(raw)
            if s.startswith("$var"):
                # $var reg 1 ! clk $end   /   $var wire 8 % O [7:0] $end
                tok = s.split()
                vid = tok[3]
                name = tok[4]
                if len(tok) > 6:
                    name += " " + tok[5]
                id2name[vid] = (name, int(tok[2]))
            if s.startswith("$enddefinitions"):
                in_defs = False
            continue
        if s.startswith("$comment") or s.startswith("$date") or s.startswith("$version"):
            header.append(raw)
            continue
        if s in ("$dumpall", "$dumpvars", "$end", "$dumpon", "$dumpoff"):
            continue
        if s.startswith("#"):
            time = int(s[1:])
            continue
        if s[0] in "bB":
            val, vid = s.split()
            events.append((time, vid, val[1:]))
        elif s[0] in "rR":
            val, vid = s.split()
            events.append((time, vid, val[1:]))
        else:
            events.append((time, s[1:], s[0]))
    return header, id2name, events


def sample_on_posedge(id2name, events):
    """Replay events; snapshot state just before each clk 0->1 transition."""
    clk_id = next(k for k, v in id2name.items() if v[0] == "clk")
    state = {k: "x" for k in id2name}
    samples = []  # (time, {name: value}) captured at posedge with pre-edge data values
    prev_clk = "x"
    # group events by time so a whole timestep is applied together
    by_time = []
    cur_t, cur = None, []
    for t, vid, val in events:
        if t != cur_t:
            if cur_t is not None:
                by_time.append((cur_t, cur))
            cur_t, cur = t, []
        cur.append((vid, val))
    if cur_t is not None:
        by_time.append((cur_t, cur))

    for t, group in by_time:
        pre = dict(state)
        for vid, val in group:
            state[vid] = val
        new_clk = state[clk_id]
        if prev_clk in ("0", "x") and new_clk == "1":
            # value sampled by the DUT = value present just before this edge
            snap = {}
            for vid, (name, _w) in id2name.items():
                snap[name] = pre.get(vid, "x") if name != "clk" else "1"
            # O/success are outputs; report the post-edge value too
            post = {id2name[v][0]: state[v] for v in id2name}
            samples.append((t, snap, post))
        prev_clk = new_clk
    return samples


def bits_to_text(bits, lsb_first):
    out = []
    for k in range(0, len(bits) - 7, 8):
        byte = bits[k:k + 8]
        if lsb_first:
            byte = byte[::-1]
        v = int("".join(byte), 2)
        out.append(chr(v) if 32 <= v < 127 else ".")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vcd", nargs="?", default=DEFAULT_VCD)
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()

    header, id2name, events = parse_vcd(args.vcd)
    samples = sample_on_posedge(id2name, events)

    lines = []
    w = lines.append
    w("=" * 78)
    w("VCD HEADER / HIDDEN TEXT")
    w("=" * 78)
    for h in header:
        w(h)
    w("")
    w("signal ids: " + ", ".join(f"{k!r}->{v[0]}({v[1]}b)" for k, v in id2name.items()))
    w("")

    w("=" * 78)
    w("PER-RISING-EDGE SAMPLE TABLE (values as seen by the DUT at the edge)")
    w("=" * 78)
    w(f"{'#':>5} {'time(ps)':>10} {'rst_n':>5} {'en':>3} {'I':>2} {'O(post)':>10} "
      f"{'O ascii':>7} {'success':>7}")
    for n, (t, pre, post) in enumerate(samples):
        o = post.get("O [7:0]", post.get("O", "x"))
        try:
            ov = int(o, 2)
            oc = chr(ov) if 32 <= ov < 127 else ""
        except ValueError:
            ov, oc = None, ""
        w(f"{n:>5} {t:>10} {pre.get('rst_n','x'):>5} {pre.get('enable','x'):>3} "
          f"{pre.get('I','x'):>2} {o:>10} {oc:>7} {post.get('success','x'):>7}")
    w("")

    w("=" * 78)
    w("INPUT BITSTREAM PER enable WINDOW  (I sampled on posedge while enable==1)")
    w("=" * 78)
    windows = []
    cur = None
    for n, (t, pre, post) in enumerate(samples):
        if pre.get("enable") == "1":
            if cur is None:
                cur = {"start_edge": n, "start_t": t, "bits": []}
            cur["bits"].append(pre.get("I", "x"))
        else:
            if cur is not None:
                cur["end_edge"] = n - 1
                windows.append(cur)
                cur = None
    if cur is not None:
        cur["end_edge"] = len(samples) - 1
        windows.append(cur)

    for wi, win in enumerate(windows):
        b = win["bits"]
        w(f"window {wi}: edges {win['start_edge']}..{win['end_edge']} "
          f"(t={win['start_t']} ps), {len(b)} bits")
        w("  bits MSB-shift-order: " + "".join(b))
        w("  hex (first-bit = MSB): " + hex(int("".join(b), 2)) if all(c in "01" for c in b) else "")
        w("  as ascii, first-bit=MSB of each byte: " + repr(bits_to_text(b, False)))
        w("  as ascii, first-bit=LSB of each byte: " + repr(bits_to_text(b, True)))
        # runs
        runs = []
        last, cnt = None, 0
        for c in b:
            if c == last:
                cnt += 1
            else:
                if last is not None:
                    runs.append((last, cnt))
                last, cnt = c, 1
        runs.append((last, cnt))
        w("  run-length: " + " ".join(f"{v}x{c}" for v, c in runs))
        w("")

    w("=" * 78)
    w("FRAMED-CHARACTER DECODE (auto-search over frame length / bit order / offset)")
    w("=" * 78)
    w("The stimulus is a serial character stream: each character occupies a fixed")
    w("frame of N clocks, of which the first 8 carry the byte and the rest are idle.")
    w("")
    hits = []
    for wi, win in enumerate(windows):
        b = "".join(win["bits"])
        if any(c not in "01" for c in b):
            continue
        for frame in range(8, 17):
            for off in range(0, min(frame, 4)):
                body = b[off:]
                nch = len(body) // frame
                if nch < 3:
                    continue
                for lsb in (True, False):
                    for pos in ("head", "tail"):
                        chars, ok = [], True
                        for k in range(nch):
                            f = body[k * frame:(k + 1) * frame]
                            byte = f[:8] if pos == "head" else f[-8:]
                            pad = f[8:] if pos == "head" else f[:-8]
                            if "1" in pad:
                                ok = False
                                break
                            if lsb:
                                byte = byte[::-1]
                            v = int(byte, 2)
                            if not (32 <= v < 127):
                                ok = False
                                break
                            chars.append(chr(v))
                        if ok and chars:
                            hits.append((wi, frame, off, lsb, pos, "".join(chars)))
    if hits:
        for wi, frame, off, lsb, pos, s in hits:
            w(f"  window {wi}: frame={frame} clk/char, offset={off}, "
              f"bit order={'LSB-first' if lsb else 'MSB-first'}, byte in frame {pos}, "
              f"pad all-zero -> {s!r}")
        best = {}
        for wi, frame, off, lsb, pos, s in hits:
            best.setdefault((frame, off, lsb, pos), {})[wi] = s
        w("")
        for key, d in best.items():
            if len(d) == len(windows):
                frame, off, lsb, pos = key
                w(f"  CONSISTENT ACROSS ALL WINDOWS (frame={frame}, off={off}, "
                  f"{'LSB' if lsb else 'MSB'}-first, byte={pos}):")
                w("    full message: " + repr("".join(d[i] for i in sorted(d))))
    else:
        w("  (no clean framing found)")
    w("")

    w("=" * 78)
    w("OUTPUT O BYTE STREAM (changes only)")
    w("=" * 78)
    last = None
    msg = []
    for t, pre, post in samples:
        o = post.get("O [7:0]", post.get("O", "x"))
        if o != last:
            try:
                ov = int(o, 2)
                ch = chr(ov) if 32 <= ov < 127 else f"<{ov}>"
            except ValueError:
                ch = "<x>"
            w(f"  t={t:>9} ps  O=0b{o:0>8}  = {ch}")
            msg.append(ch)
            last = o
    w("")
    w("  concatenated: " + "".join(msg))
    w("")
    w("  success never asserted" if all(s[2].get("success", "0") != "1" for s in samples)
      else "  success ASSERTED at some point")

    text = "\n".join(lines)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text + "\n")


if __name__ == "__main__":
    main()
