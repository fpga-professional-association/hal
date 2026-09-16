# Agilex 3 walkthrough 13 — trivium_stream

A complete, self-contained reverse-engineering walkthrough: from a real Quartus
Prime Pro synthesis of the Trivium stream cipher back to RTL, using only this
fork's headless HAL toolchain.

**Start with [`guide.html`](guide.html)** — open it in a browser straight from a
checkout. It is offline-safe: inline CSS, relative image paths, no scripts and
no external requests.

## Why this one is different

The first *stream* cipher in the series, and the counterpart to walkthrough 05.
05 is a 16-bit **LFSR**: linear feedback, a polynomial, a period. Trivium is the
same skeleton with three changes, and each one broke something:

* **288 state bits in three coupled segments.** No segment's feedback is a
  function of its own stages alone — A reads C, B reads A, C reads B — so a pass
  that only knows how to close a chain onto *itself* reports three *open* shift
  registers and finds no cryptography. Structurally the three are one machine:
  the SCC decomposition returns a single strongly connected component of all 288
  registers, and only the chain walk cuts it into 93 + 84 + 111.
* **A parallel key/IV load makes every shift link invisible.** Each stage's next
  state is `load ? init : s[i-1]`, a multiplexer, so "this register is driven by
  exactly one other register" is false for **all 288 stages at once**. Run
  against this export, `tools/hal_crypto` first reported *zero* shift chains in a
  design that is three shift registers.
* **Three AND gates.** One degree-2 term in each feedback is the entire
  difference between a keystream generator and a PRNG — and none of the three is
  an AND *gate*: each lives inside one ALM's `lut_mask` beside the XORs it shares
  the cone with.

The first two were real gaps in `tools/hal_crypto`, not problems with the
example. Both were fixed alongside this walkthrough, with synthesized fixtures
(`lfsr16_loadable`, `coupled_nlfsr`) that make each case on its own, and every
pre-existing fixture and walkthrough verdict is unchanged. The guide quotes the
failing output before the fix.

## What is here

| file | role |
| --- | --- |
| `design.v` | the original RTL, the thing being recovered |
| `spec.md` | what it was supposed to do, written before synthesis |
| `quartus/` | the `.qpf`/`.qsf`/`.tcl` the synthesis actually used |
| `trivium_stream.vo` | the Quartus Prime Pro 26.1 post-synthesis export |
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
left to right with Kahn's algorithm: **3 levels**, 613 nodes, 1884 edges, 908
cut at a register.

Level 0 is the 301 `tennm_ff` plus the two constant nodes. Level 1 holds **301
cells** — 285 plain shift multiplexers, the three feedback cells, the keystream
cell, the two terminal-count tests and the `lo` counter. Level 2 holds just
**9**: the three segment-head multiplexers (which have to wait for a feedback
cell), the `hi` counter and its enable, and the two flags. Three levels on a
cipher with 1152 warm-up steps is the whole architecture in one number — the
combinational depth of one Trivium step is *one LUT*, and everything else is
schedule.

```
python tools/hal_viz dag examples/agilex3_walkthroughs/13_trivium_stream/netlist.hal.v \
    -g plugins/gate_libraries/definitions/AGILEX_TENNM.hgl --const-hub --max-gates 700 \
    -o examples/agilex3_walkthroughs/13_trivium_stream/images/dag.svg --html
```

`images/dag_interactive.html` is the same drawing with values on it: a
self-contained clock-step page over `artifacts/dag_trace.json`, whose window is
the **end** of one 1152-step warm-up — the counter reaching 63/17, `busy`
falling, `ks_valid` rising, and `ks` carrying `z1` of the published all-zero-key
vector.

Two deliberate deviations from walkthroughs 01–10:

* `--const-hub`, as in 11 and 12. This design's constant fan-out is 4377
  consuming pins; the per-pin stub form is a wall of circles and none of the
  information. Drop the flag to render the other spelling; the level count is
  the same either way.
* `images/netlist_graph.svg` is **scoped to one gate's neighbourhood**, as
  walkthrough 12 scopes its own. At 613 gates the unlevelled whole-netlist
  drawing is not a diagram: `dot` did not finish it in ten minutes, because the
  graph is cyclic and cannot be layered, and `--engine sfdp` returns a hairball.
  The same 613 nodes *levelled* draw in seconds — which is the best argument for
  the levelled view in the series so far.

## Results, in one table

| question | answer | how |
| --- | --- | --- |
| coverage | 611 instances, all `tennm_ff`/`tennm_lcell_comb`, `proven_under_assumptions` | `hal_agilex --strict inventory` |
| plain shift links | **0 of 301** registers, until `start` is held at 0 | `hal_crypto.shiftreg` |
| state | 288 bits in three coupled segments, **93 / 84 / 111** | chain walk under `start = 0` |
| feedback | `s66+s93+s91*s92+s171`, `s162+s177+s175*s176+s264`, `s243+s288+s286*s287+s69` | ANF of the three head cones, enumerated |
| nonlinearity | 3 AND terms, degree 2, each over two **adjacent** stages | same |
| output function | a pure XOR of `s66 s93 s162 s177 s243 s288` — every one a linear feedback tap, none an AND input | cone of the `ks` cell |
| load pattern | key at `s1..s80`, iv at `s94..s173`, ones at `s286..s288`, zeros elsewhere | cofactor every stage at the load select |
| warm-up | 64 × 18 = **1152** = 4 × 288, in 1153 cycles from an accepted start | two terminal-count cells, then driving the netlist |
| family | `lfsr-stream`, `classical-style`, tier `high`, three NLFSRs and **no polynomial** | `hal_crypto identify` |
| netlist vs `design.v` | `proven_bounded`, 3600 cycles | `hal_agilex behavior` |
| netlist vs `recovered.v` | `proven_bounded`, 3600 cycles | `hal_agilex behavior` |
| negative controls | a moved tap and "AND becomes XOR" both caught at cycle 71 | `hal_agilex behavior` |
| published test vectors | all three eSTREAM Trivium vectors reproduced by the netlist itself | simulating the export |

## Reproducing

Synthesis (Windows, Quartus Prime Pro 26.1):

```
mkdir scratch && cd scratch
cp ../design.v ../quartus/trivium_stream.qpf ../quartus/trivium_stream.qsf ../quartus/build.tcl .
quartus_sh -t build.tcl
quartus_syn trivium_stream -c trivium_stream
quartus_eda --simulation --format=verilog --tool=questasim \
            --output_directory=simulation trivium_stream -c trivium_stream
```

Import and analysis (Linux, with a built HAL and Graphviz):

```
python tools/hal_agilex import trivium_stream.vo -o netlist.hal.v

export HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib PYTHONPATH=<build>/lib
sh examples/agilex3_walkthroughs/13_trivium_stream/run_analysis.sh
```

`check.py` has two tiers: with no arguments it needs nothing but a Python 3
interpreter (the structural analysis is `tools/hal_agilex` and
`tools/hal_crypto`, both HAL-free), and `--with-hal` adds the tier that loads
`netlist.hal.v` through `hal_py` and re-derives the SCC decomposition.
