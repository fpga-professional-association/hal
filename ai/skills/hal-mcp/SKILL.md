---
name: hal-mcp
description: Drive HAL through the MCP server (tools/hal_mcp) — a stateful netlist session an agent queries many times instead of paying a full plugin load per CLI call. Use when answering more than one or two questions about the same netlist.
---

# hal_mcp — HAL as an MCP server

## When to use
- More than one or two questions about the same netlist. Each `tools/hal_*`
  CLI call reloads 28 plugins and re-parses the design (1–3 s); the MCP server
  pays that once and answers later calls in milliseconds.
- An agent loop that explores a design: load once, then census → filter gates →
  read a Boolean function → walk a cone → render, all against one session.
- **Not** for one-shot batch work in a script or CI step — the CLIs are simpler
  there and need no server lifecycle. See `netlist-analysis-tools`.
- **Not** for modifying a netlist: v1 is read-only by design.

## Launching

```bash
export HAL_BASE_PATH=/path/to/hal/build      # without this, import hal_py aborts the process
export HAL_PY_PATH=/path/to/hal/build/lib
python3 tools/hal_mcp                        # serve over stdio (default)
python3 tools/hal_mcp tools                  # print the tool surface, no HAL needed
```

The repo's `.mcp.json` already carries a `hal` stanza pointing at
`tools/hal_mcp`; it expands `HAL_BASE_PATH`/`HAL_PY_PATH` from the
environment, so export them before starting the client.

## The tools

| family | tools |
| --- | --- |
| session | `hal_load_netlist` (path + optional gate_library → **session_id**), `hal_load_project`, `hal_list_sessions`, `hal_close_session` |
| inspect | `hal_netlist_stats`, `hal_list_gates` (type/name filters), `hal_gate_info`, `hal_net_info`, `hal_module_tree` |
| traverse | `hal_fan_in`, `hal_fan_out` (depth-bounded, optional stop-at-sequential), `hal_shortest_path` |
| analyze | `hal_boolean_function`, `hal_sccs`, `hal_dataflow_groups` (DANA), `hal_clock_domains` |
| visualize | `hal_render_graph` (`module_tree` \| `netlist_graph` \| `dataflow` → SVG at your path) |

Every tool after the session family takes `session_id` — there is no
`--netlist` argument anywhere else. Anything that can return many items takes
`limit` (default 100, max 1000) and `offset`, and reports the true `total`.

## Reading the results

- A tool failure (bad `session_id`, unloadable netlist) comes back as a normal
  result with `isError: true` and a message saying what to do — not as a
  JSON-RPC error. Reserve protocol errors for malformed requests.
- `hal_boolean_function` returns `function: null` for `tennm_lcell_comb` on the
  Agilex library: the behaviour lives in the instance `lut_mask` and only
  `tools/hal_agilex`'s `elaborate()` attaches it, which mutates the netlist —
  out of bounds for a read-only server. Use `tools/hal_agilex` for that.
- `hal_clock_domains` groups flip-flops structurally by their clock/reset/enable
  nets. It deliberately does **not** use `clock_tree_extractor`, which returns
  excluded vertices on a directly-driven clock (issue #63).

## Pitfalls

- **stdout is the protocol channel.** HAL's C++ logging writes to fd 1 — its
  very first line appears at `import hal_py`. The server installs an fd guard
  (dup fd 1 to a private handle, `dup2(2, 1)`) *before* importing, so all
  native logging goes to stderr. If you extend the server, never `print()` to
  the real stdout and never remove that guard; a test asserts the stream stays
  parseable while a netlist loads.
- **`HAL_BASE_PATH` is not optional.** Without it, `import hal_py` logs
  `Giving up!` and exits the interpreter — fatal for a long-lived server. The
  session layer probes the import in a throwaway subprocess first and reports a
  clean tool error instead of dying.
- Sessions live in the server process. Restarting the client drops them; call
  `hal_list_sessions` rather than assuming an id survived.
- Graphviz is optional: without `dot`, `hal_render_graph` still writes the
  `.dot` and says so in a `note`.

## Where things live
- `tools/hal_mcp/` — `server.py` (fd guard, JSON-RPC, dispatch), `session.py`
  (registry, lazy hal_py import), `tools.py` (schemas + handlers).
- `tools/hal_mcp/README.md` — full tool reference and the `.mcp.json` stanza.
- `tools/hal_mcp/test_hal_mcp.py` — 47 tests driving the server over a pipe;
  the protocol tier runs without a build. Registered as
  `runTest-hal_mcp_standalone`.
