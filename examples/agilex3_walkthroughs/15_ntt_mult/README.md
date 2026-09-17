# Agilex 3 walkthrough 15 — ntt_mult

A complete, self-contained reverse-engineering walkthrough: from a real Quartus
Prime Pro synthesis of a toy negacyclic NTT polynomial multiplier back to its
ring, its root of unity and its eighty-one twiddle constants, using only this
fork's headless HAL toolchain.

**Start with [`guide.html`](guide.html)** — open it in a browser straight from a
checkout. It is offline-safe: inline CSS, relative image paths, no scripts and
no external requests.

## Why this one is different

The first design in the series whose secrets are **numbers** rather than logic —
a prime, a root of unity, eighty-one constants — and the first whose
classical-versus-PQC verdict comes back **`pqc-style`**. Most of the guide's last
third is about what that sentence is and is not allowed to mean.

* **The modulus is not a constant anywhere in the netlist.** `q = 257` is
  `2^8 + 1`, so subtracting it is an increment and one bit flip, and Quartus
  builds the correction out of ordinary LUTs. There is not one constant-operand
  carry chain in the design. The modulus is recovered from the *function* the
  correction computes — build `q` one bit at a time, keep a prefix only while
  `select ? sum - q : sum` is still a vector the netlist contains — and the
  answer is re-checked on **every one of the 1023 sums a nine-bit adder can
  produce**.
* **A vendor subtracter is not a hand-built one.** `hal_crypto` found *zero*
  butterflies in this export, and would have found zero in any Quartus export,
  because Quartus folds the second operand's inversion into the arithmetic
  cell's own mask (independent operand polarities, not one shared one) and
  sources the carry-in of one from a **leading carry-seed cell** — an ALM's
  `cin` can only come from the previous cell's `cout`, so a constant carry has
  to be manufactured. Both are handled now; every pre-existing fixture and
  walkthrough verdict is unchanged byte for byte.
* **There is one butterfly, not thirty-two.** Four stages of eight are a
  *schedule* in a six-bit counter. `hal_crypto` reports `stage_depth: 1` and that
  is the honest structural answer — the same distinction walkthrough 13 drew
  between Trivium's three levels of logic and its 1152-step warm-up.
* **Eighty-one constants come out of the multiplier's own operand.** Hold every
  coefficient register at 1 and `t = w · 1 = w`: the multiplier reports its
  twiddle at every step, in a bit order the carry chain already fixed. A second
  sweep holding them at 3 separates a real constant table from the phase whose
  "twiddle" is the other polynomial — the pointwise multiply, which is invisible
  to the first sweep.
* **The DSP counterfactual loses less and gains worse than you would expect.**
  `ntt_dsp.vo` is the *same* RTL with `multstyle = "logic"` switched off: one
  `tennm_mac`, 75 fewer ALMs, and an export `inventory --strict` refuses. The
  butterfly and the modulus still come out, because they live outside the
  multiplier. What is lost is the ability to *check* anything — no simulation, no
  behavioural bound, no negative controls. What is gained is a spurious `arx`
  reading the covered export does not have.

The modulus blind spot was a real gap in `tools/hal_crypto`, not a problem with
the example: the pass could read a modulus only when it was expensive enough to
need a carry chain of its own. It was fixed alongside this walkthrough, and the
new tier independently reproduces `3329` on the `ntt_stage13` fixture that the
old one already handled. The guide quotes the failing output before the fix.

## What is here

| file | role |
| --- | --- |
| `design.v` | the original RTL, the thing being recovered |
| `spec.md` | what it was supposed to do, written before synthesis |
| `quartus/` | the `.qpf`/`.qsf`/`.tcl` both syntheses actually used |
| `ntt_mult.vo` | the Quartus Prime Pro 26.1 post-synthesis export |
| `ntt_dsp.vo` | the counterfactual export of section 8.1, with the DSP allowed |
| `netlist.hal.v` | the same netlist rewritten for HAL's Verilog parser |
| `reference.py` | a Python model of `design.v`, for the section 2 export check |
| `analysis.py` | the reverse-engineering steps, one function per step |
| `recovered_reference.py` | the recovered behaviour, for the netlist-vs-model check |
| `check.py` | headless re-run of every claim the guide makes |
| `run_analysis.sh` | every command the guide runs, in order |
| `images/` | every diagram the guide embeds (`.dot` + `.svg`) |
| `artifacts/` | the findings documents and step outputs the guide quotes |
| `guide.html` | **the walkthrough** |

## The netlist as a graph

Nodes are gates, edges are nets. Every cycle in the graph passes through a
flip-flop, so cutting the edges that end on a flip-flop pin leaves a DAG: the
combinational core. `images/dag.dot` (and the standalone `images/dag.html`)
levels that DAG left to right with Kahn's algorithm: **1211 nodes, 5913 edges,
43 levels**, 1161 of them cut at a register.

It is the first design in the series whose whole-netlist levelled view is not
*drawn*. Graphviz's `dot` runs for over an hour on it without finishing — 256
input-port bits feed cells at level 42, and a long edge costs a dummy node on
every rank it crosses — so `run_analysis.sh` computes the graph with `-f none`
and draws the **datapath cone** instead: `images/dag_datapath.svg`, everything
the modular sum depends on eight hops back, 901 of the 1211 gates and 34 of the
43 levels, in about a minute. Walkthrough 13 makes the neighbouring point about
*unlevelled* graphs; this is the same wall one level up.

Forty-three levels is the deepest in the series by a factor of two and a half:

| design | cells | levels | why |
| --- | --- | --- | --- |
| 12 — PRESENT (SPN) | 226 | 3 | sixteen independent 4-bit S-boxes, no carry between them |
| 13 — Trivium (stream) | 310 | 3 | one cipher step is one LUT; the warm-up is schedule |
| 14 — Keccak (sponge) | 659 | 5 | parity, substitution, wiring — all shallow |
| 11 — Speck (ARX) | 137 | 18 | a 16-bit carry ripples |
| **15 — this one** | 910 | **43** | a 9 × 9 array multiplier *and* two modular corrections between one register bank and the next |

Deep and narrow is the ARX signature at two and a half times the depth, and it
rules a lot out before anything is decoded: an SPN, a sponge and a stream cipher
all have shallow rounds by construction, because they are built from operations
chosen to be cheap. Something in here is not cheap.

```
python tools/hal_viz dag examples/agilex3_walkthroughs/15_ntt_mult/netlist.hal.v \
    -g plugins/gate_libraries/definitions/AGILEX_TENNM.hgl --const-hub --max-gates 1300 \
    -f none -o examples/agilex3_walkthroughs/15_ntt_mult/images/dag.svg --html

python tools/hal_viz dag examples/agilex3_walkthroughs/15_ntt_mult/netlist.hal.v \
    -g plugins/gate_libraries/definitions/AGILEX_TENNM.hgl --const-hub --max-gates 900 \
    --gate 'sum_mod[0]' --depth 8 --direction predecessors --render-timeout 900 \
    -o examples/agilex3_walkthroughs/15_ntt_mult/images/dag_datapath.svg --html
```

`images/dag_interactive.html` is the datapath drawing with values on it: a
self-contained clock-step page over `artifacts/dag_trace.json`, whose 32-cycle
window is one clear, one load and thirty butterflies of the first forward
transform.

Two deliberate deviations from walkthroughs 01–10, as in 11–14:

* `--const-hub`: 299 flip-flops with five tied control pins each, plus every
  ALM's unused data pins, is a wall of little circles in the per-pin spelling.
* `images/netlist_graph.svg` is **scoped to one gate's neighbourhood**. At 1209
  gates the unlevelled whole-netlist drawing is not a diagram: the gate graph is
  cyclic, so `dot` cannot layer it at all.

## Results, in one table

| question | answer | how |
| --- | --- | --- |
| coverage | 1209 instances, all `tennm_ff`/`tennm_lcell_comb`, `proven_under_assumptions` | `hal_agilex --strict inventory` |
| coverage (counterfactual) | `unsupported`: one `tennm_mac` | `hal_agilex inventory` on `ntt_dsp.vo` |
| state layout | **two banks of 16 nine-bit coefficients** (288 registers) plus 11 control | load path ∩ adder path, required to agree |
| control | a 6-bit counter and a 3-bit phase, **144 cycles**, phases 64/16/16/32/16 | the transition orbit, classified by the counter-nesting rule |
| butterfly | **one**, nine bits wide, one verified `a+b` and one verified `a-b` over the same operands | `hal_crypto arith` + `ntt` |
| modulus | **257**, prime, `2^8 + 1`; zero constant-operand carry chains | derived from the correction, checked on all 1023 sums |
| twiddles | **81** constants: two 32-entry tables, one of 16, one constant 1 | hold every coefficient at 1, sweep the orbit |
| pointwise phase | the fifth "table" is not a table — it scales as the square | a second sweep holding the coefficients at 3 |
| schedule | four stages of eight, pair distance 8 → 4 → 2 → 1, reads = writes | knock one coefficient down and see what moves |
| permutation | the two cross-bank passes are `brv4` = `0 8 4 12 2 10 6 14 1 9 5 13 3 11 7 15` | which register each value landed in |
| ring | `psi = 15` (order 32), `omega = 225`, `n⁻¹ = 241`, `Z_257[x]/(x^16 + 1)` | the unique root reproducing the forward table |
| family | `lattice-ntt`, **`pqc-style`**, tier `medium` — the modulus is in no library | `hal_crypto identify` |
| family (counterfactual) | `lattice-ntt` still, **plus a spurious `arx`** | `hal_crypto identify` on `ntt_dsp.vo` |
| netlist vs `design.v` | `proven_bounded`, 600 cycles | `hal_agilex behavior` |
| netlist vs the recovered model | `proven_bounded`, 600 cycles | `hal_agilex behavior` |
| negative controls | a moved twiddle at cycle 67, a moved modulus at 36, a moved post-scale at 132 — and that last one **passes a 200-cycle bound clean** | `hal_agilex behavior` |
| behaviour | four products agree with the schoolbook negacyclic convolution; `x^15 · x = -1` | simulating the export |

## Honesty

`n = 16`, `q = 257` is **not** a post-quantum implementation, and the guide says
so in as many words. It is the arithmetic kernel of one at parameters chosen to
fit a teaching example: a real scheme needs `n = 256`, a modulus for which none
of this design's shortcuts exist, plus sampling, encoding, hashing and a
protocol. `pqc-style` here means *lattice-style NTT / ring arithmetic is
present* — a claim about what the design computes, not about which scheme it
implements, and `hal_crypto` drops its confidence to `medium` and says the
modulus is in no published-parameter library precisely so that the report cannot
be read the other way.

## Reproducing

Synthesis (Windows, Quartus Prime Pro 26.1):

```
mkdir scratch && cd scratch
cp ../design.v ../quartus/ntt_mult.qpf ../quartus/ntt_mult.qsf ../quartus/build.tcl .
quartus_sh -t build.tcl
quartus_syn ntt_mult -c ntt_mult
quartus_eda --simulation --format=verilog --tool=questasim \
            --output_directory=simulation ntt_mult -c ntt_mult
```

The counterfactual export is the same three commands with
`quartus/ntt_dsp.qpf`/`.qsf` and the revision name `ntt_dsp` — the *same*
`design.v`, with `ALLOW_DSP` defined and the two DSP assignments absent.

Import and analysis (Linux, with a built HAL and Graphviz):

```
python tools/hal_agilex import ntt_mult.vo -o netlist.hal.v

export HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib PYTHONPATH=<build>/lib
sh examples/agilex3_walkthroughs/15_ntt_mult/run_analysis.sh
```

`check.py` has two tiers: with no arguments it needs nothing but a Python 3
interpreter (the structural analysis is `tools/hal_agilex` and
`tools/hal_crypto`, both HAL-free), and `--with-hal` adds the tier that loads
`netlist.hal.v` through `hal_py` and re-derives the SCC decomposition.
