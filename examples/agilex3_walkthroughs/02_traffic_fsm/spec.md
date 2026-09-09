# traffic_fsm — intended behaviour

This is the specification the RTL in `design.v` was written against. It is the
answer key for the walkthrough in `guide.html`: everything here should be
recoverable from the synthesized netlist alone, and the guide is honest about
the parts that are not.

## Interface

| port | dir | width | meaning |
| --- | --- | --- | --- |
| `clk` | in | 1 | the only clock; everything is rising-edge |
| `rst_n` | in | 1 | asynchronous, active low; forces the controller to RED with the dwell counter at 0 |
| `hold` | in | 1 | when high, the controller freezes: neither the state nor the dwell counter changes |
| `red` | out | 1 | red lamp |
| `yellow` | out | 1 | yellow lamp |
| `green` | out | 1 | green lamp |

## States

A Moore machine with four phases, cycling in one fixed order:

```
RED  ->  RED+YELLOW  ->  GREEN  ->  YELLOW  ->  RED  ...
```

| state | encoding in `design.v` | red | yellow | green | dwell (clocks) |
| --- | --- | --- | --- | --- | --- |
| `S_RED` | `2'b00` | 1 | 0 | 0 | 10 |
| `S_RED_YELLOW` | `2'b01` | 1 | 1 | 0 | 3 |
| `S_GREEN` | `2'b10` | 0 | 0 | 1 | 13 |
| `S_YELLOW` | `2'b11` | 0 | 1 | 0 | 5 |

The lamp outputs are combinational functions of the state register only (Moore).
No output depends on `hold`.

## Dwell counter

A 4-bit up-counter `tick`:

* `tick` counts `0, 1, 2, …` while the phase runs;
* the phase ends on the cycle where `tick == limit(state)`, with
  `limit = 9 / 2 / 12 / 4` for RED / RED+YELLOW / GREEN / YELLOW;
* on that cycle `tick` restarts at 0 and the state advances;
* while `hold` is high, `tick` keeps its value and the state does not move.

So a phase occupies `limit + 1` clock cycles and the full light cycle is
`10 + 3 + 13 + 5 = 31` clocks.

## Reset

`rst_n` is asynchronous and active low. While it is low, `state = S_RED` and
`tick = 0`. There is no synchronous reset and no synchronous load anywhere in
the design; this is deliberate, see the header comment in `design.v`.

## What a reverse engineer should be able to prove

1. There are exactly 6 flip-flops, all on one clock, all with the same
   asynchronous clear.
2. Two of them form a strongly connected component — the state register.
3. Four of them form a counter (a self-dependency chain, not an SCC).
4. The state register has 4 reachable states and a single deterministic cycle
   through them.
5. Each state drives a distinct lamp pattern, which names the states.

## What is *not* recoverable from the netlist

* the identifiers `S_RED`, `tick`, `limit`, `advance` — synthesis keeps only
  mangled derivatives of the register names;
* the module hierarchy — there is none here, and a flattened design cannot tell
  you whether there once was;
* the *source* encoding, if the synthesizer re-encoded the state register;
* the fact that `limit` was written as a `case` over four named constants
  rather than as one Boolean expression.
