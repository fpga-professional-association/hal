# The answer key

**Stop.** If you are doing the exercise, `cores/` is the exercise and this
directory is not. Nothing reachable from `analysis.py blind` opens a path under
`ground_truth/`, and `check.py` asserts that by grepping `analysis.py`'s own
source. Reading this first does not break any check; it just means you did a
reading-comprehension exercise instead of a reverse-engineering one.

`MANIFEST.json` is this file in machine-readable form, plus the sha256 of every
committed artifact. `analysis.py reveal` reads that, never this.

---

## What the five cores are

| core | design | family | style | from |
| --- | --- | --- | --- | --- |
| `core_a` | Keccak-f[200] sponge permutation, one round per cycle | `sponge` | `undetermined` | walkthrough 14 |
| `core_b` | byte-framing receiver for a serial link — **the decoy** | `none-detected` | `undetermined` | written for this walkthrough |
| `core_c` | Trivium keystream generator, one bit per cycle | `lfsr-stream` | `classical-style` | walkthrough 13 |
| `core_d` | Speck32/64 block cipher, encryption only | `arx` | `classical-style` | walkthrough 11 |
| `core_e` | negacyclic NTT multiplier, n = 16, q = 257 | `lattice-ntt` | `pqc-style` | walkthrough 15 |

The letters were assigned by shuffling. Walkthrough 12's PRESENT-80 datapath is
deliberately absent: an S-box that matches a published table by name is the one
case where the answer arrives without any method at all, and a capstone made of
that case would measure nothing.

The four cryptographic cores are their walkthroughs' own `design.v`, verbatim
below the header comment, with the top-level module renamed and re-synthesized
into its own Quartus revision. That is what makes the key exact: the answer is
not an interpretation of these netlists, it is the RTL they came from. It is
also why no file here diffs against anything in walkthroughs 11–15 — the
exports are fresh, and `anonymize.py`'s scrambling is positional, so the blinded
copies differ too.

## The census, per core

Everything in this table is recovered blind; it is repeated here so that the
reveal can be read without flipping back.

| | core_a | core_b | core_c | core_d | core_e |
| --- | --- | --- | --- | --- | --- |
| instances | 866 | 120 | 611 | 241 | 1209 |
| flip-flops | 207 | 49 | 301 | 104 | 299 |
| ALM cells | 659 | 71 | 310 | 137 | 910 |
| carry chains | 0 | 1 | 0 | 2 | 10 |
| levels (DAG) | 5 | 18 | 3 | 18 | 43 |
| register components | 200, 6 | 31 | 288, 12 | 64, 32, 7 | 288, 10 |

## Core A — Keccak-f[200]

The **canonical** export of walkthrough 14, not its retimed counterfactual:
`chi` reads the register bank *through* `theta`, so every chi cone is 33
flip-flops wide, the S-box extraction refuses to enumerate cones that wide, and
`hal_crypto identify` returns `none-detected` on a real Keccak permutation. That
choice is deliberate — walkthrough 14 predicted this outcome, and the capstone
measures it under blinding.

Key facts, and whether the blind pass got them:

* **recovered** — 200 flip-flops in one register component (`a-state-200`);
* **recovered** — no carry chain anywhere (`a-no-arithmetic`);
* **recovered** — 40 cells, each the parity of exactly five state bits, with
  pairwise-disjoint sources: `theta`'s column parities (`a-parity-layer`);
* **recovered** — 200 next-state cones too wide to enumerate, which is the
  nonlinear layer showing up as a refusal (`a-nonlinear-layer`);
* **not attempted** — the 5 × 5 array of 8-bit lanes, the 25 rho offsets and the
  pi transposition (`a-lane-geometry`). Walkthrough 14 derives all of it; doing
  so again here would measure patience;
* **not attempted** — 18 rounds (`a-round-count`).

## Core B — the decoy

A byte-framing receiver: hunt for `0x7E` one bit at a time, take a length byte,
take that many payload bytes, require a closing `0x7E`. No scrambler, no CRC, no
substitution, no permutation, no keyed anything. It is built out of the parts a
cipher is built out of, on purpose:

* an eight-stage shift register clocked every cycle — but its head reads a
  primary input, so it absorbs data instead of feeding itself;
* a **sixteen-bit carry chain**, exactly as wide as each of core_d's two, and
  behind **seventeen** levels of logic, exactly as deep as core_d's round. What
  separates them is that this chain adds the constant 1 and core_d's two add
  two operand vectors each;
* two eight-bit equality comparisons against constants.

The watchdog those sixteen bits count is **unreachable**: a frame is at most 255
payload bytes, so it cannot last 65536 cycles while bits keep arriving. That is
deliberate and it is the reveal's second negative control — a wrong reference
model that a behaviour check of any length passes clean.

Key facts: `b-not-cryptographic`, `b-absorbing-chain` and `b-counter-chain` are
recovered; the protocol itself (`b-frame-protocol`) is not attempted.

## Core C — Trivium

Walkthrough 13's design. 288 state bits in three coupled nonlinear feedback
registers of 93, 84 and 111 stages, one keystream bit per cycle after a
1152-step warm-up.

Recovered blind: the three segment lengths, that all three are coupled rather
than self-contained, that every feedback has algebraic degree 2, and that none
of it is visible until `port_i1` (the `start` net, under its blinded name) is
held at 0. Not attempted: the warm-up length.

## Core D — Speck32/64

Walkthrough 11's design. 22 rounds, one per cycle, `alpha = 7`, `beta = 2`.

Recovered blind: two carry chains of the same width each adding two operand
vectors; a 64-bit register component driving a 32-bit one with nothing coming
back — a key schedule driving a data path; seventeen levels of combinational
depth. **Not** recovered: the rotation amounts, which are the one thing
`hal_crypto`'s ARX pass would have handed over on the named export and does not
on the blinded one. See the guide's section 7 and `artifacts/score.json`'s
`blinding_delta`.

## Core E — the NTT multiplier

Walkthrough 15's design. `n = 16`, `q = 257 = 2^8 + 1`, one butterfly, 144
cycles per product.

Recovered blind: the butterfly and the modulus 257 — the latter through
`hal_crypto`'s second tier, which derives `q` from what the correction logic
*computes* because 257 is too cheap to need a constant-operand chain. Not
attempted: the ring degree, the root of unity and the eighty-one twiddles.

`pqc-style` is a claim about the shape of the arithmetic. `n = 16`, `q = 257`
are toy parameters; no deployed scheme uses them, `hal_crypto` says so by
dropping to `medium` confidence, and the blind call inherits that ceiling.

## Regenerating any of this

```
cd <scratch>
cp <example>/ground_truth/designs/core_a.v .
cp <example>/ground_truth/quartus/core_a.qpf <example>/ground_truth/quartus/core_a.qsf .
cp <example>/ground_truth/quartus/build.tcl .
quartus_sh -t build.tcl core_a
quartus_syn core_a -c core_a
quartus_eda --simulation --format=verilog --tool=questasim \
            --output_directory=simulation core_a -c core_a

python3 tools/hal_agilex --strict inventory simulation/core_a.vo
python3 tools/hal_agilex import simulation/core_a.vo \
    -o <example>/ground_truth/exports/core_a.hal.v
python3 <example>/anonymize.py <example>/ground_truth/exports/core_a.hal.v \
    <example>/cores/core_a.anon.hal.v <example>/ground_truth/maps/core_a.map.json
```

Quartus Prime Pro 26.1, device `A3CW135BM16AE6S`, family Agilex 3 — the same
flow every walkthrough in the series uses. All five exports are
`--strict inventory` clean.
