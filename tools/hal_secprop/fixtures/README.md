# `secreg` — a locked register, done right and done wrong

Three gate-level fixtures, one generator, one ground truth. Everything here is
produced by [`generate.py`](generate.py) from a single description, so the
netlists, the policies and the expected verdicts cannot drift apart:

```bash
python tools/hal_secprop/fixtures/generate.py
```

Every cell comes from `NangateOpenCellLibrary`
(`plugins/gate_libraries/definitions/NangateOpenCellLibrary.hgl`), which HAL
reads with the stock Verilog parser and `hal_apb_recover`'s offline reader reads
with the same primitive semantics. **Names are stripped**: ports are `io_NN`,
nets `nNNN`, instances `gNNN`. Nothing in the netlist says which flip-flop holds
the secret — that is what the policy file is for, and it is exactly the sort of
list `hal_apb_recover` produces when it recovers a register map.

## The design

A tiny APB-style peripheral, 3 address bits and 4 data bits:

| slot | register | behaviour |
| --- | --- | --- |
| 0 | `LOCK` | one bit, set by writing 1, cleared **only** by reset |
| 1 | `SECRET` | four bits, written through the normal bus write path |
| 2 | *(debug)* | a **second** write path into `SECRET`, gated by the `dbg_en` pin |

`PRDATA` exposes `LOCK` at slot 0 and `SECRET` at slot 1. `PREADY` is a
registered `PSEL & PENABLE`.

```
                       ┌────────── we_main = write & slot1 & !lock
   bus ── decode ──────┤                                              ┌──────┐
                       └────────── we_dbg  = write & slot2 & dbg_en   │SECRET│
                                              & !lock   ← ok only     └──────┘
```

## `secreg_ok`

Both write paths consult the lock. Every obligation the policy asks for holds up
to the bound, and each one is *exercised* inside it — a locked write really is
attemptable, so the pass is not vacuous.

## `secreg_faulty`

The same design with two deliberate defects, which are two different classes of
bug on purpose:

1. **The debug write path does not consult the lock** — one `AND2_X1` fewer than
   `secreg_ok`. This is what the bug looks like in a real design when a second
   write port is added late. It violates
   `secprop/locked-write-blocked/SECRET`, and the witness is the transaction
   sequence *engage the lock, then write through slot 2 with `dbg_en` high*.
2. **`SECRET[3]` comes out of reset as 1** (a `DFFS_X1` instead of a `DFFR_X1`)
   while the policy declares a reset value of 0. This violates
   `secprop/reset-clears/SECRET` at the first checked cycle, and it is why the
   fixture has a reset obligation at all.

`secprop/lock-integrity` still holds here: the lock itself is not the defect,
and a fixture where everything fails would not distinguish the checks.

### Why this pair is the point of the whole tool

The two designs have **identical structural cones**. The lock still reaches
`SECRET` through `we_main`, so the fan-in of the register — inputs, depths,
external controls, everything — is the same in both. A reachability walk cannot
tell them apart; only the bounded symbolic check can. That claim is asserted by
`test_correct_and_faulty_have_the_same_cone` and again by
`tests/headless_smoke/secprop_smoke.py`, and it is why cone results are reported
as `heuristic` candidate paths and never as a verdict.

## `secreg_blackbox`

`secreg_ok` with `PREADY` driven by a `DLH_X1` transparent latch. Neither front
end models a latch, so the run must report `unsupported` for every obligation
and name `DLH_X1` — an incomplete result stated as such, rather than a report
that reads like a clean one.

## Ground truth

[`ground_truth.json`](ground_truth.json) records, per design: the deliberate
defects, the expected exit code, the expected outcome and finding status of every
obligation, the failing bits and the earliest cycle at which a violation is
*possible* (which cycle ≥ that the solver actually picks is its own business and
is not part of the truth), the expected cone shape, and the structural claim
above. It also records the internal net names the policies refer to, so a
regenerated fixture whose numbering shifted is caught rather than silently
re-blessed.

Both `test_hal_secprop.py` and `tests/headless_smoke/secprop_smoke.py` read this
file; neither hard-codes a verdict.
