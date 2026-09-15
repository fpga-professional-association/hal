# hal_viz — headless visualization for HAL

`hal_viz` renders the artifacts of a HAL analysis to files you can open in a
browser or image viewer. It is a batch tool: no Qt, no interactive windows, no
event loop. It always writes a Graphviz `.dot` file, and additionally renders
an SVG/PNG/PDF when the Graphviz `dot` binary is available.

Subcommands:

| command | what it draws |
| --- | --- |
| `netlist_graph` | gate-level graph — gates as nodes, nets as edges, scoped by module or by a depth-limited gate neighborhood |
| `dag`           | the same graph *levelled*: feedback cut at every flip-flop and latch, Kahn levels laid out left to right, so combinational depth is what you read off the picture |
| `module_tree`   | the module hierarchy of a netlist, with per-module gate counts |
| `dataflow`      | the register groups recovered by the DANA `dataflow_analysis` plugin |
| `clock_tree`    | the clock tree extracted by the `clock_tree_extractor` plugin |
| `clock_step`    | one standalone, steppable HTML page: a `dag` drawing with a bounded per-cycle trace painted onto it |
| `report`        | a static HTML report over findings documents and the artifacts above |

`dataflow` and `clock_tree` deliberately reuse the DOT exporters those plugins
already ship (`dataflow.Result.write_dot` / `ClockTree.export`) instead of
reimplementing their graphs; `hal_viz` runs the analysis, collects the output
and renders it. `report` goes one step further and re-derives nothing at all:
its input is what the other commands already wrote. `clock_step` is the same
kind of command: its inputs are a `dag` SVG plus its `.dot`, and a trace
recorded elsewhere.

## Requirements

* **A built HAL.** `hal_viz` imports `hal_py`, so HAL must be built and its
  library directory reachable. See the Build Instructions in the top-level
  `README.md`. There is no pip package, and nothing here can run against a
  netlist without HAL. The exceptions are `report` and `clock_step`, which read
  JSON and files and therefore run on a plain interpreter.
* **Python 3.8+**, standard library only. No third-party Python dependencies.
* **Graphviz** (the `dot` executable), *optional*. Without it you still get the
  `.dot` files; `hal_viz` prints a warning and carries on. Install from
  <https://graphviz.org/download/>.

## Invocation

From the repository root, with HAL's library directory known:

```bash
export HAL_PY_PATH=/path/to/hal/build/lib      # or pass --hal-lib
python tools/hal_viz --help
```

Equivalent forms:

```bash
python tools/hal_viz <command> ...                      # run the directory
PYTHONPATH=tools python -m hal_viz <command> ...        # run as a module
python tools/hal_viz <command> ... --hal-lib /path/to/hal/build/lib
```

`hal_viz` prints progress to **stderr** and the path of every file it produced
to **stdout**, one per line, so it composes with shell pipelines:

```bash
python tools/hal_viz module_tree ./fsm -o out/ -q | tail -1   # the SVG path
```

From inside `hal --python-script`, import the CLI rather than shelling out —
note that HAL's Python shell does not set `__file__` and puts `--python-args`
directly into `sys.argv[0:]`, so pass the argument list explicitly:

```python
import sys
sys.path.insert(0, "/path/to/hal/tools")
from hal_viz.cli import main
main(["module_tree", "/path/to/fsm", "-o", "/tmp/out/"])
```

## Inputs

The `netlist` argument accepts any of:

* a **HAL project directory** — e.g. an unzipped `examples/*.zip`; loaded via
  `NetlistFactory.load_hal_project`
* a **`.hal` file** — carries its own gate library
* an **HDL netlist** (`.v`, `.vhd`, `.vhdl`, …) — requires
  `--gate-library <file.hgl|.lib>`

Bundled gate libraries live in `plugins/gate_libraries/definitions`, and every
`examples/*.zip` archive ships the library its netlist needs.

## Examples

All examples below use the shipped `examples/fsm.zip`, unzipped to `./fsm`:

```bash
unzip examples/fsm.zip -d .
```

### Module hierarchy

```bash
python tools/hal_viz module_tree ./fsm -o out/fsm_modules.svg
# -> out/fsm_modules.dot
# -> out/fsm_modules.svg
```

Limit how deep the tree goes (cut-off points are marked with an explicit
"… submodules hidden" node so a truncated picture never looks complete):

```bash
python tools/hal_viz module_tree ./fsm --depth 2 -o out/
```

### Gate-level graph

A full netlist is almost never renderable, so scope the view. By module:

```bash
python tools/hal_viz netlist_graph ./fsm --module top --cluster-modules \
    -o out/fsm_top.svg
```

By gate neighborhood — everything within 2 hops of a gate, in both directions,
with the nets that leave the scope drawn as dashed stubs:

```bash
python tools/hal_viz netlist_graph ./fsm --gate FSM_sequential_STATE_reg_0 \
    --depth 2 --show-boundary --pin-labels -o out/around_state_reg.svg
```

`--module` and `--gate` accept a numeric HAL id, an exact name, or an
unambiguous substring of a name. `--max-gates` (default 400) refuses to render
scopes that would produce an unreadable picture; raise it deliberately.

**Constants and the legend.** GND and VCC gates are not drawn as gates. One
constant gate drives hundreds of pins on a real netlist, and a single node with
that fan-out is a spider that hides the circuit, so each constant-driven edge
gets its own little source stub labelled `0` or `1`, pinned to the source rank;
the driving gate is named in the stub's tooltip and the count is noted in the
`.dot` comment header. `--const-hub` brings the old shared node back. Every
drawing also carries a `legend` cluster decoding the node shapes, the edge
styles and the tie-offs; `--no-legend` omits it.

For large-but-still-plausible graphs, switch layout engine:

```bash
python tools/hal_viz netlist_graph ./fsm --module top --recursive \
    --max-gates 2000 --engine sfdp --no-net-labels -o out/big.png
```

### Levelled graph (`dag`)

`netlist_graph` shows *what is connected*; `dag` shows *how deep it is*. A
gate-level netlist is a directed graph, and its combinational part is a DAG
once the feedback is cut at the registers — that is the graph every timing,
cone and dataflow argument is actually made about, and this draws it:

* every edge that **ends** at a flip-flop or latch is a *cut* edge (dashed
  red), so a register output is a source of the remaining graph and a register
  input is one of its sinks;
* levels come from Kahn's algorithm on the cut graph: level 0 is the primary
  inputs, the constant tie-offs and the register outputs, and every other gate
  sits one level behind its deepest driver;
* each level is a `rank=same` group, so with the default `rankdir=LR` level 0
  is on the far left and depth increases to the right.

```bash
python tools/hal_viz dag ./fsm --module top -o out/fsm_dag.svg --html
```

It takes exactly the scoping and shared options of `netlist_graph`
(`--module`/`--recursive`, `--gate`/`--depth`/`--direction`, `--max-gates`,
`-o/-f/--engine/--html/-g/--hal-lib/-q/--traceback`), plus `--net-labels`,
`--pin-labels`, `--no-level-labels`, `--no-legend` and `--const-hub`.

A **real combinational loop** — one that survives the cut — is neither an error
nor a reason to drop gates: the loop members are levelled anyway, drawn with a
red outline, named in a warning on stderr and repeated in the `.dot` comment
header and on the HTML page.

`--html` writes `<base>.html`: one standalone page with the SVG **inlined**, a
caption (design, scope, gate/edge/level counts) and the legend. Inline CSS, no
scripts, no request to anything outside the file, so it survives being copied
or archived on its own.

### Clock-step page (`clock_step`)

`dag` draws the structure; `clock_step` puts the values on it. It takes the SVG
`dag` already rendered, the `.dot` it was rendered from, and a per-cycle trace
(`tools/hal_agilex trace` for an Agilex export), and writes **one** HTML file:

```bash
python3 tools/hal_agilex trace export.vo --reference reference.py \
    --cycles 32 --hold en=1 -o out/dag_trace.json
python  tools/hal_viz clock_step out/fsm_dag.svg --trace out/dag_trace.json \
    -o out/dag_interactive.html
```

Inline SVG, inline CSS, inline script, inline trace: no request leaves the
page. Controls are prev/next, a cycle scrubber, play/pause, a zoom slider and
the ←/→ keys. Nets and edges are coloured by the value they carry (green 1,
grey 0, amber dotted unknown), flip-flop outputs and primary outputs get a
small `0`/`1` label, and the page states its cycle bound and the exact
stimulus that produced it.

The join needs no new attributes in the drawing: Graphviz already writes the
DOT node and edge identifiers into the SVG's `<title>` elements, and the `.dot`
carries each node's gate name. An edge whose endpoints share more than one net
is *ambiguous* — Graphviz gives both arrows the same title — and is drawn
unknown rather than assigned a value; so is a drawn gate the trace does not
mention. Both counts appear on the page.

`--name-map JSON` maps trace gate names to drawing gate names, for a
walkthrough whose `dag` is drawn on an anonymised netlist; it accepts a flat
`{"real": "anon"}` object or one with a `"gates"` member of that shape, and a
key spelled `name$b3` also answers to `name[3]` (the anonymisers split buses
into scalars). Passing a map also makes the page carry a spoiler warning,
because the trace's port names are the un-blinded ones.

Options: `--trace JSON` (required), `--dot PATH` (default: the SVG's `.dot`
sibling), `--name-map JSON`, `-o/--output`, `--title`, `--caption`, `-q`,
`--traceback`.

### Dataflow analysis (DANA)

Runs `dataflow_analysis` and renders the recovered register groups. Output
goes into a directory:

```bash
python tools/hal_viz dataflow ./fsm -o out/dataflow --html
# -> out/dataflow/graph.dot     (written by the plugin)
# -> out/dataflow/groups.txt    (written by the plugin)
# -> out/dataflow/graph.svg
# -> out/dataflow/index.html
```

Tuning is passed through to DANA's `Configuration`:

```bash
python tools/hal_viz dataflow ./toy_cipher -o out/dataflow \
    --min-group-size 4 --expected-size 8 --expected-size 32 \
    --stage-identification
```

### Clock tree

```bash
python tools/hal_viz clock_tree ./fsm -o out/fsm_clocks.svg
```

### Findings report

`report` answers a different question from the commands above: not "what does
this circuit look like" but "what did the analyses claim, how strong is each
claim, and what can I replay". Its input is the shared findings contract
(`tools/hal_findings`, one JSON document per analysis run) plus the artifacts
those documents point at — including the `.dot`/`.svg` files the other hal_viz
subcommands produced. Nothing is re-derived: the graph extraction lives in
`extract.py` and is only ever reached through `netlist_graph`/`module_tree`.

```bash
python tools/hal_viz report results/*.json -o out/report.html
python tools/hal_viz report results/run.json --artifact out/top.svg \
    --copy-evidence -o out/report.html
```

Because it never imports `hal_py`, it needs no built HAL, no netlist and no
`--hal-lib`. The page it writes is self-contained: inline CSS, inline SVG,
relative links, no script and no request to any host, so it opens from a file
manager, an artifact archive or a CI download.

What the page shows, per finding:

* the **status** as a badge that is distinct in label, glyph *and* colour, so
  the nine statuses of the schema stay apart in greyscale: `PROVEN` vs
  `PROVEN (bounded)` vs `REFUTED` vs `REFUTED (bounded)` vs `HEURISTIC` vs
  `UNKNOWN` vs `TIMEOUT` vs `ERROR` vs `UNSUPPORTED`, each with the sentence
  that says what it does and does not claim;
* a loud `BOUNDED: only up to N cycle(s)` marker whenever the claim is bounded
  — a bounded result is never allowed to read like a proof;
* the **assumptions**, with whether each was discharged by the run or merely
  assumed;
* the **scope**: the artifacts (with content hashes, or an explicit "unhashed"
  marker) and the gates/nets/modules the finding is about;
* **evidence**: relative download links for logs, traces, waveforms and
  witnesses, inlined SVG for diagrams, the exact command when one was recorded;
* **the scope, outlined in the diagram**: every Graphviz node whose title is one
  of the finding's objects (`g<id>`/`m<id>` as hal_viz names them, or the plain
  object name as other exporters do) is drawn with a red outline, and the page
  says how many were matched. A diagram with *no* match is called out — that is
  the case where the picture is not the scoped view of the finding;
* the things that are easiest to lose: truncated lists (`TRUNCATED: N further
  gate(s) not shown`), unsupported primitives, unknown/timeout/error results,
  and evidence files that are not where the document says they are
  (`artifact not found`).

Findings are ordered worst-first (refutations, then failures, then
inconclusive, then proofs), and every document is validated against the
findings schema on the way in; a document that fails is still rendered, with a
banner saying so, and `--strict` turns that into exit code 1.

Untrusted input is treated as untrusted: every netlist-derived string (gate,
net, module, design and gate-type names, summaries, assumption text) is HTML
escaped, and an embedded SVG is stripped of scripts, event handlers and any
`href` that is not a local fragment before it goes into the page.

| option | meaning |
| --- | --- |
| `-o, --output PATH` | HTML file to write; a trailing separator means `findings_report.html` in that directory. |
| `--title TEXT` | report title. |
| `--artifact PATH` | attach a hal_viz output no finding references (repeatable). |
| `--no-embed` | link diagrams instead of inlining them. |
| `--max-embed-bytes N` | do not inline an SVG bigger than this; link it with a visible marker (default 4 MiB). |
| `--max-items N` | rows per list before an explicit truncation marker (default 25). |
| `--render-dot {auto,always,never}` | `auto` renders a `.dot` only when it has no `.svg` sibling. Renders into a scratch directory, so nothing is written next to your artifacts. |
| `--copy-evidence` | copy referenced files into `<report>_evidence/` so the directory can be moved or archived. |
| `--strict` | exit 1 when a document fails findings-schema validation. |
| `--engine`, `--dot-binary`, `--render-timeout` | as above, for `.dot` evidence only. |

Relation to the rest of the toolchain:

* **Diagrams** come from `netlist_graph`, `module_tree`, `dataflow` and
  `clock_tree`. Scope a picture there (`--gate ... --depth ...`), record its
  path as evidence in the findings document, and `report` embeds exactly that
  picture — which is why the scope in the text and the scope in the drawing
  cannot drift apart.
* **Waveforms** are linked for download, not rendered. HAL's simulator writes
  VCD; Saleae Logic 2 (driven through the `logic2` MCP tools, see
  `.claude/skills/using-hal`) is for *captured* traces and is not assumed to
  accept simulation output. A `waveform` or `trace` evidence entry therefore
  becomes a download link, and choosing a viewer stays a human decision.

## Options shared by every netlist subcommand

These apply to `netlist_graph`, `dag`, `module_tree`, `dataflow` and
`clock_tree`; `report` takes no netlist and no `--hal-lib` and has its own
option table above.

| option | meaning |
| --- | --- |
| `-o, --output PATH` | base name for the outputs; a trailing `/` (or an existing directory) means "put the default file name in here". A known extension (`.svg`/`.png`/`.pdf`) also selects the format. |
| `-f, --format {svg,png,pdf,none}` | rendered format; `none` writes only the `.dot`. Default `svg`. |
| `--engine {dot,neato,fdp,sfdp,circo,twopi,osage}` | Graphviz layout, passed as `dot -K<engine>`. Only the `dot` binary is needed. |
| `--dot-binary PATH` | explicit path to `dot`; otherwise `$HAL_VIZ_DOT`, otherwise `PATH`. |
| `--render-timeout SECONDS` | abort a runaway layout (default 600). |
| `--html` | also write an `index.html` next to the outputs that embeds the SVG/PNG. `dag` instead writes `<base>.html`, a standalone page with the SVG inlined. |
| `-g, --gate-library FILE` | gate library for HDL netlists. |
| `--hal-lib DIR` | directory containing `hal_py` (repeatable; also `$HAL_PY_PATH`). |
| `-q, --quiet` | suppress progress messages on stderr. |
| `--traceback` | show the full Python traceback instead of a one-line error. |

## Exit codes

`0` success, `1` a handled error (no `hal_py`, netlist would not load, scope
too large, Graphviz failed, a findings document could not be read, or
`report --strict` saw an invalid document), `2` bad command line, `130`
interrupted.

A missing `dot` binary is **not** an error: the `.dot` file is the primary
artifact and is always written first.

## Environment variables

| variable | meaning |
| --- | --- |
| `HAL_PY_PATH` | `os.pathsep`-separated directories prepended to `sys.path` before importing `hal_py`. |
| `HAL_VIZ_DOT` | full path to the Graphviz `dot` executable. |

## Layout and tests

The package is split so that everything except `halenv.py` is importable
without a built HAL:

```
tools/hal_viz/
  dot.py       Graphviz DOT emission: quoting, escaping, clusters. Pure stdlib.
  levels.py    Kahn levelling + SCC detection on plain node keys. Pure stdlib.
  extract.py   netlist objects -> DotGraph. Duck-typed; never imports hal_py.
  render.py    finds and drives the `dot` binary; writes the HTML index and
               the standalone single-diagram page.
  report.py    findings documents + artifacts -> one static HTML page. Pure stdlib.
  clockstep.py a dag .dot/.svg + a per-cycle trace -> one interactive page. Pure stdlib.
  halenv.py    the only module that imports hal_py / hal_plugins.
  cli.py       argument parsing and the subcommands.
```

Because `dot.py`, `extract.py`, `report.py` and `clockstep.py` never touch
`hal_py`, the formatting, escaping, graph-building, reporting and value-join
logic is unit tested with stub objects, synthetic findings and a synthetic
trace on any machine:

```bash
python tools/hal_viz/test_hal_viz.py
python tools/hal_viz/test_hal_viz_report.py
python tools/hal_viz/test_hal_viz_clockstep.py
# or all of them at once
python -m unittest discover -s tools/hal_viz -t tools -p "test_*.py"
```

The report tests cover what the page is trusted for: that hostile netlist names
cannot break out of the HTML, that all nine statuses render and stay
distinguishable, that missing artifacts, truncated lists, bounded claims and
coverage gaps produce visible markers, and that the page contains no script and
no external reference. The clock-step tests cover the join: which node and
which arrow gets which net, that a tie-off carries its constant, that an
unresolvable gate or an ambiguous arrow is reported rather than guessed, and
that the emitted page embeds the trace, carries its controls and fetches
nothing. These tests do **not** exercise `hal_py` itself. `ctest` runs them as
`runTest-hal_viz_standalone`, registered in `tests/headless_smoke/`.

The end-to-end path — real bindings, a real plugin, a real netlist — is covered
by `tests/headless_smoke/real_netlist_smoke.py`, which needs a built HAL. It
unpacks `examples/uart.zip` (407 gates, ships the `example_library.hgl` it
needs), loads it with `NetlistFactory.load_hal_project`, runs the
`graph_algorithm` plugin, round-trips the project through `ProjectManager`,
drives `netlist_graph` and `module_tree` here and checks the emitted DOT and SVG
by exact node ids, and finally writes a findings document about that same
seven-gate cone and renders it with `report`, checking that the finding, the
embedded diagram and the witness link agree on the scope:

```bash
HAL_BASE_PATH=<build> PYTHONPATH=<build>/lib \
    python3 tests/headless_smoke/real_netlist_smoke.py
```

It never skips a check quietly: a missing `hal_py`, plugin or binding is a
failure. Only the optional Graphviz binary is allowed to be absent, and
`--require-graphviz` (which CI passes) takes that away too.
