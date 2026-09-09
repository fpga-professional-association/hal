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
