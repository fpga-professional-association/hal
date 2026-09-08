# hal_fsm — discover, solve and explain finite state machines

`plugins/solve_fsm` already reconstructs a state transition graph. It just needs
somebody to tell it *which flip-flops are the state register and which gates are
the transition logic* — and it says nothing about what the result means: not
which state the machine starts in, not whether the reset the design has is even
part of the model, not whether the graph is complete, and not how to get to a
particular state.

`hal_fsm` is that missing workflow, built on `solve_fsm` rather than around it:

```bash
export HAL_BASE_PATH=/path/to/hal/build
python tools/hal_fsm analyze tests/fixtures/fsm_controller/controller.v \
    --gate-library plugins/gate_libraries/definitions/example_library.hgl \
    --reference tests/fixtures/fsm_controller/ground_truth.json \
    --targets 2 -o build/hal_fsm/controller --print-summary
```

```
build/hal_fsm/controller/
    findings.json                 every claim, with its status and assumptions
    transitions-machine01.json    the transition table + its state bit order
    state-diagram-machine01.dot   initial state, reachability, witness path
    solve_fsm-<id>.dot            the graph solve_fsm wrote itself, as evidence
    request.json                  exactly what the in-HAL side was asked to do
    logs/stdout.log, stderr.log   HAL's own output, kept whatever happens
```

```
fsm/candidate/001               heuristic  Candidate state register: 2 flip-flop(s) [nx_a1_reg, nx_b2_reg]
fsm/candidate/002               heuristic  Candidate state register: 3 flip-flop(s) [cnt_r0, cnt_r1, cnt_r2]
fsm/machine01/transitions       proven_under_assumptions  3 states, 5 transitions
fsm/machine01/relation-consistency  proven_under_assumptions  deterministic and total
fsm/machine01/reachable-states  proven_under_assumptions  3 state(s)
fsm/machine01/witness/2         proven_under_assumptions  State 10 (2) is reached in 2 cycle(s)
fsm/machine01/reference-comparison  proven_under_assumptions  matches the reference
fsm/coverage/uncovered-sequential-gates  unsupported  gates outside every solved register
```

## The one thing this tool is careful about

A recovered state machine is two claims stacked on top of each other, and they
are not the same strength:

* **which flip-flops are the state register** is a guess from feedback
  structure. It is a `heuristic` finding with a `confidence` and the features
  that produced it, and it stays one however well it scores;
* **what the transitions are** is what `solve_fsm` derived. It is a *separate*
  `proven_under_assumptions` finding — and the candidate is listed as one of its
  assumptions, undischarged. A reader who does not believe the state register
  does not have to believe the transitions, and consumers see that in the
  document instead of having to know it.

Everything else follows from taking that seriously. `hal_findings` is the schema
([`tools/hal_findings`](../hal_findings/README.md)); this tool adds no vocabulary
of its own.

## Candidate proposal

Three independent generators, merged; agreement between them raises the score
slightly:

| source | what it proposes |
| --- | --- |
| `scc` | non-trivial strongly connected components of the flip-flop dependency graph — a controller's bits are mutually dependent |
| `self_loop_cluster` | self-dependent flip-flops grouped by weak connectivity — this is what catches a counter, which is **not** an SCC (`c1` reads `c0`, never the reverse) |
| `dataflow` | DANA's register groups, intersected with the flip-flops that have feedback, with one-bit groups whose next-state functions read each other closed up into one candidate -- DANA sees a counter as a chain of one-bit groups (optional; needs the plugin) |

The score is a published weighted sum of structural features — size, mutual
feedback, how much of each member's dependencies stay inside the set, how many
nets the cone reads from outside, whether the register drives anything beyond its
own cone, whether all bits share a gate type and clock — printed with each
candidate as `data.features` and `data.weights`. A hidden weighting is not a
heuristic, it is a magic number.

Flip-flops with no feedback path to their own next-state function are never
proposed, and that they were excluded is recorded as a note rather than left
implicit.

**Ambiguity is reported, not broken by sort order.** When two candidates score
within `ambiguity_margin` (default 0.05) the run emits
`fsm/candidates/ambiguous`, lists them, and says the choice is not supported by
the evidence.

**Everything is overridable.** `state_registers` in the configuration file (or
`--state-registers`) replaces the search entirely; the choice is then recorded as
a `user_provided` assumption and carries no confidence, because a user's pick is
not evidence. `transition_logic` and `exclude_gates` are available for the same
reason.

## Assumptions, especially the ones solve_fsm makes silently

Every transition relation carries these, each marked discharged or not:

| assumption | discharged when |
| --- | --- |
| `state-register` | never — it is the heuristic |
| `state-bit-order` | always: bit *i* is the output of `bit_order[i]`, recorded on the finding |
| `transition-cone-complete` | no recovered condition names a net that combinational logic drives (a "hole" would mean `solve_fsm` treated a gate's output as a free input) |
| `asynchronous-control-inactive` | every set/reset pin of the state register is tied to a constant |
| `single-clock-domain` | all state flip-flops share one clock net |
| `initial-state` | the value was read from the flip-flops' `INIT` attributes rather than assumed |
| `closed-machine` | no condition reads a net driven by a flip-flop outside the register |
| `library-next-state` | never — the gate library's `next_state` expression is taken on trust |

The asynchronous one matters more than it looks. `solve_fsm` builds its
next-state function from the gate type's `next_state` expression only, which for
this repository's `FFR` is `(D & CE)` — the `clear_on` behaviour is not in it. A
design whose state register has a real reset therefore gets a transition graph
in which the reset does not appear at all. `hal_fsm` resolves those pins, and
when one is driven by logic it emits `fsm/<machine>/asynchronous-control` with
status `unsupported` instead of quietly producing a graph that is only true while
the reset is idle.

## What the tool checks about its own result

* **determinism and totality** — for every state, every assignment of the
  variables its outgoing conditions mention is enumerated and the conditions are
  evaluated. Exactly one successor must be enabled. Two is an ambiguous
  recovery, none is an incomplete one; either becomes a `counterexample`
  finding with the offending assignment as its witness.
* **the explored set** — the reachable closure is recomputed here, from the
  relation `solve_fsm` returned, and compared with the states it actually
  explored. A mismatch downgrades the reachability claim to `unknown` (see the
  state-encoding note below).
* **cone holes and external state** — every condition variable is resolved back
  to its net and classified as a primary input, another register's output, or —
  the bad case — the output of a combinational gate that was not in the
  transition logic.

## Witnesses

`--targets 2` asks for a path to state 2. The search is a breadth-first walk of
the recovered relation bounded by `max_cycles`; each step's condition is then
solved for a concrete input assignment (by enumeration, bounded by
`max_condition_vars`) and **re-evaluated under it**, so a witness that is
reported is a witness that was checked.

The three outcomes are three different findings, and the distinction is the
point:

* found → `proven_under_assumptions`, with the per-cycle inputs;
* the state is not in a *complete* reachable set → `proven_under_assumptions`
  for the negative claim ("no input sequence reaches it");
* not found within the bound → `unknown`, carrying the bound, and saying in as
  many words that this is not evidence of unreachability.

## Limits, and where they actually bite

```json
{ "limits": { "max_state_bits": 12, "smt_timeout_s": 60, "max_cycles": 32 } }
```

`solve_fsm` is an SMT loop in C++ with no cancellation point, and **it returns
nothing on failure** — no partial graph, not even the states it had already
explored. Two consequences:

* the only limits that prevent work are the ones applied *before* the call:
  `max_state_bits` (a candidate above it becomes an `unsupported` finding with
  kind `scale`, and the plugin is never called) and
  `brute_force_max_states`;
* the only hard time limit is the process boundary. `python tools/hal_fsm
  analyze --timeout N` runs `hal` through `hal_runner`'s executor, which
  escalates SIGTERM → SIGKILL across the child's process group. When that fires,
  the CLI writes the `timeout` findings document itself — a run that vanished
  without a record is how a pipeline ends up believing an FSM was recovered.

`smt_timeout_s` is handed to the plugin as its *per query* timeout. It is not a
wall clock: a machine with many states can honour it on every query and still
run for hours.

**Partial recovery, honestly.** After a limit is hit there is no transition
relation, so there is no partial state machine to report. What survives is
everything that was established before the solver ran: the candidates and their
scores, the asynchronous-control gap, the sequential gates nothing covered. Those
findings are still written, and the failure is its own `timeout`/`error` finding
next to them. The `solver: "brute_force"` mode is the other half of the answer:
it needs no SMT backend at all and, within `brute_force_max_states`, it recovers
the machine including its unreachable states — a different and in some ways
stronger claim than the SMT mode's "reachable from the initial state".

## A note on the state encoding

`solve_fsm` numbers a state by the position of a flip-flop in the `state_reg`
list: bit *i* is the output of `state_reg[i]`. Its `initial_state` argument is
encoded the *other way round* (`initial_state_num = (num << 1) + value` while
walking `state_reg` in order, so `state_reg[0]` becomes the most significant
bit — `plugins/solve_fsm/src/solve_fsm.cpp`). For an all-zero initial state —
the common case, and the one the plugin defaults to — the two agree and there is
nothing to decide.

For anything else there is. `hal_fsm` compensates by default
(`initial_state_encoding: "auto"`, i.e. `transition_index`) so the solver really
starts at the state the report names, records the decision as part of the run,
and then **checks it**: if the states `solve_fsm` explored are not the ones
reachable from the declared initial state, the reachability finding becomes
`unknown` rather than a claim. `initial_state_encoding: "argument_order"` passes
the bits through as the binding documents them, which is what a fix upstream
would make correct.

## Layout

```
config.py        configuration, overrides and resolved limits
candidates.py    flip-flop dependency graph, SCCs, proposal and scoring
transitions.py   transition tables, bit orders, reachability, witnesses, comparison
diagram.py       state diagrams via hal_viz.dot
reference.py     ground-truth files and comparison
findings.py      findings documents via hal_findings.model
extract.py       netlist -> dependency graph                       (needs hal_py)
solve.py         the solve_fsm wrapper                             (needs hal_py)
run.py           the in-HAL orchestration
run_in_hal.py    the `hal --python-script` entry point
cli.py           analyze / validate-config / compare / diagram
stubs.py         a netlist and a solve_fsm that behave like HAL's
```

Only `extract.py` and `solve.py` touch `hal_py`, and both go through
`hal_findings.adapters.common.call`, so everything else runs on a plain
interpreter — which is what lets `stubs.py` drive the entire workflow in the unit
tests.

## Command line

```bash
python tools/hal_fsm analyze <netlist> [--gate-library L] [--config C] ...
python tools/hal_fsm validate-config tools/hal_fsm/examples/controller.json --show
python tools/hal_fsm compare build/.../transitions-machine01.json ground_truth.json \
    --reachable-only            # exit 1 on any difference
python tools/hal_fsm diagram build/.../transitions-machine01.json -o graph.dot
python tools/hal_findings summary build/.../findings.json
```

The document is an ordinary `hal_findings` document, so anything that consumes
those consumes this one — including `tools/hal_viz`'s findings report once
[#18](https://github.com/fpga-professional-association/hal/issues/18) lands. The
`.dot` files it writes render with `dot -Tsvg`.

Only `analyze` needs a HAL build.

| exit code | meaning |
| --- | --- |
| 0 | the analysis ran and wrote a findings document |
| 1 | `hal` failed, was killed at the limit, or wrote nothing (a `timeout`/`error` document is written) |
| 2 | the run could not start: bad configuration, missing input, no `hal` binary |

## Tests

```bash
python -m unittest discover -s tools/hal_fsm -t tools -p "test_*.py"     # 93 tests, no HAL
HAL_BASE_PATH=<build> python3 tests/headless_smoke/fsm_discovery_smoke.py \
    --hal-binary <build>/bin/hal --work-dir <build>/fsm_smoke --keep
```

The unit tests run the *whole* workflow — extraction, proposal, solving,
checking, diagrams, findings — against `stubs.py`, which reproduces HAL's
conventions including the reversed initial-state encoding. What they cannot
cover is whether the real bindings still behave that way; that is the smoke
test, which recovers the fixture controller through a built HAL and compares it
against `tests/fixtures/fsm_controller/ground_truth.json`, then repeats the
exercise for the exhaustive mode, a user override, a deliberately wrong
candidate and a wall-clock limit.

## Integration notes

Nothing outside this directory is modified. Two hooks are worth making later,
each a one-line change to a file this work deliberately did not touch:

* `tests/headless_smoke/CMakeLists.txt` — register the unit suite as
  `runTest-hal_fsm_standalone` (`python -m unittest discover -s tools/hal_fsm -t
  tools -p "test_*.py"`), and run `fsm_discovery_smoke.py` from the ubuntu
  workflow the way `runner_smoke.py` is run;
* `tools/hal_runner/analyses.py` — an `Analysis` entry named `solve_fsm.discover`
  pointing at a thin `steps/` module that calls `hal_fsm.run.analyse` would make
  FSM recovery a step in a reproducible `hal_runner` run, with checkpoints and a
  manifest. The in-HAL side is already shaped for it: `hal_fsm.run.analyse`
  returns `(document, artifacts, metrics, notes)`, which is a `StepOutcome` in
  all but name.
