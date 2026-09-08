# `hal_secprop` — can the interface reach the sensitive state?

A netlist cannot tell you what is secret. It can tell you, exactly, whether a
run of at most *k* cycles exists in which the pins an attacker drives change a
register the analyst declared protected while the lock is engaged — and, if one
does, hand back the transaction sequence that does it.

That is what this tool answers. Everything else it refuses to answer out loud.

```bash
# a design that is correct, with the assumptions the claim rests on
python tools/hal_secprop check tools/hal_secprop/fixtures/secreg_ok.policy.json \
    -o build/secprop_ok/findings.json          # exit 0

# the same design with one AND gate missing from the debug write path
python tools/hal_secprop check tools/hal_secprop/fixtures/secreg_faulty.policy.json \
    -o build/secprop_faulty/findings.json      # exit 1

# and the witness, as a transaction sequence, replayed against the netlist
python tools/hal_secprop replay \
    build/secprop_faulty/evidence/secprop_locked_write_blocked_SECRET.replay.json
```

```
   cycle  7  -                        dbg_en=0 paddr=0x0 penable=0 psel=0 pwdata=0x0 pwrite=0
   cycle  8  lock_write               dbg_en=0 paddr=0x0 penable=1 psel=1 pwdata=0x1 pwrite=1
>> cycle  9  LOCKED debug_write       dbg_en=1 paddr=0x2 penable=1 psel=1 pwdata=0x0 pwrite=1
   cycle 10  LOCKED                   dbg_en=0 paddr=0x0 penable=0 psel=0 pwdata=0x0 pwrite=0
```

## The one thing to understand first

**Structural reachability is a lead. It is never the answer.**

The two shipped fixtures differ by exactly one AND gate: in `secreg_faulty` the
second (debug) write path does not consult the lock. Their fan-in cones are
*identical* — same inputs, same depths, same external controls, and the lock is
in both, because it still reaches the register through the other write path. A
graph walk cannot separate a correct design from a broken one here, and the test
suite asserts that it cannot (`test_correct_and_faulty_have_the_same_cone`).

So cone results are emitted as findings with status `heuristic`, the word
*candidate* in the title, and a `confidence` of 0.3. Every verdict in the
document comes from the bounded symbolic check instead. Cones still earn their
place: they say which controls reach a register and through how many register
stages, they give a reviewer a path to follow, and they cost no solver time.

## The policy is the input, and it is the assumption

```json
{
  "schema": "fpgapa.security-policy",
  "schema_version": "1.0.0",
  "name": "secreg_ok",
  "design": {"netlist": "secreg_ok.v", "gate_library": "...NangateOpenCellLibrary.hgl"},
  "clock":  {"signal": "io_00", "edge": "rising"},
  "reset":  {"signal": "io_01", "active_low": true, "cycles": 2},
  "lock":   {"signal": "n013", "locked_value": 1},
  "external_write_controls": {
    "signals":  {"psel": "io_02", "penable": "io_03", "pwrite": "io_04",
                 "dbg_en": "io_05", "paddr": ["io_06", "io_07", "io_08"],
                 "pwdata": ["io_09", "io_10", "io_11", "io_12"]},
    "accesses": [
      {"id": "apb_write",   "condition": {"psel": 1, "penable": 1, "pwrite": 1,
                                          "paddr": [1, 0, 0]}},
      {"id": "debug_write", "condition": {"psel": 1, "penable": 1, "pwrite": 1,
                                          "dbg_en": 1, "paddr": [0, 1, 0]}}
    ]
  },
  "observation_points": [{"name": "PRDATA[0]", "signal": "io_13"}],
  "sensitive_registers": [
    {"name": "SECRET",
     "bits": [{"name": "SECRET[0]", "signal": "n024", "reset_value": 0}],
     "properties": ["locked_write", "reset_clears"],
     "write_accesses": ["apb_write", "debug_write"]}
  ],
  "environment": {"quiescent_inputs": {}},
  "options": {"bound": 10}
}
```

Signals are transition-system signal names, which are net names — the same
strings [`hal_apb_recover`](../hal_apb_recover/README.md) reports when it
recovers a register map, so the two tools compose without a translation step.
Buses are least significant bit first. Nothing is inferred from a name.

Validation is strict on purpose: an unknown key, an unknown option, a signal
that plays two roles, an access whose condition constrains nothing, a
`locked_write` obligation with no declared write path, a reset held for fewer
than two cycles, and a `clock.signal` that is a *list* are all hard errors. That
last one is the MVP boundary — this is a **single-clock** model, and a second
domain needs a different abstraction, not a longer list.

```bash
python tools/hal_secprop validate-policy my.policy.json --check-design
```

### `accesses` are the difference between a proof and a green tick

An access is not a protocol model. It is the analyst saying "this signal
combination is what an attacker asserts when they try to write". It is used for
exactly two things: deciding whether a check was *exercised*, and decoding a
witness back into readable transactions.

That first use is the important one. After proving that no run violates
`locked-write-blocked`, the engine asks a second question — *could a declared
external write to this register even happen while the lock was engaged?* If not,
the pass is reported as `unknown` with `data.vacuous: true`. A policy naming the
wrong pin lands there instead of looking like a proof.

## The property catalogue

| id | means | horizon |
| --- | --- | --- |
| `secprop/locked-write-blocked/<REG>` | while the lock is engaged **and reset is inactive**, no bit of `<REG>` changes | 1 |
| `secprop/reset-clears/<REG>` | while reset is asserted, every bit with a declared `reset_value` reaches it | 1 |
| `secprop/lock-integrity` | once engaged, the lock stays engaged until reset | 1 |

Reset is excluded from the access-control antecedent deliberately: a register
being forced to its reset value is not a break-in, and folding the two together
would report every reset as one. Bits with no declared `reset_value` are not
checked and produce a `secprop/coverage/reset-value-unknown/<REG>` finding — a
missing reset value is never assumed to be zero.

`lock-integrity` is not decoration. A locked-write proof is worth little on an
interface that can simply clear the lock.

```bash
python tools/hal_secprop properties my.policy.json
python tools/hal_secprop exclusions
```

### Deliberately out of scope

Emitted as `unsupported` findings on **every** run, so a reader never has to
infer them from silence: read-side confidentiality and information flow (a
two-execution property; it needs a self-composed model, not this one), anything
multi-clock or clock-domain-related (use [`hal_cdc`](../hal_cdc/README.md)),
timing/power/glitch side channels, and the standing fact that the policy itself
is trusted input.

## What a result may claim

Every result is a [`hal_findings`](../hal_findings/README.md) document (schema
1.0.0) and the status vocabulary is used strictly:

| what happened | status |
| --- | --- |
| no run of ≤ *k* cycles violates it, and it *was* exercised | `proven_bounded` |
| a run of ≤ *k* cycles violates it, and the witness replays | `bounded_counterexample` |
| nothing within *k* cycles exercises it | `unknown` (vacuous) |
| the bound is smaller than the obligation's lookahead | `unknown` |
| the design does not contain the signals the policy names | `unsupported` |
| the design uses a primitive the model does not cover | `unsupported` |
| the search hit its decision/conflict/time budget | `timeout` |
| structural cone, i.e. a candidate path | `heuristic` |

`proven_under_assumptions` never appears. This is bounded model checking, not
induction: there is no way to earn an unbounded claim here, and the schema would
let the code pretend otherwise, so it simply never does.

Exit codes, usable in CI as-is:

| code | meaning |
| --- | --- |
| 0 | every obligation held up to the bound, or was reported as a non-failure |
| 1 | a policy violation with a witness that replays |
| 2 | the run is not usable: bad policy, missing design, overconstrained environment, internal error, or `--strict` with anything inconclusive |

## Evidence

Alongside the findings document `check` writes an `evidence/` directory:

* `<obligation>.smt2` — the exact satisfiability query behind the verdict, so it
  can be re-decided independently (`z3 evidence/foo.smt2`); the coverage query
  behind a `proven_bounded` is exported too, because "was this exercised?" is
  part of the claim;
* `<obligation>.replay.json` — the witness bundle;
* `<obligation>.transactions.txt` — the same witness as a transaction sequence;
* `<obligation>.vcd` — the same witness as a waveform, one step per clock edge.

Evidence paths are stored relative to the findings document, so a results
directory can be copied or uploaded as one unit.

### Witness replay

```bash
python tools/hal_secprop replay evidence/secprop_locked_write_blocked_SECRET.replay.json
```

The bundle carries the whole policy, the reset schedule, the environment
assumptions in force, the initial register state, one value per input per cycle,
the decoded transaction sequence, and the resolved path and **content hash** of
the netlist. Replay re-simulates from those inputs and then checks four things:

1. the trace reproduces bit-for-bit;
2. the obligation really does fail at the recorded cycle;
3. every environment assumption still holds on the replayed stimulus — a witness
   produced under an assumption is worthless if the stimulus quietly breaks it,
   because then the bug is the testbench;
4. the obligation's **exercise condition** holds at the failing cycle, so a
   "violation" that involves no external write at all is refused rather than
   reported as an interface attack.

A netlist that no longer hashes to what the bundle recorded is refused outright
(`--allow-changed-design` overrides). And `check` replays every witness *before*
writing it into the report, so a witness that cannot be reproduced is never
published.

## Layout

```
policy.py       the policy document and its validation          (no HAL)
properties.py   the obligation catalogue and the exclusions     (no HAL)
context.py      one signal reader, symbolic or from a trace     (no HAL)
cones.py        structural fan-in, as candidate reachability    (no HAL)
engine.py       violation / coverage / overconstraint queries   (no HAL)
witness.py      bundles, transaction decoding, replay, VCD      (no HAL)
findings.py     hal_findings documents                          (no HAL)
transitions.py  structural Verilog -> TransitionSystem          (no HAL)
halsource.py    hal_py -> TransitionSystem                      (needs HAL)
cli.py          check / replay / cones / properties / validate
fixtures/       the correct, the faulty and the latch fixture
scripts/        check_in_hal.py, for `hal --python-script`
```

Nothing under `tools/hal_secprop/` duplicates a sibling's work:

* **`hal_apb_check`** supplies the Boolean term algebra (`expr`), the vendored
  CDCL solver (`sat`), the `TransitionSystem` and its unrolling (`system`), the
  model-to-trace conversion and the VCD writer (`witness`), and the entire
  `hal_py` netlist front end (`netlist`). `halsource.py` is a thin adapter over
  the last of these; its only real job is turning HAL's `UnsupportedPrimitives`
  into ours so a latch produces the same report on both paths.
* **`hal_apb_recover`** supplies the `.hgl` reader and the structural Verilog
  reader, which is what makes the offline path — and therefore the offline test
  suite — possible at all. `transitions.py` lifts its `Circuit` into a
  `TransitionSystem` symbolically, re-reading the gate functions from the same
  library expressions, because a three-valued evaluator is right for register
  recovery and useless for a SAT query.
* **`hal_findings`** is a hard dependency, not an optional formatter.

The two front ends are required to agree: `test_hal_secprop_hal.py` and
`tests/headless_smoke/secprop_smoke.py` both build the model twice and assert
the transition relations equivalent with a **miter** — for every register, the
two next-state functions are asserted to differ and the solver must answer
`unsat` — rather than comparing a handful of traces.

## Fixtures

See [`fixtures/README.md`](fixtures/README.md). Three variants, all generated
from one description by `fixtures/generate.py` against the shipped
`NangateOpenCellLibrary`, with `ground_truth.json` written alongside:

* `secreg_ok` — both write paths consult the lock;
* `secreg_faulty` — the debug path does not, *and* `SECRET[3]` comes out of
  reset as 1 while the policy declares 0. Two different defect classes, one
  netlist;
* `secreg_blackbox` — `secreg_ok` with PREADY behind a transparent latch, so
  the "unsupported primitive" path has something real to report.

## Tests

Offline, no HAL build, no third-party packages (52 tests, about one second):

```bash
python -m unittest discover -s tools/hal_secprop -t tools -p "test_hal_secprop.py"
```

They cover every way a policy can lie, the offline front end (including its
refusal of a latch and of a gated clock), the cone-identity claim above, all
four result classes the issue asks for — correct, violating, unsupported,
timeout — plus vacuity and overconstraint, the witness and its replay
(including a bundle that breaks its own assumptions and one whose trace has been
edited), and the findings documents against the shipped schema.

With a built HAL — this is what proves the Verilog fixtures mean the same thing
to HAL as to the offline reader:

```bash
HAL_BASE_PATH=/opt/hal-build HAL_PY_PATH=/opt/hal-build/lib \
  PYTHONPATH=/opt/hal-build/lib \
  python -m unittest discover -s tools/hal_secprop -t tools -p "test_*_hal.py"
```

End to end, including a run driven by the `hal` binary itself:

```bash
HAL_BASE_PATH=/opt/hal-build PYTHONPATH=/opt/hal-build/lib \
  python3 tests/headless_smoke/secprop_smoke.py \
      --hal-binary /opt/hal-build/bin/hal \
      --work-dir /opt/hal-build/secprop_smoke --keep
```

## Running it inside HAL

So that the verdict becomes HAL's exit code:

```bash
export HAL_SECPROP_TOOLS=$PWD/tools
export HAL_SECPROP_POLICY=$PWD/tools/hal_secprop/fixtures/secreg_faulty.policy.json
export HAL_SECPROP_LIBRARY=$PWD/plugins/gate_libraries/definitions/NangateOpenCellLibrary.hgl
export HAL_SECPROP_OUTPUT=/tmp/secprop/findings.json
export HAL_SECPROP_REPLAY=1
"$HAL_BUILD/bin/hal" --python-script tools/hal_secprop/scripts/check_in_hal.py
```

`scripts/check_in_hal.py` takes its configuration from the environment because
`--python-args` is split on spaces and cannot carry a path containing one. It
exits 1 when a policy is violated and 2 when a witness fails to replay.

## Integration notes

* **`hal_findings`** — `findings.py` emits documents against the shared schema.
  Validate with `python tools/hal_findings validate <file>`.
* **`hal_viz`** — `python tools/hal_viz report` renders these documents as
  static HTML with no extra work; a witness VCD pairs naturally with a scoped
  netlist graph of the same design, and both land in the same results directory.
* **`hal_runner`** — `check` is a well-behaved manifest step: deterministic,
  bounded by explicit budgets, and with meaningful exit codes. A netlist-backed
  run with `--source hal` needs `HAL_BASE_PATH` and `PYTHONPATH` pointing at a
  build tree and `plugin_manager.load_all_plugins()` to succeed, because the
  Verilog parser is a plugin. `--source offline` needs neither.
* **`hal_apb_recover`** — the natural upstream: recover the register map from a
  stripped netlist, then name the recovered storage bits in a policy here. The
  signal names line up because both tools speak net names.
* **CI** — `tests/headless_smoke/secprop_smoke.py` is written to be run as its
  own CI step with `--work-dir`, like `fault_campaign_smoke.py`, so the whole
  work directory can be uploaded when it fails. The offline unit suite is fast
  enough to register with ctest; see *Limits* below.
* **This directory owns no shared files.** Nothing outside
  `tools/hal_secprop/` and `tests/headless_smoke/secprop_smoke.py` was added or
  edited.

## Limits worth repeating

* **Bounded.** `proven_bounded` means "no violating run of at most *k* cycles".
  It says nothing about cycle *k*+1. k-induction would be the way to turn this
  into a real unbounded claim, and it is not implemented.
* **Single clock, cycle-accurate.** One model step is one active edge. Gated,
  divided and derived clocks are refused by the front end rather than
  approximated. Asynchronous set/clear are sampled in the current cycle and
  applied at the edge, which is a synchronous over-approximation.
* **The policy is trusted.** A wrong policy produces a confidently wrong answer.
  That is why it is hashed into the document as an artifact of its own, and why
  the vacuity check exists.
* **One trace at a time.** Confidentiality — whether a secret can be *observed*
  — is not expressible here. Observation points are recorded in every witness so
  a trace can be inspected, and nothing is claimed about leakage.
* **`QN` is refused.** A flip-flop with its inverted output connected is
  rejected by the offline front end rather than modelled, because HAL's front
  end would treat that net as a free input and the two models must not diverge.
* This tool measures a netlist against a policy. A clean report is evidence
  about a design under stated assumptions, not a certificate for it.
