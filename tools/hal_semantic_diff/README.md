# hal_semantic_diff — what *behaviour* changed between two builds

A textual diff of two synthesized netlists says that something moved. A graph
diff says which gates moved. Neither says whether the circuit still computes
the same thing — and that is the question a re-synthesis, a tool upgrade or a
suspicious third-party build actually raises.

`hal_semantic_diff` answers the narrow, useful version of it:

> For two builds with the same boundary and a **declared** correspondence
> between their registers: at every observation point, is the function still
> the same? If not, *which* one changed, *which gates* in its cone changed,
> and *what input* makes the two builds disagree?

Everything it emits is a [`hal_findings`](../hal_findings/README.md) document,
so the status vocabulary — `proven_under_assumptions`, `counterexample`,
`unknown`, `timeout`, `unsupported` — is the shared one, and a proof can never
be confused with a solver that gave up.

## What it does that `z3_utils.compare_netlists` does not

`plugins/z3_utils` already answers a related question, and its contract is
worth stating precisely before building on it (read
`plugins/z3_utils/src/netlist_comparison.cpp`; the honest reading is written
down in `tools/hal_findings/adapters/netlist_comparison.py`):

* it matches sequential gates **implicitly, by name**, and returns `OK(false)`
  for a *structural* mismatch — a missing or differently-typed register — which
  is indistinguishable from a behavioural difference in the returned boolean;
* it folds a solver `unknown` into that same boolean via `fail_on_unknown`;
* it builds its queries `without_model_generation()`, so **no counterexample
  can be recovered through it**;
* it answers for the whole netlist at once: one bool, no localization.

This tool keeps the same *model* — same combinational frontier, same boundary
variable naming — and changes what is reported:

| | `z3_utils.compare_netlists` | `hal_semantic_diff` |
| --- | --- | --- |
| result | one `Optional[bool]` | one finding per observation point, plus a summary |
| correspondence | implicit, by name | an explicit, validated mapping file |
| renamed register | reported as inequivalence | reported as a gap, or handled by renaming the variable |
| solver `unknown` | folded into the bool | `unknown` / `timeout`, never equivalence |
| counterexample | not available | a witness from `hal_py.SMT`, replayed through both cones |
| localization | none | the changed cone, the changed gates, a diagram |

The `z3_utils` binding is still used, as a **cross-check**: when the
correspondence is the identity mapping it requires, every point is also handed
to `compare_nets` with both polarities of `fail_on_unknown`, and a disagreement
is reported rather than silently resolved.

## The model, stated once

**Observation points.** The comparison is done at

* every top-module **output pin present in both builds** (matched through the
  mapping), and
* every **input pin of every pair of corresponding sequential gates**.

That is exactly the set `compare_netlists` covers. Note the consequence: a
registered output pin's cone is a single free variable, so for such a pin the
information lives at the driving register's `D` input, not at the pin. The
report says so explicitly in the `semantic_diff/coverage/observation-points`
finding rather than leaving you to notice.

**Cones and the boundary.** Each point's combinational cone is the set of gates
carrying the `combinational` gate-type property that feed it, cut at every net
whose driver is not such a gate. Cut nets become variables, named exactly as
`z3_utils::substitute_net_ids` names them:

| boundary net | variable |
| --- | --- |
| top-module input | `GLOBAL_IN_<pin name>` |
| driven by a sequential gate | `<gate name>_<output pin name>` |
| anything else | the raw net name (flagged as `dangling`/`unmodelled`) |

**The claim.** UNSAT on `cone_a XOR cone_b == 1` proves the two functions equal
for *all* inputs and all states — under the assumptions listed on every
finding. It is a next-state and output equivalence, not a temporal one.

**Bounds.** Nothing is unrolled, so nothing here is `proven_bounded` or
`bounded_counterexample`. A difference of the compared functions has no cycle
bound, and inventing one to fit a "bounded" status would misrepresent the
claim (the schema forbids a bounded status without a real `cycle_bound`
anyway).

### Explicitly out of scope

* **Retiming.** Moving logic across a register changes what the matched
  registers hold, which is the `matched-state-correspondence` assumption.
  `fixtures/decoder_retimed.v` is a fixture *for* this: the tool reports nine
  differing points, and the right reading is "outside the model", not "the
  retimed build is broken".
* **State re-encoding.** Same reason, more so: there is no correspondence
  between the state elements at all.
* **Different boundaries.** Pins present in only one build are reported, never
  compared, and never counted as agreement.
* **Multi-driven nets and combinational loops.** Refused with an `error`
  finding, as `z3_utils` refuses them.

## The correspondence file

```json
{
  "version": "1.0",
  "description": "base build vs. de-morgan'd rewrite",
  "netlist_a": {"label": "base",    "path": "decoder_base.v"},
  "netlist_b": {"label": "rewrite", "path": "decoder_rewrite.v"},
  "top_inputs":  {"A0": "A0", "A1": "A1", "EN": "EN", "CLK": "CLK", "RST": "RST"},
  "top_outputs": {"HIT": "HIT", "SEL0": "SEL0"},
  "sequential_gates": {"sel_reg_0": "sel_reg_0", "sel_reg_3": "status_reg_3"},
  "ignore_outputs": [],
  "notes": ["..."]
}
```

* Any mapping may be the string `"by_name"` — the identity over shared names.
  `--auto-correspondence` uses that for all three and says so in the run.
* A mapping must be **injective**; a duplicate target is rejected at load time.
* A **rename is supported**: `sel_reg_3 -> status_reg_3` rewrites the boundary
  variable `sel_reg_3_Q` to `status_reg_3_Q` before the comparison. This is the
  TODO the C++ implementation carries; because the plugin cannot express it,
  the cross-check is skipped for such runs and an extra assumption is recorded.
* A register the mapping does not cover is a `unsupported` finding, and any
  observation point whose cone *reads* uncovered state is `unsupported` too —
  not `counterexample`. A rename is neither equivalence nor a regression, and
  the tool refuses to call it either.

## Usage

```bash
export HAL_PY_PATH=/path/to/hal/build/lib      # or pass --hal-lib

python tools/hal_semantic_diff compare \
    tools/hal_semantic_diff/fixtures/decoder_base.v \
    tools/hal_semantic_diff/fixtures/decoder_changed.v \
    --gate-library plugins/gate_libraries/definitions/example_library.hgl \
    --correspondence tools/hal_semantic_diff/fixtures/correspondence_changed.json \
    -o out/semantic_diff
```

Inputs are anything `hal_viz` accepts: a HAL project directory, a `.hal` file,
or an HDL netlist plus `--gate-library`.

Outputs, in `-o`:

| file | what |
| --- | --- |
| `findings.json` | the findings document, schema-validated before it is written |
| `report.html` | evidence-linked static report, diagrams inlined |
| `cone_<point>.dot` / `.svg` | the two cones of a changed point, changed gates highlighted |
| `functions_<point>.txt` | both cone functions, the witness and its replay |
| `measurements.json` | runtimes, query counts, per-point wall times |

### Exit codes

| code | meaning |
| --- | --- |
| 0 | ran, and the verdict satisfies `--fail-on` |
| 1 | a handled failure (no `hal_py`, no local solver, unparseable netlist, malformed correspondence, a document that would not validate) |
| 2 | bad command line |
| 3 | ran, and the verdict does **not** satisfy `--fail-on` |
| 130 | interrupted |

1 and 3 are deliberately different: "the tool broke" and "the designs differ"
are different facts, and collapsing them is how an equivalence check ends up
green for the wrong reason. `--fail-on inconclusive` also gates on anything
short of a proof — unknowns, timeouts, correspondence gaps — which is the
setting a release gate wants.

### Useful options

| option | why |
| --- | --- |
| `--structural-fast-path` | skip the solver where the two cones are literally the same circuit over the same variables; those findings then report a `structural` method, not a formal one |
| `--no-timings` | drop wall-clock values so two runs on identical inputs produce byte-identical `findings.json` |
| `--diagrams {changed,all,none}` | how many cone diagrams to draw (default: only the points that are not proven equivalent) |
| `--no-cross-check` | do not also ask `z3_utils.compare_nets` about each point |
| `--solver-timeout` | per-point SMT timeout; a point that hits it becomes `timeout`, never equivalence |

### From inside `hal --python-script`

HAL's Python shell provides neither `__file__` nor `sys.argv`, and
`--python-args` is split on spaces — so, like `hal_runner`, the request comes
through the environment:

```bash
cat > /tmp/request.json <<'JSON'
{"tools_dir": "/hal/tools",
 "argv": ["compare", "/work/a.v", "/work/b.v",
          "--gate-library", "/hal/plugins/gate_libraries/definitions/example_library.hgl",
          "--correspondence", "/work/map.json", "-o", "/work/out"]}
JSON
HAL_SEMANTIC_DIFF_REQUEST=/tmp/request.json \
    hal --python-script tools/hal_semantic_diff/hal_script.py
```

HAL propagates a failing python script as a failing run, so the exit codes
above survive.

## Solver backend

The comparison uses **HAL's own** `hal_py.SMT` bindings, not the `z3` Python
package and not `z3_utils`' solver path, because they are the only ones that
expose `QueryConfig.with_model_generation()`, `SolverResult.model` and
`Model.model` — i.e. the only way to *keep* a counterexample. No extra Python
dependency is introduced.

`SmtEngine` picks the first backend this build can actually call, preferring
`Z3/Binary`. Note that HAL's Z3 *library* call path is a stub that returns an
error (`Z3::query_library` in `src/netlist/boolean_function/solver.cpp`), so in
practice **the `z3` binary must be on `PATH`** — the same requirement
`z3_utils` has. When no backend is available the run fails with exit code 1 and
an actionable message, rather than producing a pile of `error` findings.

## Fixtures

`fixtures/` holds hand-written gate-level Verilog against the shipped
`plugins/gate_libraries/definitions/example_library.hgl`, plus the explicit
correspondence for each pair and `ground_truth.json` recording the expected
outcome of every case.

| fixture | role |
| --- | --- |
| `decoder_base.v` | build A: 2-to-4 address decoder, enable gating, registered selects, combinational `HIT` |
| `decoder_rewrite.v` | equivalent rewrite: De Morgan'd decode, MUX-based gating, OR2 tree — not one combinational gate survives |
| `decoder_changed.v` | the `SEL2`/`SEL3` decode terms swapped: two register inputs change, `HIT` does not |
| `decoder_renamed.v` | `sel_reg_3` renamed to `status_reg_3`; nothing else changed |
| `decoder_retimed.v` | the enable gating moved across the registers — a *rejected* input |

The changed-decoder case is the interesting one: the difference function of
both changed points is exactly `GLOBAL_IN_EN & GLOBAL_IN_A1`, so **every**
counterexample sets `EN = 1` and `A1 = 1` — which is what the tests assert. And
`HIT` stays provably equivalent, because swapping two decode terms does not
change the OR of all four: a checker that only looked at top-level outputs
would report this regression as equivalent.

## Tests

Pure Python, no HAL, no network:

```bash
python -m unittest discover -s tools/hal_semantic_diff -t tools -p "test_*.py"
```

54 tests. The netlists are stub objects shaped like the `hal_py` bindings and
the solver is a brute-force engine over the same interface `halbridge.SmtEngine`
implements, so the whole classification pipeline — including witness replay and
the refusal to report an unreplayable model — is exercised. The stub designs
are transcriptions of the fixtures, and `GroundTruthTest` asserts that they and
`fixtures/ground_truth.json` do not drift apart.

What that cannot cover — Verilog parsing, `SubgraphNetlistDecorator`, a real
SMT query, whether `SolverResult.model` still carries a model — is what
`tests/headless_smoke/semantic_diff_smoke.py` is for. It needs a built HAL:

```bash
HAL_BASE_PATH=<build> PYTHONPATH=<build>/lib \
    python3 tests/headless_smoke/semantic_diff_smoke.py --work-dir out/semdiff --keep
```

It parses every fixture and checks its gate histogram, runs every ground-truth
case and checks the verdicts, the localization, the witness constraints and the
replay, checks that `--no-timings` is byte-reproducible, that a malformed
correspondence exits 1 (not 3), and writes the measured runtimes to
`runtimes.json`.

## Integration with the rest of the fork

* **`hal_findings`** — every document is built with `hal_findings.model` and
  validated with `hal_findings.validate` before it is written, so
  `python tools/hal_findings summary --strict out/semantic_diff/findings.json`
  works on it unchanged.
* **`hal_viz`** — the cone diagrams are built on `hal_viz.dot` and rendered
  with `hal_viz.render`; no DOT emitter was reimplemented. Each changed point
  also carries a `command` evidence entry with the exact
  `hal_viz netlist_graph --gate … --depth 2` invocation that widens the view
  around the changed gate.
* **`hal_viz report`** (issue #18) — `--report-backend auto` prefers
  `hal_viz`'s `report` subcommand as soon as it exists (feature-detected via
  `hal_viz.cli.cmd_report`) and falls back to the built-in renderer otherwise.
  Both consume the same document, so nothing downstream changes when the
  switch happens.
* **`hal_runner`** — not wired in yet, on purpose: `hal_runner`'s step model
  loads exactly *one* netlist per step (`request["netlist"]`, `StepContext`),
  and this analysis needs two plus a correspondence file. Registering it would
  mean changing `hal_runner/analyses.py` and the step request shape, which is
  out of scope for a parallel-safe change. See "Known gaps" below.

## Known gaps

* **No `hal_runner` step.** Registering `semantic_diff.compare` needs (a) an
  entry in `tools/hal_runner/analyses.py`, and (b) a second netlist plus a
  correspondence path in the step request — `hal_runner.protocol`/`config`
  currently model a single input. Until then, drive this tool directly (or
  through `hal_script.py`); its exit codes and `measurements.json` are designed
  for that.
* **No CI wiring.** `tests/headless_smoke/semantic_diff_smoke.py` is not
  referenced from `.github/workflows/` or `tests/headless_smoke/CMakeLists.txt`
  (both are outside this change). It is written to the same shape as
  `real_netlist_smoke.py`, so adding it is one workflow step.
* **Recursive cone traversal.** Signature and cone construction recurse over
  the cone; the CLI raises the interpreter recursion limit to 20 000 and
  `--max-cone-gates` bounds the traversal, but a pathologically deep
  combinational chain is untested at scale.
* **Hierarchy is flattened.** Observation points are derived from the *top*
  module's pins and from sequential gates anywhere in the netlist; submodule
  boundaries are not observation points.
* **One witness per point.** The solver returns one satisfying assignment; the
  report never claims it is the only one, and records which variables the
  replay had to default to `0`.
