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

**Substitution is recoverable exactly; the permutation around it usually is
not.** In `12_present_sbox` the 4-bit S-box comes out of the LUT cones as a
table and matches the published one, while the bit permutation is pure wiring
that no pass reports -- it had to be read off which cone drives which flip-flop.
Same lesson as the ARX rotations: **the layer that costs no logic is the layer
no tool hands you**, in both families. Recover it from index arithmetic over an
ordered layer, and say so.

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

**What survives synthesis is decided by the RTL, not by the algorithm.**
`12_present_sbox` ships two counterfactual exports of the *same* cipher: drop a
`keep` attribute and the S-box still extracts but matches nothing in the
library; move where the registers sit and the substitution layer stops existing
as cones at all (`none-detected`). Before concluding "this design contains no
S-box", ask whether the coding style could have dissolved one -- a negative from
a structural pass is a statement about the netlist you were given.

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
  {01_blinky_counter..13_trivium_stream}/guide.html` + that directory's
  `README.md` ("what it teaches" table). Per walkthrough: `analyze.py`/
  `analysis.py` (structure), `probe.py` (behaviour), `reference.py`/
  `recovered_reference.py` (step 7/8 models), and `check.py` -- the
  pattern for asserting claims instead of eyeballing them: re-derives
  every load-bearing number, exits nonzero the moment a HAL/plugin change
  invalidates it (`--with-hal` gates the tier needing a built HAL).
