# `16_mystery_cores` — the experimental design

Walkthroughs 01–15 each hand you a netlist and then tell you, in the same
directory, what it is. This one does not. Five anonymized Agilex 3 exports sit
in [`cores/`](cores/); a fixed procedure is run over all five without reading
anything else; the conclusions are written down; *then* the answer key in
[`ground_truth/`](ground_truth/) is opened and the calls are scored.

This file is the protocol. It was written **before** the five exports were
analysed, and it is the thing the guide is measured against — so it states what
will be measured, how the call is made, and what would count as getting it
wrong. `analysis.py blind` implements section 4 exactly; `analysis.py reveal`
implements section 6.

## 1. What is being measured

Not "can HAL identify cryptography" — walkthroughs 11–15 already answered that
for five specific netlists whose answers were known while the passes were being
written. The question here is narrower and more useful:

> Given only a blinded gate-level export, does the published method — census,
> shape, state partition, nonlinearity, `hal_crypto identify`, and a written
> decision rule over those — produce the right family and the right
> classical-versus-PQC call, and does it produce an honest *negative* when there
> is nothing to find?

Two things make that a real measurement rather than a demonstration:

* the decision rule is fixed in section 4 of this file, in advance, as a table
  of `(observation → call)` rules with identifiers. The guide reports which
  rules fired. A call that needed a rule invented for it would be visible.
* one of the five cores is **not cryptography**. If the procedure cannot say
  `none-detected` and mean it, it has learned nothing, it has only learned to
  say yes.

## 2. The five cores

Each is a real Quartus Prime Pro 26.1 synthesis for `A3CW135BM16AE6S`, Agilex 3,
`tennm_lcell_comb` + `tennm_ff` only, post-synthesis EDA export, imported with
`tools/hal_agilex import` and then blinded with the `anonymize.py` conventions of
walkthroughs 02, 03 and 10:

* the module becomes `top`, ports become `port_i<n>` / `port_o<n>`, internal nets
  become `n<n>`, instances become `u<n>`, and all of that numbering is scrambled;
* **every internal vector declaration is split into unrelated scalar wires.**
  This is the part that matters. A Quartus export keeps the RTL's
  `wire [15:0] x_sum;`, which hands a reverse engineer the word boundaries for
  free; a netlist recovered from a bitstream has no such thing. Splitting them
  is what makes this exercise resemble the target it is meant to prepare for —
  and, as section 7 records, it is also what breaks one of `hal_crypto`'s passes.

Four of the five are the designs of walkthroughs 11, 13, 14 and 15,
**re-synthesized from scratch** under neutral top-level names into their own
Quartus revisions, so that no committed file here is a copy of a file committed
there and the blinded exports do not diff against anything. Walkthrough 12's
PRESENT datapath is deliberately *not* in the set: an S-box that matches a
published table by name is the one case where the answer arrives without any
method at all.

The fifth is new RTL written for this walkthrough and for no other purpose.

The letters carry no information: the mapping from letter to design was fixed by
shuffling, and neither the order in `cores/` nor the size of a file says which
walkthrough a core came from.

## 3. The procedure, run identically on all five

`analysis.py blind <core>` runs six steps and writes one findings document per
core. It reads `cores/<core>.anon.hal.v` and nothing else — no path in the blind
code ever names `ground_truth/`, and `check.py` asserts that.

| step | what it produces | why it is step *n* |
| --- | --- | --- |
| 1 `census` | gate-type histogram, port widths, flip-flop count, carry chains and their lengths | flip-flop count bounds the state; a carry chain is the one unambiguous object a vendor export contains |
| 2 `shape` | combinational depth (levels), cells per level | ARX-versus-SPN, iterative-versus-unrolled and logic-versus-schedule are all in the column profile (see walkthrough 11 vs 12 vs 13) |
| 3 `state` | flip-flop-only dependency graph, its SCCs and their one-way couplings; the longest run of registers each reading exactly one other; and the shift structures `hal_crypto.shiftreg` finds (including what holding a mode net buys) | the partition *is* the architecture: 64 driving 32 one-way is a key schedule driving a data path |
| 4 `nonlinearity` | per-register next-state ANF degree histogram, and the census of cells that are a pure XOR of exactly *k* flip-flops | degree 1 everywhere is an LFSR/CRC/FSM; degree 2 with a parity layer on top is a permutation round; this is what separates walkthrough 05 from 13 |
| 5 `identify` | `hal_crypto identify` verdict: family, style, confidence, which passes fired | the tool's own answer, recorded verbatim, *including when it is wrong* |
| 6 `call` | the blind verdict: family, style, confidence, recovered facts, rules fired | section 4 |

Steps 1–4 never consult step 5, and step 6 is a pure function of steps 1–5. The
point of that ordering is the one walkthrough 03 makes about structure and
behaviour: two methods that never read each other agreeing is evidence, and them
disagreeing is information. Here the two methods are *the published manual
method* and *the tool*, and the guide's most useful sections are the cores where
they disagree.

## 4. The decision rule, fixed in advance

Rules are tried in order; the first whose antecedent holds decides the family.
Every rule names only quantities steps 1–5 produce.

| id | antecedent | family | style |
| --- | --- | --- | --- |
| `R1-lattice` | `identify` reports `lattice-ntt`, **or** the chain census contains an `add` and a `subtract` of the same width, both over two non-constant operand vectors | `lattice-ntt` | `pqc-style` |
| `R2-sponge` | no carry chain anywhere, **and** ≥ 8 cells that are each a pure XOR of exactly 5 flip-flop outputs, with pairwise-disjoint source sets | `sponge` | `undetermined` |
| `R3-spn` | `identify` reports an S-box of ≥ 4 bits at tier `exact` or `xor_constant`, in ≥ 4 instances | `spn` | `classical-style` |
| `R4-arx` | ≥ 2 carry chains of the same width ≥ 8 that each add **two operand vectors** — neither of them a constant — **and** combinational depth ≥ 10 | `arx` | `classical-style` |
| `R5-stream` | a shift structure of ≥ 32 stages that is autonomous (no external data absorbed) and whose feedback has ANF degree ≥ 1, possibly only under a held mode net | `lfsr-stream` | `classical-style` |
| `R6-none` | none of the above | `none-detected` | `undetermined` |

Three of those antecedents are worth reading twice.

**R4 turns on what the chain computes, not on the fact that it exists.** Every
carry chain in this set is a dedicated `cout -> cin` wire and unmissable; what
`arith.adders` adds is an *evaluation* of each one, so `a + b` and `a + 1` are
distinguishable. That distinction is the only thing in the census that separates
the decoy from the block cipher: they have the same chain width, the same
combinational depth, and the same "the chain reads a register bank and writes it
back".

**R2's disjointness is the claim.** Forty cells that each read five registers is
a coincidence; forty cells that read *disjoint* fives, together covering the
state, is a layer.

**R6's style is `undetermined`, not a word of its own.** A design with no
cryptography in it has no position on the classical/PQC axis, which is exactly
what `hal_crypto` already says when the family is `none-detected`. Minting a
second vocabulary for the same non-claim would make the score table measure word
choice.

Confidence is assigned by a second fixed rule, not by feel:

* **high** — the structural rule and `hal_crypto identify` name the same family;
  or `R6-none` fired and no step produced *any* cryptographic evidence at all.
* **medium** — the structural rule fired and `identify` did not agree (either it
  said `none-detected`, or it named a different family). The disagreement is
  reported in the finding, not smoothed over.
* **low** — the structural rule fired on a partial antecedent (a threshold met
  within one unit). Reserved; if nothing lands here the column simply says so.
* **the clamp** — when the deciding rule *quoted* `hal_crypto`'s verdict, the
  call may not come out more confident than the tool it quoted. Core E is where
  that bites: the tool says `medium` because the modulus it recovered is in no
  published-parameter library, and a method that echoed the tool's answer with
  more confidence than the tool had would be manufacturing certainty.

`style` is copied from the table and never argued up. In particular `R2-sponge`
yields `undetermined` with **no** confidence number, exactly as `hal_crypto`
does and for the reason walkthrough 14 gives: a Keccak permutation is SHA-3 and
is equally the XOF inside ML-KEM, ML-DSA and SPHINCS+, so its presence is
evidence about what the design computes and none about which family of scheme
uses it.

### What the rule table is and is not

It is a distillation of walkthroughs 11–15, and it was calibrated on **their**
exports, which are named, and whose answers the author knew. That is stated
plainly because it bounds the claim: this capstone measures whether a method
derived from five known netlists survives **blinding** and transfers to
independently re-synthesized copies — not whether the method was invented
without knowledge of any cipher. Nobody has ever reverse engineered anything
without knowing what ciphers look like.

Two consequences follow and both are in the report:

* the decoy is the only core in the set that nothing was calibrated on. It is
  the one genuinely out-of-sample point, and it is the one whose answer is
  "nothing";
* `R2-sponge` was revised once during drafting. It originally also required
  every register's next state to have ANF degree ≤ 2, which is wrong on its face
  and walkthrough 14 says so: `chi` makes every next-state cone 33 flip-flops
  wide, so those cones are *refused*, not degree-2. The correction was made
  after measuring the cone widths of the five exports, it is recorded here
  rather than quietly applied, and section 7 of the guide says what it means for
  the strength of the result.

What would falsify the table, and what the scoring in section 6 therefore looks
for: a core where the rules fire on the wrong family, a core where they fire on
nothing and there was something, or a core where they fire on something and
there was nothing.

## 5. What is deliberately *not* attempted blind

The blind pass stops at family, style and whatever constants fall out of steps
1–5. It does not attempt:

* the rotation amounts of an ARX round, or the 5 × 5 × 8 lane geometry of a
  sponge, or an S-box table — each of those is an entire walkthrough (11, 14, 12
  respectively) and re-deriving one here would measure patience, not method;
* any behavioural check. Nothing in the blind section simulates a core against a
  reference model, because a reference model *is* a hypothesis about what the
  design is, and there is nothing to compare against until the reveal.

Both restrictions are the reason the reveal section can bind behaviour at all:
the family references live in `ground_truth/references/`, and the ground-truth
section runs them against the **named** exports with negative controls, which is
what makes "core C really is Trivium" a checked statement rather than an
assertion.

## 6. Scoring

For each core the reveal compares four things against the answer key:

| column | what a correct value means |
| --- | --- |
| family | the blind family equals the ground-truth family string |
| style | the blind style equals the ground-truth style (`classical-style`, `pqc-style` or `undetermined`) |
| tool | what `hal_crypto identify` alone said, scored separately against the same truth |
| facts | which of the ground-truth "key facts" the blind pass actually recovered, as a fraction |

Four outcomes are possible per core and all four are reportable results:

* **correct** — family and style both match.
* **partial** — family matches, style does not, or fewer than all key facts.
* **wrong** — family does not match and something was claimed.
* **missed** — `none-detected` was called on a core that is cryptography (a
  false negative), or a family was called on the decoy (a false positive).

The score table in the guide and in `artifacts/score.json` is generated, not
typed, and `check.py` re-derives it. **No pass in `tools/hal_crypto` was changed
for this walkthrough.** Where the tool gets a core wrong, that is recorded as a
measurement and written up as a gap, and the fix belongs to a separate issue —
a capstone that tunes the instrument it is calibrating measures nothing.

## 7. The one thing known in advance to be hard

Blinding splits internal vectors. Several of `hal_crypto`'s passes ask a
question of the form "which bit of *which word*", and in a vendor export a word
is a vector declaration. Whether a pass survives blinding is therefore a
property of the pass, and the exports here are the first place in this
repository where that is measured on more than one design at a time.

The reveal runs the **same** `hal_crypto identify` on both copies of every core
— `ground_truth/exports/<core>.vo` with its names intact, and
`cores/<core>.anon.hal.v` without — and writes the difference to
`artifacts/score.json` as `blinding_delta`. That control is what makes a wrong
verdict attributable: a family the tool finds on the named export and loses on
the blinded one is a *blinding* failure, and one it never finds on either is
something else.

Gaps found this way are written up in the guide and filed as issues. They are
not patched here: a capstone that tunes the instrument it is calibrating
measures nothing.

## 8. Layout

```
cores/                     the five blinded exports -- the only input to the blind pass
ground_truth/
  GROUND_TRUTH.md          the answer key, in prose
  MANIFEST.json            every file's sha256 and the letter -> design mapping
  designs/                 the RTL each core was synthesized from
  quartus/                 one build.tcl, five .qpf/.qsf revisions
  exports/                 the *named* .vo exports and their hal_agilex imports
  maps/                    the anonymize.py mappings (blinded name -> original)
  references/              Python reference models, for the reveal's behaviour bounds
anonymize.py               walkthrough 10's blinding script, byte for byte
analysis.py                blind | reveal | all
check.py                   re-derives every blind finding and the whole score table
run_analysis.sh            every command the guide runs, in order
artifacts/                 the findings documents, the step data and score.json
images/                    one levelled DAG per core, plus three close-ups
guide.html                 the walkthrough: blind first, then the reveal
```

`cores/` is what a reverse engineer has. Everything under `ground_truth/` is what
nobody has on a real target, and the blind half of the guide cites none of it.
