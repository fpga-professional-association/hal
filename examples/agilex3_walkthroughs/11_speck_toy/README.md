# Agilex 3 walkthrough 11 — speck_toy

A complete, self-contained reverse-engineering walkthrough: from a real Quartus
Prime Pro synthesis of a Speck32/64 ARX block cipher back to RTL, using only
this fork's headless HAL toolchain.

**Start with [`guide.html`](guide.html)** — open it in a browser straight from a
checkout. It is offline-safe: inline CSS, relative image paths, no scripts and
no external requests.

## Why this one is different

The first cryptographic design in the series, and the lesson is where the cipher
*isn't*. An ARX round is Add, Rotate, XOR; in this export

* the **add** is two verified 16-bit carry chains — easy;
* the **rotate** costs no logic, so it is not a net, not a cell and not a
  `lut_mask` bit. Both rotation constants had to be recovered from *orderings*:
  which register bit lands on which carry-chain slice (α = 7), and which single
  bit of its own bank each register bit's next-state cell reads (β = 2);
* the **XOR** layer is 54 cells and **not one of them is an XOR** — each was
  packed into an ALM together with the load multiplexer beside it. Holding one
  input at a constant recovers all 54.

`tools/hal_crypto` classified this export `none-detected` when it was first run
against it, for both of those reasons. That was a real gap in the tool, not a
problem with the example; the guide quotes the failing output and the fix landed
alongside this walkthrough.

## What is here

| file | role |
| --- | --- |
| `design.v` | the original RTL, the thing being recovered |
| `spec.md` | what it was supposed to do, written before synthesis |
| `quartus/` | the `.qpf`/`.qsf`/`.tcl` the synthesis actually used |
| `speck_toy.vo` | the Quartus Prime Pro 26.1 post-synthesis export |
| `netlist.hal.v` | the same netlist rewritten for HAL's Verilog parser |
| `reference.py` | a Python model of `design.v`, for the section 2 export check |
| `analysis.py` | the reverse-engineering steps, one function per step |
| `recovered.v` | the RTL as reconstructed from the netlist alone |
| `recovered_reference.py` | the recovered behaviour, for the netlist-vs-model check |
| `check.py` | headless re-run of every claim the guide makes |
| `run_analysis.sh` | every command the guide runs, in order |
| `images/` | every diagram the guide embeds (`.dot` + `.svg`) |
| `artifacts/` | the findings documents and step outputs the guide quotes |
| `guide.html` | **the walkthrough** |

## The netlist as a graph

Nodes are gates, edges are nets. The graph has cycles — the cipher state feeds
itself every cycle — but every cycle passes through a flip-flop, so cutting the
edges that end on a flip-flop pin leaves a DAG: the combinational core.
`images/dag.svg` (and the standalone `images/dag.html`) draws that DAG levelled
left to right with Kahn's algorithm: **18 levels**, 243 nodes, 432 edges, 413
cut at a register.

Level 0 is the 104 `tennm_ff` plus the two constant gates. Level 1 holds 43
cells — the two carry-chain heads, the enable, the flags, the counter, and 32
pure load multiplexers. Levels 2–16 hold **exactly six cells each**: one slice of
*each* of the two carry chains plus the four cells that consume the previous
slice's sum. Level 17 holds the last four consumers. Eighteen levels on a
22-round cipher is the point: the depth is one round's 16-bit ripple carry, not
the schedule — iterative, not unrolled.

```
python tools/hal_viz dag examples/agilex3_walkthroughs/11_speck_toy/netlist.hal.v \
    -g plugins/gate_libraries/definitions/AGILEX_TENNM.hgl --const-hub \
    -o examples/agilex3_walkthroughs/11_speck_toy/images/dag.svg --html
```

`images/dag_interactive.html` is the same drawing with values on it: a
self-contained clock-step page over `artifacts/dag_trace.json`, whose window is
one whole encryption of the published test vector — the clear, the load, the 22
rounds, and the cycle `done` comes up with `ct = 0xa86842f2`.

`--const-hub` is a deliberate deviation from walkthroughs 01–10, which draw one
`0`/`1` stub per consuming pin. This design's constant fan-out is 1514 pins: the
stub form is a wall of circles, 1.7 MB of SVG and eight minutes of Graphviz.
Drop the flag to render the other spelling; the level count is the same either
way.

## Results, in one table

| question | answer | how |
| --- | --- | --- |
| coverage | 241 instances, all `tennm_ff`/`tennm_lcell_comb`, `proven_under_assumptions` | `hal_agilex --strict inventory` |
| word width | 16 bits | declared bank widths, confirmed by the chains |
| α (right rotation) | **7** | chain slice *i* reads bank bit `(i+7) mod 16` |
| β (left rotation) | **2** | bank bit *i*'s next-state cell reads bit `(i-2) mod 16` |
| XOR layer | 54 cells, **0 standalone**, 53 recovered under `run = 1` | per-cell truth table, one held input |
| rounds | 22, in 23 cycles from an accepted start | driving the netlist |
| family | `arx`, `classical-style`, tier `high`, matching the published `speck_32` rotation set | `hal_crypto identify` |
| netlist vs `design.v` | `proven_bounded`, 2000 cycles | `hal_agilex behavior` |
| netlist vs `recovered.v` | `proven_bounded`, 2000 cycles | `hal_agilex behavior` |
| negative controls | α = 8 and β = 3 both caught at cycle 3 | `hal_agilex behavior` |
| published test vector | `6574694c` / `1918111009080100` → `a86842f2` | simulating the export |

## Reproducing

Synthesis (Windows, Quartus Prime Pro 26.1):

```
mkdir scratch && cd scratch
cp ../design.v ../quartus/speck_toy.qpf ../quartus/speck_toy.qsf ../quartus/build.tcl .
quartus_sh -t build.tcl
quartus_syn speck_toy -c speck_toy
quartus_eda --simulation --format=verilog --tool=questasim \
            --output_directory=simulation speck_toy -c speck_toy
```

Import and analysis (Linux, with a built HAL and Graphviz):

```
python tools/hal_agilex import speck_toy.vo -o netlist.hal.v

export HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib PYTHONPATH=<build>/lib
sh examples/agilex3_walkthroughs/11_speck_toy/run_analysis.sh
```

`check.py` has two tiers: with no arguments it needs nothing but a Python 3
interpreter (the structural analysis is `tools/hal_agilex` and
`tools/hal_crypto`, both HAL-free), and `--with-hal` adds the tier that loads
`netlist.hal.v` through `hal_py`.
