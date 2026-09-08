# hal_explain — evidence-linked block models from analyses you already ran

Three analyses in this fork recover *something* from a stripped netlist and none
of them produces an explanation:

| analysis | what it establishes | how strong |
| --- | --- | --- |
| `plugins/dataflow_analysis` (DANA) | flip-flops that plausibly form one word-level register | `heuristic` |
| `plugins/module_identification` | a gate cone implements a specific word-level operation | `proven_under_assumptions` |
| `plugins/solve_fsm` via [`tools/hal_fsm`](../hal_fsm/README.md) | the transition relation of a nominated state register | `proven_under_assumptions`, on a `heuristic` |

`hal_explain` reads their **results** — the `tools/hal_findings` documents they
already write — and composes them into a **recovered-block model**: a versioned
JSON document where every block carries the gates it is made of and every claim
carries a reference back to the finding that produced it.

```bash
# in the container, with a built HAL
python tools/hal_explain collect tools/hal_explain/fixtures/accumulator.v \
    --gate-library plugins/gate_libraries/definitions/example_library.hgl \
    -o build/explain

# anywhere, on a plain interpreter
python tools/hal_explain compose \
    --inventory build/explain/inventory.json \
    --findings  build/explain/findings-dataflow.json \
    --findings  build/explain/findings-module-identification.json \
    --findings  build/hal_fsm/accumulator/findings.json \
    -o build/explain/blocks.json \
    --dot build/explain/blocks.dot \
    --report build/explain/report.md
```

```
build/explain/
    inventory.json     the netlist snapshot every claim is resolved against
    findings-*.json    what each analysis reported (the shared schema)
    blocks.json        the recovered-block model
    blocks.dot         the block diagram        (render with dot -Tsvg)
    report.md          the templated report
    result.json        what the in-HAL half did, including what it could not do
```

## The one thing this tool is careful about

**A verified function and a heuristic label are not the same claim, and a reader
must never be able to confuse them.** So:

* a block's `confidence` is *derived* from the status of the findings that
  support it — `hal_explain.model.confidence_for_status` is the only place the
  mapping exists, and `model.claim()` raises if a caller declares a confidence
  its status does not support. `validate` re-checks it on the document, so a
  hand-edited file cannot promote a guess either;
* the diagram draws the four confidences differently (solid double green /
  dashed amber / dotted grey / red) and ships a legend *inside the drawing*;
* the report separates them at three levels: section, badge and wording. Every
  verified claim prints the assumptions its finding did **not** discharge next
  to it, because a proof under an undischarged assumption is conditional.

| status | confidence | rendered as |
| --- | --- | --- |
| `proven_under_assumptions`, `proven_bounded` | `verified` | solid, double border, green |
| `heuristic` | `heuristic` | dashed, amber |
| `counterexample`, `bounded_counterexample` | `refuted` | red, block marked *contested* |
| `unknown`, `timeout`, `error`, `unsupported` | `unknown` | dotted, grey |
| anything a future schema adds | `unknown` | dotted, grey |

That last row matters: an unrecognised status can never become `verified`.

## Unknown regions are not optional

Every gate that no analysis claimed ends up in an explicit `unknown_region` — a
connected component of unclaimed gates, drawn in the diagram with a `?` and
listed in the report with its gate-type histogram and how many of its gates are
sequential. `compose` asserts the arithmetic (`model.coverage` raises if blocks
+ unknown regions ≠ every gate), and `validate` re-checks it.

Clustering never merges through a shared control net: clock pins and constant
nets are excluded, and a net with more loads than `--max-net-loads` (default 8)
is excluded *and reported in the document's notes*. A hidden clustering
threshold is a magic number, not a heuristic.

## Overlaps are recorded, not resolved

`module_identification` builds candidates around known registers, so its
verified adder cone contains the same flip-flops DANA grouped. Two analyses
disagreeing about where a boundary is *is* the result:

* identical gate sets **merge** into one block carrying both claims — this is
  how DANA's register group and hal_fsm's state machine become a single
  `state_machine` block with two heuristic and two verified claims;
* overlapping-but-different gate sets stay **separate**, both keep listing the
  gate, and `overlaps` records which block owns it for connectivity purposes
  (higher confidence first, then the lower `block_id`). The diagram draws a
  dotted link labelled *shares N gate(s)* so neither block looks orphaned.

## The intermediate representation

`schema/recovered-blocks-1.0.0.schema.json` is the contract; `model.py` are the
builders. Shape:

```jsonc
{
  "schema_version": "1.0.0",
  "design":   { "artifact_id": "netlist", "sha256": "...", "gate_count": 33, ... },
  "sources":  [ { "source_id": "findings-dataflow", "adapter": "dataflow",
                  "status_counts": {...}, "unresolved_gates": [...] } ],
  "blocks":   [ { "block_id": "block/arithmetic/0001", "kind": "arithmetic",
                  "confidence": "verified", "gates": [ /* gate refs */ ],
                  "ports": { "inputs": [], "control": [], "outputs": [] },
                  "claims": [ { "text": "...", "status": "...", "gates": [3,4,5],
                                "evidence": [ { "source_id": "...",
                                                "finding_id": "...",
                                                "open_assumptions": [...] } ] } ] } ],
  "unknown_regions": [ { "region_id": "unknown/0001", "gates": [...],
                         "gate_types": {...}, "sequential_gate_count": 0 } ],
  "ports": [...], "edges": [...], "overlaps": [...],
  "coverage": { "gates_total": 33, "gates_in_blocks": 26, "gates_unclassified": 7, ... }
}
```

`validate` enforces what JSON Schema cannot: derived confidences, evidence
pointing only at declared sources, claims naming only gates of their own block,
no gate in both a block and a region, coverage that adds up, and edges between
declared nodes only.

## No language model in the core path

`compose`, `diagram` and `report` are deterministic templates over structured
evidence. Two runs over the same inputs are byte-identical (`serialize` sorts
every order-insensitive list; `document_digest` excludes the wall-clock fields).

`python tools/hal_explain prose blocks.json` exists for people who want an LLM
to paraphrase. **It calls no model.** It emits the evidence bundle such a step
would consume, with an instruction block that requires every confidence label to
survive the paraphrase. Nothing else in the pipeline depends on it, and the
report you already have needs none.

## Adapters

| adapter | reads | produces |
| --- | --- | --- |
| `dataflow` | `hal_findings.adapters.dataflow` documents | one `register` block per group; the coverage finding becomes a document note |
| `module_identification` | documents written by this package's own adapter | `arithmetic` / `counter` / `comparator` blocks for verified candidates, `other` blocks at `unknown` confidence for checked-and-unverified ones |
| `fsm` | `tools/hal_fsm` documents | one `state_machine` block per machine, carrying the transition relation, the reachability claim, determinism, reference comparison and the asynchronous-control gap as separate claims |

The adapter is chosen from `analysis.plugin.name`, never from the file name;
`--adapter` forces it. A document that matches nothing is an error, not a guess.

`module_identification` has no adapter in `tools/hal_findings` yet, so
`adapters/module_identification.py` provides **both** halves: `build_document()`
wraps a `module_identification.Result` in a findings document (written against
the accessors in `plugins/module_identification/python/python_bindings.cpp`, but
importing nothing from HAL, so it is testable against stubs), and
`contributions()` maps that document onto blocks. A verified candidate is the
only thing in this tool that gets `proven_under_assumptions`; a checked and
unverified one is `unknown` — *never* "no arithmetic here".

## Gate resolution, and what happens when it fails

Findings gate references resolve against the inventory by ID first (IDs are only
meaningful inside one artifact) and by unique name second, which lets a document
produced in an earlier HAL session still be composed. Anything that resolves by
neither is recorded in `sources[].unresolved_gates`, noted in the document, and
printed in the report — so a block built from a document about *another* netlist
comes out visibly incomplete instead of quietly smaller. `--strict` turns that
into exit 1.

## Layout

```
schema.py       versioned schema lookup
model.py        the IR builders, and the status -> confidence mapping
serialize.py    deterministic JSON + digests
validate.py     schema + cross-reference validation
inventory.py    the netlist snapshot (built from hal_cdc.netlist_view)
adapters/       findings documents -> block contributions
compose.py      contributions + inventory -> the model
diagram.py      the block diagram (via hal_viz.dot)
report.py       the templated Markdown report
collect.py      netlist + plugins -> inventory + findings     (needs hal_py)
run_in_hal.py   the `hal --python-script` entry point
cli.py          collect / compose / diagram / report / validate / prose / inventory
fixtures/       accumulator.v, its ground truth, and recorded findings documents
```

Only `collect.py` touches `hal_py`. `inventory.py` reuses
`hal_cdc.netlist_view`, which already has both a `hal_py` and a
dependency-free flavour, so no second Verilog parser exists in this tree.

## Command line

```bash
python tools/hal_explain inventory <netlist> --gate-library L [--fixture-reader] -o inv.json
python tools/hal_explain collect   <netlist> --gate-library L -o build/explain [--timeout N]
python tools/hal_explain compose   --inventory inv.json --findings F [--findings G] -o blocks.json
python tools/hal_explain diagram   blocks.json -o blocks.dot
python tools/hal_explain report    blocks.json -o report.md
python tools/hal_explain validate  blocks.json
python tools/hal_explain prose     blocks.json -o prose-input.json
```

Only `collect` needs a HAL build.

| exit code | meaning |
| --- | --- |
| 0 | the command ran and nothing crossed a `--fail-*`/`--min-classified` threshold |
| 1 | it ran and something did, or the HAL run failed |
| 2 | it could not run: bad input, no HAL, unreadable document — **not** a statement about the design |

## Tests

```bash
python -m unittest discover -s tools/hal_explain -t tools -p "test_*.py"   # 64 tests, no HAL
python tools/hal_explain/fixtures/_generate.py --check                     # fixture drift check
HAL_BASE_PATH=<build> python3 tests/headless_smoke/explain_blocks_smoke.py \
    --hal-binary <build>/bin/hal --work-dir <build>/explain_smoke --keep
```

The unit tests run the whole composition — adapters, merging, unknown regions,
connectivity, schema, diagram, report, CLI — against the fixture netlist and the
recorded findings documents. What they cannot cover is whether HAL's parser and
the plugins still behave that way; that is the smoke test, which collects
through a built HAL, solves the fixture's controller with `hal_fsm`, and checks
that HAL's netlist agrees with the fixture reader gate for gate.

## Integration notes

Nothing outside this directory is modified. Four hooks are worth making later,
each a one-line change to a file this work deliberately did not touch:

* `tests/headless_smoke/CMakeLists.txt` — register the unit suite as
  `runTest-hal_explain_standalone` (`python -m unittest discover -s
  tools/hal_explain -t tools -p "test_*.py"`), and run `explain_blocks_smoke.py`
  from the ubuntu workflow the way `runner_smoke.py` is run;
* `tools/hal_findings/adapters/__init__.py` + a `module_identification.py` next
  to `dataflow.py` — `hal_explain/adapters/module_identification.py`'s
  `build_document()` is a general findings adapter that only lives here because
  this work could not add files to that package. Moving it makes verified
  arithmetic available to every consumer of findings documents, not just this
  one, and `hal_explain` would then import it instead;
* `tools/hal_fsm/findings.py` — the transitions finding records
  `data.transition_cone.gates` as a *count*. Recording the gate IDs as well
  (they exist as `outcome.transition_logic_ids` in `hal_fsm/run.py`) would let
  the state-machine block include its next-state logic instead of leaving those
  gates in an unknown region. Until then the fixture's `f_*` gates are honestly
  unclassified, which is what the ground truth records;
* `tools/hal_runner/analyses.py` — an `Analysis` entry named
  `hal_explain.collect` pointing at a thin `steps/` module that calls
  `hal_explain.collect.collect` would make block recovery a step in a
  reproducible `hal_runner` run. The in-HAL side is already shaped for it:
  `collect()` returns a result record with artifacts, per-step statuses and
  notes.
