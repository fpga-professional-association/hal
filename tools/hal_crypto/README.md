# hal_crypto — structural crypto identification for Agilex 3 netlists

Given an anonymized post-synthesis netlist, three questions come in order:
*is there cryptography in here at all*, *what structural family is it*, and
*is that family a classical symmetric one or a lattice/PQC-style one*. This
package answers all three from structure only, and it is built so that it
cannot answer them more confidently than the structure allows.

Everything except `hal_adapter.py` runs on a plain Python 3 interpreter with the
standard library only — no HAL build, no Quartus, no network. The `.vo` reader
and the `tennm_*` primitive semantics come from `tools/hal_agilex`; this package
imports them and never re-implements them, because a second reader is a second
chance to be wrong about what the vendor wrote.

## The six passes

| module | question | positive control | negative control |
| --- | --- | --- | --- |
| `sbox.py` | which LUT cones form an *n*-bit bijection — whether each output reads every input (PRESENT, AES) or a subset of them (Keccak/Ascon `chi`) — and does it equal a published S-box? | `fixtures/present_sbox_layer.vo`, `fixtures/keccak_chi_layer.vo` | `fixtures/unknown_sbox_layer.vo`, `fixtures/counter8.vo` |
| `shiftreg.py` | is a register chain closed by feedback -- by its own stages or by a sibling's; is the feedback linear (polynomial) or not (ANF); Fibonacci or Galois? | `fixtures/lfsr16_fibonacci.vo`, `fixtures/lfsr16_galois.vo`, `fixtures/lfsr16_loadable.vo`, `fixtures/coupled_nlfsr.vo` | `fixtures/nlfsr16.vo`, `fixtures/shift16_plain.vo` |
| `arx.py` | are adders, fixed rotations and an XOR layer present **and wired together**? | `fixtures/arx_round8.vo` | `fixtures/counter8.vo`, `fixtures/rotate16.vo` |
| `permutation.py` | which pure-wire bit maps exist, and do they equal a published pLayer or rotation set? and which maps survive one cell per link? | `fixtures/rotate16.vo` (wiring), `fixtures/spn_round16.vo` (cone support) | `fixtures/counter8.vo`, `fixtures/mux_bank16.vo` |
| `ntt.py` | is there an add/subtract butterfly over the same operands, and what modulus is behind it — in a constant-operand chain, or in what the correction *computes*? | `fixtures/ntt_stage13.vo` (chain tier), `fixtures/ntt_fermat17.vo` (reduction tier, and a vendor-shaped subtracter) | `fixtures/butterfly4.vo` (butterfly, no modulus) |
| `classify.py` | all of the above, as one family verdict and one classical/PQC verdict | every fixture declares its expected verdict in `fixtures/MANIFEST.json` | `examples/agilex3_walkthroughs/01_blinky_counter` |

Two shared layers sit under them: `netlist_model.py` (cone extraction and exact
truth-table evaluation) and `arith.py` (carry chains read as verified
arithmetic). `boolfunc.py` is the Boolean algebra; `known.py` is the library of
published constants.

## Commands

```bash
# no HAL needed for any of these
python tools/hal_crypto identify examples/agilex3_walkthroughs/05_lfsr_prng/lfsr_prng.vo
python tools/hal_crypto identify examples/agilex3_walkthroughs/01_blinky_counter/blinky_counter.vo

# one pass at a time, same findings format
python tools/hal_crypto sbox        tools/hal_crypto/fixtures/present_sbox_layer.vo
python tools/hal_crypto lfsr        tools/hal_crypto/fixtures/lfsr16_galois.vo
python tools/hal_crypto arx         tools/hal_crypto/fixtures/arx_round8.vo
python tools/hal_crypto permutation tools/hal_crypto/fixtures/rotate16.vo
python tools/hal_crypto ntt         tools/hal_crypto/fixtures/ntt_stage13.vo

# regenerate / verify the synthesized fixtures
python tools/hal_crypto fixtures --check
python tools/hal_crypto fixtures --write
```

Exit codes: `0` success, `1` failure, `2` with `--strict` when the findings
contain an `error` or `unsupported` result. `unknown` is deliberately *not*
blocking — `none-detected` is a result, not a failure.

## What the claims are allowed to say

The wording discipline is the point of the package, not a decoration on it.

* **Table equality is a fact; algorithm identity is not.** A finding may say
  "these four cones compute a bijection equal to the PRESENT S-box under a bit
  permutation". It may not say "this is PRESENT". The same S-box appears in
  more than one construction and an S-box on its own is not a cipher.
* **The match tier always travels with the match.** `exact`, `xor_constant` and
  `bit_permutation` are three different claims (see the module docstring of
  `boolfunc.py`), and the pass chooses the bit order itself, so a
  bit-permutation hit is the *normal* outcome, not a near miss. Where the
  permutation search was too expensive to run — seven and eight bits — the
  finding says the search was **not performed**, rather than reporting a
  silent negative.
* **The PQC verdict is about structure.** `pqc-style` means "lattice-style
  NTT / ring arithmetic is present". It never names a scheme. Symmetrically,
  `classical-style` does not rule out post-quantum cryptography: a hash-based
  or code-based scheme contains neither NTTs nor S-boxes and lands in
  `none-detected`.
* **A sponge is not placed on the axis at all.** `sponge` is the one family
  whose style verdict is `undetermined`, with the finding at `unknown` status
  and no confidence number: SHA-3 and SHAKE are classical hashing, and the same
  SHAKE is the extendable-output function inside ML-KEM and ML-DSA and the
  whole of SPHINCS+. The *family* verdict is still `high` confidence — "this is
  a sponge" is a strong structural claim and "therefore it is classical" is not
  a claim. What decides the axis is the arithmetic around the sponge.
* **`none-detected` is a first-class outcome**, emitted with a per-pass list of
  what each pass looked for and did not find, so the negative can be argued
  with instead of shrugged at.
* **Exhaustive and sampled results are different statuses.** An S-box table or
  a feedback polynomial comes from enumerating a cone's whole input space, so
  those findings are `proven_under_assumptions` with the assumptions spelled
  out. An adder verified on 256 seeded operand vectors is `heuristic`, and the
  vector count is in the finding.

## Two conventions that would otherwise be silent errors

**Storage polarity.** Quartus implements an asynchronous load of a non-zero
seed by storing some register stages inverted and putting an inverter on the
output — `examples/agilex3_walkthroughs/05_lfsr_prng` has eight of its sixteen
stages that way. Half the shift links then read `d = !q_prev`. `shiftreg.py`
works in a gauge (`stage i = q_i XOR c_i`, `c` the running inverter parity) in
which the tap set is invariant, normalises the remaining freedom so the
feedback constant is zero when it can be, and records which encoding it chose.

**Polynomial numbering.** A tap at stage *t* is written `x^(t+1)` here, with
stage 0 the chain head. That is the convention `05_lfsr_prng/spec.md` uses, and
it reproduces `x^16 + x^15 + x^13 + x^4 + 1` exactly. A CRC specification
numbers the register from the other end, which gives the reciprocal polynomial:
`10_crc8_checker` comes out as `x^8 + x^7 + x^6 + 1` under this convention and
`x^8 + x^2 + x + 1` — its stated 0x07 generator — as the reciprocal. Both are
reported, always, with the convention named.

### A shift register in a vendor export may have no visible links, and may not close onto itself

Both were measured on `examples/agilex3_walkthroughs/13_trivium_stream`, a
Quartus Prime Pro export of Trivium, where the plain reading found **zero**
chains in a design that is three shift registers:

- **a parallel key/IV load hides every link.** `s[i] <= load ? init[i] :
  s[i-1]` is a multiplexer, so "this register's next state is exactly another
  register's output" is false for all 288 stages at once. `shiftreg.py` ranks
  the external nets by how many next-state functions read them *nonlinearly*,
  holds the best one or two at each value, and looks again — the same cofactor
  move `arx.xor_nets` uses, applied to the links instead of the XORs. A
  structure found that way carries `mode: {"net": "start", "value": 0}` and
  every finding built from it says the claim holds in that operating mode.
  A held net can only *add* feedback registers: a structure that is merely an
  open chain under a held select is dropped, because a register bank that
  becomes a shift chain once its load select is held is just a loadable
  register bank, which is what most register banks are.
- **the feedback may close through a sibling.** Trivium, Grain and the rest of
  the hardware stream ciphers are *coupled* registers: no segment's feedback is
  a function of its own stages alone. Every chain is therefore found before any
  of them is classified, and a head that reads stages of another chain is
  reported `coupled: true`, with `coupled_chains`, the foreign taps named one by
  one, and variables written `s<chain>[<stage>]` — because `s[65]` means two
  different flip-flops once two registers are in play. A coupled register has
  **no feedback polynomial**: a polynomial describes a recurrence over one
  register's own history, so `polynomial` is `None` there and the ANF is the
  whole report.

### An ARX round in a vendor export names neither its R nor its X

Both were measured on `examples/agilex3_walkthroughs/11_speck_toy`, a Quartus
Prime Pro export of Speck32/64, and both defeat the obvious reading:

- **the rotation is not a vector.** Rotating a word is free wiring, so a
  synthesiser keeps no net for it: in that export not one declared vector, and
  not one register bank's `d` pins, carries a rotated word. What survives is
  the *order an ordered layer of cells reads one bank* — the operand of carry
  chain slice *i*, or the single bank bit that bit *i*'s next-state cell reads.
  `permutation.vector_rotation` and `permutation.register_bank_rotations`
  classify those, and every reported rotation says under `read_from` which of
  the three readings produced it;
- **the XOR is packed with the multiplexer beside it.** `x <= load ? pt : (sum
  ^ k)` is four inputs and fits one ALM, so all sixteen XOR cells of the Speck
  round are multiplexers and none is an XOR of anything. `arx.xor_nets` also
  accepts a cell that becomes an XOR once **one** input is held at a constant,
  and reports it as `conditional` with the `cofactor` that recovered it.
  Standalone and conditional XOR cells are counted separately in the finding,
  because one is stronger evidence than the other.

Holding one input is deliberately the limit: freeze enough inputs and almost
any function turns affine.

### A pLayer in a vendor export is not a wire either

Measured on `examples/agilex3_walkthroughs/12_present_sbox`, a Quartus Prime Pro
export of PRESENT-80. Both of that cipher's bit maps are fully present in the
netlist and neither is visible to a pass that requires wiring: the round-key
XOR sits between the substitution layer and the datapath register, and a
parallel key load puts a multiplexer on every link of the 80-bit key register.
One 3-input ALM per bit hides a 64-bit pLayer and an 80-bit rotation.

`permutation.cone_support_maps` reads them off the *cone support* instead. For
every register bank it asks, of each bit's next-state function, **which bits of
vector V does it depend on** — with the walk cut at every declared vector, so
the answer names the layer above the logic rather than the registers behind it,
and with each cone enumerated exhaustively, so a dead LUT input cannot
manufacture a link. Exactly one bit is a link; a bijection of the bank, or a
single consistent rotation amount, is a map. On that export it returns the
pLayer (matching the published `present_player`, all 64 bits) and the key
schedule's rotation left by 61 (76 of 80 bits — the other four go through the
S-box).

This tier is **weaker than the wiring tier and never replaces it**. Its
findings are `hal_crypto/permutation/cone-support`, carry
`read_from: "next-state cone support"`, and carry an explicit
`one-side-input-per-link` assumption: the claim is *which bit each register
reads*, modulo whatever else the cone reads, so it describes the operating mode
in which the source reaches the register. Three rules keep it honest:

- a map the wiring tier, or `register_bank_rotations`, already reports is not
  reported again — the reading that needs less inference stands alone;
- when two sources each explain every link into a bank, nothing is reported:
  that is a multiplexer choosing between operands, which is what
  `fixtures/mux_bank16.vo` pins;
- a *partial* map is only ever a rotation, never a general permutation, and
  never one by ±1: a bank whose bits each read their neighbour with the head
  left over is an open shift chain, and `shiftreg.py` names it properly, with
  its feedback.

## Fixtures

`fixtures/*.vo` are **synthesized shapes, not vendor exports**: they are
assembled by `fixtures/synth.py` out of the same `tennm_lcell_comb` /
`tennm_ff` semantics that `tools/hal_agilex/fixtures` validated against real
Quartus output. They prove things about the recognizers and nothing about
vendor primitive behaviour, and `fixtures/GROUND_TRUTH.md` says so. They are
regenerated with `fixtures --write` and a stale committed copy fails
`fixtures --check` and the unit suite.

Every emitted netlist goes through `hal_agilex.vo_netlist` in the tests, so a
fixture the shared reader would refuse cannot exist.

## Tests

```bash
python -m unittest discover -s tools/hal_crypto -t tools -p "test_*.py"
```

115 tests, no HAL, ~16 s. Registered with ctest as
`runTest-hal_crypto_standalone` in `tests/headless_smoke/CMakeLists.txt`. The
end-to-end cases are the acceptance criteria of the issues this package came
from: `05_lfsr_prng` must classify `lfsr-stream` with the polynomial its own
specification states, `01_blinky_counter` must classify `none-detected`, and
`11_speck_toy` must classify `arx` with the published SPECK-32/64 rotation set
quoted and no claim that the design *is* SPECK, and `13_trivium_stream` must
classify `lfsr-stream` as three coupled NLFSRs of 93, 84 and 111 stages with the
published Trivium feedback functions recovered and no polynomial claimed for any
of them, and `12_present_sbox` must yield the PRESENT pLayer and the key
register's rotation by 61 from the cone support, with its `variants/` exports
showing what a different register placement costs. `14_keccak_toy` adds the pair
that makes the coverage limit explicit:
`keccak_toy.vo` must classify `none-detected` at `medium` confidence, because
chi reads the register bank through theta and 400 cones are too wide to
enumerate, and `keccak_retimed.vo` — the same permutation with the register
moved half a round — must classify `sponge` at `high` with forty `keccak_chi_5`
matches and style `undetermined`. `15_ntt_mult` is the arithmetic case: it must
classify `lattice-ntt` / `pqc-style` at `medium` confidence with the modulus
**257** named and reported as matching nothing published, from a netlist that
contains no constant-operand carry chain at all.

### A vendor's subtracter, and a modulus that is not a constant

Two things went wrong on the first real NTT export and both were the package's
fault, not the design's.

The **subtracter** was not recognised, so there was no butterfly. `a - b` is
`a + ~b + 1`, and the hand-written fixtures spell that with an inverter cell and
a carry-in tied to `vcc`. A synthesiser can do neither: it folds the inversion
into the arithmetic cell's own mask (so the two operands carry *independent*
polarities, and the mask halves read `XNOR(a,b)` and `a AND NOT b`), and an
ALM's `cin` comes only from the previous cell's `cout`, so it manufactures the
constant carry with a leading **carry-seed** cell — no data operands, no sum
output. `arith.carry_seed` reads that cell and `arith._slice_operands` searches
the four operand polarities instead of two.

The **modulus** was not there to be read. `sum - q` only gets an arithmetic
chain when `q` is expensive to add; at `q = 2**k + 1` it is an increment and one
bit flip, built out of ordinary LUTs. `ntt.reduction_moduli` derives `q` from
what the correction computes instead: for each candidate select net it builds
`q` one bit at a time — bit *k* of `sum - q` depends only on bits 0..*k* of `q`,
because a borrow only travels up — and keeps a candidate only when the corrected
vector can be located again on a sweep of **every attainable chain result**. It
reproduces `3329` on `ntt_stage13`, where the chain tier already had it, which
is what makes the two a cross-check rather than two guesses.
`fixtures/ntt_fermat17.vo` is the minimal case for both.

### An S-box whose output bits read a *subset* of the inputs

Every published S-box in `known.py` except the `chi` family has output bits that
read *every* input bit, which is why the extraction can key its search on the
support of a single cone. `chi` does not: `y_i = x_i ^ (~x_{i+1} & x_{i+2})`
reads three of five, so a five-lane row is five cones covering five sources with
no cone covering them all. `sbox.cluster_supports` names those candidate
supports by growing a set from one cone, each time adding the neighbouring cone
that brings the **fewest new sources**. Following *any* neighbour instead merges
the substitution layer with the parallel-load multiplexers above it — on a real
Keccak export that turns forty five-source clusters into one cluster of
everything — so the "cheapest" qualifier is the whole of the rule.
`fixtures/keccak_chi_layer.vo` is the minimal case, and
`fixtures/GROUND_TRUTH.md` states what must come out of it.

`elaborate` is the one subcommand that loads a netlist in HAL's own process, so
it also has a case in `tests/headless_smoke/tool_cli_plugin_load_smoke.py`,
which runs it against a real build in a fresh interpreter. `hal_crypto` reaches
`hal_py.NetlistFactory` through `hal_agilex.hal_adapter` rather than calling it
itself, and that indirection is only correct as long as the module it delegates
to keeps loading HAL's plugins — which is precisely what that audit checks.

## Where things live

- `boolfunc.py` — truth tables, ANF, S-box algebra, the three match tiers.
- `known.py` — the published constants (S-boxes, pLayers, rotation sets,
  moduli), derived rather than transcribed wherever they can be.
- `netlist_model.py` — cones, sources, exact and sampled evaluation.
- `arith.py` — carry chains classified *and verified* as add / subtract /
  add-constant.
- `sbox.py`, `shiftreg.py`, `arx.py`, `permutation.py`, `ntt.py` — the passes.
- `classify.py` — the aggregation rules and every findings builder.
- `findings.py` — artifact, method descriptors and document envelope.
- `hal_adapter.py` — the only module that may import `hal_py`.
- `fixtures/` — `synth.py`, the generated `.vo` files, `MANIFEST.json`,
  `GROUND_TRUTH.md`.
- Skill: `ai/skills/hal-crypto-id/SKILL.md`.
