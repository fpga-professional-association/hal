# hal_mcp — a Model Context Protocol server for HAL

Every `hal` CLI invocation loads all 28 plugins and re-parses the netlist. That
is one to three seconds *per question*, and the walkthrough transcripts are
full of the repeated `loading plugin` lines to prove it. An agent that asks
twenty questions about one design pays it twenty times.

`hal_mcp` pays it once. `hal_load_netlist` returns a **session id**; the parsed
`Netlist` stays in the server process; every later tool call answers against
it in milliseconds. The statefulness is the whole point — there is no
`--netlist` argument on any other tool, only `session_id`.

- **Pure standard library.** No new dependency. MCP's stdio transport is
  newline-delimited JSON-RPC 2.0, implemented directly in `server.py`.
- **Read only.** No tool modifies or writes a netlist or a project. The one
  filesystem write is `hal_render_graph`, into the path you give it.
- **Bounded.** Every tool that can return many items takes `limit` (default
  100, hard maximum 1000) and `offset`, and reports the true total. A
  100k-gate netlist never lands in one response.

## Launching it

The server needs a **built HAL** to load anything (the protocol itself works
without one). Two environment variables matter:

| variable | why |
| --- | --- |
| `HAL_BASE_PATH` | HAL's build or install directory. **Without it `import hal_py` aborts the interpreter** — see *HAL API notes* below. |
| `HAL_PY_PATH` | directory holding `hal_py` (`<build>/lib`), added to `sys.path` by `hal_viz.halenv`. |

```bash
export HAL_BASE_PATH=/path/to/hal/build
export HAL_PY_PATH=/path/to/hal/build/lib
python3 tools/hal_mcp            # 'serve' is the default
python3 tools/hal_mcp tools      # print the tool surface; needs no HAL
python3 tools/hal_mcp --help
```

Run it from the repository root, or with `tools/` on `PYTHONPATH`: the server
reuses `hal_viz.halenv` for the `hal_py` import and the netlist loading, and
`hal_viz.extract` / `hal_viz.render` for `hal_render_graph`.

Graphviz's `dot` is optional. Without it `hal_render_graph` still writes the
`.dot` file and says so in a `note`.

### Client configuration

Add this stanza to `.mcp.json` at the repository root (this file does **not**
edit `.mcp.json` for you), next to the `logic2` entry that is already there:

```json
{
  "mcpServers": {
    "hal": {
      "command": "python3",
      "args": ["tools/hal_mcp"],
      "env": {
        "HAL_BASE_PATH": "/path/to/hal/build",
        "HAL_PY_PATH": "/path/to/hal/build/lib"
      }
    }
  }
}
```

Outside this repository, give absolute paths and set the working directory
accordingly:

```bash
claude mcp add hal -- python3 /path/to/hal/tools/hal_mcp
```

## The session model

```
hal_load_netlist(path, gate_library)  ->  session_id "s1"
        │
        ├─ hal_netlist_stats(session_id="s1")
        ├─ hal_list_gates(session_id="s1", type_contains="ff")
        ├─ hal_fan_in(session_id="s1", gate="count[3]", depth=3)
        └─ ...
hal_close_session(session_id="s1")
```

- Session ids are `s1`, `s2`, … and are unique for the life of the process.
- Sessions live until `hal_close_session` or until the server exits. Closing
  writes nothing; the netlist file is untouched.
- Several sessions can be open at once — load two revisions of a design and
  ask both the same question.
- `hal_list_sessions` recovers an id you lost track of.
- HAL's plugins are loaded once per process, on the first load.

**Failure is data.** A bad session id, a netlist that will not parse, a plugin
that was not built — none of these are JSON-RPC errors. They come back as a
normal `tools/call` result with `"isError": true` and a message that says what
to do instead. JSON-RPC errors (`-32700`, `-32600`, `-32601`, `-32602`) are
reserved for protocol faults.

## Tools

### Session

| tool | what it does |
| --- | --- |
| `hal_load_netlist` | Load a `.hal` file, an HDL netlist (`.v`/`.vhd`, needs `gate_library`) or a project directory. Returns `session_id` plus design name and gate/net/module counts. **The expensive call — make it once.** |
| `hal_load_project` | Same, for a HAL project directory (`hal --import-netlist … --project-dir …`, or an unzipped `examples/*.zip`). |
| `hal_list_sessions` | Every loaded netlist with its id, source and size. Paginated. |
| `hal_close_session` | Close one session and free its netlist. |

### Inspect

| tool | what it does |
| --- | --- |
| `hal_netlist_stats` | Gate/net/module counts, how many gates are sequential, the gate-type histogram (most frequent first, paginated), the top module, and the global input/output/GND/VCC nets. The usual first call. |
| `hal_list_gates` | Gates filtered by a case-insensitive substring of the type (`type_contains`) and/or name (`name_contains`), optionally `sequential_only`. Paginated. |
| `hal_gate_info` | One gate: type and type properties, `is_sequential`, module, placement location when the netlist carries one, and every pin with the net attached to it (including which pins sit on GND/VCC). |
| `hal_net_info` | One net: driver(s), destinations as gate + pin (paginated), and whether it is a global input, a global output, GND or VCC. |
| `hal_module_tree` | The module hierarchy from the top module (or a named module) down to `depth`, depth-first pre-order. Each row carries `depth` and `parent_id` — together, the tree — plus its direct gate and submodule counts. Modules cut off by `depth` are flagged `truncated_here`. |

Gates, nets and modules are addressed the same way everywhere: a numeric id, an
exact name, or a substring that matches exactly one object. An ambiguous
substring is an error that lists the candidates.

### Traverse

| tool | what it does |
| --- | --- |
| `hal_fan_in` | Every gate reachable backwards from a `gate` or a `net` within `depth` hops, with its distance. |
| `hal_fan_out` | The forward twin. |
| `hal_shortest_path` | The shortest gate path between two gates (`hal_py.NetlistUtils.get_shortest_path`). `search_both_directions` also looks for a shorter path from end to start — the result may then be in reverse order. |

`stop_at_sequential=true` reports flip-flops and latches but does not traverse
*through* them, which bounds the walk to one clock cycle of combinational cone.
That is usually what you want when you are asking "what feeds this register".

### Analyze

| tool | what it does |
| --- | --- |
| `hal_boolean_function` | `Gate.get_resolved_boolean_function` for one output pin, or for every output pin. `use_net_variables=false` (default) gives variables named after the gate's **input pins**; `true` gives `net_<id>` **net variables**, which is what you want when composing functions across gates. (The flag used to mean the opposite; see the CHANGELOG entry for the fix.) |
| `hal_sccs` | Strongly connected components of the gate graph via `graph_algorithm` (igraph). Combinational logic is a DAG, so every component larger than one vertex is a feedback loop — the cheapest first cut between logic and state on an unknown netlist. `min_size` defaults to 2. |
| `hal_dataflow_groups` | DANA (`dataflow_analysis`) over the flip-flops: `Configuration(netlist).with_flip_flops()`, plus optional `min_group_size`, `expected_sizes`, `stage_identification`, `type_consistency`. Each group is a *hypothesis* that those flip-flops form one word. The slow tool. |
| `hal_clock_domains` | Groups every sequential gate by the nets on its clock / reset / set / enable pins, read structurally from the gate library's pin types. Constant-tied control pins are ignored; flip-flops with no clock net are called out separately. |

`hal_clock_domains` deliberately does **not** use the `clock_tree_extractor`
plugin: it has a known bug returning vertices it was asked to exclude
(issue #63). Reading the pin types is exact and needs no plugin.

Two flip-flops sharing a clock, a reset and an enable *could* be one register;
two that do not share them cannot be. It is a necessary condition, never a
sufficient one — cross-check a domain against `hal_dataflow_groups`.

### Visualize

| tool | what it does |
| --- | --- |
| `hal_render_graph` | `kind=module_tree` draws the module hierarchy; `kind=netlist_graph` draws a gate-level graph (scope it with `module` or `gate` plus `depth` — `max_gates`, default 400, refuses a hairball); `kind=dataflow` draws DANA's register groups. |

`output_path` names the image; the extension picks the format (`.svg`, `.png`,
`.pdf`, default `.svg`). The Graphviz `.dot` source is always written next to
it, so a machine without Graphviz still gets a usable artifact — the response
reports both paths and, when rendering was skipped, why.

## Protocol details

- **stdio transport**: one UTF-8 JSON object per line, newline-delimited, no
  embedded raw newlines. Batched requests are rejected.
- **Methods**: `initialize`, `notifications/initialized` (and other
  notifications, ignored), `ping`, `tools/list`, `tools/call`. Anything else is
  `-32601`. Malformed JSON is `-32700`. A message with no `id` never gets a
  response.
- **`initialize`** echoes the client's `protocolVersion` when it is one of
  `2025-06-18`, `2025-03-26`, `2024-11-05`, and otherwise answers with the
  newest. It advertises `{"tools": {}}` and a `serverInfo` of
  `{"name": "hal-mcp", "version": …}`.
- **`tools/call` results** are MCP content blocks:
  `{"content": [{"type": "text", "text": "<JSON document>"}], "isError": false}`.

### stdout is the protocol channel

HAL's C++ logging writes to **file descriptor 1**. One `[core] [info] loading
plugin …` line between two JSON objects makes the stream unparseable, and
loading a netlist emits dozens of them.

So before anything imports `hal_py`, `server.install_stdout_guard()` dups fd 1
to a private handle that only the protocol writer holds, then dups fd 2 *onto*
fd 1. Every native log line, every stray `print()` and every library that
assumes stdout is free ends up on stderr. It is the same manoeuvre
`tools/hal_capabilities/cli.py` performs around its document output, made
permanent.

`test_hal_mcp.py` tests it explicitly: it loads a netlist (which logs heavily),
asserts every response line still parses as JSON, and asserts HAL's `[core]`
lines really did show up — on stderr.

If you write a new tool here, you do not have to do anything special. The guard
is process-wide.

## HAL API notes

- **`HAL_BASE_PATH` is not optional.** A plain interpreter that imports
  `hal_py` without it hits `log_critical("Cannot determine base path … Giving
  up!")` in `src/utilities/utils.cpp` and the **process exits**. In a one-shot
  CLI that is a bad error message; in a long-lived server it would kill every
  open session mid-conversation. `session.py` therefore imports `hal_py` in a
  throwaway subprocess first and, if that dies, reports it as a tool error.
- **Plugin bindings live under `hal_plugins.*`**, never at top level:
  `from hal_plugins import graph_algorithm`, after
  `hal_py.plugin_manager.load_all_plugins()`. HAL's *parsers* are plugins too,
  so nothing loads before that call.
- **`Netlist` has no `get_net_by_name` or `get_gate_by_name`.** Every lookup by
  name is a scan, which is why the tools accept a numeric id as well.
- **The traversal helpers are `hal_py.NetlistUtils`, capitalised.** The C++
  namespace is `netlist_utils` and `.claude/skills/using-hal/SKILL.md` says
  `hal_py.netlist_utils`, but `netlist_utils.cpp` binds the submodule as
  `m.def_submodule("NetlistUtils", …)`. `hal_py.netlist_utils` raises
  `AttributeError`.
- **Pin types carry the semantics.** `GatePin.get_type()` is `clock`, `reset`,
  `set`, `enable`, `data`, … in the gate library, which is what makes
  `hal_clock_domains` structural rather than heuristic.
- **FPGA cells have no Boolean function until they are elaborated.**
  `AGILEX_TENNM.hgl` gives `tennm_lcell_comb` no function: the behaviour is in
  the instance's `lut_mask` data. `hal_boolean_function` therefore reports
  `function: null` with an explanation for those cells, rather than pretending.
  `tools/hal_agilex/hal_adapter.elaborate()` attaches the real functions, but it
  *modifies* the netlist, so this read-only server does not call it.

## Tests

```bash
# with a build: everything runs
HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib PYTHONPATH=<build>/lib \
    python3 tools/hal_mcp/test_hal_mcp.py

# without one: the protocol tests still run, the rest skip with a reason
python3 tools/hal_mcp/test_hal_mcp.py
```

The suite drives the server as a real subprocess over its stdio pipes — full
handshake, `tools/list` schema sanity, then `tools/call` round trips for the
whole surface against `examples/agilex3_walkthroughs/01_blinky_counter`
(50 gates: 24 `tennm_ff`, 24 `tennm_lcell_comb`, `HAL_GND`, `HAL_VCC`; one
clock domain on `clk`; 24 two-gate SCCs).

`tests/cli_contract/test_cli_contract.py` enrolls this package automatically
and checks `--help`, unknown options and unknown subcommands.
