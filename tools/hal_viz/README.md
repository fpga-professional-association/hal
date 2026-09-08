# hal_viz — headless visualization for HAL

`hal_viz` renders the artifacts of a HAL analysis to files you can open in a
browser or image viewer. It is a batch tool: no Qt, no interactive windows, no
event loop. It always writes a Graphviz `.dot` file, and additionally renders
an SVG/PNG/PDF when the Graphviz `dot` binary is available.

Subcommands:

| command | what it draws |
| --- | --- |
| `netlist_graph` | gate-level graph — gates as nodes, nets as edges, scoped by module or by a depth-limited gate neighborhood |
| `module_tree`   | the module hierarchy of a netlist, with per-module gate counts |
| `dataflow`      | the register groups recovered by the DANA `dataflow_analysis` plugin |
| `clock_tree`    | the clock tree extracted by the `clock_tree_extractor` plugin |

`dataflow` and `clock_tree` deliberately reuse the DOT exporters those plugins
already ship (`dataflow.Result.write_dot` / `ClockTree.export`) instead of
reimplementing their graphs; `hal_viz` runs the analysis, collects the output
and renders it.

## Requirements

* **A built HAL.** `hal_viz` imports `hal_py`, so HAL must be built and its
  library directory reachable. See the Build Instructions in the top-level
  `README.md`. There is no pip package, and nothing here can run against a
  netlist without HAL.
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

For large-but-still-plausible graphs, switch layout engine:

```bash
python tools/hal_viz netlist_graph ./fsm --module top --recursive \
    --max-gates 2000 --engine sfdp --no-net-labels -o out/big.png
```

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

## Options shared by every subcommand

| option | meaning |
| --- | --- |
| `-o, --output PATH` | base name for the outputs; a trailing `/` (or an existing directory) means "put the default file name in here". A known extension (`.svg`/`.png`/`.pdf`) also selects the format. |
| `-f, --format {svg,png,pdf,none}` | rendered format; `none` writes only the `.dot`. Default `svg`. |
| `--engine {dot,neato,fdp,sfdp,circo,twopi,osage}` | Graphviz layout, passed as `dot -K<engine>`. Only the `dot` binary is needed. |
| `--dot-binary PATH` | explicit path to `dot`; otherwise `$HAL_VIZ_DOT`, otherwise `PATH`. |
| `--render-timeout SECONDS` | abort a runaway layout (default 600). |
| `--html` | also write an `index.html` next to the outputs that embeds the SVG/PNG. |
| `-g, --gate-library FILE` | gate library for HDL netlists. |
| `--hal-lib DIR` | directory containing `hal_py` (repeatable; also `$HAL_PY_PATH`). |
| `-q, --quiet` | suppress progress messages on stderr. |
| `--traceback` | show the full Python traceback instead of a one-line error. |

## Exit codes

`0` success, `1` a handled error (no `hal_py`, netlist would not load, scope
too large, Graphviz failed), `2` bad command line, `130` interrupted.

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
  extract.py   netlist objects -> DotGraph. Duck-typed; never imports hal_py.
  render.py    finds and drives the `dot` binary; writes the HTML index.
  halenv.py    the only module that imports hal_py / hal_plugins.
  cli.py       argument parsing and the subcommands.
```

Because `dot.py` and `extract.py` never touch `hal_py`, the formatting,
escaping and graph-building logic is unit tested with stub objects on any
machine:

```bash
python tools/hal_viz/test_hal_viz.py
# or
python -m unittest discover -s tools/hal_viz -t tools -p "test_*.py"
```

These tests do **not** exercise `hal_py` itself; verifying the end-to-end path
requires a built HAL and one of the `examples/` archives.
