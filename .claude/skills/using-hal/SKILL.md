---
name: using-hal
description: Drive headless HAL (this repo's netlist reverse-engineering framework) via hal_py, the hal CLI, or hal --python-script, when the task is to load, inspect, traverse, modify, or visualize a netlist/gate-level circuit.
---

# Using headless HAL

This fork of [emsec/hal](https://github.com/emsec/hal) has the Qt GUI and every
GUI-only plugin removed. There is no windowed application. HAL is driven three
ways, all built on the same `hal_py` Python bindings:

1. The `hal` CLI binary (netlist import/export, running plugin CLI extensions).
2. `hal --python` — an interactive Python shell with `hal_py` preloaded.
3. `hal --python-script my_script.py [--python-args "..."]` — batch/agent-friendly, no interaction.

Everything below is verified against the source in this repo (`app/main.cpp`,
`plugins/python_shell/src/plugin_python_shell.cpp`,
`src/python_bindings/bindings/*.cpp`). Don't invent API names — grep the
relevant `src/python_bindings/bindings/<thing>.cpp` file if you need something
not covered here; every binding has a docstring.

## Building (Linux/macOS only)

`install_dependencies.sh` has no Windows branch (only `linux`/`macOS`/Ubuntu
apt / Arch yay / RHEL paths). On Windows, build inside WSL2 or the repo's
Docker setup (`docker-compose run --rm hal-build`).

```bash
./install_dependencies.sh
mkdir build && cd build
cmake -G Ninja .. -DCMAKE_BUILD_TYPE=Release -DBUILD_ALL_PLUGINS=ON
ninja
```

`BUILD_ALL_PLUGINS` defaults `OFF` — pass it (as CI does) if you need plugins
beyond the CMake-default set. `hal_py`'s shared object and the `hal` binary
land under `build/lib` and `build/bin` respectively (`setup_output_directories()`
in `cmake/hal_cmake_tools.cmake`).

**`hal_py` is not on `sys.path` unless something puts it there.** Inside
`hal --python` / `hal --python-script`, the plugin does
`sys.path.append(<library dir>)` and `from hal_py import *` for you
automatically — see Pitfalls below. Outside of that, any Python interpreter
needs `build/lib` added explicitly:

```bash
export PYTHONPATH="/path/to/hal/build/lib:$PYTHONPATH"
python3 -c "import hal_py"
```

## The `hal` CLI

Run `hal --help` for the full list (plugins add their own options at runtime).
Core flags, from `app/main.cpp`:

| flag | meaning |
| --- | --- |
| `-i, --import-netlist <file>` | import a netlist (HDL, `.hal`, ...) into a new project |
| `-p, --project-dir <dir>` | open an existing HAL project directory |
| `-gl, --gate-library <file>` | gate library to use (`.hgl`/`.lib`) — required when importing HDL |
| `-gls, --gate-library-search-list <list>` | ordered, comma-separated gate library files or directories for netlists whose cells come from several libraries; earlier entries win a gate type name collision, and it takes precedence over `--gate-library` |
| `--black-box-fallback` | import cells that no gate library defines as black boxes (pins taken from the instantiation, all `inout`, no function) instead of aborting — off by default |
| `-e, --empty-project <dir>` | create an empty project (requires `--gate-library`, excludes import/project-dir) |
| `--volatile-mode` | don't write a `.hal` project file back to disk |
| `--no-log` | don't create a `.log` file |
| `--write-hdl <file>` | write the (possibly modified) netlist to VHDL/Verilog on exit |
| `-l, --logfile <file>`, `--log-time` | logging controls |
| `-v, --version`, `-h, --help`, `--licenses` | info flags |

`hal --python` and `hal --python-script <file> [--python-args "arg1 arg2"]`
come from the `python_shell` plugin's CLI extension
(`plugins/python_shell/src/plugin_python_shell.cpp`). These are **UI-plugin
flags**: main.cpp hands control to the plugin instead of running the CLI
plugins, but only *after* the project arguments have been dealt with.

Example: convert a Verilog netlist into a HAL project directory non-interactively:

```bash
hal --import-netlist design.v --gate-library plugins/gate_libraries/definitions/xilinx_unisim.hgl --project-dir ./out_project
```

### `--python-script` with a project: `netlist` is already loaded

`--python-script` composes with `--project-dir`, `--import-netlist`,
`--gate-library` and `--empty-project`. When any of those is given, HAL opens
the project and parses the netlist first, exactly as on the plain CLI path, and
binds the result to the global name **`netlist`** in the namespace the script
runs in — the name the GUI console used to provide. The same holds for the
interactive `hal --python`.

```bash
# import, then analyze in one process -- the script does not load anything itself
hal --import-netlist design.v \
    --gate-library plugins/gate_libraries/definitions/xilinx_unisim.hgl \
    --project-dir ./out_project \
    --python-script analyze.py

# reopen the project later
hal --project-dir ./out_project --python-script analyze.py --python-args "arg1 arg2"
```

```python
# analyze.py -- no NetlistFactory call needed
print(netlist.get_design_name(), len(netlist.get_gates()))
```

Semantics worth knowing:

- **No project arguments, no `netlist`.** A bare `hal --python-script x.py`
  gets a plain interpreter with `hal_py` imported and nothing loaded; the
  script must load whatever it needs itself. Guard with
  `if "netlist" not in globals(): ...` if a script has to work both ways.
- **The script's changes are saved.** After the script returns, HAL serializes
  the project just as it does for a CLI plugin, and `--write-hdl` still writes
  the netlist out. Pass `--volatile-mode` to run against the project without
  writing anything back.
- **Loading failures never reach the script.** A project directory that cannot
  be opened, or a netlist that cannot be read or parsed, ends the process with
  a nonzero exit code before the interpreter starts.
- **The netlist is borrowed, not owned.** HAL frees it after the script ends;
  don't stash it in something that outlives the run.

A chip-top netlist whose cells come from several libraries (standard cells, RAM
macros, pads) is imported against all of them at once, and unmapped cells can be
degraded to black boxes rather than failing the import. Note that such a netlist
cannot be written to and read back from a `.hal` file yet — re-import it with the
same options instead.

```bash
hal --import-netlist chip_top.v \
    --gate-library-search-list stdcells.lib,macros/ram.lib,pads.lib \
    --black-box-fallback --project-dir ./out_project
```

## Python API essentials

Inside `hal --python` / `--python-script`, `hal_py` is already imported.
Elsewhere: `import hal_py` after fixing `sys.path` (see Building).

### Load a netlist

```python
# an unzipped examples/*.zip HAL project directory
netlist = hal_py.NetlistFactory.load_hal_project("<path/to/unzipped/project>")

# a raw HDL/.hal file, needs a gate library for HDL
netlist = hal_py.NetlistFactory.load_netlist("<path/to/netlist>", "<path/to/gate_library>")

# a .hal file already carries its gate library path
netlist = hal_py.NetlistFactory.load_netlist("<path/to/design.hal>")

# cells from several gate libraries, unmapped ones degraded to black boxes;
# earlier entries of the list win a gate type name collision
netlist = hal_py.NetlistFactory.load_netlist(
    "<path/to/chip_top.v>", ["stdcells.lib", "macros/ram.lib"], black_box_fallback=True)
lib = netlist.get_gate_library()
black_boxes = [g for g in netlist.get_gates() if lib.is_black_box_gate_type(g.get_type())]
```

`NetlistFactory` functions return `None` on failure — always check.

### Save

```python
hal_py.NetlistSerializer.serialize_to_file(netlist, "<path/to/out.hal>")
```

(`hal_py.NetlistSerializer.deserialize_from_file(path, gate_lib=None)` is the
matching loader.)

### Core objects

`Netlist`, `Gate`, `Net`, `Module` mostly expose the same thing two ways: a
`get_x()` method and (where it makes sense) an equivalent `.x` property —
both are real, pick whichever reads better.

```python
for gate in netlist.get_gates():                 # == netlist.get_gates(filter=lambda g: ...)
    print(gate.id, gate.name, gate.type.name)     # gate.get_id()/.get_name()/.get_type().get_name()

for net in netlist.get_nets():
    for ep in net.get_sources():                  # Endpoint: ep.get_gate(), ep.get_pin()
        pass
    for ep in net.get_destinations():
        pass

top = netlist.get_top_module()
for mod in top.get_submodules(recursive=True):
    print(mod.name, len(mod.get_gates(recursive=False)))

gate.fan_in_nets   # == gate.get_fan_in_nets()
gate.fan_out_nets  # == gate.get_fan_out_nets()
gate.boolean_functions  # dict[str, BooleanFunction], == gate.get_boolean_functions()
```

Traversal helpers live in `hal_py.netlist_utils` (module-level free functions,
not decorators you instantiate) — `get_next_gates`, `get_next_sequential_gates`,
`get_shortest_path`, `get_path`, `get_ff_dependency_matrix`, etc. There is also
a `NetlistTraversalDecorator(netlist)` / `NetlistModificationDecorator(netlist)`
pair (`src/python_bindings/bindings/netlist_traversal_decorator.cpp`,
`netlist_modification_decorator.cpp`) for the same operations plus net-splice
/ gate-replace helpers, if you need an object with repeated calls instead of
importing free functions.

### Plugins from Python

```python
hal_py.plugin_manager.load_all_plugins()           # or load("<name>", "<path>")
hal_py.plugin_manager.get_plugin_names()
```

Plugins with a C++ Python-visible interface (e.g. `dataflow_analysis`,
`clock_tree_extractor`) are used by getting the plugin instance and calling
its methods directly — check `plugins/<name>/src/python_bindings/` or its
docstrings (`help(hal_py.<Plugin>)`) for the exact call, don't guess.

## Surviving plugins (`plugins/`)

`bitorder_propagation`, `boolean_influence`, `clock_tree_extractor`,
`dataflow_analysis` (DANA), `gate_libraries`, `genlib_writer`, `gexf_writer`,
`graph_algorithm` (igraph), `hawkeye`, `hgl_parser`/`hgl_writer`,
`liberty_parser`, `module_identification`, `netlist_preprocessing`, `perf_test`,
`python_shell`, `resynthesis`, `sequential_symbolic_execution`, `simulator`,
`solve_fsm`, `verilog_parser`/`verilog_writer`, `vhdl_parser`, `xilinx_toolbox`,
`z3_utils`. All GUI and GUI-only plugins were removed in this fork.

## Pitfalls

- **`netlist` exists only when a project argument was given.** With
  `--project-dir` / `--import-netlist` / `--empty-project`, HAL loads the
  netlist and binds it to `netlist` before the script runs (see above). Without
  them nothing is pre-loaded and the script must call
  `hal_py.NetlistFactory.load_hal_project(...)` / `load_netlist(...)` itself.
  (Before the fix for issue #31 the UI-plugin flag took over `main()` *before*
  the project arguments were processed, so the combination silently ran the
  script against nothing — a wrapper written against that behaviour still works,
  it just loads the netlist twice.)
- **The exit code of `--python-script` is trustworthy — check it.** `hal` exits
  0 only if the script ran to completion; an uncaught exception, a Python
  environment that failed to set up, and a script path that is missing, a
  directory, or not a `.py` file all exit nonzero. (Before the fix for issue
  #11 every one of those exited 0, so a wrapper that predates it may be
  ignoring the status.) A script that calls `sys.exit(n)` ends the process with
  `n`, as CPython handles `SystemExit` itself.
- **No `__file__`, and `--python-args` becomes `sys.argv`.** The plugin calls
  `PySys_SetArgv` with the *contents* of `--python-args` (split on spaces),
  not the script path — `sys.argv[0]` is your first arg, not the script name.
  Pass arguments explicitly rather than relying on script-relative paths.
- **`hal_py` must be import-able.** Running `python3 my_script.py` directly
  (not through `hal --python-script`) needs `PYTHONPATH` pointed at
  `build/lib` first — HAL only does this `sys.path` fix-up inside its own
  shell/script runner.
- **`--empty-project`, `--import-netlist`, `--project-dir` are mutually
  exclusive** in the ways `main.cpp` checks (`--empty-project` cannot combine
  with `--import-netlist` or `--project-dir`, and requires `--gate-library`).
- **`get_gates()`/`get_nets()`/`get_modules()` etc. return borrowed
  references** tied to the netlist's lifetime — don't keep them alive past
  the `Netlist` object.
- **This fork builds on Linux/macOS only**; `install_dependencies.sh` has no
  Windows path. Use WSL2 or `docker-compose run --rm hal-build` from Windows.

## Visualizing a netlist for a human

There is no GUI. To *look* at a netlist, use the batch tool added for this
purpose, `tools/hal_viz` (see `tools/hal_viz/README.md` for full detail). It
needs a **built HAL** (imports `hal_py`) and, optionally, the Graphviz `dot`
binary — without `dot` it still writes the `.dot` file, just not a rendered
image.

```bash
export HAL_PY_PATH=/path/to/hal/build/lib     # or pass --hal-lib
PYTHONPATH=tools python -m hal_viz module_tree ./unzipped_project -o out/
PYTHONPATH=tools python -m hal_viz netlist_graph ./unzipped_project --module top --cluster-modules -o out/top.svg
PYTHONPATH=tools python -m hal_viz dataflow ./unzipped_project -o out/dataflow --html
PYTHONPATH=tools python -m hal_viz clock_tree ./unzipped_project -o out/clocks.svg
```

Equivalently: `python tools/hal_viz <command> ...` from the repo root. Every
subcommand takes `-o/--output PATH` and `-f/--format {svg,png,pdf,none}`
(default `svg`); a `.dot` is *always* emitted regardless of format or whether
`dot` is installed. `netlist_graph` needs a scope (`--module` or `--gate`,
plus `--depth`) — a full netlist is rarely renderable, and `--max-gates`
(default 400) refuses to try. `dataflow` and `clock_tree` run the
`dataflow_analysis`/`clock_tree_extractor` plugins and reuse their own DOT
exporters rather than re-deriving the graph.

To hand a *result* to a human rather than a picture, render the findings
documents an analysis wrote (the `tools/hal_findings` schema) into one static
HTML page. This subcommand needs neither HAL nor a netlist, and the page opens
offline:

```bash
python tools/hal_viz report results/*.json --artifact out/top.svg -o out/report.html
```

It lists every finding with its status badge (proven / bounded / heuristic /
unknown / timeout / error / unsupported stay visually distinct), its
assumptions and bounds, relative links to evidence and witnesses, and inlines
the scoped SVG a finding points at. Truncated lists, coverage gaps and missing
artifacts are marked, never dropped.

From inside `hal --python-script`, import `hal_viz.cli.main` instead of
shelling out (remember `sys.argv[0]` isn't set the normal way — pass the
argument list explicitly):

```python
import sys
sys.path.insert(0, "/path/to/hal/tools")
from hal_viz.cli import main
main(["module_tree", "/path/to/project", "-o", "/tmp/out/"])
```

## Interactive waveform visualization (Saleae Logic 2)

There is no HAL waveform viewer in this fork either. Pick by *what the human
is doing*:

| goal | use |
| --- | --- |
| interactively explore waveforms, drive a logic analyzer, decode a protocol live | **Saleae Logic 2 via the `logic2` MCP tools** |
| a static artifact (image, report, diff) produced in batch | `tools/hal_viz`, `saleae` CLI, or a `.vcd` file |
| look at *simulated* HAL waveforms | export **VCD** and open it in a VCD viewer (see below) — **not** Logic 2 |

### Setup (user-facing)

`.mcp.json` at the repo root already registers the server, so a Claude Code
session started in this repo picks it up automatically. If the `logic2` tools
are still missing, it is on the user's side:

1. Launch Saleae Logic 2 and enable **Settings > Automation > MCP Server**
   (or click **Automation** in the bottom bar). It listens on
   `http://127.0.0.1:10530`.
2. Outside this repo, register it manually:
   `claude mcp add --transport http logic2 http://127.0.0.1:10530`

Saleae documents the server as **experimental**, supporting Logic 8, Logic
Pro 8 and Logic Pro 16 (docs: https://docs.saleae.com/mcp/guides/getting-started).
Broadly it can start captures, run protocol decoders and export data — but
**the tool surface is not documented and changes between releases. Inspect the
live tool list at runtime and call only what is actually there; never invent a
`logic2` tool name.** If the tools are absent, say so and fall back to VCD
rather than guessing.

### Data bridge: what actually works

**Logic 2 → HAL (supported, this is the real path).** HAL's SALEAE support was
written to ingest Logic 2's *binary export*. `SaleaeHeader::read()`
(`plugins/simulator/netlist_simulator_controller/src/saleae_file.cpp`) accepts
the `<SALEAE>` identifier with type `0` (digital, times as doubles in seconds);
analog files (type `1`) are rejected as `UnsupportedType`. Workflow:

1. In Logic 2, export raw data of the digital channels in **binary** format to
   a directory. That writes one `digital_<n>.bin` per channel.
2. Map HAL nets to those channel numbers and import:

```python
sim = hal_py.plugin_manager.get_plugin_instance("netlist_simulator_controller")
ctrl = sim.create_simulator_controller("from_hardware")
ctrl.add_gates(netlist.get_gates())
# {net: channel index n from digital_<n>.bin}
ctrl.import_saleae("/path/to/logic2_export_dir",
                   {netlist.get_net_by_id(3): 0, netlist.get_net_by_id(7): 1},
                   1000000000)   # timescale: multiplies the double seconds values
ctrl.run_simulation()
```

`import_saleae` copies `<dirname>/digital_<their index>.bin` into the
controller work directory and rebuilds `saleae.json`
(`VcdSerializer::importSaleae`, `src/vcd_serializer.cpp`) — the source
directory needs no `saleae.json` of its own. `import_simulation(dirname,
filter)` is the sibling for a directory that *does* already carry HAL's
`saleae.json`; `import_vcd` / `import_csv` take those formats and convert them
to the same internal SALEAE store. All of them need the controller in an
importable state (`can_import_data()`).

**HAL → Logic 2: no supported path. Do not attempt it.** Two independent
reasons, both checked:

- Logic 2 only *opens* `.sal` captures. Saleae's own FAQ ("Is it possible to
  import data into the Logic software?") says data cannot be imported —
  no VCD, no CSV, no raw binary, and the MCP docs describe no load/import tool.
- What HAL writes is not Saleae-format anyway. `SaleaeOutputFile` writes the
  header with storage format `Uint64` (`0x206c6168`, `"hal "`) or `Coded`
  (`0x786c6168`, `"halx"`) and 64-bit integer timestamps — HAL-private type
  codes Saleae software does not know. Only the *read* path understands
  genuine Saleae files.

So there is no converter under `tools/` and none should be written. Keep
Logic 2 for real-hardware capture and live exploration.

### Viewing simulated waveforms instead

Export VCD and open it in a VCD viewer (GTKWave, Surfer, Sigrok PulseView,
VS Code WaveTrace — HAL ships none of them, so let the user pick):

```python
ctrl.generate_vcd("/tmp/sim.vcd", 0, 0)      # start_time, end_time; 0,0 = everything
```

Or from an existing SALEAE work directory, without Python — the `saleae` CLI
built by `plugins/simulator/netlist_simulator_controller/saleae_cli`:

```bash
saleae ls -d <workdir>                    # list waveforms in saleae.json
saleae cat digital_0.bin -d <workdir>     # dump one waveform's header + transitions
saleae diff <other_workdir> -d <workdir>  # compare two waveform databases
saleae export out.vcd -d <workdir>        # or out.csv; -i/--id and -r/--time-range filter
```

`saleae export` only accepts `.vcd` and `.csv` extensions.
