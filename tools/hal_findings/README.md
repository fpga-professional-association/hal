# hal_findings — a versioned findings and evidence contract

HAL analyses answer very different kinds of question. `z3_utils.compare_netlists`
can *prove* something; `dataflow.analyze` can only *suggest* it; a symbolic run
may simply time out. Reports, agents and CI have to consume all of them, and the
one failure mode that matters is presenting a heuristic — or a bounded result —
as if it were a proof.

`hal_findings` is the shared answer: one JSON document shape, versioned and
validated, in which every finding states what kind of claim it is, what it rests
on, and what it is scoped to. It changes no plugin API; the adapters read what
the existing bindings already return.

## The status vocabulary

| status | meaning |
| --- | --- |
| `proven_under_assumptions` | holds for every execution, given the listed `assumptions` |
| `proven_bounded` | holds **only** up to `bounds.cycle_bound` |
| `counterexample` | refuted; no cycle bound involved |
| `bounded_counterexample` | refuted within `bounds.cycle_bound` |
| `heuristic` | evidence-based guess, never a proof |
| `unknown` | no verdict (e.g. the solver returned `unknown`) |
| `timeout` | aborted at a resource limit recorded in `limits` |
| `error` | the analysis itself failed — says nothing about the design |
| `unsupported` | outside what the analysis covers, with the gap spelled out |

The schema enforces the separation rather than trusting the writer:

* `proven_under_assumptions` requires `bounds.unbounded: true` **and** an
  explicit `assumptions` list, and forbids a `cycle_bound`;
* `proven_bounded` and `bounded_counterexample` require `bounds.unbounded: false`
  **and** a `cycle_bound`, so a bounded result can never be re-read as a full
  proof (`hal_findings.model.is_unbounded_proof` is the one predicate consumers
  need);
* `heuristic` may not use a `formal` method or claim unbounded validity;
* `timeout` must carry the limit it hit; `error` must carry the error;
  `unsupported` must carry a typed reason, and `kind: "primitive"` must name at
  least one gate type.

## Scoping: gate IDs belong to an artifact

HAL gate and net IDs are unique inside *one* netlist. A finding about an
equivalence check spans two, so every `gate_ref`/`net_ref`/`module_ref` carries
the `artifact_id` of the exact artifact it belongs to, every artifact is pinned
by `sha256` (or carries an explicit `unhashed_reason`), and validation rejects a
reference that points outside the finding's `scope.artifact_ids`.

## Layout

```
schema/findings-1.0.0.schema.json   the normative schema (JSON Schema 2020-12)
schema.py                           version constants and schema lookup
model.py                            builders that refuse contradictory findings
serialize.py                        deterministic JSON, document digests
validate.py                         schema + cross-reference validation
jsonschema_mini.py                  dependency-free validator for the subset used
adapters/dataflow.py                dataflow.analyze() results
adapters/netlist_comparison.py      z3_utils.compare_netlists() results
examples/                           one document per outcome kind
cli.py, __main__.py                 validate / normalize / summary / schema
test_hal_findings.py                unit tests (no HAL needed)
test_hal_findings_hal.py            integration tests (needs a built HAL)
```

Everything except `test_hal_findings_hal.py` runs on a plain interpreter with
the standard library only. `jsonschema` is optional: when installed,
`validate --jsonschema` and the test suite cross-check the built-in validator
against it.

## Command line

```bash
python tools/hal_findings validate  results/*.json      # exit 1 on any problem
python tools/hal_findings validate  --jsonschema r.json # use the real library
python tools/hal_findings summary   --strict r.json     # exit 1 on counterexample/error
python tools/hal_findings normalize r.json --in-place   # canonical form
python tools/hal_findings schema    --path
```

## Writing findings from an analysis

```python
import sys; sys.path.insert(0, "tools")
from hal_findings import serialize, validate
from hal_findings.adapters import dataflow as adapter

result = dataflow.analyze(dataflow.Configuration(netlist).with_flip_flops())
document = adapter.build_document(result, netlist_path="designs/toy_cipher.v")
validate.validate_document(document)
serialize.write_document(document, "results/dataflow.json")
```

For the equivalence checker, use `adapters.netlist_comparison`. It refuses to
over-claim: `compare_netlists` returns a bare `Optional[bool]`, and the plugin
folds a solver `unknown` into that boolean, so only `True` **with**
`fail_on_unknown=True` becomes a proof — everything else becomes `unknown`,
`counterexample` or `error`. The adapter also emits the preconditions it can
check itself (sequential gate name correspondence, gate types that are neither
combinational nor sequential and therefore enter the queries as free variables).

## Reading findings back

`python tools/hal_viz report <documents> -o report.html` renders one or more of
these documents — together with the artifacts they reference — into a single
static HTML page for a human: status badges that keep the nine statuses apart,
the assumptions and bounds of every claim, relative links to evidence and
witnesses, and inlined scoped diagrams. It needs no HAL. See
`tools/hal_viz/README.md`.

## Determinism

`serialize.dumps` sorts keys, sorts every order-insensitive list
(`normalize_document`) and ends with a newline, so two runs on the same inputs
produce byte-identical files. `serialize.document_digest` hashes the canonical
form with the wall-clock fields removed, so a rerun that differs only in timing
has the same digest.

## Tests

```bash
python -m unittest discover -s tools/hal_findings -t tools -p "test_*.py"
```

The unit tests validate every shipped example with both validators, assert that
the examples cover all nine statuses and come from two different analyses, check
the negative cases (a bounded result claiming to be unbounded, a heuristic with
a formal method, a gate reference without an artifact, …), and drive both
adapters with stub objects shaped like the `hal_py` bindings.

`test_hal_findings_hal.py` skips unless a built HAL and a netlist are available:

```bash
export HAL_PY_PATH=/path/to/hal/build/lib
export HAL_FINDINGS_NETLIST=/tmp/toy_cipher/<unpacked project dir>
python -m unittest discover -s tools/hal_findings -t tools -p "test_*_hal.py"
```

## Versioning

`schema_version` is mandatory and validated. `hal_findings.schema` lists the
versions this build can read; a document with any other version is rejected
outright instead of being parsed on a best-effort basis. Add a new
`schema/findings-<version>.schema.json` and extend `SUPPORTED_SCHEMA_VERSIONS`
when the shape changes; never edit a released schema file in place.
