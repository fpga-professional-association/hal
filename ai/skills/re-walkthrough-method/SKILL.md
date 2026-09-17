---
name: re-walkthrough-method
description: The reverse-engineering method distilled from examples/agilex3_walkthroughs -- census, feedback shape, register graph, chains/masks, black-box probing, reference model, negative controls, reconstruction. Use when starting from an unknown gate-level netlist and no other tool has told you where to begin.
---

# Reverse-engineering an unknown netlist -- the method

Distilled from `examples/agilex3_walkthroughs/{01,03,08}_*/guide.html` and
the series `README.md` -- executable, not illustrative: every number a
guide quotes comes from a script beside it, re-asserted by its `check.py`.
Read one guide end to end before trusting this summary; it is a map, not a
replacement.

## When to use
- A gate-level netlist (imported, blinded, or both), and no leads yet -- the
  order of operations that scales from a 24-bit counter to a real
  control/datapath split.
- Not for picking a specific `tools/hal_*` analysis once you know the
  question -- see `netlist-analysis-tools` for that.

## The steps, in order

**1. Census.** Gate-type histogram, primary I/O, module count. Flip-flop
count is a hard upper bound on state; gate diversity says shallow vs. deep
logic. Names, hierarchy and encoding are already gone in a flat export --
nothing here says what anything means.

**2. FF control-pin signatures.** Group flip-flops by (clock, clear,
enable) net. One net on every `clk`/`clrn` pin = one clock domain and a
reset polarity/value from the *primitive*, not a guess. Multiple enable
groups is design intent leaking through synthesis -- necessary, not
sufficient: two unrelated same-width registers land in the same bucket.

**3. Gate-level SCCs.** Combinational logic is a DAG, so every cycle in the
gate graph is a value feeding its own future -- state, independent of
names. Read the shapes: many size-2 components (one flop + one cell) = bit
slices, a counter or independent toggles; one big component spanning many
registers = word-level feedback (LFSR/CRC/scrambler/FSM state vector); a
huge mixed component = a control blob step 4 must take apart (the SCC
merges everything mutually reachable, no internal structure). A ripple
counter's carry chain is **invisible here** -- bit *i* flows to *i*+1 and
never back, so it looks like N size-2 SCCs, not one machine; step 4/5
resolves that.

**4. Register dependency graph.** Per flip-flop, walk backward from D (and
separately enable) through combinational cells only, stopping at another
flop's output or an unmodelled net. Take the flip-flop-only subgraph (drop
combinational cells so a shared LUT can't merge unrelated registers; drop
self-loops so "holds via its own enable" isn't "machine") and find *its*
SCCs: flops outside every loop can only pass a value along -- a shift
chain, whose stage order falls out free since each names one predecessor.
Flops still cyclic are the real control/datapath core: a minimum
feedback-vertex search (remove one flop, retest) finds a lone mode/flag
bit; enable-cone membership separates a gating counter from a gated one
when width can't -- two same-width counters is a deliberate trap in the
series. Bit order inside a non-chain group comes from nested D-support
(bit *i* depends on 0..*i*-1) for a plain counter, or the **transition
orbit** when support is symmetric across all bits (a saturating counter
needs the whole word to know "am I at an end stop"): apply the transition
repeatedly -- a path with a fixed point at each end gives every state a
position, and a flop's toggle period along it is its binary weight.

**5. Chains and `lut_mask` semantics.** A dedicated `cout -> cin` wire is
unambiguous -- walk it, no heuristic needed. Decode an ALM-style LUT's mask
directly instead of pattern-matching a known shape: split it per the
arithmetic-mode equations (`sumout = f0 XOR cin`,
`cout = (f0 AND cin) OR (NOT f0 AND f1)`) and read `f0`/`f1` against the
actual data pins. One signal operand + a tied-zero second is an increment;
two operands is `a + b`; an inverted operand with `cin=1` is a subtractor.
A recognizer correctly returning `unknown` (never `refuted`) on a
degenerate slice is the schema working -- mask-reading covers any shape.

**6. Behavioural probing of the black box.** Drive the netlist as an N-in,
M-out box, independent of anything read structurally: toggle one input at a
time from quiescent to separate control (moves an output) from payload
(does nothing alone -- it's captured, not observed continuously); pulse a
suspected request and count transitions to tell a status flag (one
excursion) from a data line (structure inside the window); toggle each
payload bit alone and time *when* the output changes -- window width is a
period measured directly, never inferred from bit-width, and window order
is transmission/significance order; vary a stimulus parameter (press
length, half-period, burst) to find the non-linearity -- state-dependent
latency or a hard threshold is a counter signature a fixed pipeline lacks.
Structure and behaviour are **independent evidence**: run both without
either reading the other's output, then check they agree (a chain's wiring
order should match its probed timing order, name for name) -- disagreement
can mean both are right, e.g. a 9-flop chain plus a 10-slot measured frame,
because synthesis deleted a constant bit; hold both facts, don't round either away.

**7. Reference model + negative controls.** Simulate the actual netlist
against a Python model of your hypothesis for N cycles and require
agreement -- meaningless without a negative control: break the model on
purpose (wrong constant, missing clamp, off-by-one), confirm the check
catches it, and note *at what cycle* (a model wrong in a way the reachable
state space never exercises still passes clean for a while). Simulation
only checks what the state space actually reaches -- a wide counter that
never leaves its low bits can hide a wrong top-bit index indefinitely.

**8. Reconstruction, optionally a re-synthesis round trip.** Write the
recovered RTL with every claim traceable to a step above, check it as in
step 7, and -- with the vendor toolchain -- resynthesize under identical
settings and diff the primitive census against the original export. A
matching census is real corroboration but **not** an equivalence proof --
two designs can share a census and differ in wiring; it's one more way the
reconstruction could have been wrong and wasn't, not a proof.

## When the question is "is this cryptography"

Steps 1-8 still run first; `hal_crypto` (see
[hal-crypto-id](../hal-crypto-id/SKILL.md)) is a *consumer* of steps 4 and 5,
not a replacement for them. What the crypto case adds:

**A cipher's defining operations do not all survive as objects.** Measured on
`11_speck_toy`, a real Quartus export of Speck32/64: the add is a carry chain
and unmissable; the **rotations are not signals at all** (rotating a word is
free wiring, so the synthesiser keeps no net for it) and had to be read off
*orderings* -- which register bit lands on carry-chain slice *i*, and which
single bit of its own bank each register bit's next-state cell reads; and
**none of the 54 XOR cells is an XOR**, because each shares its ALM with the
load multiplexer, recovered only by holding one input at a constant. Expect the
same shape in any loadable core: look for index arithmetic over an ordered cell
layer, not for a named permuted vector.

**SCCs partition a cipher into its architectural objects, and the partition's
direction is evidence.** On the same export the four components are exactly
`{k,l0,l1,l2}` (144 gates), `{x,y}` (80), `{rnd,run,pen}` (14) and `{fin}` (2).
That the first two are *separate* says the coupling is one-way, and a 64-bit
state autonomously driving a 32-bit state is a key schedule driving a data path
-- a hypothesis available before any `lut_mask` is read.

**The level count separates the two families before anything is decoded.**
`11_speck_toy` (ARX) is 18 levels over 137 cells -- deep and narrow, because a
16-bit carry ripples. `12_present_sbox` (SPN) is 3 levels over 226 cells -- wide
and shallow, because sixteen 4-bit S-boxes are sixteen independent functions
with no carry between them. `13_trivium_stream` (bit-serial stream cipher) is 3
levels over 310 -- shallow for a third reason again: one cipher step is *one
LUT*, and the 1152-step warm-up is schedule, not depth. Run `hal_viz dag` early:
ARX-versus-SPN, iterative-versus-unrolled, and logic-versus-schedule are all
visible in the column profile alone. It is also the cheapest whole-netlist
picture there is -- at 613 gates the *unlevelled* graph does not render at all,
because a cyclic graph cannot be layered.

**Substitution is recoverable exactly; the permutation around it is recoverable
only through what sits on top of it.** In `12_present_sbox` the 4-bit S-box
comes out of the LUT cones as a table and matches the published one. The bit
permutation costs no logic, so it is not a signal -- it is which cell output is
soldered to which flip-flop input, and the round-key XOR in between means no
destination bit peels back to a source through wires alone. The recovery is to
stop asking about wires and ask about *dependence*: which bit of the layer above
does each flip-flop's next state read? That is
`hal_crypto permutation`'s cone-support tier now (it returns the pLayer and the
key rotation by 61 on that export), and it is what the walkthrough did by hand
first. Same lesson as the ARX rotations: **the layer that costs no logic is the
layer no wire-level tool hands you** -- recover it from index arithmetic over an
ordered layer, and say which tier the claim came from.

**The cone-support tier needs one cell per link, so the pipeline decides whether
it fires.** `14_keccak_toy` is the case that bounds it: *two* of the round's five
steps (rho and pi) are free wiring, and the two exports of that one permutation
answer differently. On `keccak_retimed.vo` the composite sits between the kept
`theta` vector and the register with one multiplexer in between, so the tier
returns the whole 200-bit map, all 200 links -- and matches nothing, because no
library carries a 200-bit Keccak rho-pi. On `keccak_toy.vo` it returns **nothing
at all**: `chi` sits between `theta` and the register, so no destination bit's
next state reads exactly one source bit. Either way the tier gives you an *index
map*, never the 25 rotation offsets or the 5x5 lane transposition -- turning one
into the other needs the lane geometry, which is the next two paragraphs.

**Recover the geometry the algebra needs before trying to read constants
expressed in it.** Keccak's algebra is over a 5 x 5 array of 8-bit lanes; the
netlist offers `s[0..199]` in a row. Declaring bit *i* to be lane *i/8* is a
guess that happens to be right and would be worth nothing on a blinded netlist.
In `14_keccak_toy` it is *derived*: 40 cells are a pure XOR of exactly five
flip-flops (select by **function**, not fan-in -- the round-constant cells also
read five registers and are not an XOR of them), those 40 five-element sets are
disjoint and cover the state, the `theta` layer gives each class two parity
neighbours, and the only closed five-step walk in that neighbour graph is the
one using the same neighbour every time -- which separates them and yields eight
cycles of five and five cycles of eight. Eight bit positions, five columns, five
rows. Look for **the layer whose fan-in is a statement about the state's shape**.

**One coordinate usually ends up a gauge, and the fix is to name it.**
`14_keccak_toy` derives columns, rows, lanes and bit adjacency from wiring, but
the *origin and direction* of the bit index inside a lane come from matching the
measured "which four of eight positions does iota ever touch" pattern against
the published one -- 16 candidates, exactly one fits. Same move as
`05_lfsr_prng`'s inverted storage (`state_encoding: as-stored | complemented`).
It stays honest because it is falsifiable (the other fifteen reproduce no
published schedule) and because the script **fails loudly if more than one
candidate fits**. Label it a gauge in the output; do not launder it into a
measurement.

**Where the register sits decides what a structural pass can see, and the
algorithm does not.** `14_keccak_toy` ships two exports of one permutation with
one interface and identical behaviour, differing only by a half-round retiming.
In the canonical one, `chi` reads the register bank *through* `theta`, every chi
cone is 33 flip-flops wide, the S-box extraction refuses to enumerate cones that
wide, and `hal_crypto identify` returns `none-detected` on a real Keccak core.
In the retimed one, chi sits on the register outputs and the same command finds
all forty instances and returns `sponge`. `12_present_sbox` makes this point
with *coding style*; this makes it one level up, at the **pipeline**. Before
concluding a design contains no cryptography, ask whether the register placement
could have dissolved it -- and note the tool says so itself, by dropping
`none-detected` to `medium` confidence whenever it refused a cone.

**A structural test that finds *nothing* is a claim about the netlist you were
given, and the first thing to suspect is the multiplexer on top.** On
`13_trivium_stream`, a real export of Trivium, the shift-chain test found **zero**
chains in a design that is three shift registers: every stage's next state is
`load ? init : q_prev`, so "driven by exactly one register" is false for all 288
at once. Holding one external net at one constant brings all of them back --
literally the same move that recovers `11_speck_toy`'s XOR layer. Expect it
wherever a core is *keyed*: the load path is the thing that makes a cipher usable
and the thing that hides its structure. And say which mode the claim holds in: "a
shift chain" and "a shift chain while `start` is low" are different statements.

**Ask whether the object closes onto itself before deciding it is open.** Trivium,
Grain and every other modern hardware stream cipher are *coupled* registers: no
segment's feedback is a function of its own stages alone (in `13_trivium_stream`,
93 -> 84 -> 111 -> 93). A chain whose head reads a sibling looks open to any pass
that classifies one chain at a time, so find every chain first and resolve foreign
taps against the set. A coupled register also has **no feedback polynomial** --
a polynomial is a recurrence over one register's own history -- and the algebraic
normal form is then the whole answer. That the SCC decomposition of the same
netlist returns **one** component of all 288 registers is not a contradiction: a
ring is mutually reachable, so the two methods are answering different questions
and both answers are load-bearing.

**A published constant set or test vector is literature, not netlist.**
`hal_crypto` reporting the `speck_32` rotation amounts is a statement about four
numbers found in wiring; an S-box match is a statement about a table, and its
**tier** (`exact` / `xor_constant` / `bit_permutation`) is part of the claim.
Neither identifies a cipher, and neither does a matching test vector. State the
structure, cite the match, keep them apart.

**When the secret is a number, the netlist holds it as a function, not as a
constant.** Everything through `14_keccak_toy` recovered *logic* — tables, taps,
permutations, parities. `15_ntt_mult`'s secrets are a prime, a root of unity and
eighty-one twiddles, and the export contains none of them as constants. The
modulus `q = 257` is `2**8 + 1`, so subtracting it is an increment and one bit
flip and Quartus spends no carry chain on it: there is not one constant-operand
chain in the design. It comes back anyway, by asking the question the other way
round — the adder's output is known exactly on any operand vector, so for a
candidate *q* and a candidate select net the *corrected* vector is known too, and
either the netlist contains it bit for bit or it does not. The eighty-one
twiddles come back by **holding every coefficient register at 1**, which makes
the multiplier report its own operand in a bit order the carry chain already
fixed. Both moves generalise: when a constant is not a constant, drive the
thing that consumes it and read what comes out.

**Read a vendor's arithmetic, not a textbook's.** `hal_crypto` found *zero*
butterflies in a real Quartus export of a butterfly, because its subtracter
recogniser had been built against hand-written fixtures. A vendor `a - b` folds
the inversion into the arithmetic cell's own mask (independent operand
polarities, not one shared one) and manufactures its carry-in of one with a
leading **carry-seed** cell, because an ALM's `cin` can only come from the
previous cell's `cout`. Same lesson `12`, `13` and `14` each contributed one of:
a pass built against a synthetic shape has not met a synthesiser. When a
recognizer returns nothing on a design you are sure contains the thing, suspect
the *spelling* before the presence.

**Depth in silicon and depth in the algorithm are different measurements, and an
iterative core makes them disagree on purpose.** `15_ntt_mult` is 43 levels over
910 cells — the deepest in the series by a factor of two and a half, because a
9 x 9 array multiplier and two modular corrections sit between one register bank
and the next — and it contains **one** butterfly. Its four transform stages of
eight butterflies are a schedule in a six-bit counter, so `hal_crypto` reports
`stage_depth: 1` and is right. Recovering the schedule takes the *orbit*: walk
the control registers from a load, find the counter by the rule that bit *k*
toggles only where bits 0..*k*-1 are all one (not by activity — a phase bit can
toggle more often than the counter's top bit), and read the phase lengths off
where the counter resets.

**"Classical or post-quantum" is not always a question the structure answers,
and `undetermined` is the right answer when it is not.** `11`, `12` and `13` all
end `classical-style` and each time that is real. A **sponge** is the case where
the reasoning stops: SHA-3 and SHAKE are classical hashing, and the *same* SHAKE
is the extendable-output function inside ML-KEM and ML-DSA and the entirety of
SPHINCS+. "A Keccak core is present" is evidence about what the design computes
and none about which family of scheme uses it -- what decides that is the
arithmetic *around* the sponge. `hal_crypto` therefore reports `sponge` with
`high` confidence and `style: undetermined` with no confidence number at all:
the family claim is strong and the axis claim does not exist. A report naming
the ambiguity is more useful than one that picks a side, because it says what to
go and look for next.

**`pqc-style` needs a fence around it too, and the fence is the parameters.**
`15_ntt_mult` ends `pqc-style`, and that is also real: butterflies over a small
modulus are the structure lattice schemes are built from. What it does not say is
that the design *is* one. `n = 16`, `q = 257` are toy parameters; a deployed
scheme needs `n = 256`, a modulus for which none of a Fermat prime's shortcuts
exist, plus sampling, encoding, hashing and a protocol. `hal_crypto` marks the
distinction where it can measure it: the recovered modulus is in no
published-parameter library, so the confidence tier is `medium` rather than
`high` and the style text says the design is "the shape of a lattice scheme's
arithmetic and not any deployed one's parameters". *Say the number and say which
book it is absent from* is more useful than either "PQC" or silence.

**What survives synthesis is decided by the RTL, not by the algorithm.**
`12_present_sbox` ships two counterfactual exports of the *same* cipher: drop a
`keep` attribute and the S-box still extracts but matches nothing in the
library; move where the registers sit and the substitution layer stops existing
as cones at all (`none-detected`). Before concluding "this design contains no
S-box", ask whether the coding style could have dissolved one -- a negative from
a structural pass is a statement about the netlist you were given.

### The blind template, and what it scored

`16_mystery_cores` runs all of the above on five *anonymized* exports with the
decision rule written down first, then opens the answer key and scores it. Use
its `spec.md` section 4 as the template for a real target; use its `analysis.py`
as the executable form. The six steps, in order, and **steps 1-4 must not read
step 5**:

1. **census** -- gate histogram, ports, flip-flops, carry chains, and
   *evaluate* every chain. A chain is unambiguous; what it computes is the
   discriminator. The decoy in that set has a 16-cell chain and 17 levels of
   depth, identical to the Speck export's; the only census-level difference is
   that its chain adds the **constant 1** and Speck's two add two operand
   vectors each.
2. **shape** -- level the combinational core with the feedback cut at the flops
   (see the table above).
3. **state** -- the flip-flop-only dependency graph built from *support*, not
   from truth tables, so a cone too wide to enumerate still contributes its
   edges. Component sizes say how many machines; the **direction** between them
   is architecture (`64 -> 32`, one way, is a key schedule driving a data path,
   recovered blind). Walk chains by **predecessor**: `shiftreg`'s successor walk
   stops at the first stage with two successors, so a shift register whose
   stages are *tapped* vanishes entirely.
4. **nonlinearity** -- next-state ANF degrees where the cones enumerate, and a
   **count of the cones that do not**. A refusal is evidence: 200 of 207
   next-state cones too wide to read is a diffusion layer, not a missing
   measurement. Then look for cells that are a pure XOR of exactly *k* register
   outputs with **pairwise-disjoint** sources -- 40 disjoint fives is a layer,
   40 non-disjoint fives is a coincidence.
5. **ask the tool** -- record `hal_crypto identify` verbatim, including the
   negative.
6. **call it against the written rule**, report which rule fired, whether the
   tool agreed, and what was not attempted.

Three results from that run are worth carrying:

**Rule *order* can be load-bearing, so write the order down.** An NTT butterfly
and an ARX round have the same census -- an add and a subtract (or two adds) of
the same width over two operand vectors, behind deep logic. On `15_ntt_mult`'s
export both `R1-lattice` and `R4-arx` fire. Nothing in the evidence separates
them; the table's ordering does.

**Run a structural pass on a *less* blinded copy before believing a negative.**
Two cores came back `none-detected` from `hal_crypto` and one came back
correctly negative, and from the verdicts alone they are indistinguishable.
Re-running the same command on the named export separates them: Speck's `arx`
verdict exists on the named netlist and disappears on the blinded one (a
*blinding* failure -- `arx.adder_operand_rotations` groups operand bits by the
text before the `[` in their net names, so splitting vectors into scalars
destroys the word it needs), while Keccak's `none-detected` is identical on both
(a pipeline property `14` already documents). One is a bug, the other is not, and
only the control tells you which.

**`none-detected` on three of five, with one of them right, is the shape of the
problem.** A structural verdict of "nothing found" carries no information about
*why* nothing was found. Ask, in order: could the pipeline have dissolved the
layer (`14`), could the coding style have (`12`), could the blinding have
(`16`), and is the design simply not cryptography.

## The epistemics this series is actually teaching

- **Structure and behaviour are independent evidence, not a pipeline.**
  Agreement between methods that never read each other is real evidence;
  disagreement is information; both must run.
- **Every bound needs a negative control.** A green "matches for N cycles"
  means nothing until you know what a wrong answer would take to catch.
- **Simulation only checks what the reachable state space reaches.** Widen
  the window/stimulus, or fall back to structure, rather than trust a run's reach.
- **Wrong predictions get corrected in the open, and "unknown" is a real
  result, not a dead end.** A heuristic decided "by exclusion" is flagged
  weaker, not presented as clean; "nothing found" means the shape doesn't
  match what the tool knows -- fall back to the general method rather than
  try recognizer variants.

## Where things live
- Examples, teaching order: `examples/agilex3_walkthroughs/
  {01_blinky_counter..16_mystery_cores}/guide.html` + that directory's
  `README.md` ("what it teaches" table). Per walkthrough: `analyze.py`/
  `analysis.py` (structure), `probe.py` (behaviour), `reference.py`/
  `recovered_reference.py` (step 7/8 models), and `check.py` -- the
  pattern for asserting claims instead of eyeballing them: re-derives
  every load-bearing number, exits nonzero the moment a HAL/plugin change
  invalidates it (`--with-hal` gates the tier needing a built HAL).
