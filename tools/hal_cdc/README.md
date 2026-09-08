# hal_cdc — structural clock-domain and reset-domain audit

`hal_cdc` screens a gate-level HAL netlist for clock-domain crossings and
suspicious reset-release structures. It combines what the user declares (which
nets are clocks, which are resets, which primary inputs are already
synchronous) with what it can recover structurally, and reports every path that
leaves one domain and enters another — with the gates that support it.

It is **screening, not sign-off.** Every finding it emits carries
`status: heuristic`, `unknown`, `unsupported` or `error` in the
[`hal_findings`](../hal_findings/README.md) schema. It never emits a proof
status, and every report contains a `cdc/limitations` finding spelling out what
was *not* analysed — including the absence of any metastability or physical
timing model. A clean `hal_cdc` report is not a CDC sign-off.

## Quick start

```bash
# 1. see which nets actually drive clock and reset pins
python tools/hal_cdc discover designs/top.v \
    --gate-library plugins/gate_libraries/definitions/example_library.hgl \
    -o clocks.json

# 2. edit clocks.json: name the domains, drop what is not a clock,
#    add "inputs" for primary inputs that are already synchronous

# 3. run the audit
python tools/hal_cdc audit designs/top.v \
    --gate-library plugins/gate_libraries/definitions/example_library.hgl \
    --declarations clocks.json \
    -o results/cdc.findings.json \
    --dot results/cdc.domains.dot

python tools/hal_findings summary results/cdc.findings.json
dot -Tsvg results/cdc.domains.dot -o results/cdc.domains.svg
```

Exit codes (the convention this fork uses for `hal --python-script` and its
tools):

| code | meaning |
| --- | --- |
| 0 | the audit ran; nothing exceeded `--fail-on` |
| 1 | the audit ran; something did |
| 2 | the audit **could not run** — bad declarations, no HAL, unreadable netlist |

Exit 2 is never a statement about the design, and exit 1 is never a statement
about the tool. `--fail-on` takes `never`, `unsynchronized` (default) or
`any-alarm` (also fails on an unsafe reset release).

## The declaration document

A small versioned JSON file. HAL netlists carry no SDC, so nothing is guessed:
what is not declared ends up in the *unknown* domain, and unknown is reported
as unknown — never as safe.

```json
{
  "version": 1,
  "clocks": [
    {"name": "clk_sys", "net": "clk_sys", "period_ns": 10.0},
    {"name": "clk_io",  "net": "clk_io"}
  ],
  "resets": [
    {"name": "arst_n", "net": "arst_n", "active_low": true,
     "synchronous_to": null,
     "description": "board reset, released asynchronously"}
  ],
  "inputs": [
    {"net": "rx_data", "clock": "clk_io"}
  ],
  "waivers": [
    {"id": "W-CFG-1", "kind": "crossing",
     "source_domain": "clk_sys", "destination_domain": "clk_io",
     "gates": ["cfg_shadow_reg"],
     "rationale": "quasi-static configuration, only written while clk_io is held in reset; reviewed 2026-09"}
  ]
}
```

* `version` must be `1`; an unknown version is rejected outright rather than
  read on a best-effort basis.
* An unknown key is an error, so a typo in `synchronous_to` cannot silently
  disable a check.
* A declaration that names a net the netlist does not contain becomes an
  `error` finding — the run still completes, and the report says why a domain
  came out unknown.
* **A waiver without a non-empty `rationale` is rejected.** A waiver never
  deletes a finding: it keeps it, drops it to `severity: info`, tags it
  `waived`, and attaches the rationale as an *undischarged* `user_provided`
  assumption. Waivers that matched nothing are reported as
  `cdc/waivers/unused` so stale suppressions surface instead of rotting.
* Waiver scope is conjunctive: every field you set must match, fields you omit
  are wildcards. `kind` is one of `crossing`, `control_path`, `reset_release`,
  `any`.

## What it does

### 1. Clock resolution — `domains.py`

For every sequential gate, the net on its `clock`-typed pin is walked backwards
through **buffers and inverters only** (`c_buffer` / `c_inverter`; the only
gates that pass a clock through unchanged). The first driver that is not
clock-transparent is a *generated* clock and is resolved by recursively
resolving each of its inputs:

| inputs carrying a clock | verdict |
| --- | --- |
| exactly one | **derived**: the domain is inherited, flagged `ambiguous`, and the gating nets are recorded. The enable's timing is *not* analysed. |
| more than one | **unknown**: a clock mux or clock-mixing logic; which clock is live is a function of select logic this pass does not evaluate. |
| none, or the driver is sequential | **unknown**: a divided/ripple clock has no structurally determinable phase relationship. |

Multi-driver clock nets, unrouted clock nets, clock pins tied to a constant,
combinational loops and search-budget exhaustion all yield `unknown` with the
reason recorded.

### 2. Domain propagation

Domains are pushed forward to a fixpoint from every sequential-gate output,
every declared clock/reset net and every primary input. Each net ends up with a
set of *known* source domains **and** a set of *unknown* reasons
(`primary_input:rx`, `unresolved_clock:mux_reg`, `latch:l1`, `black_box:bb0`,
`asynchronous_reset:arst`). Both are reported: a net fed by `clk_a` *and* by an
undeclared input is not the same as a net fed by `clk_a` alone. Constants —
including nets driven only by `power`/`ground` tie cells — carry no domain.

### 3. Crossing enumeration — `crossings.py`

One crossing per `(capturing gate, input pin, source domain)`. Each carries the
originating sequential gates in the source domain and the combinational gates
on the path between, found by a backward walk pruned to nets that actually
carry that domain. Registers whose *own* clock is unknown are skipped rather
than given an invented destination domain.

### 4. Classification — `patterns.py`, `resets.py`

Exactly one crossing structure is recognised, and only unambiguously:

> **two_flop_synchronizer** — the crossing signal reaches a flip-flop in the
> destination domain through buffers/inverters only, from exactly one source
> domain, and that flip-flop's output has exactly one load: the data pin of a
> second flip-flop in the same domain.

Everything else is reported:

| classification | when |
| --- | --- |
| `unsynchronized` | no second stage, first stage fans out to >1 load, combinational logic in front of the first stage, or mixed source domains |
| `unsynchronized_control_path` | the crossing lands on an `enable`/`set`/`reset`/`select` pin |
| `unsupported_destination` | the crossing is captured by a latch or another primitive whose capture semantics are not modelled |
| `waived` | matched a scoped waiver (still reported, with the rationale) |

Reset release is classified per register:

| release | meaning |
| --- | --- |
| `synchronized_deassert` | released through a recognised two-stage reset synchroniser clocked by the destination domain, both stages asynchronously cleared by the raw reset |
| `reset_synchronizer_stage` | the register *is* one of those two stages; its asynchronous reset is the intended structure |
| `declared_synchronous` | the user declared the reset synchronous to this domain — taken on their word, recorded as an undischarged assumption |
| `reregistered_deassert` | registered once in the domain, but the two-stage pattern did not match |
| `asynchronous_unsynchronized_deassert` | the raw reset reaches the pin through buffers/inverters only |
| `deassert_through_combinational_logic` | the signal at the pin is not the raw reset |
| `unknown` | the register's clock, or the reset's driver, could not be resolved |

A reset that reaches more than one clock domain gets its own finding: one reset
synchroniser does not cover them all.

## Findings

`report.py` emits, per run:

| id | status | about |
| --- | --- | --- |
| `cdc/declarations/NNN` | `error` | a declaration that could not be bound |
| `cdc/domain/<clock>` | `heuristic` | the registers in a domain, and how their clocks resolved |
| `cdc/domain/<clock>/empty` | `unknown` | a declared clock that names no domain |
| `cdc/clock/unresolved/NNN` | `unknown` | registers grouped by clock-resolution outcome |
| `cdc/crossing/<src>/<dst>/<role>/<class>` | `heuristic` (or `unsupported`) | the crossing paths, with source/destination domains, source gates, path gates and synchroniser stages |
| `cdc/coherency/<src>/<dst>` | `unknown` | several recognised synchronisers on the same domain pair — per-bit synchronisation is not multi-bit coherency |
| `cdc/unknown-source/NNN` | `unknown` | sequential inputs whose source domain is unknown |
| `cdc/reset/<reset>/<domain>/<release>` | `heuristic` / `unknown` | reset release per domain |
| `cdc/reset/<reset>/multi-domain` | `heuristic` | a reset spanning domains |
| `cdc/coverage/primitives` | `unsupported` | latches, black boxes, multi-clock primitives |
| `cdc/waivers/unused` | `unknown` | waivers that matched nothing |
| `cdc/limits/propagation` | `unknown` | a search budget was hit; results are incomplete |
| `cdc/clock-tree/*` | `heuristic` / `unknown` | the `clock_tree_extractor` cross-check |
| `cdc/limitations` | `unsupported` | **always present** |

Documents are deterministic: two runs on the same inputs serialize
byte-identically and hash to the same `hal_findings.serialize.document_digest`.

## Integration notes

**`tools/hal_findings`** — every result is a findings document built with
`hal_findings.model` and validated with `hal_findings.validate` before it is
written. The CLI refuses to emit an invalid document (exit 2, reported as a
hal_cdc bug). Consume the output with `python tools/hal_findings summary`,
`validate` or `normalize` like any other analysis in this repository. Nothing
in `tools/hal_findings` was modified.

**`tools/hal_viz`** — `hal_viz.halenv` does the `hal_py` import, plugin loading
and netlist loading (so `--hal-lib`, `$HAL_PY_PATH`, project directories,
`.hal` files and `--gate-library` all behave exactly as they do for `hal_viz`),
and `hal_viz.dot` builds the `--dot` domain graph. Render it with the same
`dot -Tsvg` invocation `hal_viz` uses. Nothing in `tools/hal_viz` was modified.

**`plugins/clock_tree_extractor`** — run automatically when `hal_py` is
available (disable with `--no-clock-tree`). It is used as a *cross-check*, never
as the source of truth: `clock_tree.py` compares the recovered tree with
hal_cdc's own clock resolution and reports the agreement, and
`--clock-tree-dot` exports the extractor's own Graphviz view as evidence
attached to the finding. The audit's conclusions are identical with and without
it — `test_hal_cdc_hal.py` asserts exactly that.

Why hal_cdc resolves clocks itself rather than reading the extractor's graph:
the extractor answers "what does the clock distribution network look like",
hal_cdc has to answer "which *declared* clock does this register belong to".
They are different questions, and the extractor as it stands cannot answer the
second one for the commonest case — when a flip-flop's clock pin is driven
straight by a global input net, `ClockTree::from_netlist` records the net and
the flip-flop as vertices but adds no edge between them, so a flat "clock
straight from a port" design produces isolated vertices rather than a tree.
Nothing in `plugins/clock_tree_extractor` was modified.

**`tests/headless_smoke/real_netlist_smoke.py`** — not modified.
`test_hal_cdc_hal.py` follows its philosophy (assert results, not exit codes;
fail with an actionable message rather than skipping a check that could have
run) but is a `unittest` module so it composes with the other `*_hal.py`
integration tests in `tools/`.

## Testing

Without a HAL build — 57 tests, runs anywhere:

```bash
python -m unittest discover -s tools/hal_cdc -t tools -p "test_hal_cdc.py"
```

These are not stub tests. They read the real fixture Verilog and the real
`example_library.hgl` through `fixture_netlist.py`, run the real analysis, and
assert the ground truth written down in [`fixtures/README.md`](fixtures/README.md).

With a HAL build (in the verification container):

```bash
export HAL_PY_PATH=<build>/lib HAL_BASE_PATH=<build>
python -m unittest discover -s tools/hal_cdc -t tools -p "test_*_hal.py"
```

Those load the *same* fixtures through HAL's `verilog_parser`, re-assert the
same ground truth, and additionally assert that the full audit signature is
identical to the one the fixture reader produces. The fixture reader is a
convenience for machines that cannot build HAL; HAL's parser is the authority,
and the two are not allowed to disagree.

### Suggested CI wiring

This branch does not touch `.github/`. The two steps to add next to the
existing `Real-netlist headless smoke test` step in the ubuntu workflows are:

```yaml
      - name: hal_cdc unit tests
        shell: bash
        run: python3 -m unittest discover -s tools/hal_cdc -t tools -p "test_hal_cdc.py"

      - name: hal_cdc integration tests
        id: hal_cdc
        shell: bash
        run: python3 -m unittest discover -s tools/hal_cdc -t tools -p "test_*_hal.py"
        env:
          HAL_BASE_PATH: ${{runner.workspace}}/hal/build
          HAL_PY_PATH: ${{ github.workspace }}/build/lib
          HAL_CDC_OUTPUT_DIR: ${{ github.workspace }}/build/hal_cdc
```

The first needs no HAL at all and can run on every platform. The second skips
with a printed reason if `hal_py` is not importable, so it never turns itself
off silently on a machine where it *could* have run. `HAL_CDC_OUTPUT_DIR`
collects the findings documents and DOT files for `actions/upload-artifact`.

## Limits and how they fail

`domains.Limits` caps the clock-source search (`--max-clock-depth`,
`--max-clock-nodes`), the forward propagation (`--max-propagation-steps`) and
the path traces. Hitting a cap always produces `unknown` plus a finding that
names the cap — never a guess, and never a silently short crossing list.

## Layout

```
netlist_view.py     plain-data view of a netlist; the only file that reads hal_py
declarations.py     declaration document, binding, scoped waivers
domains.py          clock resolution and forward domain propagation
crossings.py        crossing enumeration with supporting gate paths
patterns.py         two-flop synchroniser classification
resets.py           reset fan-out and reset-release classification
clock_tree.py       clock_tree_extractor cross-check (optional)
audit.py            run_audit(): the analysis entry point, no I/O
report.py           findings document, domain graph, console summary
fixture_netlist.py  dependency-free .hgl + structural .v reader (tests/dev)
cli.py, __main__.py python tools/hal_cdc discover|audit
fixtures/           four netlists with their declarations and ground truth
```

`audit.run_audit()` takes a `NetlistView` and imports no `hal_py`, so the same
code path is exercised by the fixture-driven tests and by a real run.
