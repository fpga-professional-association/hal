---
name: hal-agilex
description: Check whether an Altera Agilex 3 Quartus .vo export is inside HAL's validated primitive coverage, import/elaborate it, validate its behavior against a Python RTL reference, or record a bounded per-cycle value trace of it. Use for anything touching tennm_lcell_comb/tennm_ff, a .vo file, or an agilex3_walkthroughs example.
---

# hal_agilex — validated Altera Agilex primitive support

## When to use
- You have a Quartus Prime Pro `.vo` export (Agilex 3, post-synthesis) and need
  to know what's covered (`tennm_lcell_comb`, `tennm_ff`) versus what's an
  unsupported gap (RAM, MAC, PLL, transceivers, IO — everything else).
- You want to import a `.vo` into HAL-readable Verilog, or validate that the
  imported netlist behaves like a Python reference model of the original RTL.
- You're working inside `examples/agilex3_walkthroughs/*` — every walkthrough
  is built from this tool's `inventory`/`behavior`/`recognize` output.
- You want the *values*, not just a verdict: `trace` records every net in every
  cycle of a bounded, seeded window, which `hal_viz clock_step` paints onto a
  `hal_viz dag` drawing.
- `inventory`, `import`, `behavior`, `trace`, `recognize`, `fixture`, `library`
  run on a plain Python 3 stdlib interpreter — **no HAL build needed**. Only
  `elaborate` (and `hal_adapter.py`) touch `hal_py`.

## Quickstart
```bash
# no HAL needed for any of these
EX=examples/agilex3_walkthroughs/01_blinky_counter
python3 tools/hal_agilex inventory "$EX/blinky_counter.vo" \
    -o "$EX/artifacts/hal_agilex_inventory.json"

python3 tools/hal_agilex recognize "$EX/blinky_counter.vo"

# netlist-vs-RTL behavior check: simulates the *exported netlist* with the
# modelled primitive semantics and compares against a Python reference model
python3 tools/hal_agilex behavior "$EX/blinky_counter.vo" \
    --reference "$EX/recovered_reference.py" --cycles 1200 \
    -o "$EX/artifacts/hal_agilex_behavior.json"

# the same run, recorded: every net's value in every cycle of a bounded
# window, as plain JSON -- feed it to `hal_viz clock_step`
python3 tools/hal_agilex trace "$EX/blinky_counter.vo" \
    --reference "$EX/recovered_reference.py" --cycles 32 \
    -o "$EX/artifacts/dag_trace.json"

# HAL-readable Verilog from the .vo (already done for the shipped examples,
# as netlist.hal.v)
python3 tools/hal_agilex import "$EX/blinky_counter.vo" -o "$EX/netlist.hal.v"

# elaborate: the one subcommand that needs a built HAL
HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib PYTHONPATH=/work/build/lib \
    python3 tools/hal_agilex elaborate "$EX/netlist.hal.v" \
    --gate-library plugins/gate_libraries/definitions/AGILEX_TENNM.hgl \
    --hal-lib /work/build/lib
```

## The commands that matter

| command | does | needs HAL |
| --- | --- | --- |
| `inventory export.vo [-o F] [--strict]` | primitive coverage → findings; `--strict` exits 2 on any unsupported/error result | no |
| `import export.vo -o out.v` | rewrite `.vo` into HAL-readable Verilog (refuses uncovered primitives outright) | no |
| `behavior export.vo --reference model.py [--cycles N] [-o F]` | simulate netlist vs. Python RTL model → `proven_bounded`/`counterexample` finding | no |
| `trace export.vo --reference model.py [--cycles N] [--skip N] [--seed N] [--hold NAME=V] [--clear-cycle N] [-o F]` | per-cycle value of every net over a bounded window → plain JSON (**not** a findings document: a trace makes no claim) | no |
| `recognize export.vo [-o F]` | structural arithmetic (adder/counter) recognition → finding | no |
| `fixture DIR [--cycles N]` | run the full pipeline against a `fixtures/*` manifest | no |
| `library [-o F] [--check]` | (re)generate/verify `AGILEX_TENNM.hgl` against `library.py` | no |
| `elaborate netlist.hal.v --gate-library FILE --hal-lib DIR` | load + attach per-gate Boolean functions HAL cannot derive from `lut_config` alone | **yes** |

Exit codes: `0` success, `1` failure, `2` with `--strict` when findings contain
a counterexample, error, or unsupported result.

## The `--reference` contract
A reference model is a plain Python module. Shape (read
`examples/agilex3_walkthroughs/08_shift_debouncer/reference.py` for the full
example):
```python
KIND = "sequential"                          # or "combinational"
INPUTS  = [("clk", 1), ("rst_n", 1), ("btn_raw", 1)]
OUTPUTS = [("btn_state", 1), ("btn_rise", 1)]
IGNORED_INPUTS = ["clk"]                      # simulator steps registers itself
ASYNC_CLEAR_INPUT = "rst_n"                   # active-low async reset

def initial_state(): ...
def outputs(state, values): ...
def next_state(state, values): ...
```
`trace` reads the same contract, and only the sequential half of it: the
driveable inputs are `INPUTS` minus `IGNORED_INPUTS`, drawn from
`random.Random(seed)` one draw per input per cycle, with `ASYNC_CLEAR_INPUT`
asserted before the first cycle and on every `--clear-cycle` (default: cycle 0
only). `--hold NAME=VALUE` pins an input for the whole run — it may not name the
clear input, which already has a schedule — and `--skip N` advances the same
seeded stream without recording, so `--skip 100 --cycles 32` is a genuine window
of one long run rather than a second, differently-seeded short one.
`state` is treated as an opaque immutable value, so a model that wraps a mutable
object has to snapshot it (see `03_uart_tx/reference.py`, which exposes both
interfaces onto one model).

## Pitfalls
- Coverage is exactly two primitives, each in **one** configuration:
  `tennm_lcell_comb` (normal + arithmetic mode, extended_lut/shared_arith off)
  and `tennm_ff` (sync capture + async active-low `clrn`, no `sclr`/`aload`).
  An instance using `sclr`, 7-input fracturable mode, or shared arithmetic is
  *out of coverage* even though it's the same gate type — it is reported
  `unsupported`, never silently given the modelled behavior.
- Coverage-bound checks are exactly that — bounded. `08_shift_debouncer`'s
  negative control is the concrete case: a saturation clamp moved one count
  early passes a 200-cycle `behavior` run clean and only fails at cycle 280 on
  a longer run. **Pair every positive `behavior` bound with a negative control**
  (a deliberately wrong reference model) — a passing bounded check without one
  proves nothing about whether the bound was long enough.
- Findings record the input file's **sha256**. A CRLF checkout of the `.vo`
  hashes differently than the committed LF version, so committed
  `inventory`/`behavior`/`recognize` findings will not reproduce byte-for-byte
  on a CRLF checkout. Run against LF files (the container checks out LF); if
  you must regenerate, write to a scratch path (`/tmp/...json`), don't
  overwrite the committed document as a side effect.
- A `trace` is *not* a findings document and deliberately does not carry a
  status: it is raw evidence for a picture. What makes it usable as evidence is
  that it is the run `behavior` checks — same simulator, same seed, same
  stimulus conventions — so keep the two in step when you change either.
- Unlike a findings document, a committed trace **is** meant to reproduce on a
  CRLF checkout, which is why `trace.differences()` (what every walkthrough's
  `check.py` uses) compares `nets`/`gates`/`ports`/`stimulus`/`window`/`frames`
  and deliberately skips `source.sha256`. The digest is still recorded; it just
  is not the reproducibility criterion.
- Pick the window for what it *shows*, and say so on the page. A 24-bit blinky
  does not need 2^24 cycles — 32 cycles out of reset already exercise the whole
  ripple carry. Where random stimulus produces a still picture, hold an input:
  `--hold btn_raw=1` is what makes `08_shift_debouncer` actually fire.
- `tennm_lcell_comb` intentionally has no `lut_config`/Boolean function in the
  `.hgl` — the ALM's 10 inputs and mode-dependent 3 outputs don't fit HAL's
  ≤6-input, one-function-per-output LUT model. Functions are attached
  per-gate by `hal_adapter.elaborate()` after load; a gate outside coverage
  gets nothing attached (a free variable), never a wrong function.

## Where things live
- Tool: `tools/hal_agilex/` — `primitives.py` (semantics + coverage
  predicates), `vo_netlist.py` (reader, refuses what it can't read),
  `simulate.py`, `behavior.py`, `trace.py` (bounded per-cycle recording, pure
  stdlib), `inventory.py`, `recognize.py`, `vo_import.py`, `library.py`,
  `hal_adapter.py` (only module needing `hal_py`), `fixtures/`.
- Gate library: `plugins/gate_libraries/definitions/AGILEX_TENNM.hgl`
  (generated by `library.py`; committed with `git add -f` since that
  directory's `.gitignore` ignores everything by default).
- Tests: `python -m unittest discover -s tools/hal_agilex -t tools -p "test_hal_agilex.py"`
  (no HAL, 49 tests, ~8s; registered with ctest as
  `runTest-hal_agilex_standalone`); `test_*_hal.py` needs
  `HAL_PY_PATH`+`HAL_BASE_PATH`.
- Real usage: every `examples/agilex3_walkthroughs/*/run_analysis*.sh` /
  `run_all.sh` — see `01_blinky_counter/run_analyses.sh` steps 0/6/7 and
  `08_shift_debouncer/run_analysis.sh` steps 0/5 for the inventory → recognize
  → behavior (+negative controls) sequence, and step 1c of either for the
  trace → `hal_viz clock_step` pair. Every walkthrough commits an
  `artifacts/dag_trace.json`; their `check.py` re-runs the exporter and
  requires the same document back.
