---
name: hal-viz
description: Render a netlist to Graphviz diagrams (module tree, gate-level graph, topologically levelled DAG, DANA dataflow groups, clock tree), build a static HTML findings report, or turn a levelled DAG plus a hal_agilex trace into an interactive clock-step page. Use when an agent needs a picture of a design, a scoped close-up around a gate, a left-to-right view of combinational depth, a human-readable page over hal_findings documents, or to watch values propagate cycle by cycle.
---

# hal_viz — headless visualization for HAL

## When to use
- You need a diagram of a netlist (module hierarchy, gate-level graph, levelled
  DAG, DANA register groups, clock tree) and cannot open a GUI — this repo has
  none.
- You need to *show* combinational depth, or that the design is a DAG once the
  feedback is cut at the registers: that is `dag`, not `netlist_graph`.
- You need one static HTML page summarizing one or more `hal_findings`
  documents, with the claimed scope outlined on the diagrams that back it.
- You need to *watch* values move through the graph rather than read a
  waveform: that is `clock_step`, over an existing `dag` SVG plus a
  `tools/hal_agilex trace` JSON.
- `report` and `clock_step` need no HAL build; every other subcommand needs
  `hal_py`.

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

# levelled view: feedback cut at the flops, combinational depth left to right,
# plus a standalone HTML page with the SVG inlined
python tools/hal_viz dag ./fsm --module top -o out/top_dag.svg --html

# DANA dataflow groups -- output is a directory, and -f svg is required
python tools/hal_viz dataflow ./fsm -o out/dataflow -f svg --html

# clock tree
python tools/hal_viz clock_tree ./fsm -o out/fsm_clocks.svg

# the levelled view again, but steppable: values from a bounded hal_agilex
# trace painted onto the SVG that dag already wrote.  No HAL, no Graphviz.
python3 tools/hal_agilex trace export.vo --reference reference.py \
    --cycles 32 --hold en=1 -o out/dag_trace.json
python tools/hal_viz clock_step out/top_dag.svg --trace out/dag_trace.json \
    -o out/dag_interactive.html

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
| `dag` | the same graph levelled: feedback cut at every FF/latch, Kahn levels as `rank=same` columns, level 0 (primary inputs, tie-offs, register outputs) on the far left | same scoping and shared options as `netlist_graph`; `--net-labels`, `--pin-labels`, `--no-level-labels`; a real combinational loop is highlighted and warned about, never dropped; `--html` writes a standalone `<base>.html` with the SVG inlined |
| `module_tree` | module hierarchy, per-module gate counts | `--depth N`, `--no-gate-counts` |
| `dataflow` | DANA register groups | writes a **directory**: `graph.dot`/`groups.txt` (from DANA) plus `graph.svg`/`index.html`; tuning via `--min-group-size`, `--expected-size` (repeatable), `--stage-identification`, `--type-consistency` |
| `clock_tree` | clock tree from `clock_tree_extractor` | same shared options as above |
| `clock_step` | one standalone HTML page that steps a `dag` drawing a clock at a time, colouring every net by its value | takes the `dag` **SVG** plus (by default) its `.dot` sibling and `--trace` from `hal_agilex trace`; `--name-map` for a drawing of an anonymised netlist; no netlist, no HAL, no Graphviz |
| `report` | static HTML over `hal_findings` documents + artifacts | no netlist, no `--hal-lib`; see options below |

Shared options (`netlist_graph`/`dag`/`module_tree`/`dataflow`/`clock_tree`):

| option | meaning |
| --- | --- |
| `-o, --output PATH` | base name; trailing `/` or an existing dir means "default name in here" |
| `-f, --format {svg,png,pdf,none}` | default `svg`; `none` writes only `.dot` |
| `--engine {dot,neato,fdp,sfdp,circo,twopi,osage}` | Graphviz layout |
| `--html` | also write `index.html` embedding the image (`dag`: a standalone `<base>.html` with the SVG inlined, caption and legend) |
| `-g, --gate-library FILE` | required for HDL (`.v`/`.vhd`) netlists |
| `--hal-lib DIR` | repeatable; or `$HAL_PY_PATH` |
| `-q` / `--traceback` | quiet progress / full Python traceback on error |

Both graph commands also take `--no-legend` and `--const-hub` (see Pitfalls).

`clock_step`-only options: `--trace JSON` (required), `--dot PATH` (defaults to
the SVG's `.dot` sibling), `--name-map JSON`, `-o/--output`, `--title`,
`--caption`.

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
- **GND/VCC are not drawn as gates** in `netlist_graph` or `dag`. Each
  constant-driven edge gets its own `0`/`1` stub on the source rank, because one
  shared hub node with a real netlist's constant fan-out is a spider that hides
  the circuit. So don't look for a `GND_inst` node in the `.dot` — look for
  `tie0_*`/`tie1_*` (the driving gate is in the stub's tooltip, the count in the
  `.dot` comment header). `--const-hub` restores the old single node.
- **Per-pin tie-off stubs stop scaling somewhere around 1000 of them.** They are
  the right default on the sizes walkthroughs 01-10 use (48 gates, 385 stubs).
  On `11_speck_toy` (241 instances, **1514** constant-driven pins) the default
  `dag` is a level-0 column of 1514 circles, 1.7 MB of SVG and roughly eight
  minutes of Graphviz; the same graph with `--const-hub` is 640 KB and five
  seconds, with the same level count. Check the constant fan-out before
  rendering a design of that size, and say in the caption which spelling you
  used — the two disagree on the edge count (413 versus 936 there), because the
  hub draws a flip-flop's constant control pins once instead of per pin.
  `12_present_sbox` is the other one, for the same reason at 2292 stubs.
- Every drawing carries a `cluster_legend` whose node ids all start with
  `legend`. If you parse an emitted `.dot`, filter those out before counting
  gates — `tests/headless_smoke/real_netlist_smoke.py` shows the pattern.
- `dag` levels are computed on the **cut** graph, so a flip-flop is always at
  level 0 and the edge into it is dashed. A level count is therefore the
  combinational depth between registers, not a path length through the design.
- `clock_step` joins the drawing to the trace through the **Graphviz `<title>`
  elements** the SVG already carries (`g12`, `tie0_7`, `g4->g10`) plus the gate
  name on the first line of each node's label in the `.dot`. That is why it
  needs *both* files and why the `dag` emitter needed no new ids. It also means
  the `.dot` you pass has to be the one that SVG was rendered from; a
  regenerated `.dot` with different gate ids silently binds nothing, and
  `clock_step` then refuses rather than drawing an all-unknown page.
- A walkthrough whose `dag` is drawn on an **anonymised** netlist (02, 03, 10)
  needs `--name-map` with the committed `{real: anon}` map, otherwise nothing
  binds. Passing a map also makes the page print a spoiler warning, because the
  trace's port names are the un-blinded ones.
- Two arrows between the same pair of gates carry the same Graphviz title, so
  when a driver and a sink share more than one net `clock_step` marks the arrow
  **unknown** instead of picking one. The count is on the page and in the
  `[hal_viz]` log line; on the eight Agilex walkthroughs it is zero.
- `netlist_graph --depth 2` can swallow a small design whole. On
  `08_shift_debouncer` (16 gates total), depth 2 around any flop *is* the whole
  netlist — use `--depth 1` for an actual close-up (7 gates) on designs that
  size. Check gate count first; don't assume depth 2 is always "a close-up".

## Where things live
- Tool: `tools/hal_viz/` — `dot.py` (DOT emission, pure stdlib), `levels.py`
  (Kahn levelling + SCC detection on plain keys, pure stdlib), `extract.py`
  (netlist → DotGraph, duck-typed), `render.py` (drives `dot`, writes the
  standalone page), `report.py` (findings → HTML, pure stdlib),
  `clockstep.py` (dag + trace → interactive HTML, pure stdlib), `halenv.py`
  (the only module importing `hal_py`), `cli.py`.
- Unit tests (no HAL): `tools/hal_viz/test_hal_viz.py`,
  `test_hal_viz_report.py`, `test_hal_viz_clockstep.py`, or
  `python -m unittest discover -s tools/hal_viz -t tools -p "test_*.py"`
  (registered with ctest as `runTest-hal_viz_standalone`).
- End-to-end (needs a built HAL): `tests/headless_smoke/real_netlist_smoke.py`.
- Real usage in context: every `examples/agilex3_walkthroughs/*/run_analysis*.sh`
  and `run_all.sh` runs `module_tree` + `netlist_graph` first, then `dag` and
  `clock_step`, then `dataflow`/`clock_tree`, then `report` last over the
  walkthrough's `artifacts/*.findings.json`. All eight walkthroughs ship
  `images/dag_interactive.html` and the `artifacts/dag_trace.json` it was
  built from; each `check.py` re-runs the exporter and requires the committed
  trace back unchanged.
