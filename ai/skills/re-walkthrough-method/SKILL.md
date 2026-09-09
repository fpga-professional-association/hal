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
  {01_blinky_counter..10_crc8_checker}/guide.html` + that directory's
  `README.md` ("what it teaches" table). Per walkthrough: `analyze.py`/
  `analysis.py` (structure), `probe.py` (behaviour), `reference.py`/
  `recovered_reference.py` (step 7/8 models), and `check.py` -- the
  pattern for asserting claims instead of eyeballing them: re-derives
  every load-bearing number, exits nonzero the moment a HAL/plugin change
  invalidates it (`--with-hal` gates the tier needing a built HAL).
