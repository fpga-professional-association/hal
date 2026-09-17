# hal_crypto fixtures — what they are and what must be found in them

**These are synthesized shapes, not vendor exports.** Every `.vo` in this
directory is assembled by [`synth.py`](synth.py) out of `tennm_lcell_comb` and
`tennm_ff` cells, using the mask decoding and register model in
`hal_agilex.primitives`. Nothing here came out of Quartus.

That distinction matters for what a green test here proves. The real exports in
`tools/hal_agilex/fixtures` are what establishes that the primitive semantics
are right — they were simulated against the RTL they were synthesised from.
These fixtures assume those semantics and exercise the *recognizers* on top of
them. A passing test here says "the pass finds what is in the netlist"; it says
nothing about whether the netlist behaves like silicon.

Regenerate with `python tools/hal_crypto fixtures --write`; verify with
`python tools/hal_crypto fixtures --check`. `MANIFEST.json` carries the SHA-256
of each generated file and is regenerated with them, so a hand-edited fixture
fails the check.

## The fixtures

| file | shape | role | `identify` family | style |
| --- | --- | --- | --- | --- |
| `present_sbox_layer.vo` | two 4-bit PRESENT S-boxes between register banks | positive: S-box extraction **and** an exact library match | `spn` | `classical-style` |
| `unknown_sbox_layer.vo` | the same shape with a fixed 4-bit bijection that is in no library | negative for *matching*: the box is extracted, nothing matches it | `spn` | `classical-style` |
| `keccak_chi_layer.vo` | two Keccak chi rows of five lanes, behind a parallel `load ? seed : chi` | positive for the *cluster* search: each output reads three of five sources, so no single cone's support names the box | `sponge` | `undetermined` |
| `lfsr16_fibonacci.vo` | 16-stage Fibonacci LFSR, taps 3/12/14/15 | positive: `x^16 + x^15 + x^13 + x^4 + 1`, maximal length | `lfsr-stream` | `classical-style` |
| `lfsr16_galois.vo` | 16-stage Galois LFSR, injections at 1/3/12 | positive: Galois form, same characteristic polynomial as above | `lfsr-stream` | `classical-style` |
| `nlfsr16.vo` | the Fibonacci taps with an AND term in the feedback | negative for *linear* feedback: an ANF, no polynomial | `lfsr-stream` | `classical-style` |
| `shift16_plain.vo` | 16 registers in a row, driven by a port | negative for the LFSR pass: a chain with no feedback | `none-detected` | `undetermined` |
| `lfsr16_loadable.vo` | the same Fibonacci LFSR behind a parallel `load ? seed : shift` | positive for the operating-mode search: the chain only exists with `load` held at 0 | `lfsr-stream` | `classical-style` |
| `coupled_nlfsr.vo` | two chains, 8 and 10 stages, each closed through the other | positive for coupled (Trivium/Grain-style) registers: no chain has a polynomial of its own | `lfsr-stream` | `classical-style` |
| `arx_round8.vo` | 8-bit add, rotate left by 3, XOR, register | positive: ARX round | `arx` | `classical-style` |
| `counter8.vo` | 8-bit accumulator, `count + step` | negative for ARX: an adder with no rotation and no XOR layer | `none-detected` | `undetermined` |
| `butterfly4.vo` | 4-bit `a+b` and `a-b` over the same registers | positive for butterfly detection, negative for the modulus | `none-detected` | `undetermined` |
| `ntt_stage13.vo` | the same butterfly plus a conditional subtract of 3329 | positive: lattice-style modular transform | `lattice-ntt` | `pqc-style` |
| `ntt_fermat17.vo` | a butterfly shaped the way Quartus shapes one, reduced modulo the Fermat prime 17 with LUTs instead of a chain | positive for the vendor subtracter and for reading a modulus out of the correction | `lattice-ntt` | `pqc-style` |
| `rotate16.vo` | two 16-bit banks joined by a rotation of 5 | positive for the permutation pass, negative for ARX | `none-detected` | `undetermined` |
| `spn_round16.vo` | one 16-bit SPN round: four PRESENT S-boxes, a `4i mod 15` permutation, and a round-key XOR plus a parallel load between them and the register | positive for the cone-support tier: the permutation is behind one cell per link | `spn` | `classical-style` |
| `mux_bank16.vo` | two 2-to-1 datapath multiplexer banks: one selecting between two source banks, one between two rotations of the same bank | negative for the cone-support tier: a multiplexer is not a permutation layer, in either shape | `none-detected` | `undetermined` |

## The exact claims the tests assert

**`present_sbox_layer.vo`** — two groups of four cones, over `state[0..3]` and
`state[4..7]`. Each group's extracted table is
`C5 6B 90 AD 3E F8 47 12` (the PRESENT S-box, CHES 2007 table 1), matched at
the **`exact`** tier because the pass's own bit order happens to line up with
the published one. Algebraic degree 3, differential uniformity 4.

**`unknown_sbox_layer.vo`** — the same two groups, table
`0 3 5 8 B E 1 6 D 2 F 4 9 C 7 A`. Bijective, non-affine, and matched by
nothing in the library at the `exact`, `xor_constant` or `bit_permutation`
tier. The family is still `spn` — two extracted substitutions are a
substitution layer whether or not anyone has published them — and the finding
is `heuristic`, not `proven_under_assumptions`.

**`keccak_chi_layer.vo`** — two groups of five cones, over `state[0..4]` and
`state[5..9]`. Each group's extracted table equals `keccak_chi_5` at the
**`exact`** tier: `y_i = x_i ^ (~x_{i+1} & x_{i+2})` over a five-lane row, FIPS
202 section 3.2.4. Algebraic degree 2, differential uniformity 8.

Two things are being asserted here and neither is about chi itself. The first
is that a substitution whose output bits read a *subset* of the inputs is found
at all: keying the search on the support of one cone -- which is enough for
PRESENT, AES and every other S-box whose output bits read every input bit --
finds **nothing** here, because no cone reads more than three of the five
sources. The second is that the parallel load on top does not hide it. Those
ten multiplexer cells read the same registers while each dragging in `load` and
one `seed` bit, so a cluster search that follows any neighbour merges the two
rows and the load path into one twelve-source blob and reports nothing;
following the neighbour that adds the *fewest* new sources walks along the chi
row and stops at the multiplexers. `hal_crypto.sbox.cluster_supports` is that
rule, and this fixture is the case that fails without it.

The style verdict is `undetermined`, not `classical-style`, and that is the
point of the family being separate: the same permutation is SHA-3 and is the
SHAKE inside ML-KEM, ML-DSA and SPHINCS+, so a sponge on its own places a
design on neither side of the axis.

**`lfsr16_fibonacci.vo` / `lfsr16_galois.vo`** — both come out as
`x^16 + x^15 + x^13 + x^4 + 1`, period 65535, maximal. The Galois one is
labelled `galois` with `injection_stages = [1, 3, 12]`; its polynomial is
derived by simulating the state map and running Berlekamp-Massey on it, not
assumed from an index formula.

**`nlfsr16.vo`** — the chain and the tap positions are the same, but the
feedback is `s[12] ^ s[3] ^ (s[14] & s[15])`. There is no `polynomial` key in
the result at all; the finding reports the ANF and the algebraic degree.

**`shift16_plain.vo`** — a 16-stage chain whose head is driven by the `din`
port. Reported as `shift_register` with the reason that it does not feed back.
Family `none-detected`: a shift register is not a cipher.

**`lfsr16_loadable.vo`** — the same 16 stages and the same taps as
`lfsr16_fibonacci.vo`, but every stage's next state is `load ? seed[i] :
shifted`. Read with nothing held, **no chain exists at all**: not one register
is driven by exactly one other register. The pass ranks the external nets by how
many nonlinear next-state functions read them, holds the winner (`load`) at 0
and at 1, and keeps what that buys only when it is a *feedback* register. At
`load = 0` the result is the identical LFSR — taps 3/12/14/15,
`x^16 + x^15 + x^13 + x^4 + 1`, period 65535 — carrying
`mode: {"net": "load", "value": 0}`, and every finding built from it says the
claim holds in that operating mode. At `load = 1` there is nothing, and the open
chains a held select would otherwise manufacture are deliberately dropped: a
register bank that becomes a shift chain once its load select is held is just a
loadable register bank.

**`coupled_nlfsr.vo`** — chain 0 is `a_0..a_7`, chain 1 is `b_0..b_9`, and
neither head is a function of its own stages alone:
`s0[5] ^ s1[9] ^ (s1[7] & s1[8])` drives the first and
`s0[7] ^ s1[6] ^ (s0[5] & s0[6])` the second. Both are reported as `nlfsr` with
`coupled: true`, `coupled_chains`, and the foreign taps named one by one; the
variables are written `s<chain>[<stage>]` precisely because `s[5]` means two
different flip-flops once two registers are in play. Neither carries a
`polynomial` — a feedback polynomial describes a recurrence over one register's
own history, and this recurrence has none. Family `lfsr-stream`: the pair is one
18-bit nonlinear generator.

**`arx_round8.vo`** — one verified 8-bit adder (`x + y`), one rotation
(`rot[i] = y[(i-3) mod 8]`, reported as `rotate_left_by: 3` and
`rotation: 5`), and eight pure-XOR cells that read the adder's sums. All three
ingredients *and* the wiring between them, so `arx-candidate`. The rotation
amount matches no published quarter-round set, and the finding says so by
listing none.

**`counter8.vo`** — the adder is found and verified (`count + step`, 8 bits),
and that is all: no rotation, no XOR cell. `not-arx`, with both missing
ingredients named. This is the fixture that keeps "has an adder" from becoming
"is a cipher".

**`butterfly4.vo`** — `a + b` and `a - b` over `a[0..3]` / `b[0..3]`, both
verified exhaustively (8 operand bits, 256 vectors, every one checked). The
butterfly is found; there is no constant-operand chain, so there is no modulus,
so the verdict stays `no-modular-transform` and the family stays
`none-detected`. A butterfly without a modulus is a sum/difference unit.

**`ntt_stage13.vo`** — the same butterfly at 13 bits, plus a chain that adds
the constant `4863`. Since `2^13 - 4863 = 3329`, that is a subtraction of the
ML-KEM/Kyber ring modulus, and the pass reports `3329` as a *named modulus
candidate*. Verdict `lattice-style-modular-transform`, family `lattice-ntt`,
style `pqc-style` — and the finding text says explicitly that this does not
identify a scheme. The *reduction* tier below reaches the same `3329`
independently, which is what makes it a cross-check rather than a second guess.

**`ntt_fermat17.vo`** — the shape a real Quartus export has, which none of the
fixtures above did, at 5 bits and `q = 17 = 2^4 + 1`. Three differences, all of
them load-bearing:

* the subtracter's second operand is inverted **inside the arithmetic cell's own
  mask** (`XNOR(a,b)` and `a AND NOT b`), not by an inverter cell, so the two
  operands have *independent* polarities;
* its carry-in of one comes from a leading **carry-seed** cell with no data
  operands and no sum output — an ALM's `cin` comes only from the previous
  cell's `cout`, so a constant carry has to be manufactured;
* both modular corrections are **plain LUTs**. `q = 2^4 + 1` is an increment and
  one bit flip, so nothing spends an arithmetic chain on it: there is *no*
  constant-operand carry chain anywhere, and the modulus tier that reads one
  finds nothing.

`17` is recovered anyway, from what the correction computes — the candidate is
built one bit at a time and then re-checked on all 63 sums the 5-bit adder can
produce. Verdict `modular-arithmetic-candidate` (not `lattice-style-...`: 17 is
in no published-parameter library), family `lattice-ntt`, style `pqc-style`, and
the finding says the number and says it matches nothing published.

**`rotate16.vo`** — `back[i] = front[(i-5) mod 16]` through pure wiring. The
permutation pass reports a 16-bit rotation; the ARX pass reports `not-arx`
because there is no adder and no XOR layer. Family `none-detected`. The
cone-support tier finds the same map and *drops* it: reading it off the wires
needs less inference, so that reading stands alone.

**`spn_round16.vo`** — `state[P(i)] <= load ? plain[P(i)] : sub[i] ^ rkey[P(i)]`
with `P(i) = 4i mod 15`, `P(15) = 15`, and `sub` four PRESENT S-boxes over the
four nibbles of `state`. Not one destination bit peels back to a source through
wires alone, so the pure-wire tier reports **nothing at all** — and the
cone-support tier reports the whole map: 16 of 16 links, source `sub`,
destination `register bank state`, `permutation = [0, 4, 8, 12, 1, 5, 9, 13, 2,
6, 10, 14, 3, 7, 11, 15]` (the same map read in the other direction),
`link_form: gated` with `load` shared by every link and at most three other
operands per link. Family `spn`: four extracted substitutions and a permutation
layer between them. This is `12_present_sbox` at toy width; the map is 16 bits
wide, so it matches no published pLayer and the finding lists none.

**`mux_bank16.vo`** — two banks that a support test would happily mistake for
permutation layers, and neither may be reported. `y[i] <= sel ? a[(i-3) mod 16]
: b[(i-5) mod 16]` gives *two* complete candidate maps, one per source, which
is the signature of a multiplexer choosing between operands rather than of a
layer; the pass reports nothing and records the refusal with both sources
named. `z[i] <= sel ? a[i] : a[(i-1) mod 16]` is the other shape: every bit
reads two bits of the same source, so there is no link anywhere and no
candidate is ever formed. Family `none-detected`.

## Walkthrough exports used as end-to-end cases

The real Quartus exports outside this directory are part of the same
acceptance set, because they are what the tool has to work on:

| export | expected | why |
| --- | --- | --- |
| `examples/agilex3_walkthroughs/05_lfsr_prng/lfsr_prng.vo` | `lfsr-stream`, `x^16 + x^15 + x^13 + x^4 + 1`, period 65535 | matches the polynomial in that walkthrough's own `spec.md`, through eight inverted-storage stages |
| `examples/agilex3_walkthroughs/01_blinky_counter/blinky_counter.vo` | `none-detected` | a 24-bit counter is not cryptography, and the 23-bit carry chain in it must not talk anyone out of that |
| `examples/agilex3_walkthroughs/10_crc8_checker/netlist/crc8_checker.vo` | a data-absorbing Galois register, reciprocal polynomial `x^8 + x^2 + x + 1` | that walkthrough's stated 0x07 generator; the family stays `none-detected` because a CRC absorbs data rather than generating a keystream |
| `examples/agilex3_walkthroughs/11_speck_toy/speck_toy.vo` | `arx`, `classical-style`, four 16-bit rotations (7, 7, 2, 2) matching the published `speck_32` set | the case the synthesized `arx_round8` cannot make: a real export names **no** rotated vector and has **no** standalone XOR cell, so the round is only visible in the order a cell layer reads a register bank and under a held multiplexer select |
| `examples/agilex3_walkthroughs/13_trivium_stream/trivium_stream.vo` | `lfsr-stream`, `classical-style`, three coupled NLFSRs of 93, 84 and 111 stages under `start = 0` | the case the two synthesized fixtures above make one at a time, made together by a real export: a parallel key/IV load hides all 288 shift links, and every segment's feedback closes through a sibling |
| `examples/agilex3_walkthroughs/12_present_sbox/present_sbox.vo` | `spn`, `classical-style`, the PRESENT pLayer (`present_player`, 64 of 64 links) and the key register's rotation left by 61 (76 of 80 links), both from the cone support | what `spn_round16` makes at toy width, made by a real export: the round key sits between the substitution layer and the datapath register and a key load sits on every link of the key register, so the pure-wire tier reports nothing at all |
| `examples/agilex3_walkthroughs/12_present_sbox/variants/present_textbook.vo` | `none-detected`, and the key rotation **but not** the pLayer | the counterfactual: with the register holding the state *before* the key addition, the substitution layer is not a signal, so there is no source vector for a pLayer to be a map of. The key schedule does not depend on the substitution, and its rotation still comes out |
