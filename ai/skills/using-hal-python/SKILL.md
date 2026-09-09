---
name: using-hal-python
description: Drive headless HAL from Python -- hal --python, hal --python-script, or a plain interpreter with hal_py on sys.path. Use for the mechanics of getting hal_py imported, a netlist loaded, and plugins usable, before doing any actual analysis.
---

# Using hal_py -- the portable digest

This is the short form. The full authority, with every pitfall's reasoning and
the visualization/waveform sections, is `.claude/skills/using-hal/SKILL.md` --
read that when something here is ambiguous or you need the "why".

## When to use
- Any task that needs `hal_py` from Python: loading a netlist, traversing
  gates/nets, calling a plugin, or writing a `--python-script` for CI.
- Not for building HAL itself (see `container-build-and-test`), and not for
  choosing which analysis tool answers a question (see
  `netlist-analysis-tools`).

## Quickstart -- the three ways in

```bash
# 1. interactive shell, hal_py preloaded, nothing else
hal --python

# 2. batch: --python-script composes with the project-loading flags. Give it
#    a project and the netlist is bound to `netlist` before the script runs.
hal --import-netlist design.v \
    --gate-library plugins/gate_libraries/definitions/example_library.hgl \
    --project-dir ./out_project \
    --python-script analyze.py --python-args "arg1 arg2"

# 3. plain interpreter -- hal_py is NOT on sys.path unless you put it there
PYTHONPATH=/path/to/hal/build/lib python3 -c "import hal_py"
```

```python
# analyze.py, run via way 2 above -- no NetlistFactory call needed
print(netlist.get_design_name(), len(netlist.get_gates()))
```

## The rules that matter

**`netlist` exists only when a project argument was given.** With
`--project-dir` / `--import-netlist` / `--empty-project`, HAL opens the
project and binds the result to the global `netlist` before your script runs
-- same for interactive `hal --python`. A bare `hal --python-script x.py`
gets `hal_py` imported and nothing loaded; load it yourself. Guard scripts
meant to run both ways with `if "netlist" not in globals(): ...`.

**Load plugins before parsing anything.** HAL's gate-library and netlist
parsers *are* plugins. With none loaded, nothing is registered for
`.hgl`/`.v`/`.vhd`/`.hal`, and every load silently returns `None` -- the
failure mode behind "no gate library parser registered for file extension
.hgl". `hal --python`/`--python-script` load plugins for you before binding
`netlist`; a plain interpreter must call
`hal_py.plugin_manager.load_all_plugins()` itself, first.

**Plugin Python bindings live under `hal_plugins.*`, never at top level.**
`import graph_algorithm` raises `ModuleNotFoundError`; the working form is
`from hal_plugins import graph_algorithm`, and only after
`load_all_plugins()`. This is this repo's single most-repeated scripting
mistake -- three of the Agilex walkthrough scripts independently made it.

**`hal_py.Netlist` has no `get_gate_by_name`.** Filter `netlist.get_gates()`
or use `get_gate_by_id`. (A convenience fix is filed as issue #52; don't
assume it exists until it lands.)

```python
gate = next(g for g in netlist.get_gates() if g.get_name() == "count_reg_3")
```

**The `--python-script` exit code is trustworthy -- check it.** `hal` exits 0
only if the script ran to completion. An uncaught exception, a Python
environment that failed to set up, or a script path that's missing/a
directory/not `.py` all exit nonzero (fixed for issue #11; a wrapper written
against the old always-0 behavior may be silently ignoring failures). A
script calling `sys.exit(n)` ends the process with `n`.

**Borrowed pointers.** `get_gates()`/`get_nets()`/`get_modules()` return
references tied to the `Netlist` object's lifetime -- don't keep them alive
past it. The `netlist` bound by `--python-script` is likewise borrowed; HAL
frees it after the script ends, so don't stash it somewhere that outlives the
run.

**No `__file__`; `--python-args` becomes `sys.argv`.** The plugin calls
`PySys_SetArgv` with the *contents* of `--python-args` split on spaces --
`sys.argv[0]` is your first arg, not the script name. Pass paths explicitly.

## Core objects, fast

```python
netlist = hal_py.NetlistFactory.load_hal_project("<unzipped_project_dir>")
netlist = hal_py.NetlistFactory.load_netlist("<file.v>", "<gate_library.hgl>")
# NetlistFactory functions return None on failure -- always check.

for gate in netlist.get_gates():
    print(gate.id, gate.name, gate.type.name)   # .get_id()/.get_name() also work

for net in netlist.get_nets():
    net.get_sources()        # list[Endpoint]: ep.get_gate(), ep.get_pin()
    net.get_destinations()

hal_py.NetlistSerializer.serialize_to_file(netlist, "out.hal")
```

`hal_py.netlist_utils` has the free traversal functions
(`get_next_gates`, `get_shortest_path`, `get_ff_dependency_matrix`, ...).
`NetlistTraversalDecorator(netlist)` / `NetlistModificationDecorator(netlist)`
wrap the same operations as an object, plus net-splice/gate-replace helpers.

## Where things live
- Full authority: `.claude/skills/using-hal/SKILL.md` (CLI flags, project
  loading semantics, multi-library import, plugin list, hal_viz, Saleae/VCD).
- Bindings: `src/python_bindings/bindings/*.cpp` -- every binding has a
  docstring; grep there before guessing an API name.
- Plugin bootstrap pattern, done right: `tools/hal_viz/halenv.py`
  (`ensure_plugins_loaded`, `import_plugin`).
