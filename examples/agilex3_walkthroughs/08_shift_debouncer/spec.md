# `shift_debouncer` — intended behaviour

This is the specification the RTL in `design.v` was written from. It is the
*answer key* for the reverse-engineering walkthrough in `guide.html`: read it
before you write the RTL, or after you have finished the walk — not in the
middle.

## Interface

| port | dir | width | meaning |
| --- | --- | --- | --- |
| `clk` | in | 1 | the only clock in the design |
| `rst_n` | in | 1 | asynchronous, active-low reset |
| `btn_raw` | in | 1 | raw button pin — **asynchronous** to `clk`, and bouncy |
| `btn_state` | out | 1 | debounced button level, synchronous to `clk` |
| `btn_rise` | out | 1 | one `clk` pulse per clean 0→1 transition of `btn_state` |

## Three textbook structures, stacked

```
btn_raw --> [ sync[0] ] --> [ sync[1] ] --> [ 4-bit saturating   ] --> [ state ] --> btn_state
            two-flop synchronizer            up/down counter          hysteresis
                                             clamps at 0 and 15         |
                                                                        +--> [ state_d ] --+
                                                                        |                  |
                                                                        +--> AND !----------+--> btn_rise
```

1. **Two-flop synchronizer.** `btn_raw` is asynchronous, so it is sampled twice
   before anything looks at it. `sync[0]` is the metastability catcher;
   `sync[1]` is the flop whose output is safe to use as a logic signal.
2. **Saturating up/down counter.** 4 bits. It counts **up** while `sync[1]` is
   high and **down** while it is low, and **clamps** at `0` and `15` instead of
   wrapping. The clamp is what makes this a debouncer rather than a
   free-running counter: bounce averages out, and an end stop is only reached
   after enough *consecutive* stable samples.
3. **Hysteresis (Schmitt) output.** `state` is **set** when the counter is at
   15, **cleared** when it is at 0, and **holds** at all fourteen values in
   between. That is a Schmitt trigger in the digital domain, and it is why a
   burst of bounce cannot toggle the output.
4. **Rising-edge detector.** `state_d` is `state` delayed one cycle;
   `btn_rise = state & ~state_d`.

## Timing

* **Reset.** `rst_n` low asynchronously clears every flop: `sync = 0`,
  `cnt = 0`, `state = 0`, `state_d = 0`, so `btn_state = 0` and `btn_rise = 0`.
* **Traversal.** The counter needs **15** increments to go from `0` to `15`, and
  15 decrements to come back. So a press has to be held for **15 consecutive
  stable samples** before it is believed.
* **Latency.** From a cold `0`, an ideal press costs
  `2 (synchronizer) + 15 (traversal) + 1 (the state flop) = 18` clock cycles
  before `btn_state` rises. Release costs the same 18. This is not a fixed
  pipeline depth: the delay depends on where the counter *was*, so a press that
  follows a burst of bounce arrives sooner.
* **Rejection.** A press shorter than 15 samples produces **no output change at
  all**. There is no partial response and no smooth roll-off — it is a
  threshold, not a filter.
* **Pulse.** `btn_rise` is exactly one cycle wide, coincident with the cycle in
  which `btn_state` first reads 1.

## Parameters

`DEBOUNCE_MAX` is not a parameter in this design: the traversal length is the
counter's *width*, `2**4 - 1 = 15`, and the two comparators against `4'h0` and
`4'hF` are the ends of that width. After synthesis there is no constant `15`
stored anywhere — recovering "15" means enumerating the counter's transition
function, or measuring it.

## Deliberate design choices that matter for the walkthrough

* **Two two-flop chains, indistinguishable by shape.** `sync[0]→sync[1]` and
  `state→state_d` are the same structure: flop into flop, no enable, no
  feedback, one clock. Nothing in the register graph tells a *synchronizer*
  from an *edge-detector delay*. Only what feeds the chain, and whether
  anything compares the two stages, does.
* **No clock enables anywhere.** Every hold in this design — the counter's
  saturation, the hysteresis bit's hold — lives in the LUT that drives a `D`
  pin, never in the flip-flop's `ena`. Register grouping by control-pin
  signature therefore produces exactly one group of eight and says nothing.
* **Four bits, on purpose.** Wide enough for the saturation to be visible and
  narrow enough that every next-state cone fits in one 6-input ALM, which keeps
  the export inside the primitive coverage `tools/hal_agilex` has validated.
  A real board would use a counter wide enough for milliseconds of bounce; the
  *structure* is what this walkthrough is about.
* **No carry chain.** Because the counter must both increment and decrement and
  saturate, Quartus did not map it onto the ALM carry chain at all — each bit's
  next state is a single 5-input LUT. `hal_agilex recognize` therefore finds
  no adder here, which is the correct answer and not a failure.

## Reference behaviour

`reference.py` is an executable version of this document: a pure-Python model
of the RTL, used by `check.py` and by `tools/hal_agilex behavior` to compare
against the exported netlist simulated with the validated primitive semantics.
