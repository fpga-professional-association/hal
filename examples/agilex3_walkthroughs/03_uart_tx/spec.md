# `uart_tx` — intended behaviour

This is the specification the RTL in `design.v` was written from. It is the
*answer key* for the reverse-engineering walkthrough in `guide.html`: read it
before you write the RTL, or after you have finished the walk — not in the
middle.

## Interface

| port | dir | width | meaning |
| --- | --- | --- | --- |
| `clk` | in | 1 | the only clock in the design |
| `rst_n` | in | 1 | asynchronous, active-low reset |
| `tx_start` | in | 1 | request to send; sampled only while idle |
| `tx_data` | in | 8 | the byte to send; captured at load time |
| `tx` | out | 1 | serial line |
| `tx_busy` | out | 1 | high while a frame is in flight |

## Frame format — 8N1, LSB first

Ten bit slots, each exactly `CLK_DIV = 2**DIV_W = 16` clock cycles long:

```
slot     0      1  2  3  4  5  6  7  8      9
line   start   d0 d1 d2 d3 d4 d5 d6 d7    stop     idle
value    0     <-------- tx_data -------->   1        1
```

* one start bit, low;
* eight data bits, least significant first;
* one stop bit, high;
* no parity bit; the line idles high.

## Timing

1. **Reset.** `rst_n` low asynchronously clears the design: `busy = 0`,
   both counters zero, the shift register set to all ones, so `tx = 1`.
2. **Idle.** While `busy = 0` the prescaler and the frame counter are held at
   zero. `tx` keeps whatever the shift register last shifted in, which is 1.
3. **Load.** On the first rising edge with `busy = 0` and `tx_start = 1`, the
   shift register is loaded with `{1'b1, tx_data, 1'b0}` and `busy` goes high.
   `tx` becomes the start bit on that same edge.
4. **Shift.** While busy, the 4-bit prescaler increments every clock and wraps
   on its own. On the cycle where it is all ones (`baud_tick`), the shift
   register shifts right by one — feeding a 1 in at the top so the line goes
   idle after the frame — and the frame counter increments.
5. **End.** The frame counter reaches 10 one prescaler period after the stop
   bit began, i.e. after the stop bit has been held for a full slot.
   `busy` then falls on the **next** clock edge; `tx_busy` is therefore high for
   `10 * 16 + 1 = 161` clock cycles per frame. That one-cycle tail is a
   deliberate consequence of keeping every next-state function inside six
   inputs (see below) and is part of the spec, not a bug.
6. `tx_start` is ignored while `busy` is high.

## Parameters

`DIV_W` (default 4) is the prescaler width; the bit period is `2**DIV_W`
clocks. It is a compile-time parameter: there is no divisor register, so
after synthesis the divisor survives only as the *width of the counter* — it
is not a constant stored anywhere in the netlist. Recovering "16" from the
netlist therefore means counting flip-flops, not finding a constant.

## Deliberate design choices that matter for the walkthrough

* **Two 4-bit counters in one clock domain.** The prescaler and the frame
  counter are the same width, so nothing that groups registers by width can
  tell them apart. They differ only in *dataflow*: the prescaler is enabled
  every cycle while busy; the frame counter is enabled only on the prescaler's
  terminal count.
* **Six-input control logic.** Every next-state function was kept to at most
  six inputs so Quartus never reaches for the ALM's fracturable 7-input mode
  or the flip-flop's synchronous-clear pin, both of which are outside the
  validated `tools/hal_agilex` primitive coverage. The qsf also sets
  `ALLOW_SYNCH_CTRL_USAGE OFF` for the same reason.
* **The top shift-register bit is a constant.** `shreg[9]` is loaded with 1
  and shifted in as 1, so it is a constant and synthesis deletes it. The
  export has **nine** shift flip-flops, not ten. That is a real and very
  common reverse-engineering trap and the guide walks it explicitly.

## Reference behaviour

`reference.py` is an executable version of this document: a pure-Python model
of the RTL, used by `check.py` to compare against the exported netlist
simulated with `tools/hal_agilex`.
