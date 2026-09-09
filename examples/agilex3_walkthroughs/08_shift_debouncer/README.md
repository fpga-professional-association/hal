# Agilex 3 walkthrough 08 — shift_debouncer

A complete, self-contained reverse-engineering walkthrough: from a real Quartus
Prime Pro synthesis of a button debouncer back to RTL, using only this fork's
headless HAL toolchain.

The design is three textbook structures stacked — a two-flop synchronizer, a
4-bit saturating up/down counter, and a hysteresis (Schmitt) output bit — and
the point of the walkthrough is that the three are *not* equally visible from
the two directions of attack. One is invisible structurally, one is invisible to
random stimulus, one falls out of both immediately.

**Start with [`guide.html`](guide.html)** — open it in a browser straight from a
checkout. It is offline-safe: inline CSS, relative image paths, no scripts and
no external requests.

## What is here

| file | role |
| --- | --- |
| `design.v` | the original RTL, the thing being recovered |
| `spec.md` | what it was supposed to do, written before synthesis |
| `quartus/` | the `.qpf`/`.qsf` the synthesis actually used |
| `shift_debouncer.vo` | the Quartus Prime Pro 26.1 post-synthesis export |
| `netlist.hal.v` | the same netlist rewritten for HAL's Verilog parser |
| `reference.py` | the RTL as an executable model, for netlist-vs-model checks |
| `analyze.py` | the structural half: one numbered artifact per step |
| `probe.py` | the behavioural half: drives the export as a black box |
| `recovered.v` | the RTL as reconstructed from the netlist alone |
| `recovered_reference.py` | the reconstruction as an executable model |
| `check.py` | headless re-run of every claim the guide makes, two tiers |
| `run_analysis.sh` | every command the guide runs, in order |
| `images/` | every diagram the guide embeds (`.dot` + `.svg`) |
| `artifacts/` | the text and JSON outputs the guide quotes |
| `guide.html` | **the walkthrough** |

## Reproducing

Synthesis (Windows, Quartus Prime Pro 26.1):

```
mkdir scratch && cd scratch
cp ../design.v ./shift_debouncer.v
cp ../quartus/shift_debouncer.qpf ../quartus/shift_debouncer.qsf .
quartus_syn shift_debouncer -c shift_debouncer
quartus_eda --simulation --format=verilog --tool=questasim \
            --output_directory=simulation shift_debouncer -c shift_debouncer
```

Import and analysis (Linux, with a built HAL and Graphviz `dot`):

```
python3 tools/hal_agilex import shift_debouncer.vo -o netlist.hal.v

export HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib PYTHONPATH=<build>/lib:tools
sh examples/agilex3_walkthroughs/08_shift_debouncer/run_analysis.sh
```

`run_analysis.sh` regenerates `images/`, the numbered files in `artifacts/`, the
two negative controls and `artifacts/check.txt`. It deliberately does **not**
rewrite `artifacts/inventory.findings.json`,
`artifacts/recognize.findings.json` or `artifacts/behavior.findings.json`:
those record the sha256 of the `.vo` as it was when they were made, and a
checkout with CRLF line endings hashes differently.

## Checks

```
python3 examples/agilex3_walkthroughs/08_shift_debouncer/check.py
HAL_PY_PATH=<build>/lib PYTHONPATH=<build>/lib:tools \
    python3 examples/agilex3_walkthroughs/08_shift_debouncer/check.py --with-hal
```

* **tier 1** (no HAL build) — primitive coverage, `netlist.hal.v` versus the
  vendor `.vo`, agreement with `reference.py` and `recovered_reference.py` over
  3000 cycles, two negative controls, and every number `probe.py` prints.
* **tier 2** (`--with-hal`) — runs `analyze.py` and asserts the structural
  conclusions: one clock domain with no enables, the 4-flop feedback group, the
  saturating transition relation, the 32-row hysteresis table and the
  edge-detector cell.

Both tiers exit 0.
