---
name: hal-crypto-id
description: Decide whether an Agilex 3 .vo netlist computes cryptography, which structural family it is (spn / arx / lfsr-stream / sponge / lattice-ntt / none-detected), and whether that family is classical or PQC-style. Use for S-box extraction and matching, LFSR/NLFSR feedback polynomials, ARX round detection, permutation layers, and NTT butterflies with a modulus.
---

# hal_crypto — structural crypto identification

## When to use
- You have an anonymized Quartus `.vo` export and the question is *is there
  cryptography in here*, not *what does this counter count to*.
- You need a feedback polynomial, an S-box table, a rotation amount or a
  modulus **read out of the netlist** rather than guessed from names.
- You are writing or checking a reverse-engineering walkthrough that has to
  say "this is an LFSR-based generator" or "this is lattice-style ring
  arithmetic" without over-claiming.
- Everything except `hal_adapter.py` runs on a plain Python 3 stdlib
  interpreter — **no HAL build needed** for any command below.
- Reach for [hal-agilex](../hal-agilex/SKILL.md) first if the question is
  primitive coverage or netlist-versus-RTL behaviour; this tool assumes that
  layer and analyses on top of it.

## Quickstart
```bash
# the aggregate verdict: family + classical/PQC, one findings document
python3 tools/hal_crypto identify \
    examples/agilex3_walkthroughs/05_lfsr_prng/lfsr_prng.vo
#   -> family lfsr-stream, x^16 + x^15 + x^13 + x^4 + 1, period 65535

python3 tools/hal_crypto identify \
    examples/agilex3_walkthroughs/13_trivium_stream/trivium_stream.vo
#   -> family lfsr-stream: three *coupled* NLFSRs, 93 + 84 + 111 stages, every
#      feedback of degree 2 -- and none of it visible without holding start = 0

python3 tools/hal_crypto identify \
    examples/agilex3_walkthroughs/14_keccak_toy/keccak_retimed.vo
#   -> family sponge, style *undetermined*: 40 chi rows matching keccak_chi_5.
#      The same permutation in 14_keccak_toy/keccak_toy.vo -- one retiming away
#      -- is none-detected, because there chi reads the registers through theta

python3 tools/hal_crypto identify \
    examples/agilex3_walkthroughs/15_ntt_mult/ntt_mult.vo
#   -> family lattice-ntt, style pqc-style, confidence *medium*: one butterfly
#      and "modulus 257, in no published-parameter library".  257 = 2^8 + 1 is
#      too cheap to need a carry chain, so it came off the correction logic

python3 tools/hal_crypto identify \
    examples/agilex3_walkthroughs/01_blinky_counter/blinky_counter.vo
#   -> family none-detected, with a per-pass list of what was looked for

# one pass at a time while debugging a recognizer
python3 tools/hal_crypto sbox tools/hal_crypto/fixtures/present_sbox_layer.vo
python3 tools/hal_crypto ntt  tools/hal_crypto/fixtures/ntt_stage13.vo \
    -o /tmp/ntt.findings.json
```

## The commands that matter

| command | does | needs HAL |
| --- | --- | --- |
| `identify export.vo [-o F] [--strict]` | run all six passes → family verdict + classical/PQC verdict + per-pass findings | no |
| `sbox export.vo [-o F]` | cluster LUT cones into *n*→*n* bijections and match the library (tier reported) | no |
| `lfsr export.vo [-o F]` | shift chains: LFSR with polynomial, NLFSR with ANF, or open chain | no |
| `arx export.vo [-o F]` | adders + fixed rotations + XOR layer, only when wired together | no |
| `permutation export.vo [-o F]` | pure-wire bit maps, **plus** maps read off the next-state cone support when one cell sits on every link; matches PRESENT/GIFT pLayers and rotation sets | no |
| `ntt export.vo [-o F]` | add/subtract butterflies and the modulus behind a constant-operand chain | no |
| `fixtures [--check\|--write]` | verify/regenerate the synthesized fixture netlists | no |
| `elaborate netlist.hal.v --gate-library FILE --hal-lib DIR` | load through `hal_py`, then run the passes over the sibling `.vo` | **yes** |

Exit codes: `0` success, `1` failure, `2` with `--strict` on an `error` or
`unsupported` finding. `unknown` is **not** blocking — `none-detected` is a
result, not a failure.

## Reading the verdict

`family` is one of `lattice-ntt`, `sponge`, `spn`, `arx`, `lfsr-stream`,
`none-detected`, reported most-specific-first with everything that fired listed
under `families_present`. `style` is `pqc-style`, `classical-style` or
`undetermined`. Both carry a `confidence_tier` (`low`/`medium`/`high`) and an
`evidence` list of structural observations.

What the words are allowed to mean:

- `pqc-style` = **lattice-style NTT/ring arithmetic is present**. It never
  names a scheme. Reducing modulo 3329 is what ML-KEM does *and* what anything
  else on that ring does. When the recovered modulus is in **no** published
  library the confidence drops to `medium` and the style text adds that this is
  "the shape of a lattice scheme's arithmetic and not any deployed one's
  parameters" — which is the honest reading of `15_ntt_mult`'s `q = 257`, a real
  modulus at toy parameters. Read `recovered_moduli` for the number and
  `named_moduli` for whether anyone has published it.
- `classical-style` does **not** rule out post-quantum: a hash-based or
  code-based scheme has neither NTTs nor S-boxes and lands in `none-detected`.
- **`sponge` is never placed on the axis at all** — it gets `undetermined`, and
  the finding drops to `unknown` status with no confidence number. A Keccak
  permutation is SHA-3 and is equally the SHAKE inside ML-KEM, ML-DSA and the
  whole of SPHINCS+, so the structure carries no information about the family of
  scheme; the arithmetic *around* the sponge decides that. Note the family
  verdict is still `high` confidence — "this is a sponge" is a strong claim and
  "therefore it is classical" is not a claim.
- An S-box match is a fact about the extracted table, not an identification of
  the cipher. The **tier** (`exact` / `xor_constant` / `bit_permutation`) is in
  the title and the data, and a permuted match is never reported as exact.

## Pitfalls
- **Storage polarity flips half the shift links.** Quartus loads a non-zero
  seed by storing some stages inverted — eight of sixteen in `05_lfsr_prng` —
  so `d = !q_prev` all over the chain. The pass normalises this into a gauge
  and reports `state_encoding: as-stored | complemented`. If you read taps out
  of a netlist by hand and get a constant term you did not expect, this is why.
- **Polynomial numbering is a convention, and the two in use are reciprocals.**
  `hal_crypto` writes a tap at stage *t* as `x^(t+1)` with stage 0 the chain
  head, which reproduces `05_lfsr_prng`'s stated `x^16 + x^15 + x^13 + x^4 + 1`.
  A CRC specification numbers from the other end: `10_crc8_checker` reports
  `x^8 + x^7 + x^6 + 1` and `polynomial_reciprocal: x^8 + x^2 + x + 1`, and the
  latter is its documented 0x07 generator. **Always check both fields** before
  saying a polynomial does not match.
- **A vendor export names neither the R nor the X of an ARX round.** Rotating
  a word is free wiring, so Quartus keeps no net for it, and `x <= load ? pt :
  (sum ^ k)` fits one ALM, so the XOR cells are multiplexers. On
  `11_speck_toy` — a real Speck32/64 export — *zero* rotated vectors and *zero*
  standalone XOR cells exist, and the round is still found: rotations are read
  off the order a cell layer reads a register bank (each carries `read_from`:
  `named vector` / `carry-chain operand order` / `register-bank next-state
  cells`) and XORs are recovered by holding **one** input at a constant
  (`conditional: true` plus the `cofactor`). `standalone_xor_cells` and
  `conditional_xor_cells` are reported separately; the first is the stronger
  evidence. Do not read `xor_cells: 54` as 54 XOR gates.
- **A *tapped* shift chain is not found at all.** `shiftreg`'s walk follows
  successors and stops at the first stage with more than one, so a shift register
  whose stages each also drive a capture register — the receive path of
  `16_mystery_cores`' `core_b`, and any Galois-style layout — comes back as no
  structure whatsoever, not as a short one. Walking the *predecessor* instead
  (each stage has exactly one register feeding it, and fan-out cannot change
  that) finds it; that is what that walkthrough's `analysis.py` does.
- **A parallel load makes every shift link invisible, and the pass holds a net
  to get them back.** `s[i] <= load ? init[i] : s[i-1]` is a multiplexer, so on
  `13_trivium_stream` -- a real Trivium export -- the plain reading found *zero*
  chains in a design that is three shift registers. `shiftreg` ranks the
  external nets by how many next-state functions read them nonlinearly, holds
  the top one or two at each value, and reports what that bought under
  `mode: {"net": "start", "value": 0}`. **Read the `mode` field before quoting a
  chain**: "these registers form a shift chain" and "... while `start` is low"
  are different claims. A held net can only add *feedback* registers; an open
  chain that only exists under a held select is dropped on purpose, because that
  is what every loadable register bank looks like.
- **A stream cipher's registers usually do not close onto themselves.** Trivium
  and Grain are *coupled*: segment A's feedback reads segment C, C's reads B,
  B's reads A. Such a chain is reported `coupled: true` with `coupled_chains`
  and the foreign taps named, and its ANF variables are written `s0[65]`,
  `s1[110]` -- qualified by chain, because `s[65]` is ambiguous once there is
  more than one register. **A coupled register has `polynomial: None`**, even
  when its feedback is linear: a polynomial is a recurrence over one register's
  own history. Do not read a missing polynomial as "the pass failed".
- **A pLayer sits behind the round key, so the wiring never shows it.** On
  `12_present_sbox` — a real PRESENT-80 export — the datapath register's `d`
  pins are driven by the key-XOR cells and every key-register link goes through
  the load multiplexer, so the pure-wire tier reports **nothing** for two bit
  maps that are both entirely in the netlist. `permutation.cone_support_maps`
  reads them off the *cone support* instead — which bit of a declared vector
  each next-state function depends on — and returns the pLayer
  (`present_player`, 64/64 links) and the key rotation (left by 61, 76/80
  links). Those findings are `hal_crypto/permutation/cone-support`, carry
  `read_from: "next-state cone support"` and an explicit
  `one-side-input-per-link` assumption, and are **weaker than the wiring
  tier**: the claim is which bit each register reads, modulo whatever else the
  cone reads, so it holds in the operating mode where the source reaches the
  register. Quote the tier with the map. Nothing is reported when two sources
  explain the same bank (that is a multiplexer), when a stronger reading
  already has the map, or when a partial map's amount is ±1 (that is an open
  shift chain — `lfsr` names it, with its feedback).
- **A vendor subtracter does not look like a hand-written one, and for a while
  none was recognised.** Quartus folds the second operand's inversion into the
  arithmetic cell's *own mask* (`XNOR(a,b)` and `a AND NOT b`, so the two
  operands carry **independent** polarities) instead of spending an inverter
  cell, and it cannot tie `cin` to a constant at all — an ALM's carry comes only
  from the previous cell's `cout` — so it prefixes the chain with a **carry-seed
  cell**: no data operands, no sum output, one constant `cout`. Every Quartus
  subtract chain has both. Before `15_ntt_mult` the pass found *zero*
  butterflies in any real export; `arith.carry_seed` and the per-operand
  polarity search are what fixed it, and `fixtures/ntt_fermat17.vo` is the
  shape on its own.
- **A cheap modulus is not a carry chain, and the constant-operand tier finds
  nothing.** `sum - q` only gets an arithmetic chain when *q* is expensive to
  add. At `q = 2**k + 1` (a Fermat prime: 257, 17, 65537) the correction is an
  increment and one bit flip and Quartus builds it out of ordinary LUTs, so
  `modulus_candidates` is empty on a design whose modulus is perfectly real.
  The second tier, `ntt.reduction_moduli`, derives *q* from what the correction
  **computes**: it takes each net that could be the select, builds *q* one bit
  at a time (bit *k* of `sum - q` depends only on bits 0..*k* of *q*), and keeps
  a candidate only when the corrected vector is re-located on a sweep of **every
  attainable chain result**. It reports `select_net`, `sum_nets`,
  `difference_nets` and `checked_every_sum`, and it reproduces `3329` on
  `ntt_stage13` where the first tier already had it. A missing
  `reduction_moduli` key means the tier did not fire, not that it failed.
- **`stage_depth` counts butterflies chained through *wiring*.** An iterative
  core chains them through a *counter*, so `15_ntt_mult`'s four stages of eight
  come back as `stage_depth: 1` and that is the honest structural answer. Same
  distinction as `13_trivium_stream`'s three levels of logic versus its
  1152-step warm-up.
- **A data-absorbing feedback register is not a keystream generator.** A CRC or
  scrambler XORs an external bit in every step; it gets `autonomous: false` and
  does *not* make the family `lfsr-stream`. The polynomial is still reported.
- **An S-box is found through the support of *one* cone, or through a grown
  cluster, and the two find different things.** PRESENT, AES and the DES rows
  all have output bits that read *every* input bit, so one cone's support names
  the whole box. Keccak and Ascon `chi` do not — `y_i = x_i ^ (~x_{i+1} &
  x_{i+2})` reads three of five — so those are found only by
  `sbox.cluster_supports`, which grows a candidate from one cone by repeatedly
  taking the neighbour that adds the **fewest new sources**. That qualifier is
  load-bearing: following any neighbour merges the substitution layer with the
  load multiplexers on top of it (on a real Keccak export, forty five-source
  clusters become one cluster of everything).
- **A substitution behind another combinational layer is invisible, and that is
  a statement about the netlist.** Cones are cut at the flip-flops, so a design
  whose S-box does not sit *on* a register bank loses it: in
  `14_keccak_toy/keccak_toy.vo` chi reads the registers through theta and every
  chi cone is 33 flip-flops wide, over the 12 the extraction enumerates, so
  `identify` returns `none-detected` on a real Keccak permutation. The same
  permutation retimed by half a round (`keccak_retimed.vo`, identical
  behaviour) returns `sponge` with all forty matches. Ask whether the *pipeline*
  could have dissolved the layer, not only the coding style.
- **Bit-permutation equivalence is not searched above 6 bits.** The cost is
  `n! * 2^n`. At 7 and 8 bits the finding's `bit_permutation_search` field says
  the search was *not performed* — a negative there means "exact and
  xor_constant did not match", not "no equivalence exists". An AES S-box with
  scrambled bit order will not be found.
- **Wide cones are refused, not approximated.** Feedback classification
  enumerates a cone's full input space up to 16 sources; S-box extraction stops
  at 12 (an S-box output bit reads at most eight). Anything wider is *counted
  and reported* as a rejection reason and drops the `none-detected` confidence
  tier to `medium` — it is never turned into a clean negative. Adder
  verification falls back to 256 seeded operand vectors and the finding then
  says `heuristic` with the vector count.
- **Blinding a netlist costs the ARX verdict, and nothing else measured so far.**
  `16_mystery_cores` runs `identify` on the *named* and the *anonymized* copy of
  five real exports. Four verdicts are identical; Speck32/64 goes from `arx`
  (high) to `none-detected` (medium). The adders and all 54 XOR cells are still
  found — what is lost is the **rotations**, 4 to 0, because
  `arx.adder_operand_rotations` groups an operand's source nets by the text
  before the `[` in their names to decide which *word* they belong to, and
  blinding splits every internal vector into scalars. A netlist recovered from a
  bitstream has no vector declarations at all, so expect this on real targets and
  **run the pass on the least-blinded copy you have before believing a negative**.
  `sbox`, `shiftreg`, `ntt` and the permutation tiers were unaffected on that set.
- **`--strict` will not catch `none-detected`.** It is deliberately an
  `unknown` finding. Gate on `data.family` if you want a script to react to it.
- Findings record the input's **sha256**, so the CRLF pitfall from
  [hal-agilex](../hal-agilex/SKILL.md) applies here too: run against LF files
  and write regenerated documents to a scratch path.

## Where things live
- Tool: `tools/hal_crypto/` — `boolfunc.py` (truth tables, ANF, match tiers),
  `known.py` (published S-boxes/pLayers/rotations/moduli, derived where
  possible), `netlist_model.py` (cones, exact + sampled evaluation),
  `arith.py` (carry chains verified as add/subtract/add-constant),
  `sbox.py` (cone grouping by single-net support *and* by grown cluster),
  `shiftreg.py` (chains, operating-mode cofactor, coupling),
  `arx.py`, `permutation.py`, `ntt.py`,
  `classify.py` (aggregation rules and every findings builder),
  `hal_adapter.py` (only module needing `hal_py`).
- Fixtures: `tools/hal_crypto/fixtures/` — **synthesized shapes, not vendor
  exports**, generated by `synth.py`; `GROUND_TRUTH.md` states the exact
  expected result for each and `MANIFEST.json` pins their hashes.
- Tests: `python -m unittest discover -s tools/hal_crypto -t tools -p "test_*.py"`
  (no HAL, 115 tests, ~16 s), registered with ctest as
  `runTest-hal_crypto_standalone`. `elaborate` additionally has a case in
  `tests/headless_smoke/tool_cli_plugin_load_smoke.py`, which needs a build.
- Reader and primitive semantics come from `tools/hal_agilex` and are not
  duplicated here.
