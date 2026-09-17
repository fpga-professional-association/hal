# Agilex 3 walkthrough 14 — keccak_toy

A complete, self-contained reverse-engineering walkthrough: from a real Quartus
Prime Pro synthesis of the Keccak-f[200] sponge permutation back to RTL, using
only this fork's headless HAL toolchain.

**Start with [`guide.html`](guide.html)** — open it in a browser straight from a
checkout. It is offline-safe: inline CSS, relative image paths, no scripts and
no external requests.

## Why this one is different

The first design in the series whose classical-versus-PQC verdict is honestly
**`undetermined`** — and the first that ships two exports of the *same* cipher
to show why a structural negative is a statement about the netlist, not about
the algorithm.

* **A sponge sits on both sides of the axis.** SHA-3 and SHAKE are classical
  hashing; the same SHAKE is the extendable-output function inside ML-KEM and
  ML-DSA, and hashing is the entirety of SPHINCS+. "A Keccak core is present" is
  evidence about what the design computes and no evidence about which family of
  scheme uses it. `hal_crypto` used to report a sponge as `classical-style`; it
  now reports `undetermined` and says why.
* **The register placement, not the algorithm, decides what a pass can see.**
  `keccak_toy.vo` and `keccak_retimed.vo` are the same permutation, the same
  interface, the same 19 cycles and the same answer. One stores the round input
  and one stores it half a round later. In the first, χ reads the register bank
  *through* θ and every χ cone is 33 flip-flops wide, so `identify` returns
  `none-detected`. In the second, χ sits on the register outputs and the same
  command finds all **40** instances of `keccak_chi_5`.
* **Two layers of the round cost no logic at all.** ρ and π are a lane rotation
  and a lane transposition, so there is no net, no cell and no vector to find.
  Twenty-five rotation offsets and a 5×5 lane map are recovered from *which*
  θ net each χ cell reads, and from nothing else. The permutation pass's
  **wiring** tier reports nothing on either export; its **cone-support** tier
  reports nothing on `keccak_toy.vo` (χ sits between θ and the register, so no
  destination bit reads exactly one source bit) and the entire 200-bit map on
  `keccak_retimed.vo` — matching nothing, because no library carries a 200-bit
  Keccak ρ·π. An index map is not twenty-five offsets; that still takes the
  lane geometry.
* **The 5×5×8 grid is recovered from XOR fan-in.** 40 cells are a pure XOR of
  five flip-flops; the 40 classes they define have two neighbours each; the only
  closed five-step walk in that graph separates the unrotated neighbour from the
  rotated one, and its cycle structure gives eight bit positions and five
  columns. No name and no assumption involved.

The χ blind spot was a real gap in `tools/hal_crypto`, not a problem with the
example: the S-box pass keyed its search on the support of a single cone, which
finds every S-box whose output bits read *every* input bit and never finds one
whose outputs read three of five. It was fixed alongside this walkthrough, with
a synthesized fixture (`keccak_chi_layer`) that makes the case on its own, and
every pre-existing fixture and walkthrough verdict is unchanged byte for byte.
The guide quotes the failing output before the fix.

## What is here

| file | role |
| --- | --- |
| `design.v` | the original RTL, the thing being recovered |
| `retimed.v` | the counterfactual: the same permutation, register moved half a round |
| `spec.md` | what it was supposed to do, written before synthesis |
| `quartus/` | the `.qpf`/`.qsf`/`.tcl` both syntheses actually used |
| `keccak_toy.vo` | the Quartus Prime Pro 26.1 post-synthesis export |
| `keccak_retimed.vo` | the counterfactual export of section 6 |
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

Nodes are gates, edges are nets. The graph has cycles — the state feeds itself
every round — but every cycle passes through a flip-flop, so cutting the edges
that end on a flip-flop pin leaves a DAG: the combinational core.
`images/dag.svg` (and the standalone `images/dag.html`) draws that DAG levelled
left to right with Kahn's algorithm: **5 levels**, 868 nodes, 3352 edges, 821
cut at a register.

On this design the level profile *is* the round, step for step:

| level | nodes | what is in it |
| --- | --- | --- |
| 0 | 209 | the 207 flip-flops, plus the shared GND/VCC hub |
| 1 | 51 | θ's 40 column-parity cells, plus the control logic that reads registers directly (8 round-constant cells, the terminal-count test, one counter cell, the load select) |
| 2 | 208 | θ proper: 200 cells, one per state bit, plus the counter's next state and the two flags |
| 3 | 200 | χ: 200 cells, one per state bit |
| 4 | 200 | the load multiplexer, one cell per state bit — with ι fused into eight of them, the eight bits of lane (0,0) |

Five levels where 12 and 13 are both 3, and for a third reason again: a sponge
round is a short fixed pipeline of *named* steps, each one LUT deep because each
one is a small function of a few bits. There is no carry chain anywhere in the
datapath, because nothing in Keccak adds.

```
python tools/hal_viz dag examples/agilex3_walkthroughs/14_keccak_toy/netlist.hal.v \
    -g plugins/gate_libraries/definitions/AGILEX_TENNM.hgl --const-hub --max-gates 900 \
    -o examples/agilex3_walkthroughs/14_keccak_toy/images/dag.svg --html
```

`images/dag_interactive.html` is the same drawing with values on it: a
self-contained clock-step page over `artifacts/dag_trace.json`, whose 32-cycle
window is one complete permutation of the published all-zero-state vector — the
counter running 0…17, `busy` falling, `done` rising, and `dout` carrying
`3C 28 26 …` for exactly one cycle before the next load.

Two deliberate deviations from walkthroughs 01–10:

* `--const-hub`, as in 11, 12 and 13. This design's constant fan-out is **5979**
  consuming pins — the largest in the series — and the per-pin stub form is a
  wall of circles and none of the information. Drop the flag to render the other
  spelling; the level count is the same either way.
* `images/netlist_graph.svg` is **scoped to one gate's neighbourhood**, as 12 and
  13 scope theirs. At 868 gates the unlevelled whole-netlist drawing is not a
  diagram: the gate graph is cyclic, so `dot` cannot layer it.

## Results, in one table

| question | answer | how |
| --- | --- | --- |
| coverage | 866 instances, all `tennm_ff`/`tennm_lcell_comb`, `proven_under_assumptions` | `hal_agilex --strict inventory` |
| state geometry | **5 × 5 lanes of 8 bits**, recovered from XOR fan-in alone | 40 pure-XOR parity cells + the closed five-step walk |
| θ | 40 column parities, each fed back one column left unrotated and one column right rotated by a bit | 3-input XOR cells over parity nets |
| ρ, π | **zero cells, zero nets**; 25 offsets `0 4 3 1 2 / 1 4 2 5 2 / 6 6 3 7 5 / 4 7 1 5 0 / 3 4 7 0 6` and the map `(x,y) → (y, 2x+3y)` | index arithmetic over the χ cells' `b0` operands |
| ρ·π, as the tool sees it | wiring tier: nothing, either export. Cone-support tier: nothing on `keccak_toy.vo`, the whole 200-bit map (200/200 links, no library match) on `keccak_retimed.vo` | `hal_crypto permutation` |
| χ | one 5-bit map, **40 instances**, degree 2, DU 8, `keccak_chi_5` at tier `exact` | ANF role assignment + the `b1` row chain |
| ι | 8 cells, 4 of them constant zero; `01 82 8A 00 8B 01 81 09 8A 88 09 0A 8B 8B 89 03 02 80` | enumerating the counter cones |
| rounds | one 5-input terminal-count cell → **18** rounds, 19 cycles from an accepted `start` | the counter cone |
| family (`keccak_toy.vo`) | `none-detected`, `undetermined`, tier `medium` — 400 cones too wide to enumerate | `hal_crypto identify` |
| family (`keccak_retimed.vo`) | `sponge`, **`undetermined`**, tier `high`, 40 × `keccak_chi_5` | `hal_crypto identify` |
| netlist vs `design.v` | `proven_bounded`, 600 cycles | `hal_agilex behavior` |
| netlist vs `recovered.v` | `proven_bounded`, 600 cycles | `hal_agilex behavior` |
| negative controls | a moved ρ offset and a linearised χ caught at cycle 9; a wrong last-round constant at cycle 26, and **missed entirely** by a 40-cycle bound | `hal_agilex behavior` |
| published test vectors | both XKCP Keccak-f[200] vectors reproduced by the netlist itself | simulating the export |

## Reproducing

Synthesis (Windows, Quartus Prime Pro 26.1):

```
mkdir scratch && cd scratch
cp ../design.v ../quartus/keccak_toy.qpf ../quartus/keccak_toy.qsf ../quartus/build.tcl .
quartus_sh -t build.tcl
quartus_syn keccak_toy -c keccak_toy
quartus_eda --simulation --format=verilog --tool=questasim \
            --output_directory=simulation keccak_toy -c keccak_toy
```

The counterfactual export is the same three commands with `retimed.v`,
`quartus/keccak_retimed.qsf` and the revision name `keccak_retimed`.

Import and analysis (Linux, with a built HAL and Graphviz):

```
python tools/hal_agilex import keccak_toy.vo -o netlist.hal.v

export HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib PYTHONPATH=<build>/lib
sh examples/agilex3_walkthroughs/14_keccak_toy/run_analysis.sh
```

`check.py` has two tiers: with no arguments it needs nothing but a Python 3
interpreter (the structural analysis is `tools/hal_agilex` and
`tools/hal_crypto`, both HAL-free), and `--with-hal` adds the tier that loads
`netlist.hal.v` through `hal_py` and re-derives the SCC decomposition.
