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
| `rotate16.vo` | two 16-bit banks joined by a rotation of 5 | positive for the permutation pass, negative for ARX | `none-detected` | `undetermined` |

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
identify a scheme.

**`rotate16.vo`** — `back[i] = front[(i-5) mod 16]` through pure wiring. The
permutation pass reports a 16-bit rotation; the ARX pass reports `not-arx`
because there is no adder and no XOR layer. Family `none-detected`.

## Walkthrough exports used as end-to-end cases

Two real Quartus exports outside this directory are part of the same
acceptance set, because they are what the tool has to work on:

| export | expected | why |
| --- | --- | --- |
| `examples/agilex3_walkthroughs/05_lfsr_prng/lfsr_prng.vo` | `lfsr-stream`, `x^16 + x^15 + x^13 + x^4 + 1`, period 65535 | matches the polynomial in that walkthrough's own `spec.md`, through eight inverted-storage stages |
| `examples/agilex3_walkthroughs/01_blinky_counter/blinky_counter.vo` | `none-detected` | a 24-bit counter is not cryptography, and the 23-bit carry chain in it must not talk anyone out of that |
| `examples/agilex3_walkthroughs/10_crc8_checker/netlist/crc8_checker.vo` | a data-absorbing Galois register, reciprocal polynomial `x^8 + x^2 + x + 1` | that walkthrough's stated 0x07 generator; the family stays `none-detected` because a CRC absorbs data rather than generating a keystream |
| `examples/agilex3_walkthroughs/11_speck_toy/speck_toy.vo` | `arx`, `classical-style`, four 16-bit rotations (7, 7, 2, 2) matching the published `speck_32` set | the case the synthesized `arx_round8` cannot make: a real export names **no** rotated vector and has **no** standalone XOR cell, so the round is only visible in the order a cell layer reads a register bank and under a held multiplexer select |
| `examples/agilex3_walkthroughs/13_trivium_stream/trivium_stream.vo` | `lfsr-stream`, `classical-style`, three coupled NLFSRs of 93, 84 and 111 stages under `start = 0` | the case the two synthesized fixtures above make one at a time, made together by a real export: a parallel key/IV load hides all 288 shift links, and every segment's feedback closes through a sibling |
