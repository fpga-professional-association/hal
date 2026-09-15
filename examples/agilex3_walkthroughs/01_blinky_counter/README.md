# Agilex 3 walkthrough 01 — blinky_counter

A complete, self-contained reverse-engineering walkthrough: from a real
Quartus Prime Pro synthesis of a 24-bit blinky counter back to RTL, using only
this fork's headless HAL toolchain.

**Start with [`guide.html`](guide.html)** — open it in a browser straight from a
checkout. It is offline-safe: inline CSS, relative image paths, no scripts and
no external requests.

## What is here

| file | role |
| --- | --- |
| `design.v` | the original RTL, the thing being recovered |
| `spec.md` | what it was supposed to do, written before synthesis |
| `quartus/` | the `.qpf`/`.qsf`/`.tcl` the synthesis actually used |
| `blinky_counter.vo` | the Quartus Prime Pro 26.1 post-synthesis export |
| `netlist.hal.v` | the same netlist rewritten for HAL's Verilog parser |
| `analysis.py` | the reverse-engineering steps, one function per step |
| `check.py` | headless re-run of every claim the guide makes |
| `recovered.v` | the RTL as reconstructed from the netlist alone |
| `recovered_reference.py` | the recovered behaviour, for the netlist-vs-model check |
| `images/` | every diagram the guide embeds (`.dot` + `.svg`) |
| `artifacts/` | the JSON/report outputs the guide quotes |
| `guide.html` | **the walkthrough** |

## The netlist as a graph

Nodes are gates, edges are nets. The graph has cycles — each counter bit feeds
the cell that recomputes it — but every cycle passes through a flip-flop, so
cutting the edges that end on a flip-flop pin leaves a DAG: the combinational
core. `images/dag.svg` (and the standalone `images/dag.html`) draws that DAG
levelled left to right with Kahn's algorithm: **24 levels**, 48 gates, 456
edges, 216 cut at a register.

Level 0 is exactly the 24 `tennm_ff`. Levels 2–23 hold one gate each — the
ripple carry chain, `add_0~106` down to `add_0~1`, one cell per counter bit —
so the longest combinational path is 23 gates. 385 of the 456 edges are
constant tie-offs, drawn as one small `0`/`1` circle per consuming pin rather
than a shared GND/VCC hub; only 71 edges carry a signal.

```
python tools/hal_viz dag examples/agilex3_walkthroughs/01_blinky_counter/netlist.hal.v \
    -g plugins/gate_libraries/definitions/AGILEX_TENNM.hgl \
    -o examples/agilex3_walkthroughs/01_blinky_counter/images/dag.svg --html
```

## Reproducing

Synthesis (Windows, Quartus Prime Pro 26.1 — same flow as
`tools/hal_agilex/README.md`):

```
mkdir scratch && cd scratch
cp ../design.v ./blinky_counter.v
cp ../quartus/blinky_counter.qpf ../quartus/blinky_counter.qsf ../quartus/build.tcl .
quartus_sh -t build.tcl
quartus_syn blinky_counter -c blinky_counter
quartus_eda --simulation --format=verilog --tool=questasim \
            --output_directory=simulation blinky_counter -c blinky_counter
```

Import and analysis (Linux, with a built HAL):

```
python tools/hal_agilex import blinky_counter.vo -o netlist.hal.v

export HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib PYTHONPATH=<build>/lib
python examples/agilex3_walkthroughs/01_blinky_counter/analysis.py all \
    -o examples/agilex3_walkthroughs/01_blinky_counter/artifacts
python examples/agilex3_walkthroughs/01_blinky_counter/check.py
```
