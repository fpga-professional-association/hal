# blinky_counter — specification

The design under study in Agilex 3 walkthrough 01. This document states the
*intended* behaviour, written before synthesis. It is the ground truth the
reverse-engineered result (`recovered.v`) is finally checked against — not an
input to the analysis.

## Ports

| port | direction | width | meaning |
| --- | --- | --- | --- |
| `clk` | input | 1 | the only clock; everything is synchronous to its rising edge |
| `rst_n` | input | 1 | asynchronous, active-low reset |
| `led` | output | 1 | blink output |

## Behaviour

There is one register bank, `count`, 24 bits wide.

1. While `rst_n` is low, `count` is 0. The clear is **asynchronous**: it takes
   effect without a clock edge.
2. On every rising edge of `clk` with `rst_n` high, `count <= count + 1`,
   modulo 2^24 (the carry out of bit 23 is discarded — the counter wraps).
3. There is no enable and no load: the counter is free-running.
4. `led = count[23]`, combinationally.

## Consequences worth naming

* `led` toggles every 2^23 clock cycles and has a period of 2^24 cycles. At a
  50 MHz `clk` that is a 0.336 s half-period, ~2.98 Hz.
* The counter's reachable state set is all 2^24 values; it is a maximal-length
  binary counter, not an LFSR and not a modulo-N counter.
* The design has exactly one clock domain and no clock-domain crossings.
* The full state is 24 bits, but only 1 bit is observable at the boundary. A
  black-box observer needs 2^23 cycles just to see one transition, which is
  why the reverse engineering below is structural rather than behavioural.

## What synthesis is expected to do

Stated in advance so that the guide can be honest about which predictions the
netlist confirmed and which it did not:

* 24 `tennm_ff` instances, one per counter bit, all on the same `clk`, all
  with `clrn = rst_n` and `ena` tied to the constant 1.
* A chain of `tennm_lcell_comb` cells in **arithmetic mode**, wired
  `cout -> cin`, computing the increment. For a `+1` the propagate/generate
  masks degenerate: each slice only needs `count[i]` itself.
* No I/O buffers, because the flow stops after `quartus_syn` and never fits
  the design to pins.
* Two constant cells (`gnd`, `vcc`) materialised by the HAL import rewrite,
  not by Quartus.

## Non-goals

The design is deliberately trivial in function. It is the *structure* that the
walkthrough teaches: register grouping, carry chains, feedback loops and the
gap between "I can see 24 flip-flops" and "this is a 24-bit binary counter".
