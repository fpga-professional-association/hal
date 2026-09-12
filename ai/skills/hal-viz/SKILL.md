---
name: hal-viz
description: Render a netlist to Graphviz diagrams (module tree, gate-level graph, DANA dataflow groups, clock tree) or build a static HTML findings report. Use when an agent needs a picture of a design, a scoped close-up around a gate, or a human-readable page over hal_findings documents.
---

# hal_viz — headless visualization for HAL

## When to use
- You need a diagram of a netlist (module hierarchy, gate-level graph, DANA
  register groups, clock tree) and cannot open a GUI — this repo has none.
- You need one static HTML page summarizing one or more `hal_findings`
  documents, with the claimed scope outlined on the diagrams that back it.
- `report` needs no HAL build; every other subcommand needs `hal_py`.

## Quickstart
```bash
# unzip a bundled example once
unzip examples/fsm.zip -d .

# module hierarchy
HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib PYTHONPATH=/work/build/lib \
    python tools/hal_viz module_tree ./fsm -o out/fsm_modules.svg

# gate-level graph, scoped to one module, boundary nets shown as dashed stubs
python tools/hal_viz netlist_graph ./fsm --module top --show-boundary -o out/top.svg

# close-up: everything within 1 hop of a gate (use --depth 1 on small designs;
# see Pitfalls)
python tools/hal_viz netlist_graph ./fsm --gate FSM_sequential_STATE_reg_0 \
    --depth 1 --show-boundary --pin-labels -o out/around_state_reg.svg

# DANA dataflow groups -- output is a directory, and -f svg is required
python tools/hal_viz dataflow ./fsm -o out/dataflow -f svg --html

# clock tree
python tools/hal_viz clock_tree ./fsm -o out/fsm_clocks.svg

# findings report -- pure stdlib, no HAL, no --hal-lib needed
python tools/hal_viz report results/*.json --artifact out/top.svg -o out/report.html
```
Capture output cleanly (HAL's native log lines land on stdout, not stderr):
```bash
python tools/hal_viz module_tree ./fsm -o out/fsm_modules.svg 2>&1 \
    | grep -vE "\[info\]|\[warning\]"
```

## The commands that matter

| command | draws | notes |
| --- | --- | --- |
| `netlist_graph` | gates as nodes, nets as edges | scope with `--module NAME[/ID]` (+`--cluster-modules`/`--recursive`) or `--gate NAME` +`--depth N` `--direction {both,successors,predecessors}`; `--max-gates` (400) refuses an unreadable render |
| `module_tree` | module hierarchy, per-module gate counts | `--depth N`, `--no-gate-counts` |
| `dataflow` | DANA register groups | writes a **directory**: `graph.dot`/`groups.txt` (from DANA) plus `graph.svg`/`index.html`; tuning via `--min-group-size`, `--expected-size` (repeatable), `--stage-identification`, `--type-consistency` |
| `clock_tree` | clock tree from `clock_tree_extractor` | same shared options as above |
| `report` | static HTML over `hal_findings` documents + artifacts | no netlist, no `--hal-lib`; see options below |

Shared options (`netlist_graph`/`module_tree`/`dataflow`/`clock_tree`):

| option | meaning |
| --- | --- |
| `-o, --output PATH` | base name; trailing `/` or an existing dir means "default name in here" |
| `-f, --format {svg,png,pdf,none}` | default `svg`; `none` writes only `.dot` |
| `--engine {dot,neato,fdp,sfdp,circo,twopi,osage}` | Graphviz layout |
| `--html` | also write `index.html` embedding the image |
| `-g, --gate-library FILE` | required for HDL (`.v`/`.vhd`) netlists |
| `--hal-lib DIR` | repeatable; or `$HAL_PY_PATH` |
| `-q` / `--traceback` | quiet progress / full Python traceback on error |

`report`-only options: `--title`, `--artifact PATH` (repeatable, attach an
output no finding references), `--no-embed`, `--max-embed-bytes`,
`--max-items`, `--render-dot {auto,always,never}`, `--copy-evidence`,
`--strict` (exit 1 on schema-invalid input).

## Pitfalls
- `dataflow` writes a **directory**, not a single file — every other
  subcommand writes one file (plus its `.dot`). Point `-o` at a directory.
- Before commit `8585b23d7`, omitting `--format` on `dataflow` crashed with a
  `TypeError` *after* DANA had already run and written its output (the command
  built `"graph." + args.format` with `args.format is None`). It is fixed now —
  `--format` defaults to `svg` like every other subcommand. Don't cargo-cult a
  `-f svg` workaround into new code on the assumption it's still required; it's
  just the (now-correct) default.
- HAL's native log lines print to **stdout**, not stderr. If you're capturing
  the emitted file path from stdout (per the README's pipeline pattern), filter
  first: `grep -vE "\[info\]|\[warning\]"`.
- `netlist_graph --depth 2` can swallow a small design whole. On
  `08_shift_debouncer` (16 gates total), depth 2 around any flop *is* the whole
  netlist — use `--depth 1` for an actual close-up (7 gates) on designs that
  size. Check gate count first; don't assume depth 2 is always "a close-up".

## Where things live
- Tool: `tools/hal_viz/` — `dot.py` (DOT emission, pure stdlib), `extract.py`
  (netlist → DotGraph, duck-typed), `render.py` (drives `dot`), `report.py`
  (findings → HTML, pure stdlib), `halenv.py` (the only module importing
  `hal_py`), `cli.py`.
- Unit tests (no HAL): `tools/hal_viz/test_hal_viz.py`,
  `test_hal_viz_report.py`, or
  `python -m unittest discover -s tools/hal_viz -t tools -p "test_*.py"`.
- End-to-end (needs a built HAL): `tests/headless_smoke/real_netlist_smoke.py`.
- Real usage in context: every `examples/agilex3_walkthroughs/*/run_analysis*.sh`
  and `run_all.sh` runs `module_tree` + `netlist_graph` first, then
  `dataflow`/`clock_tree`, then `report` last over the walkthrough's
  `artifacts/*.findings.json`.
