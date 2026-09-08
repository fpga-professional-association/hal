# `fsm_controller` — a hand-written FSM fixture with recorded ground truth

`controller.v` is a gate-level netlist against the shipped
`plugins/gate_libraries/definitions/example_library.hgl`. It exists so that FSM
recovery can be *checked* rather than eyeballed: `ground_truth.json` records the
transition relation that the gates below implement, derived by hand from the
next-state functions and re-derived mechanically in the smoke test.

## What is in it, and why

| register | flip-flops | shape | role in the fixture |
| --- | --- | --- | --- |
| controller | `nx_a1_reg`, `nx_b2_reg` | strongly connected: each next-state function reads both bits | the machine to recover. The names carry no hint — nothing in the netlist says "state" |
| counter | `cnt_r0`, `cnt_r1`, `cnt_r2` | each bit depends on itself and on the lower bits, but no bit depends on a higher one — **not** a strongly connected component | the decoy. It is a state machine too, so a candidate search must rank it, not hide it |
| pipeline | `dp_x0`, `dp_x1` | no feedback at all | must never be proposed as a state register |

The controller's asynchronous clear is tied to `GND`; the counter's is driven by
`i_rst`. `solve_fsm` models neither — it builds the next-state function from the
gate library's `next_state` expression, which for `FFR` is `(D & CE)` — so the
counter is also the fixture for "an asynchronous reset that the model does not
contain". The first case is an assumption that can be discharged structurally,
the second is one that cannot.

Every flip-flop carries `INIT(1'h0)`, so the initial state can be read out of
the design instead of assumed.

## The controller

```
a = nx_a1_reg (state bit 0)      b = nx_b2_reg (state bit 1)
state value = a + 2*b

a' = !b & (a ? !i_fin : i_go)        (INV, MUX, AND2)
b' = !b &  a &  i_fin                (INV, AND3)
```

| state | name | on | next |
| --- | --- | --- | --- |
| 0 | IDLE | `!i_go` / `i_go` | 0 / 1 |
| 1 | RUN | `!i_fin` / `i_fin` | 1 / 2 |
| 2 | DONE | always | 0 |
| 3 | UNUSED | always | 0 |

State 3 is **not reachable** from the initial state. An SMT run explores forward
from the initial state and therefore reports states 0–2 and five transitions; an
exhaustive (`solver: "brute_force"`) run reports state 3 and its outgoing edge as
well. `ground_truth.json` records the total relation plus `reachable_states`, so
both kinds of run can be compared against the same file —
`hal_fsm compare --reachable-only` restricts the comparison to the reachable part.

## The counter

`c0' = c0 ^ i_tick`, `c1' = c1 ^ (c0 & i_tick)`, `c2' = c2 ^ (c0 & c1 & i_tick)`
— all eight states, `s -> s` on `!i_tick` and `s -> (s+1) mod 8` on `i_tick`,
all reachable from 0.

## State values depend on the bit order

A state value only means something together with the list of flip-flops that
produced it: bit *i* is the output of `state_registers[i]`. That is why the
reference records its own order and why the comparison permutes the recovered
table into it (`hal_fsm.transitions.bit_permutation`). Renaming the state bits
of a design — which is exactly what an obfuscated netlist does — changes the
order any tool discovers them in, and a comparison that ignored that would only
ever pass by luck.

## Using it

```bash
# needs a built HAL
python tools/hal_fsm analyze tests/fixtures/fsm_controller/controller.v \
    --gate-library plugins/gate_libraries/definitions/example_library.hgl \
    --reference tests/fixtures/fsm_controller/ground_truth.json \
    -o build/hal_fsm/controller --print-summary

# no HAL needed: compare a table a run produced against the reference
python tools/hal_fsm compare build/hal_fsm/controller/transitions-machine01.json \
    tests/fixtures/fsm_controller/ground_truth.json --reachable-only
```

`wrong_candidate.json` and `counter_override.json` in this directory are the
configurations the smoke test uses for the negative cases: a state register the
user picked badly, and the counter (whose asynchronous reset is *not* modelled)
solved on purpose.
