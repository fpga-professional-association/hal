---
name: netlist-analysis-tools
description: Pick the right tools/hal_* analysis for a question -- CDC screening, FSM recovery, semantic diffing, security reachability, fault campaigns, migration assessment, or reproducible multi-step runs. Use when deciding which tool answers a netlist question, before reaching for any one of them.
---

# netlist-analysis-tools -- which tool answers which question

All eight write a [`hal_findings`](../../../tools/hal_findings/README.md)
document (status vocabulary: `proven_under_assumptions`, `proven_bounded`,
`heuristic`, `counterexample`/`bounded_counterexample`, `unknown`, `timeout`,
`unsupported`, `error`). None of them ever silently promotes a guess to a
proof -- read the status, not just the summary line.

## hal_cdc -- does a signal cross clock/reset domains unsafely?
Structural screening, not timing sign-off -- no metastability or physical
timing model, ever, and every report says so in a `cdc/limitations` finding.

```bash
python tools/hal_cdc discover design.v --gate-library LIB.hgl -o clocks.json
# edit clocks.json: name domains, add synchronous primary inputs
python tools/hal_cdc audit design.v --gate-library LIB.hgl \
    --declarations clocks.json -o results/cdc.findings.json
```
Findings land in `-o`'s findings JSON; render the domain graph with
`--dot out.dot && dot -Tsvg out.dot -o out.svg`. Exit 2 means the audit
couldn't run at all (bad declarations) -- never a statement about the design.

## hal_fsm -- what are this state register's states and transitions?
Two stacked claims: *which flops are the state register* is a `heuristic`
guess from feedback structure; *what the transitions are* is a separate
`proven_under_assumptions` finding built on top of it.

```bash
python tools/hal_fsm analyze design.v --gate-library LIB.hgl \
    --targets 2 -o build/hal_fsm/design --print-summary
```
Writes `build/hal_fsm/design/findings.json`, `transitions-*.json`,
`state-diagram-*.dot`. Don't run this on a wide free-running counter (state
space explodes) -- it pays off on small, irregular control logic.
Implementation detail worth knowing before scripting against it directly:
`candidates.propose(graph, ...)` returns `(candidates, notes)`, not a bare
list, and `candidates.rank()` takes the bare candidate list -- passing the
tuple from `propose()` straight into `rank()` is a real bug, not a style
nit. `Candidate` objects expose `.sources` and `.gate_ids`, not `.gates`.

## hal_semantic_diff -- did behaviour change between two builds?
Narrower and stronger than a graph diff: given a **declared** register
correspondence, is the function at every observation point still the same,
and if not, which gates and which input?

```bash
python tools/hal_semantic_diff compare base.v changed.v \
    --gate-library LIB.hgl --correspondence correspondence.json -o out/
```
Findings + an evidence-linked `report.html` land in `-o`. Retiming and
state re-encoding are explicitly out of scope (reported as gaps, not bugs).

## hal_explain -- what does the design look like, with evidence?
Composes results *already written* by DANA (`dataflow_analysis`),
`module_identification`, and `hal_fsm` into one block model -- it runs no
analysis itself.

```bash
python tools/hal_explain collect design.v --gate-library LIB.hgl -o build/explain
python tools/hal_explain compose --inventory build/explain/inventory.json \
    --findings build/explain/findings-dataflow.json \
    -o build/explain/blocks.json --dot build/explain/blocks.dot \
    --report build/explain/report.md
```
Output lands in `-o`: `blocks.json` (the model), `blocks.dot`/`.svg`,
`report.md`. Every gate not claimed by an analysis becomes an explicit
`unknown_region` -- coverage always adds up to the whole gate count.

## hal_secprop -- can an attacker's interface reach a protected register?
A bounded symbolic check, not a cone walk: cone reachability alone cannot
tell a correct design from a one-gate-missing broken one (their cones are
identical) -- the property catalogue is what actually decides it.

```bash
python tools/hal_secprop check my.policy.json -o build/secprop/findings.json
python tools/hal_secprop replay build/secprop/evidence/*.replay.json   # witness
```
Findings in `-o`; a witness replay directory sits under `evidence/`. Single
clock domain only; multi-clock needs `hal_cdc` instead.

## hal_fault_campaign -- does a bit-flip get detected, or leak silently?
Simulates one register-bit transient flip per run against a fault-free
baseline of the *same instrumented netlist*, and classifies each faulty run
from the design's own outputs/detectors inside a stated cycle window.

```bash
python tools/hal_fault_campaign run tools/hal_fault_campaign/fixtures/campaign_short_window.json \
    --hal-binary /work/build/bin/hal
```
Writes `build/hal_fault_campaign/<name>/`: `manifest.json` (replay record),
`findings.json`, `traces.json`. `unobserved_in_window` is never "masked" --
widen the window (`hold_cycles`) before believing a fault was safe.

## hal_migration -- what would porting this design to another target need?
Answers two questions honestly instead of converting anything: what's in the
netlist (`inventory`), and what a *reviewed* target catalogue says it can
take (`assess`) -- it never rewrites or re-synthesizes.

```bash
python tools/hal_migration run design.v --gate-library LIB.hgl \
    --catalogue ice40ultra-to-generic-fpga --report assessment.md
```
Assessment (a `hal_findings` document) + `assessment.md` land where you
point `-o`/`--report`. Resolution only ever moves *down* (`supported` ->
`unresolved`), never up -- an undeclared primitive is `unresolved`, silence
is never approval.

## hal_runner -- how do I make an analysis run reproducible?
Wraps any of the above (or `graph_algorithm`/`dataflow` steps) into a pinned,
content-hashed, cacheable run -- the run configuration is the artifact.

```bash
python tools/hal_runner run tools/hal_runner/examples/uart_components.json \
    --hal-binary /work/build/bin/hal
```
Writes `build/hal_runner/<name>/`: `manifest.json` (pins every input by
hash), `steps/<id>/findings.json` per step. Re-running with unchanged
netlist/config/HAL version reuses cached steps instead of recomputing them.

## Where things live
- Every tool: `tools/hal_<name>/README.md` -- read it before scripting
  against internals; this file is the index, not the reference.
- Shared schema/vocabulary: `tools/hal_findings/README.md`.
- Rendering any tool's findings as a page: `tools/hal_viz report`.
