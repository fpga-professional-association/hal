# Fork Capabilities and FPGA Support Matrix

This document is the factual inventory of what this fork's plugins actually do, how they are
turned on, and which device/gate-library families each capability has been verified against.
It exists because "a Verilog/VHDL parser is built" is not the same claim as "simulation works
for this device" or "arithmetic recognition works for this device" — those are separate
plugins with separate, narrower architecture support. See
[fpga-professional-association/hal#13](https://github.com/fpga-professional-association/hal/issues/13).

Reviewed against commit `f47948e` of this fork (see [Starting points](https://github.com/fpga-professional-association/hal/issues/13)); re-check
`plugins/*/CMakeLists.txt` and the source pointers below if `master` has moved since.

Upstream HAL (with its GUI and full plugin set) is developed at
[emsec/hal](https://github.com/emsec/hal). Its [Wiki](https://github.com/emsec/hal/wiki) and
generated [C++](https://emsec.github.io/hal/doc/) / [Python](https://emsec.github.io/hal/pydoc/)
API docs are **upstream documentation** — they describe upstream's plugin set, which is a
superset of what this headless fork ships (no GUI, no GUI-only plugins), and this fork does not
currently publish its own generated API docs. Treat any behavior described only in those links
as unverified for this fork until checked against the source pointers here.

## 1. Plugin inventory

Every subdirectory of `plugins/` that has its own `CMakeLists.txt`, with its build flag and
default (`ON`/`OFF` from that file's own `option(...)` line, or from `plugins/CMakeLists.txt`
where noted; every plugin is also built if the umbrella `BUILD_ALL_PLUGINS` cache option is
`ON`, which itself defaults `OFF`, see `CMakeLists.txt:69`).

| Plugin | What it does | Flag | Default | Source |
|---|---|---|---|---|
| `bitorder_propagation` | Infers/propagates multi-bit pin-group bit order (`propagate_bitorder`, `reorder_module_pin_groups`) | `PL_BITORDER_PROPAGATION` | OFF | `plugins/bitorder_propagation/CMakeLists.txt:1` |
| `boolean_influence` | Computes Boolean influence of gate outputs on inputs | `PL_BOOLEAN_INFLUENCE` | OFF | `plugins/boolean_influence/CMakeLists.txt:1` |
| `clock_tree_extractor` | Recovers clock trees from a gate-level netlist | `PL_CLOCK_TREE_EXTRACTOR` | ON | `plugins/clock_tree_extractor/CMakeLists.txt:1` |
| `dataflow_analysis` | DANA: dataflow analysis / register-group recovery ([paper](https://eprint.iacr.org/2020/751.pdf)) | `PL_DATAFLOW` | OFF | `plugins/dataflow_analysis/CMakeLists.txt:1` |
| `example_analysis` | Reference output of `tools/new_plugin.py`, kept so CI compiles the plugin template: groups sequential gates by the net driving their clock pin (structural clock domains) | `PL_EXAMPLE_ANALYSIS` | OFF | `plugins/example_analysis/CMakeLists.txt:1`, [walkthrough](documentation/plugin_development.md) |
| `gate_libraries` | Installs the bundled `.hgl` gate-library definitions (no code, just copies files) | `PL_GATE_LIBRARIES` | ON | `plugins/gate_libraries/CMakeLists.txt:1-2` |
| `genlib_writer` | Serializes a gate library to ABC `genlib` format | `PL_GENLIB_WRITER` | OFF | `plugins/genlib_writer/CMakeLists.txt:1` |
| `gexf_writer` | Serializes a netlist graph to GEXF (for external graph tools, e.g. Gephi) | `PL_GEXF_WRITER` | ON | `plugins/gexf_writer/CMakeLists.txt:1` |
| `graph_algorithm` | [igraph](https://igraph.org) integration for graph-theoretic algorithms over the netlist graph | `PL_GRAPH_ALGORITHM` | ON | `plugins/graph_algorithm/CMakeLists.txt:1` |
| `hawkeye` | HAWKEYE: recovers symmetric-crypto primitives from netlists ([paper](https://eprint.iacr.org/2024/860.pdf)) | `PL_HAWKEYE` | OFF | `plugins/hawkeye/CMakeLists.txt:1` |
| `hgl_parser` | Parses HAL's own `.hgl` JSON gate-library format | `PL_HGL_PARSER` | ON | `plugins/hgl_parser/CMakeLists.txt:1` |
| `hgl_writer` | Serializes a gate library to `.hgl` | `PL_HGL_WRITER` | ON | `plugins/hgl_writer/CMakeLists.txt:1` |
| `liberty_parser` | Parses standard Liberty (`.lib`) gate-library files | `PL_LIBERTY_PARSER` | ON | `plugins/liberty_parser/CMakeLists.txt:1` |
| `module_identification` | Structural/functional recognition of arithmetic operations (add, sub, compare, const-mult, counter, ...), SMT-verified | `PL_MODULE_IDENTIFICATION` | OFF | `plugins/module_identification/CMakeLists.txt:1` |
| `netlist_preprocessing` | Netlist cleanup/normalization passes (remove redundant logic, propagate constants, reconstruct pin groups, DEF-file parsing, ...) | `PL_NETLIST_PREPROCESSING` | ON | `plugins/CMakeLists.txt:9` (option moved out of the plugin's own `CMakeLists.txt` because its dependencies, `z3_utils`/`resynthesis`, must build first — see `plugins/netlist_preprocessing/CMakeLists.txt:1-4`) |
| `perf_test` | Internal performance benchmarking harness, not a netlist-analysis capability | `PL_PERF_TEST` | OFF | `plugins/perf_test/CMakeLists.txt:1` |
| `python_shell` | Interactive Python shell preloaded with `hal_py` (`hal --python`) | `PL_PYTHON_SHELL` | ON | `plugins/python_shell/CMakeLists.txt:1` |
| `resynthesis` | Gate/subgraph resynthesis and decomposition (e.g. into primitive gates) | `PL_RESYNTHESIS` | OFF | `plugins/resynthesis/CMakeLists.txt:1` |
| `sequential_symbolic_execution` | Symbolic execution across sequential (clocked) netlist state | `PL_SEQUENTIAL_SYMBOLIC_EXECUTION` | OFF | `plugins/sequential_symbolic_execution/CMakeLists.txt:1` |
| `simulator` | Event-based gate-level netlist simulator (`hal_simulator`), a `netlist_simulator_controller` orchestration layer, and an optional Verilator-backed engine (`verilator/`) | `PL_SIMULATOR` (engine), `PL_VERILATOR` (Verilator backend) | OFF / OFF | `plugins/simulator/CMakeLists.txt:1`, `plugins/simulator/verilator/CMakeLists.txt:1` |
| `solve_fsm` | Brute-force/SAT-ish FSM solving and FSM graph rendering (`solve_fsm`, `solve_fsm_brute_force`, `generate_dot_graph`) | `PL_SOLVE_FSM` | ON | `plugins/solve_fsm/CMakeLists.txt:1` |
| `verilog_parser` | Parses Verilog netlists | `PL_VERILOG_PARSER` | ON | `plugins/verilog_parser/CMakeLists.txt:1` |
| `verilog_writer` | Serializes a netlist to synthesizable Verilog | `PL_VERILOG_WRITER` | ON | `plugins/verilog_writer/CMakeLists.txt:1` |
| `vhdl_parser` | Parses VHDL netlists | `PL_VHDL_PARSER` | ON | `plugins/vhdl_parser/CMakeLists.txt:1` |
| `xilinx_toolbox` | Xilinx-specific netlist utilities: splitting merged LUTs/shift registers, XDC constraint-file parsing | `PL_XILINX_TOOLBOX` | OFF | `plugins/xilinx_toolbox/CMakeLists.txt:1` |
| `z3_utils` | Z3-based Boolean-function <-> SMT conversion, simplification, netlist/subgraph comparison; shared dependency of several other plugins | `PL_Z3_UTILS` | ON | `plugins/z3_utils/CMakeLists.txt:1` |

**Correction vs. the README's plugin summary:** the README's "Shipped Plugins" list names
"VHDL & Verilog Writers" (plural). There is no `vhdl_writer` plugin directory and no
`VhdlWriter`/`vhdl_writer` symbol anywhere in this tree (`plugins/` listing above; confirmed by
source search) — only `verilog_writer` exists. This fork can write netlists back out to Verilog,
not VHDL. The README wording has been corrected as part of this change (see below).

There is no top-level VHDL writer or Verilog-only "recognizer" beyond what is listed above; if a
plugin isn't in this table, it isn't shipped in this fork.

## 2. FPGA / device support matrix

Parsing a netlist written in a given HDL, and having correct gate-library semantics for a given
device family, are **independent** capabilities from simulation, symbolic execution, or
arithmetic-operation recognition for that same family. The rows below are split accordingly.
"Tested" means this fork's own CI/test suite exercises it (see the `test/` directories cited);
"unknown" means no test fixture or example exercises it in this repository and no claim is made
either way.

### 2a. Netlist parsing (HDL front end)

| HDL | Plugin | Status | Evidence |
|---|---|---|---|
| Verilog | `verilog_parser` | Tested | `plugins/verilog_parser/test/` (built when `PL_VERILOG_PARSER=ON`, the default) |
| VHDL | `vhdl_parser` | Tested | `plugins/vhdl_parser/test/` (built when `PL_VHDL_PARSER=ON`, the default) |
| HAL project format (`.hgl`-adjacent project dirs) | core (`NetlistFactory::load_hal_project`) | Tested | exercised by the `fsm` example in the README quickstart |

Parsing is HDL-syntax-level: it produces a generic gate/net graph annotated with the gate types
declared in whatever gate library you pass alongside the netlist. It says nothing on its own
about whether that gate library's *semantics* (below) have been validated for a real device.

### 2b. Gate-library availability

Bundled `.hgl` definitions in `plugins/gate_libraries/definitions/` (installed by the
`gate_libraries` plugin, default `ON`):

| Library (`"library"` field) | File | Target |
|---|---|---|
| `XILINX_UNISIM` | `XILINX_UNISIM.hgl` | Xilinx UNISIM primitives |
| `XILINX_UNISIM_WITH_HAL_TYPES` | `XILINX_UNISIM_hal.hgl` | same, with HAL-normalized gate types |
| `XILINX_SIMPRIM` | `XILINX_SIMPRIM.hgl` | Xilinx SIMPRIM (post-synthesis) primitives |
| `ICE40ULTRA` | `ice40ultra.hgl` | Lattice iCE40 Ultra primitives |
| `ICE40ULTRA_WITH_HAL_TYPES` | `ice40ultra_hal.hgl` | same, with HAL-normalized gate types |
| `NanGate_15nm_OCL` | `NanGate_15nm_OCL.hgl` | NanGate 15nm open-cell ASIC library |
| `NangateOpenCellLibrary` | `NangateOpenCellLibrary.hgl` | Nangate open-cell ASIC library |
| `lsi_10k` | `lsi_10k.hgl` | LSI Logic 10K ASIC library |
| `EXAMPLE_GATE_LIBRARY` | `example_library.hgl` | Synthetic library used only for docs/tests |

Plus generic helper libraries in `plugins/gate_libraries/definitions/helper_libs/`
(`aoixm_hal_i4.hgl`, `im_hal_i4.hgl`).

**No Intel/Altera library is bundled** (no Agilex, no Stratix/Cyclone/Arria `.hgl`). Any
Verilog/VHDL parsing of an Agilex/Quartus-generated netlist would still require an
Intel-primitive gate library that does not exist in this repository — parsing support for the
HDL is not device support. Adding Agilex/Quartus support is tracked separately in
[fpga-professional-association/hal#22](https://github.com/fpga-professional-association/hal/issues/22)
and is explicitly **not** claimed here.

### 2c. Simulation support

| Engine | Plugin | Status | Notes / Evidence |
|---|---|---|---|
| Built-in event simulator | `simulator` (`hal_simulator`, `PL_SIMULATOR`, default OFF) | Partial | Per its own README (`plugins/simulator/hal_simulator/readme.md:33-35`): tri-state `Z` not supported, gate propagation delays not supported. Device-agnostic (operates on whatever gate library's Boolean functions were parsed), so no device family is more supported than another here — untested per-family, so **unknown** beyond the general engine limitations above. |
| Verilator-backed engine | `plugins/simulator/verilator` (`PL_VERILATOR`, default OFF) | Unknown | Requires a real Verilator toolchain found via `find_package(verilator ...)` at configure time (`plugins/simulator/verilator/CMakeLists.txt:1-8`); not exercised in this pass. |

### 2d. Symbolic execution support

| Capability | Plugin | Status | Evidence |
|---|---|---|---|
| Sequential symbolic execution | `sequential_symbolic_execution` (`PL_SEQUENTIAL_SYMBOLIC_EXECUTION`, default OFF) | Unknown | Device-agnostic at the API level (built on `z3_utils`'s Boolean-function-to-SMT conversion, not on gate-library-specific primitives); no per-family test fixture inspected in this pass, so marked unknown rather than assumed working. |

### 2e. Arithmetic / module recognition

`module_identification`'s structural-candidate generation dispatches on the netlist's gate
library **by exact name string**, in
`plugins/module_identification/src/api/module_identification.cpp:29-45`
(`generate_structural_candidates`):

```cpp
if (gl_name == "ICE40ULTRA" || gl_name == "ICE40ULTRA_iPhone" || gl_name == "ICE40ULTRA_WITH_HAL_TYPES")
{
    candidates = lattice_ice40::generate_structural_candidates(nl);
}
else if (gl_name == "XILINX_UNISIM_WITH_HAL_TYPES" || gl_name == "XILINX_UNISIM")
{
    candidates = xilinx_unisim::generate_structural_candidates(nl);
}
else
{
    return ERR("arithmetic structure generation not available for gate_lib: " + gl_name);
}
```

| Gate library | Status | Notes |
|---|---|---|
| `ICE40ULTRA` / `ICE40ULTRA_WITH_HAL_TYPES` | Supported (dispatch exists) | Implemented in `module_identification/architectures/lattice_ice40.h`. Correctness on real designs not verified in this pass — "supported" here means "has a dispatch path and an implementation," not "verified against a known-good netlist." |
| `XILINX_UNISIM` / `XILINX_UNISIM_WITH_HAL_TYPES` | Supported (dispatch exists) | Implemented in `module_identification/architectures/xilinx_unisim.h`. Same caveat as above. |
| `ICE40ULTRA_iPhone` | Checked in code, no matching library shipped | The dispatch string exists in the source but no `.hgl` file in this repo declares that library name — dead branch as far as the bundled libraries go. |
| `XILINX_SIMPRIM`, `NanGate_15nm_OCL`, `NangateOpenCellLibrary`, `lsi_10k`, `EXAMPLE_GATE_LIBRARY`, any Intel/Agilex library | **Not supported** | Falls through to the `else` branch and returns an error (`"arithmetic structure generation not available for gate_lib: ..."`) — there is no structural-candidate generator for these libraries at all. |

Recognizable operation types, from
`plugins/module_identification/include/module_identification/types/candidate_types.h`
(`CandidateType` enum, `all_checkable_candidate_types`): addition, addition with constant
offset, subtraction (verified the same way as addition per the header's own comment),
counter, negation, absolute value, constant multiplication (with/without offset), equality,
less-than / less-or-equal (signed and unsigned), and generic value-check against a constant.
Candidates are proposed structurally and then verified with an SMT solver
(`execute_on_structural_candidates` in the same file) — a returned candidate is a checked
result for that specific netlist, not a general claim about every design in that gate library.

### 2f. Summary: do not conflate these rows

A "yes" in 2a (parsing) for a given HDL says nothing about 2b (whether the target device's gate
library is bundled), which says nothing about 2c/2d (whether simulation or symbolic execution
have been validated for that library), which says nothing about 2e (whether
`module_identification` has a dispatch path for that library at all). In particular:

- **Agilex / Quartus is not supported by this fork.** Verilog and VHDL parsing exist as
  generic HDL front ends, but no Intel/Altera gate library ships in `plugins/gate_libraries`,
  so there is nothing for `module_identification` to dispatch on and nothing that gives Agilex
  primitives correct simulation semantics. Tracked in
  [#22](https://github.com/fpga-professional-association/hal/issues/22); not claimed here.
- Only `XILINX_UNISIM(_WITH_HAL_TYPES)` and `ICE40ULTRA(_WITH_HAL_TYPES)` have an arithmetic
  recognition path at all; every other bundled library (including the Xilinx `SIMPRIM` and all
  three ASIC libraries) explicitly errors out of `module_identification`.
- Simulation and symbolic execution are implemented generically against the Boolean-function
  representation rather than per-device, so they are marked "unknown" rather than "supported"
  or "unsupported" per family — no fixture in this repository was run through them in this pass
  to confirm correctness for any specific family.

## 3. What this document does not verify

This fork cannot be built on the Windows machine this document was written on (see `CLAUDE.md`,
"Building and testing" — HAL builds on Linux/macOS only). Everything under "Tested" above is
tested by *this fork's own test suites as they exist in source*, not re-run as part of writing
this document. Anyone re-verifying this table should build via
`docker-compose run --rm hal-build` or the `ubuntu22.04.yml` / `ubuntu24.04.yml` /
`ubuntu26.04.yml` GitHub Actions workflows (`workflow_dispatch`-triggerable on this fork), and
update the "Status"/"Evidence" columns with the actual command and result rather than trusting
this document indefinitely.
