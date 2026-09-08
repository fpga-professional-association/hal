# Writing a HAL analysis plugin

A walkthrough from an idea to a callable, tested, headless analysis: what the
conventions are, how to generate a plugin that already works, how to declare what
it needs, and how to tell a contributor's afternoon apart from a wasted one.

Everything here is verified against this fork's source. If a command below stops
working, that is a bug in the tooling, not in this document.

---

## 1. First decide whether you need a plugin at all

A C++ plugin is not the default answer, and reaching for one first is the most
common way to spend a week on something that was an afternoon's work.

**A Python workflow over `hal_py` is enough when** — and this covers most
analyses:

* you are walking the netlist, filtering gates, following nets, reading gate
  types, modules and pin groups;
* you are combining plugins that already exist (`graph_algorithm`,
  `dataflow_analysis`, `z3_utils`, `netlist_preprocessing`, …);
* the design is up to a few hundred thousand gates and you are doing a bounded
  number of passes over it;
* you want to iterate on the *question*, not on the implementation.

Write it as a script, drive it with `hal --python-script` (its exit codes are
trustworthy) or `tools/hal_runner`, and emit `tools/hal_findings` documents. See
`.claude/skills/using-hal/SKILL.md` and `tools/hal_viz/README.md`.

**A C++ plugin is warranted when** at least one of these is true:

* **Per-gate work in an inner loop.** Anything that touches every gate many times
  — fixpoint iterations, subgraph enumeration, per-gate SMT queries — pays the
  Python call overhead once per crossing, and that is the whole cost.
* **You need a C++ library.** Z3, igraph, ABC and Verilator are linked, not
  imported. `plugins/z3_utils` and `plugins/graph_algorithm` exist for exactly
  this reason.
* **Other plugins must call it.** A plugin can `LINK_LIBRARIES` another plugin;
  a script cannot be linked against.
* **It is a parser or a writer.** File-format support is registered through
  `FacExtensionInterface`, which is a C++ interface (`plugins/verilog_parser`,
  `plugins/hgl_writer`).
* **It has to ship with the tool.** A plugin is discoverable through
  `plugin_manager`, has a build flag, a version and a capability declaration;
  a script in someone's home directory has none of that.

If none of those apply, write the script. You can always promote the inner loop
to a plugin later — and by then you will know which loop.

---

## 2. The conventions, read off the existing plugins

Every plugin in `plugins/` follows the same shape. `plugins/hawkeye` and
`plugins/bitorder_propagation` are the smallest complete examples; the paragraphs
below are what they have in common.

### Build registration

`plugins/CMakeLists.txt` adds *every* subdirectory that has a `CMakeLists.txt` of
its own, so **a new plugin needs no edit anywhere else**:

```cmake
SUBDIRLIST(SUBDIRS ${CMAKE_CURRENT_SOURCE_DIR})
FOREACH(subdir ${SUBDIRS})
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/${subdir}/CMakeLists.txt")
        ADD_SUBDIRECTORY(${subdir})
    endif()
ENDFOREACH()
```

The plugin's own `CMakeLists.txt` is an option, three globs and one call:

```cmake
option(PL_HAWKEYE "PL_HAWKEYE" OFF)
if(PL_HAWKEYE OR BUILD_ALL_PLUGINS)
    file(GLOB_RECURSE HAWKEYE_INC        ${CMAKE_CURRENT_SOURCE_DIR}/include/*.h)
    file(GLOB_RECURSE HAWKEYE_SRC        ${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp)
    file(GLOB_RECURSE HAWKEYE_PYTHON_SRC ${CMAKE_CURRENT_SOURCE_DIR}/python/*.cpp)

    hal_add_plugin(hawkeye
                   SHARED
                   HEADER ${HAWKEYE_INC}
                   SOURCES ${HAWKEYE_SRC} ${HAWKEYE_PYTHON_SRC}
                   PYDOC SPHINX_DOC_INDEX_FILE ${CMAKE_CURRENT_SOURCE_DIR}/documentation/hawkeye.rst
                   LINK_LIBRARIES graph_algorithm)
endif()
```

`hal_add_plugin` (`cmake/hal_plugin.cmake`) links `hal::core`, `hal::netlist`,
Python and pybind11 for you, drops the `lib` prefix, and installs the result into
`lib/hal_plugins/`. The option defaults to `OFF` for most plugins;
`-DBUILD_ALL_PLUGINS=ON` (what CI uses) turns everything on.

### Directory layout

```
plugins/<name>/
  CMakeLists.txt
  include/<name>/plugin_<name>.h    the BasePluginInterface subclass
  include/<name>/<name>.h           the analysis API
  src/                              implementations
  python/python_bindings.cpp        the hal_plugins.<name> module
  test/                             gtest suite (add_subdirectory(test))
```

The `include/<name>/` nesting is not decoration: `hal_add_plugin` puts
`<plugin>/include` on the include path, so headers are included as
`"<name>/<name>.h"` from anywhere.

### The plugin interface

```cpp
class PLUGIN_API HawkeyePlugin : public BasePluginInterface
{
public:
    std::string get_name() const override;             // required
    std::string get_version() const override;          // required
    std::string get_description() const override;
    std::set<std::string> get_dependencies() const override;
};

extern std::unique_ptr<BasePluginInterface> create_plugin_instance()
{
    return std::make_unique<HawkeyePlugin>();
}
```

`create_plugin_instance` is the factory `plugin_manager` looks for; without it
the shared object is not a plugin. `get_dependencies()` returns the plugins that
must be loaded first — `plugin_manager` uses it, and so does the capability
declaration described below.

### Python bindings

```cpp
PYBIND11_MODULE(hawkeye, m)     // MUST match the library file name
{
    py::class_<HawkeyePlugin, RawPtrWrapper<HawkeyePlugin>, BasePluginInterface> py_plugin(m, "HawkeyePlugin", R"(...)");
    ...
}
```

Three things bite here:

* the module name must equal the library file name, or importing it fails with
  *"dynamic module does not define module export function"*;
* plugin classes are held by `RawPtrWrapper<T>` (a `unique_ptr` with `nodelete`)
  because the plugin manager owns them;
* **anything that returns a `Gate*`, `Net*`, `Module*` or `GateType*` must use
  the fork's `borrowed()` call policy**, e.g.
  `py::cpp_function(getter, py::is_method(cls), borrowed())`. It hands the object
  over without ownership and keeps its *owner* — the netlist — alive. The default
  policy would try to take ownership of an object the netlist already owns.

Return an error, do not swallow it. Upstream's older bindings log and return
`None`; a caller that ignores the log then cannot tell a failure from an empty
result. The scaffold raises `RuntimeError` with the plugin's own message instead.

### Tests

`test/CMakeLists.txt` builds one gtest binary and registers it with ctest:

```cmake
if(BUILD_TESTS)
    include_directories(${gtest_SOURCE_DIR}/include ${gtest_SOURCE_DIR}
                        ${CMAKE_SOURCE_DIR}/include ${CMAKE_SOURCE_DIR}/tests
                        ${CMAKE_SOURCE_DIR}/plugins/<name>/include)
    add_executable(runTest-<name> <name>.cpp)
    target_link_libraries(runTest-<name> <name> pthread gtest hal::core hal::netlist test_utils)
    add_test(runTest-<name> ${CMAKE_BINARY_DIR}/bin/hal_plugins/runTest-<name> ...)
endif()
```

`test_utils` gives you `create_empty_netlist()`, a gate library with `BUF`,
`AND2`, `DFF`, `RAM` and friends, `connect()`, `NO_COUT_BLOCK` and the
`TEST_START`/`TEST_END` macros. Build your fixture from those rather than from a
netlist file, so the test states its own preconditions.

---

## 3. Generate a plugin

```bash
python3 tools/new_plugin.py my_analysis
```

You get a plugin that **already builds, loads, runs and is tested** — not a stub:

```
plugins/my_analysis/
  CMakeLists.txt                     build flag, capability embedding, test registration
  capabilities.json                  what the plugin needs (see below)
  README.md
  include/my_analysis/my_analysis.h        the analysis API
  include/my_analysis/plugin_my_analysis.h the plugin interface
  src/my_analysis.cpp
  src/plugin_my_analysis.cpp
  python/python_bindings.cpp         the hal_plugins.my_analysis module
  python/run_my_analysis.py          driver, emits a hal_findings document
  test/CMakeLists.txt
  test/my_analysis.cpp               gtest suite
```

The analysis it ships with groups sequential gates by the net driving their clock
pin. It is deliberately real: it reads gate-type semantics rather than pin names,
reports what it could not evaluate, and errors out on a netlist it cannot look at
— which is the shape your analysis should have too. Replace the body, keep the
wiring.

`plugins/.gitignore` is an allow list — it ignores `*` and re-includes each
plugin by name — so a generated plugin would otherwise be invisible to
`git status`. The generator adds the two lines for you (`--no-gitignore` to skip
it).

`plugins/example_analysis` is the same output, checked in unmodified, so that CI
compiles the template and runs its tests on every build.
`tools/test_new_plugin.py` fails if the two ever diverge — **edit
`tools/plugin_template/`, then regenerate; never edit the example in place**:

```bash
python3 tools/new_plugin.py example_analysis --force --description \
  "Group sequential gates by the net that drives their clock pin (reference output of tools/new_plugin.py)."
```

### Build, test, run

```bash
cd build
cmake .. -DPL_MY_ANALYSIS=ON -DBUILD_TESTS=ON     # or -DBUILD_ALL_PLUGINS=ON
cmake --build . --target my_analysis
ctest -R runTest-my_analysis --output-on-failure

export HAL_PY_PATH=$PWD/lib
python3 ../plugins/my_analysis/python/run_my_analysis.py <netlist-or-project-dir> \
    --output findings.json
python3 ../tools/hal_findings validate findings.json
```

From Python:

```python
import hal_py
hal_py.plugin_manager.load_all_plugins()
from hal_plugins import my_analysis

report = my_analysis.analyze(netlist)     # raises RuntimeError with the reason
```

---

## 4. Say what your plugin needs: `capabilities.json`

A plugin being *built* says nothing about whether it can answer anything about
the netlist in front of you. `capabilities.json` makes that machine-checkable.
It is validated against
`tools/hal_capabilities/schema/plugin-capabilities-1.0.0.schema.json`:

```json
{
  "capabilities_version": "1.0.0",
  "plugin": {"name": "my_analysis", "version": "0.1", "kind": "analysis",
             "description": "...", "build_option": "PL_MY_ANALYSIS"},
  "dependencies": {"plugins": ["graph_algorithm"], "python_modules": []},
  "requires": {
    "gate_type_properties": {"all_of": ["sequential"]},
    "gate_type_pin_types": ["clock"],
    "netlist": {"min_gates": 1, "needs_gate_library": true}
  },
  "supported_gate_libraries": {"mode": "any", "verified": ["EXAMPLE_GATE_LIBRARY"]},
  "findings": {"schema_version": "1.0.0", "statuses": ["heuristic", "unsupported"]},
  "resources": {"complexity": "linear in the number of gates", "deterministic": true}
}
```

| field | why it is there |
| --- | --- |
| `dependencies.plugins` | must equal `get_dependencies()`; a mismatch is reported as a defect |
| `requires.gate_type_properties` | the *primitive semantics* you read — `sequential`, `ff`, `ram`, `c_lut`, … — checked against the actual `GateTypeProperty` vocabulary |
| `requires.gate_type_pin_types` | pins the matched gate types must expose (`clock`, `enable`, …); types that do not are reported as unsupported instead of dropped |
| `supported_gate_libraries` | `any` is a claim of library independence; `allow_list`/`deny_list` say where you have actually looked |
| `findings.statuses` | which `hal_findings` statuses you can legitimately emit — a structural analysis listing a proof status is over-claiming |
| `resources` | what running it costs, so an orchestrator can budget |

The declaration is the single source of truth. The generated `CMakeLists.txt`
compiles it into the plugin (`MyAnalysisPlugin::get_capabilities()`) **and**
copies it to `<build>/share/hal/plugin_capabilities/my_analysis.json`, so the
discovery command works with or without a source tree — and reports drift when
the two disagree.

---

## 5. Discover what is available: `tools/hal_capabilities`

Four questions, four separate answers:

```bash
python3 tools/hal_capabilities list --build-dir <build> --probe --netlist <netlist>
```

```
plugin            declared  built  loadable  netlist
----------------  --------  -----  --------  ---------
example_analysis  yes       yes    yes       supported
graph_algorithm   no        yes    yes       -
hawkeye           no        no     no        -
```

* **declared** — the plugin ships a `capabilities.json`. Says nothing about a build.
* **built** — its shared object is in `<build>/lib/hal_plugins/`. Its option was on.
* **loadable** — `plugin_manager` instantiated it. A plugin can be built and still
  fail here, when a dependency did not load.
* **netlist** — `supported`, `partial` (it runs, but some gate types it matched are
  missing something it needs) or `unsupported` (it cannot run at all), *for this
  design*.

```bash
python3 tools/hal_capabilities show  my_analysis
python3 tools/hal_capabilities check my_analysis --netlist <netlist>
python3 tools/hal_capabilities validate            # every plugins/*/capabilities.json
```

Exit codes: `0` supported (or `partial`), `1` invalid declaration or failure,
`3` unsupported for this netlist, `4` a declared dependency is missing or the
plugin does not load.

### Missing dependencies and unsupported gate types must be *actionable*

This is the part worth caring about. An analysis that returns nothing because it
could not look is indistinguishable, in a report, from one that looked and found
nothing — and the second is a much stronger claim than the first.

The scaffold enforces the distinction in three places:

1. **The analysis returns an error, not an empty result**, when its precondition
   fails, and the message names the missing property and the command that checks
   it without running anything:

   > `cannot analyze netlist 'toy': not one of its 12 gates has a type carrying the
   > 'sequential' gate type property … Gate library: 'EXAMPLE_GATE_LIBRARY'. …
   > check it with 'python tools/hal_capabilities check my_analysis --netlist <path>'`

   The bindings raise that as a `RuntimeError` rather than logging it and
   returning `None`.

2. **Gate types it matched but could not evaluate are reported by name**, with a
   count and a reason, in `Report.unsupported` — and become an `unsupported`
   finding whose summary says outright that their absence from the results is a
   gap in the analysis, not evidence about the design.

3. **`hal_capabilities check` answers the same question before you run anything**,
   from the declaration alone, and `hal_capabilities list` reports a dependency
   that does not exist, is not built, or failed to load.

---

## 6. Emit findings, not prose

`tools/hal_findings` is the shared result contract: one versioned JSON document
in which every finding states what kind of claim it is (`heuristic`,
`proven_under_assumptions`, `proven_bounded`, `unsupported`, `timeout`, …), what
it rests on, and which artifact its gate IDs belong to. Read its
[README](../tools/hal_findings/README.md) before choosing a status — the schema
rejects a bounded result that claims to be unbounded, and a heuristic that claims
a formal method.

The generated `python/run_<name>.py` already does this: one finding per result,
an `unsupported` finding for the coverage gap, and — if the run fails — a single
`error` finding rather than a stack trace, so a caller reading the JSON can tell
"the analysis failed" from "the analysis found nothing".

---

## 7. Before you open a pull request

```bash
python3 tools/test_new_plugin.py                       # scaffold still consistent
python3 -m unittest discover -s tools/hal_capabilities -t tools -p "test_*.py"
python3 tools/hal_capabilities validate                # your declaration
ctest -R runTest-<name> --output-on-failure            # your gtest suite
```

Add your plugin's row to [`CAPABILITIES.md`](../CAPABILITIES.md) — that file is
the fork's inventory of what actually exists, and a plugin missing from it makes
the document wrong.

Against a built HAL, the end-to-end check for the scaffold itself is:

```bash
HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib \
    python3 tests/headless_smoke/plugin_scaffold_smoke.py --build-dir <build>
```

## Where things live

| path | what it is |
| --- | --- |
| `tools/new_plugin.py` | the generator |
| `tools/plugin_template/` | the templates it stamps out — change these, not the output |
| `tools/hal_capabilities/` | capability schema, validation, discovery command |
| `tools/hal_findings/` | the findings and evidence contract |
| `plugins/example_analysis/` | unmodified generator output, compiled by CI |
| `tests/headless_smoke/plugin_scaffold_smoke.py` | end-to-end check against a built HAL |
| `cmake/hal_plugin.cmake` | `hal_add_plugin` |
| `plugins/CMakeLists.txt` | the glob that picks up every plugin directory |
