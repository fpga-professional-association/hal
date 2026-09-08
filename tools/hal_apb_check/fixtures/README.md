# APB fixtures and their ground truth

Five hand-written gate-level netlists, one bus mapping each. Three are
completers, two are requesters; three are deliberately broken, each in exactly
one way, so a failing check names a real defect rather than a soup of them.

Everything is built from `EXAMPLE_GATE_LIBRARY`
(`plugins/gate_libraries/definitions/example_library.hgl`, the same library
shipped inside `examples/uart.zip`). Only `BUF`, `INV`, `AND2`, `AND3`, `OR2`,
`XOR`, `MUX`, `VCC` and `FFR` are used.

## The one thing to know about `FFR`

`example_library.hgl` defines the flip-flop as

```
next_state = (D & CE)      clocked_on = C      clear_on = R
```

so `Q' = R ? 0 : (D & CE)` — **`CE` low loads a zero, it does not hold**. Every
fixture therefore ties `CE` to a `VCC` cell, and `PRESETn` is inverted into the
active-high `R`. This is a property of the library, not an APB convention, and
it is exactly the sort of thing the checker must read out of the gate type
instead of assuming; `hal_apb_check/netlist.py` does.

## Reset and cycle conventions

Every mapping declares `reset: {signal: PRESETn, active_low: true, cycles: 2}`,
which the engine turns into: `PRESETn` is low in cycles 0 and 1 and high from
cycle 2 on. Registers start **unconstrained** — there is no initial-state
assumption — so the reset is what establishes a known state, and properties are
instantiated from cycle 1 (`engine.CHECK_FROM`), by which point the first reset
edge has settled every register.

One model step is one rising `PCLK` edge; `PCLK` is not a variable.

## The fixtures

| fixture | role | what it does | what it breaks |
| --- | --- | --- | --- |
| `apb_completer_ok.v` | completer | one wait state: `PREADY` in the second ACCESS cycle; `PSLVERR = PREADY & ERR_IN` | nothing |
| `apb_completer_broken_slverr.v` | completer | `PSLVERR = access & ~PREADY & ERR_IN` — the error is flagged *before* the transfer completes | `apb/completer/slverr_requires_ready` |
| `apb_completer_broken_stall.v` | completer | `PREADY = access & w_q & STALL_N`, so the environment can stall forever | `apb/completer/ready_within_bound` |
| `apb_requester_ok.v` | requester | holds `PADDR`/`PWRITE`/`PWDATA` from SETUP through every wait state | nothing |
| `apb_requester_broken_stability.v` | requester | the address hold mux is replaced by a bypass buffer, so `PADDR` tracks `ADDR_IN` | `apb/requester/control_stable_wait` and `apb/requester/control_stable_setup_to_access` |

`ERR_IN`, `STALL_N`, `START`, `ADDR_IN`, `WR_IN` and `WDATA_IN` are ordinary
primary inputs. The checker leaves them free, which is what lets it construct
the stall or the mid-transfer address change on its own.

## Expected verdicts

With the shipped mappings (`bound: 12`, `max_wait_states: 4`, APB4):

### `apb_completer_ok`

| property | outcome | finding status |
| --- | --- | --- |
| `apb/completer/ready_within_bound` | holds | `proven_bounded` |
| `apb/completer/slverr_requires_ready` | holds | `proven_bounded` |

### `apb_completer_broken_slverr`

| property | outcome | finding status |
| --- | --- | --- |
| `apb/completer/ready_within_bound` | holds | `proven_bounded` |
| `apb/completer/slverr_requires_ready` | **violated** | `bounded_counterexample` |

### `apb_completer_broken_stall`

| property | outcome | finding status |
| --- | --- | --- |
| `apb/completer/ready_within_bound` | **violated** | `bounded_counterexample` |
| `apb/completer/slverr_requires_ready` | holds | `proven_bounded` |

### `apb_requester_ok`

All seven APB4 requester obligations hold: `reset_inactive`,
`enable_requires_select`, `setup_to_access`, `control_stable_setup_to_access`,
`access_exit`, `wait_hold`, `control_stable_wait` — each `proven_bounded`.

### `apb_requester_broken_stability`

`control_stable_wait` and `control_stable_setup_to_access` are
`bounded_counterexample`; the other five hold.

**No check on any fixture is vacuous, and no run is overconstrained.** That is
asserted by `test_no_fixture_check_is_vacuous`, because a fixture whose
properties never fire would make the whole suite meaningless.

The same table lives in executable form in `EXPECTED` at the top of
`../test_hal_apb_check.py`; if you change a fixture, change both.

## Reproducing

Without a HAL build (uses the reference transition systems in
`../reference_models.py`, which are proven equivalent to the Verilog by the
container tests):

```bash
python tools/hal_apb_check check \
    tools/hal_apb_check/fixtures/apb_requester_broken_stability.map.json \
    --reference-model requester_broken_stability \
    -o results/apb.json
python tools/hal_apb_check replay results/evidence/apb_requester_control_stable_wait.replay.json \
    --reference-model requester_broken_stability
```

With a HAL build, drop `--reference-model` and the mapping's `design.netlist` is
parsed instead:

```bash
python tools/hal_apb_check check \
    tools/hal_apb_check/fixtures/apb_requester_broken_stability.map.json \
    -o results/apb.json
```

Exit codes: `0` clean, `1` a counterexample was found, `2` the run was not
usable (bad mapping, missing design, overconstrained environment).
