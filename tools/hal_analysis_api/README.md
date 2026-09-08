# hal_analysis_api — bounded, structured HAL tools for agents

An agent driving HAL today writes Python, runs it through `hal --python-script`
and reads whatever it printed. For a human that is fine: you can look at 400
gate names and decide what matters. For an agent it fails in one of two ways —
it receives a dump it cannot afford to read, or a summary it cannot verify.
Both are the same mistake: **the interface has no bounds**.

This package is the local API that fixes that. Every operation is a named tool
with a JSON Schema for its request *and* its response, every answer arrives in
an explicit envelope, every listing is paginated, and every traversal that
could be cut says whether it was.

```bash
python tools/hal_analysis_api tools                      # what exists
python tools/hal_analysis_api schema netlist.cone        # exact request/response schema
python tools/hal_analysis_api call project.open --arg path=examples/uart.zip
python tools/hal_analysis_api call netlist.cone \
    --json '{"project":"prj-82eca4dad323","seed_gate_ids":[12],"direction":"fan_in","depth":2}'
python tools/hal_analysis_api mcp                        # the same tools over MCP
```

MCP is an **adapter**, not the product: it advertises the schemas this API
already publishes and forwards calls to it. Nothing here requires a cloud
model, a network, or the `mcp` package.

## The envelope

```json
{"ok": true,  "api_version": "1.0.0", "tool": "netlist.cone", "result": { ... }}
{"ok": false, "api_version": "1.0.0", "tool": "netlist.cone",
 "error": {"code": "unknown_object", "message": "no gate with id 9999 in this netlist",
           "hint": "every seed of a cone must exist; find gates with netlist.gates",
           "data": {"kind": "gate", "id": 9999}}}
```

A caller branches on `error.code`, never on prose:

| code | means |
| --- | --- |
| `invalid_request` | the request does not match the schema, or contradicts itself |
| `unknown_tool` | no such tool in this API version |
| `unknown_project` | no such project handle — open one with `project.open` |
| `unknown_object` | no gate/net/module with that id **in this netlist** |
| `unknown_analysis` | no such analysis; see `analysis.list` |
| `unknown_job` / `unknown_artifact` | no such job, or the job never wrote that file |
| `not_ready` | the answer does not exist yet (the job is still running) |
| `limit_exceeded` | the request asks for more than the bounds allow |
| `timeout` | the operation hit its limit and was stopped; carries the limit |
| `hal_unavailable` | no usable `hal` binary for this operation |
| `hal_error` | HAL ran and failed; the logs are the evidence |
| `internal` | this API broke — a bug report, not a design answer |

An unknown id is never an empty result, a cut is never a short list, and a
crash is never a plausible-looking answer. Those three are the whole point.

## The tools

`hal.capabilities` reports all of this at runtime, including whether a `hal`
binary was found. `mutates` is part of the contract: **every read is
`nothing`**, and the two operations that do change something say so.

| tool | mutates | needs HAL | what it answers |
| --- | --- | --- | --- |
| `hal.capabilities` | nothing | no | tools, analyses, limits, error codes, hal binary |
| `hal.schema` | nothing | no | one tool's request and response schema, self-contained |
| `project.open` | nothing | no | a handle, pinned to the content's digest |
| `project.list` / `project.describe` | nothing | no | which handles exist; does the content still match |
| `project.close` | session | no | forget a handle (the project files are untouched) |
| `netlist.summary` | nothing | **yes** | design name, counts, gate-type histogram |
| `netlist.gates` / `.nets` / `.modules` | nothing | **yes** | one filtered page of objects |
| `netlist.gate` / `.net` | nothing | **yes** | one object, with what drives it and what it drives |
| `netlist.cone` | nothing | **yes** | the bounded fan-in/fan-out cone around seed gates |
| `analysis.list` | nothing | no | the analyses `hal_runner` can run, with their options |
| `analysis.submit` | job | **yes** | start one analysis, return a job id |
| `analysis.status` | nothing | no | where a job stands; optionally wait, bounded |
| `analysis.cancel` | job | no | stop a job and its HAL process group |
| `analysis.jobs` | nothing | no | the jobs in this workspace |
| `findings.get` | nothing | no | the job's findings, validated, one page at a time |
| `artifact.list` / `artifact.get` | nothing | no | what the job wrote, up to a byte budget |

## One scoped investigation

The sequence in `examples/scoped_investigation.py`, which is also what the
smoke test runs:

```
hal.capabilities                     -> 21 tools, hal at <build>/bin/hal
project.open   examples/uart.zip     -> prj-82eca4dad323, sha256 0921079a645f74a9…
netlist.summary                      -> test_ext_uart: 407 gates, 409 nets, 258x FFR
netlist.gates  gate_type=FFR limit=5 -> page 1 of 258 (has_more, next_offset 5)
netlist.gate   id=<one of them>      -> its pins, and what drives each one
netlist.cone   fan_in depth=2        -> the scope: N gates, M edges, truncation
netlist.cone   max_gates=5           -> truncated: {"kind": "gates", "limit": 5, …}
analysis.submit graph_algorithm.connected_components
analysis.status wait_s=30            -> running … succeeded
findings.get   limit=3               -> 4 findings; the UART's 68/50/15 feedback loops
artifact.list                        -> manifest.json, findings.json, the step's logs
```

The last step is what makes a finding usable rather than a claim: every
finding is scoped to an `artifact_id`, and that artifact carries the same
`sha256` the project handle was opened with. The evidence is bound to an exact
input, which is the rule [`hal_findings`](../hal_findings/README.md) exists to
enforce.

## Bounds

| bound | default | max | applies to |
| --- | --- | --- | --- |
| page `limit` | 50 | 500 | `netlist.gates` / `.nets` / `.modules`, `analysis.jobs` |
| findings `limit` | 20 | 200 | `findings.get` |
| `max_gate_types` | 32 | 256 | `netlist.summary` |
| `max_endpoints` | 64 | 1000 | `netlist.gate` / `netlist.net` |
| cone `depth` | 3 | 32 | `netlist.cone` |
| cone `max_gates` | 200 | 2000 | `netlist.cone` |
| `timeout_s` (query) | 300 | 3600 | every `netlist.*` call |
| `timeout_s` (analysis) | 900 | 86400 | `analysis.submit` |
| `wait_s` | — | 300 | `analysis.status` |
| `max_bytes` | 65536 | 1048576 | `artifact.get` |

The maxima are enforced *by the schema*, so an over-large request is rejected
before a HAL process starts. What is returned carries the cut:

```json
"page":       {"offset": 0, "limit": 100, "returned": 100, "total": 258,
               "has_more": true, "next_offset": 100}
"truncation": {"truncated": true, "kind": "gates", "limit": 5,
               "reason": "the cone reached the 5-gate budget after 2 hop(s); 7 further
                          gate(s) were adjacent to it and were not expanded"}
```

`truncation` is present with `truncated: false` when nothing was cut, so a
caller never has to guess whether the field is missing or the answer is
complete.

## How a read cannot modify a project

Each `netlist.*` call is one `hal --python-script` process
(`inhal/query_script.py`). It loads the netlist, answers, and exits **without
saving**: no `ProjectManager` serialization, no `create_*`, no writer of any
kind is ever called. The guarantee is the process boundary, not a promise —
even a bug in a query function cannot persist anything.

That also buys a limit that bites (a HAL analysis is C++ with no cancellation
points; a process can be killed, a thread cannot — see
[`hal_runner`](../hal_runner/README.md)) and a blast shield: a segfault in a
parser costs one call.

The price is a HAL start and a netlist load per call, and it is why the tools
are shaped the way they are: ask for a cone or a filtered page, not a
per-gate loop. `hal.capabilities` says so in its `notes`.

## Project handles

```bash
python tools/hal_analysis_api call project.open --arg path=examples/uart.zip
# -> "project": "prj-82eca4dad323"
```

A handle is derived from the resolved path **and the content digest** (a
`sha256` for a file, `sha256-tree/1` for a directory — the same vocabulary
`hal_runner` uses). Re-opening the same content returns the same handle;
changing the file produces a different one, and `project.describe` reports
`still_matches_digest: false` for the old one. Gate ids are only meaningful
inside one netlist, so quoting a handle with an id is not bureaucracy — it is
what makes the id mean anything.

Handles live in the workspace (`$HAL_ANALYSIS_API_WORKSPACE`, else
`build/hal_analysis_api`) as small JSON files, not in process memory, because
the analysis worker is a different process and a CLI call is its own.

## Analyses run through hal_runner

`analysis.submit` builds a one-step [`hal_runner`](../hal_runner/README.md) run
configuration, validates it **before** spawning anything (an unknown analysis
or a mistyped option is an error at submit time, not a job that dies two
minutes later), writes it to the job directory and starts a detached worker.

```
<workspace>/jobs/job-1a2b3c4d5e6f/
    job.json            state, timings, exit code, where the findings are
    run_config.json     re-runnable by hand: python tools/hal_runner run <this>
    worker.pid          written by whoever spawned the worker; the worker owns
                        job.json from the moment it starts, so there is exactly
                        one writer per file and a fast job cannot be overwritten
                        with the "queued" copy the submitter still holds
    worker.log
    run/manifest.json   inputs pinned by digest, tool versions, limits, outcome
    run/steps/analysis/ findings.json, result.json, logs/
```

Everything the runner already guarantees is inherited rather than
reimplemented: content-pinned inputs, per-step limits killed across the process
group, schema-validated findings, and a diagnostic findings document for a step
that failed or timed out. `findings.get` labels that case `source:
"diagnostic"` — it is a real findings document, never a substitute for a result
about the design.

States are `queued → running → succeeded | failed | timeout | cancelled`, plus
`lost`: a worker that vanished without recording an outcome. A status that can
never resolve is worse than a failure, so it is reported.

`analysis.status` with `wait_s` blocks up to five minutes. **An expired wait is
not an error** — the answer is the real state with `wait_timed_out: true`.

## MCP

The adapter (`mcp_adapter.py`) is ~150 lines and contains no analysis logic. It
lists the tools this API already describes and forwards calls; error envelopes
come back as themselves with the transport's `isError` set.

```bash
python -m pip install 'mcp>=1,<3'
python tools/hal_analysis_api mcp        # stdio server
```

```jsonc
// an MCP client's server entry
{"command": "python", "args": ["tools/hal_analysis_api", "mcp",
                               "--hal-binary", "/path/to/hal/build/bin/hal"],
 "env": {"HAL_BASE_PATH": "/path/to/hal/build"}}
```

**Verified against the real SDK, not from memory.** The official package is
`mcp` (`modelcontextprotocol/python-sdk`), and its server interface changed
between majors:

| | `mcp` 1.x (checked against 1.29.0) | `mcp` 2.x (checked against 2.2.0) |
| --- | --- | --- |
| low-level server | `mcp.server.lowlevel.Server` | `mcp.server.Server` |
| handlers | `@server.list_tools()` / `@server.call_tool()` | constructor args `on_list_tools=` / `on_call_tool=`, signature `(ctx, params)` |
| high-level class | `FastMCP` | renamed `MCPServer` |
| tool schema field | `inputSchema` | `input_schema`, with `inputSchema` as an alias |
| transport | `mcp.server.stdio.stdio_server()` + `Server.run(read, write, init_options)` | unchanged |

`availability()` detects which one is installed and picks the matching path; an
`mcp` major outside 1–2 is **refused with the installed version named** rather
than half-driven, and a missing `mcp` package is reported with the install
command. Both paths were exercised over a real in-memory client/server session
(21 tools listed, a call forwarded, an error envelope returned with `isError`
set) with:

```bash
python -m pip install --target /tmp/mcp2 'mcp==2.2.0'
PYTHONPATH=/tmp/mcp2 python -m unittest \
    hal_analysis_api.test_hal_analysis_api.McpAdapterTests
```

## Layout

```
errors.py          typed errors and the response envelope
limits.py          every numeric bound, in one place
schema/            the versioned request/response schemas
schemas.py         the tool registry, request/response validation
handles.py         project handles, pinned by content digest
netlist_query.py   the read-only queries, duck-typed against hal_py
inhal/             the in-HAL side, run by 'hal --python-script'
query_protocol.py  the request/response contract across that boundary
queries.py         host side of a query: subprocess, limit, typed errors
jobs.py            analysis jobs on top of hal_runner
worker.py          the detached process that executes one job
api.py             the dispatch table -- the API itself
cli.py             python tools/hal_analysis_api call <tool> ...
mcp_adapter.py     the optional MCP transport
fixtures/          a hand-crafted netlist with recorded ground truth
examples/          the worked agent workflow
```

Everything except `inhal/` runs on a plain interpreter with the standard
library only.

## Tests

```bash
python -m unittest discover -s tools/hal_analysis_api -t tools -p "test_*.py"
```

71 tests, no HAL and no netlist. The query layer is duck-typed against the
`hal_py` bindings, so a stub netlist built from
`fixtures/cone_fixture.ground_truth.json` exercises pagination, filtering,
cones and truncation for real; and the API reaches HAL through exactly one
object (`QueryRunner.executor`), so a stub executor exercises timeouts, crashes
and typed errors crossing the process boundary. The negative cases are the
point: an unknown id that must not come back empty, a cut that must be visible,
a wait that expires without becoming a failure, a worker that vanished, an
artifact path that tries to leave the job directory.

What that cannot cover is whether HAL agrees. That is
`tests/headless_smoke/analysis_api_smoke.py`, which needs a built HAL and does
not skip without one:

```bash
HAL_BASE_PATH=<build> python3 tests/headless_smoke/analysis_api_smoke.py \
    --hal-binary <build>/bin/hal --work-dir <build>/analysis_api_smoke --keep
```

It parses the hand-crafted fixture and checks **every cone in the ground truth
against HAL's own answer**, re-derives the UART's 407/409 counts and its
gate-type histogram through the API, walks all 258 flip-flops by page without a
repeat, proves a millisecond limit really kills a query, submits the component
analysis and checks that the findings carry the UART's 68/50/15 feedback loops
bound to the archive's own `sha256`, and drives the CLI and the example
workflow as subprocesses.

## Integration notes

Two hooks are deliberately **not** made here, to keep this change to new files
only:

* `tests/headless_smoke/CMakeLists.txt` registers the standalone suites of
  `hal_viz` and `hal_runner` as ctest cases. The equivalent for this package is

  ```cmake
  add_test(NAME runTest-hal_analysis_api_standalone
           COMMAND ${Python3_EXECUTABLE} -m unittest discover
                   -s ${CMAKE_SOURCE_DIR}/tools/hal_analysis_api
                   -t ${CMAKE_SOURCE_DIR}/tools -p "test_*.py"
           WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
  ```

* `.github/workflows/ubuntu24.04.yml` runs `real_netlist_smoke.py` and
  `runner_smoke.py` as their own steps. `analysis_api_smoke.py` belongs beside
  them:

  ```yaml
  - name: Analysis API smoke test
    run: |
      HAL_BASE_PATH=$PWD/build python3 tests/headless_smoke/analysis_api_smoke.py \
        --hal-binary $PWD/build/bin/hal --work-dir $PWD/build/analysis_api_smoke
  ```

* When `tools/hal_capabilities` lands (issue #17), `hal.capabilities` already
  probes for it: `api._optional_integrations()` imports it if it is there and
  offers its inventory through its own entry point, and reports it as
  unavailable otherwise. No interface is guessed at.
