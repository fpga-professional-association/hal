# hal_apb_check — bounded APB protocol checks with replayable counterexamples

Automatic bus recognition is hard. Checking a bus you have already identified is
not, and it is what actually catches wait-state bugs. `hal_apb_check` takes that
trade deliberately: you write a small JSON file saying which net is `PSEL`, which
is `PREADY`, and which APB revision and role you mean; the tool then does bounded
model checking of the AMBA APB protocol properties on the design's real
transition relation, and every counterexample it reports comes with a stimulus
that reproduces it.

Nothing here guesses. There is no name heuristic and no port-order inference, so
a wrong mapping produces a confidently wrong answer — which is why the mapping
file is hashed into the findings document as an artifact of its own, alongside
the netlist.

## What it will and will not claim

Every result is a `hal_findings` document (`tools/hal_findings`, schema 1.0.0),
and the status vocabulary is used strictly:

| what happened | status |
| --- | --- |
| no run of ≤ *k* cycles violates the property | `proven_bounded` |
| a run of ≤ *k* cycles violates it, and the witness replays | `bounded_counterexample` |
| nothing within *k* cycles even *activates* the property | `unknown` (vacuous) |
| the bound is smaller than the property's lookahead | `unknown` |
| the mapping does not provide the signals | `unsupported` |
| the search hit its decision/conflict/time budget | `timeout` |
| something in the analysis broke | `error` |

`proven_under_assumptions` never appears. This is bounded model checking, not
induction: it has no way to earn an unbounded claim, and the schema would let it
pretend otherwise, so the code simply never emits it.

Two failure modes get first-class treatment because they are the ones that make
a green report worthless:

* **Vacuity.** After proving no violation exists, the engine asks a second
  question — *could the antecedent ever have been true?* If not, the pass is
  reported as `unknown` with `data.vacuous: true`. A mapping typo that ties a
  signal to a constant lands here instead of looking like a proof.
* **Overconstraint.** Before anything else, the engine checks that the
  environment assumptions are satisfiable at all, and that they still admit an
  ACCESS phase. If not, a dedicated `apb/diagnostics/overconstrained` finding is
  emitted, every obligation is downgraded, and the CLI exits `2`.

## Obligations vs assumptions

A requester property is an obligation when the DUT is the requester and an
*assumption about the environment* when the DUT is the completer. Getting that
backwards is how a design gets "proven" correct by constraining its bug away, so
the direction is data in `spec.py`, not a comment, and every finding lists the
assumptions that were in force with `kind: "environment"`.

One rule is worth calling out: `apb/completer/ready_within_bound` is marked
**not assumable**. It is our own bounded liveness budget, not an APB
requirement, and assuming it while checking a requester would erase exactly the
long wait states a stability bug hides in.

## Property catalogue (APB2 / APB3 / APB4)

```bash
python tools/hal_apb_check properties --revision APB4 --role completer
python tools/hal_apb_check exclusions
```

Requester obligations: `reset_inactive`, `enable_requires_select`,
`setup_to_access`, `control_stable_setup_to_access`, `access_exit` (APB3+),
`access_exit_single_cycle` (APB2 only), `wait_hold` (APB3+),
`control_stable_wait` (APB3+).

Completer obligations: `ready_within_bound` (APB3+, bounded liveness against
`max_wait_states`), `slverr_requires_ready` (APB3+), and the optional house rule
`ready_low_when_deselected`.

`PREADY`/`PSLVERR` do not exist before APB3 and `PSTRB`/`PPROT` do not exist
before APB4, so a mapping that names them on an older revision is rejected
rather than checked.

### Deliberately out of scope

Reported as `unsupported` findings on every run, so a reader never has to infer
them: APB5 signalling (`PWAKEUP`, parity, RME, user signals), AXI4-Lite,
automatic bus discovery, multi-completer address decoding, `PRDATA` payload
correctness, `PSTRB`/`PPROT` *semantics* (only their stability is checked), and
everything sub-cycle — clock trees, gated or divided clocks, multi-clock
crossings, setup/hold, glitches, X-propagation.

## The bus mapping file

```json
{
  "mapping_version": "1.0",
  "name": "uart_regs",
  "protocol": {"family": "APB", "revision": "APB4", "dut_role": "completer"},
  "design":   {"netlist": "regs.v", "gate_library": "example_library.hgl"},
  "clock":    {"signal": "PCLK", "edge": "rising"},
  "reset":    {"signal": "PRESETn", "active_low": true, "cycles": 2},
  "signals":  {
    "PRESETn": "PRESETn",
    "PSEL": "psel", "PENABLE": "penable", "PWRITE": "pwrite",
    "PADDR": ["paddr_0", "paddr_1"],
    "PREADY": "pready", "PSLVERR": "pslverr"
  },
  "options":  {"bound": 12, "max_wait_states": 4}
}
```

Buses are listed **least significant bit first**. `design` names exactly one of
`netlist` (plus `gate_library`), `project` (a HAL project directory), or
`reference_model`; relative paths resolve against the mapping file.
`reset.cycles` must be ≥ 2 so that the state is settled *and* at least one
checked cycle still has reset asserted.

Options: `bound` (unrolling depth, default 12), `max_wait_states` (the liveness
budget, default 8), `check_ready_low_when_deselected`, `decision_limit`,
`conflict_limit`, `timeout_s`. Anything else is rejected — a typo in an option
name must not silently disable a check.

```bash
python tools/hal_apb_check validate-mapping my.map.json --check-design
```

## Running a check

```bash
# needs a built HAL: parses design.netlist through hal_py
python tools/hal_apb_check check my.map.json -o results/apb.json

# no HAL needed: uses one of the shipped reference transition systems
python tools/hal_apb_check check tools/hal_apb_check/fixtures/apb_completer_ok.map.json \
    --reference-model completer_ok -o results/apb.json
```

Exit codes — usable in CI as-is:

| code | meaning |
| --- | --- |
| 0 | every obligation held up to the bound (or was reported as a non-failure) |
| 1 | a bounded counterexample was found |
| 2 | the run was not usable: bad mapping, missing design, overconstrained environment, internal error, or `--strict` with an inconclusive result |

Alongside the findings document the tool writes an `evidence/` directory:

* `<property>.smt2` — the exact satisfiability query behind the verdict, so it
  can be re-decided with an independent solver (`z3 evidence/foo.smt2`);
* `<property>.replay.json` — the counterexample bundle;
* `<property>.vcd` — the same counterexample as a waveform, one step per edge.

Evidence paths are stored relative to the findings document, so a results
directory can be copied or uploaded as one unit.

## Counterexample replay

```bash
python tools/hal_apb_check replay results/evidence/apb_requester_control_stable_wait.replay.json
```

The bundle carries the full bus mapping, the reset sequence, the list of
environment assumptions that were in force, the initial register state and one
value per input per cycle. Replay re-simulates the design from those inputs and
then checks three things:

1. the trace reproduces bit-for-bit;
2. the property really does fail at the recorded cycle;
3. **every environment assumption still holds on the replayed stimulus.**

The third is the one that matters. A counterexample produced under environment
assumptions is worthless if the stimulus quietly violates one of them — then the
"bug" is the testbench. Replay refuses such a bundle, and `check` runs replay on
every counterexample *before* writing it into the report, so a witness that
cannot be reproduced is never published.

## How the checking works

```
mapping.py     the user's bus mapping, validated                (no HAL)
spec.py        the property catalogue: revision, role, horizon  (no HAL)
system.py      transition system + its k-cycle unrolling        (no HAL)
expr.py        Boolean terms, constant folding, SMT-LIB export  (no HAL)
sat.py         Tseitin + CDCL, with explicit budgets            (no HAL)
engine.py      violation / vacuity / overconstraint queries     (no HAL)
witness.py     counterexample bundles, replay, VCD              (no HAL)
findings.py    hal_findings documents                           (no HAL)
netlist.py     hal_py -> TransitionSystem                       (needs HAL)
```

Only `netlist.py` imports `hal_py`. It splits gates by the gate type's own
`sequential` property, requires an `FFComponent` on every sequential gate (a
latch is refused *by name*, never modelled as a flip-flop), builds each
register's next state from the gate type's `next_state` / `async_reset` /
`async_set` functions, and gets every combinational net's function from
`SubgraphNetlistDecorator.get_subgraph_function` over the combinational gates
only. Clock nets are removed from the model; a clock net that also feeds data
logic is a gated or derived clock and is refused, because the one-step-per-edge
abstraction would be wrong for it.

### Why a SAT solver is vendored

The verdicts must be identical on a laptop, in CI and in the container, and
"proven up to k" has to be available even where no solver is installed. An
optional `z3` dependency would make both untrue. `sat.py` is therefore a
self-contained Tseitin encoder plus CDCL — watched literals, first-UIP learning,
non-chronological backjumping, activity-ordered decisions with phase saving,
geometric restarts. (Plain DPLL was tried first: it finds counterexamples fine
and cannot prove anything, because a proof means refuting every assignment of
~100 free input bits.) Budgets raise an explicit exception, so an exhausted
search is reported as `timeout` and can never be mistaken for `unsat`.
`expr.to_smt2` exports every query so a real solver can check the answer
independently.

## Assumptions on every finding

Recorded explicitly rather than left implied:

* `apb/tool/cycle-abstraction` — one step = one active `PCLK` edge;
* `apb/tool/async-reset-sampled` — asynchronous set/clear are sampled in the
  current cycle and applied at the edge (a synchronous over-approximation);
* `apb/tool/mapping-is-trusted` — the signals are whatever the mapping says;
* `apb/tool/no-initial-state` — registers start free, the reset establishes the
  state;
* `apb/env/reset-sequence` — the declared reset schedule;
* one entry per environment assumption actually applied, with its cycle range.

## Tests

Pure Python, no HAL, no third-party packages (58 tests):

```bash
python -m unittest discover -s tools/hal_apb_check -t tools -p "test_hal_apb_check.py"
```

They cover the SAT engine against brute force on random formulas, every way a
mapping can lie, all five fixtures against the ground truth in
`fixtures/README.md`, vacuity and overconstraint on designs built to trigger
them, replay (including a bundle that breaks its own assumptions), and the
findings documents against the shipped schema.

With a built HAL — this is what proves the Verilog fixtures match the reference
models, by an equivalence miter rather than a handful of traces:

```bash
HAL_BASE_PATH=/opt/hal-build PYTHONPATH=/opt/hal-build/lib \
  python -m unittest discover -s tools/hal_apb_check -t tools -p "test_*_hal.py"
```

## Integration notes

* **`tools/hal_findings`** is a hard dependency, not an optional formatter.
  `findings.py` adds `tools/` to `sys.path` when needed, so running from the
  repository root just works.
* **CI**: `check` is safe to run in a workflow — it needs no HAL when pointed at
  a reference model, and its exit codes are meaningful. A netlist-backed run
  needs `HAL_BASE_PATH` and `PYTHONPATH` set to a build tree, and
  `plugin_manager.load_all_plugins()` to succeed, because the Verilog parser is
  a plugin.
* **`tools/hal_viz`**: a counterexample VCD pairs naturally with a scoped netlist
  graph of the same design; both write into the same results directory.
* **Follow-ups**, in order of usefulness: AXI4-Lite (independent channels, no
  shared properties with APB), automatic APB interface discovery to generate the
  mapping, `PSTRB`/`PPROT` legality, k-induction to turn `proven_bounded` into a
  real unbounded proof, and driving the counterexample through
  `netlist_simulator_controller` so the replay runs in HAL's simulator as well
  as in this model.
