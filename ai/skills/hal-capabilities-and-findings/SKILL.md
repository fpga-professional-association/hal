---
name: hal-capabilities-and-findings
description: Check whether a plugin can run on a given netlist before trusting its output (hal_capabilities), and validate/read the JSON documents analyses emit about what they proved, refuted, or couldn't (hal_findings). Use before treating "the analysis found nothing" as "there's nothing there".
---

# hal_capabilities + hal_findings — know what a plugin can say, and what its answer means

## When to use
- Before running or trusting a plugin: does it exist, is it built, does it
  load, and can it say anything about *this* netlist — `dataflow_analysis`
  needs sequential gates, `xilinx_toolbox` needs a Xilinx library, `hawkeye`
  needs `graph_algorithm` loaded first.
- Before treating an analysis result as a proof: every result should be a
  `hal_findings` document, and the status vocabulary is what stands between
  "proven" and "heuristic guess" in a report.
- Building a new plugin: its `capabilities.json` and any findings it emits
  both get validated the same way CI validates them.

## Quickstart
```bash
# capabilities: four questions, not one
python tools/hal_capabilities list                          # declared, source tree only
python tools/hal_capabilities --build-dir /work/build list --probe
python tools/hal_capabilities --build-dir /work/build --json list --probe --netlist ./fsm
python tools/hal_capabilities show example_analysis
python tools/hal_capabilities check example_analysis --netlist ./fsm
python tools/hal_capabilities validate                      # every plugins/*/capabilities.json

# findings: validate and summarize what an analysis wrote
python tools/hal_findings validate results/*.json
python tools/hal_findings summary --strict results/dataflow.json
python tools/hal_findings normalize results/dataflow.json --in-place

# render findings for a human -- see the hal-viz skill
python tools/hal_viz report results/*.json -o out/report.html
```

## The commands that matter

`hal_capabilities` states: `declared` (ships `capabilities.json`, no HAL
needed) → `built` (`.so` in `<build>/lib/hal_plugins/`, needs `--build-dir`)
→ `loadable` (`plugin_manager` instantiated it, needs `--probe` + built HAL)
→ `supported`/`partial`/`unsupported` (can it say something about *this*
netlist, needs `--netlist`; `partial` = it ran but some matched gate types are
missing something it needs).

| command | does |
| --- | --- |
| `list [--probe] [--build-dir D] [--netlist N] [--gate-library F] [--json]` | the four-state table |
| `show PLUGIN` | print one plugin's declaration |
| `check PLUGIN --netlist N [--gate-library F]` | supported/partial/unsupported for plugin+netlist |
| `validate [FILES...] [--jsonschema]` | schema + closed-vocabulary check of `capabilities.json` |
| `schema --path` | print the capability schema's path |

Exit codes: `0` supported/partial, `1` invalid declaration/failure, `3`
unsupported for this netlist, `4` a declared dependency missing or unloadable.

`hal_findings` commands: `validate FILES... [--jsonschema]` (exit 1 on any
problem), `summary [--strict] FILE` (`--strict` exits 1 on
counterexample/error), `normalize FILE [--in-place]` (canonical deterministic
JSON), `schema --path`.

Status vocabulary (the whole point of the contract):

| status | means |
| --- | --- |
| `proven_under_assumptions` | holds for every execution given `assumptions`; requires `bounds.unbounded: true`, forbids `cycle_bound` |
| `proven_bounded` | holds **only** up to `bounds.cycle_bound` — never re-readable as a full proof |
| `counterexample` / `bounded_counterexample` | refuted (unbounded / within a cycle bound) |
| `heuristic` | evidence-based guess; may not use a `formal` method or claim unbounded validity |
| `unknown` | solver returned unknown, no verdict |
| `timeout` | aborted at a resource limit (must carry `limits`) |
| `error` | the analysis itself failed (must carry the error) — says nothing about the design |
| `unsupported` | outside coverage; `kind: "primitive"` must name the gate type(s) |

## Writing findings from Python
```python
import sys; sys.path.insert(0, "tools")
from hal_findings import serialize, validate
from hal_findings.adapters import dataflow as adapter

result = dataflow.analyze(dataflow.Configuration(netlist).with_flip_flops())
document = adapter.build_document(result, netlist_path="designs/toy_cipher.v")
validate.validate_document(document)
serialize.write_document(document, "results/dataflow.json")
```
`adapters.netlist_comparison` only emits a proof when `compare_netlists`
returned `True` *and* `fail_on_unknown=True` was set; a solver `unknown`
becomes finding status `unknown`, never a proof.

## Pitfalls
- `hal_capabilities --json` is guaranteed clean on stdout (fd-guard since
  `93ef475a4`) — don't add your own stderr-redirect workaround; parse stdout
  directly.
- `--probe --netlist X` answers "will this plugin work on *this* design", not
  "is it built" — `built`+`loadable` can still be `unsupported`. Don't confuse the two.
- `jsonschema` (PyPI) is optional; when absent or `<4`, both tools fall back
  to the built-in `jsonschema_mini` validator — `--jsonschema` only
  cross-checks when the library is actually installed.
- A `capabilities_version`/`schema_version` outside
  `SUPPORTED_CAPABILITIES_VERSIONS`/`SUPPORTED_SCHEMA_VERSIONS` is rejected
  outright, not parsed best-effort — don't hand-edit a version string to make
  an old document pass.

## Where things live
- `tools/hal_capabilities/` — `schema/plugin-capabilities-1.0.0.schema.json`,
  `validate.py`, `support.py` (declaration × netlist → verdict), `discover.py`
  (declared/built/loadable + drift detection), `cli.py`. Declarations live at
  `plugins/<name>/capabilities.json` (from `tools/new_plugin.py`), compiled
  into the plugin's `.so`, copied to `<build>/share/hal/plugin_capabilities/`.
- `tools/hal_findings/` — `schema/findings-1.0.0.schema.json`, `model.py`
  (refuses contradictory findings), `serialize.py` (deterministic JSON +
  digests), `validate.py`, `jsonschema_mini.py`, `adapters/dataflow.py`,
  `adapters/netlist_comparison.py`, `examples/` (one doc per status).
- Tests: `python -m unittest discover -s tools/hal_capabilities -t tools -p "test_*.py"`
  and the same for `hal_findings` (stdlib-only, no HAL);
  `tests/headless_smoke/plugin_scaffold_smoke.py` covers compiled-in
  declarations matching the source tree.
- See also `documentation/plugin_development.md` and the `hal-viz` skill's
  `report` command (renders findings into static HTML).
