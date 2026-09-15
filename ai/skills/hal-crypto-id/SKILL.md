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
| `permutation export.vo [-o F]` | pure-wire bit maps; matches PRESENT/GIFT pLayers and rotation sets | no |
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
  else on that ring does.
- `classical-style` does **not** rule out post-quantum: a hash-based or
  code-based scheme has neither NTTs nor S-boxes and lands in `none-detected`.
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
- **A data-absorbing feedback register is not a keystream generator.** A CRC or
  scrambler XORs an external bit in every step; it gets `autonomous: false` and
  does *not* make the family `lfsr-stream`. The polynomial is still reported.
- **Bit-permutation equivalence is not searched above 6 bits.** The cost is
  `n! * 2^n`. At 7 and 8 bits the finding's `bit_permutation_search` field says
  the search was *not performed* — a negative there means "exact and
  xor_constant did not match", not "no equivalence exists". An AES S-box with
  scrambled bit order will not be found.
- **Cones wider than 16 sources are refused, not approximated.** S-box
  extraction and feedback classification enumerate a cone's full input space;
  anything wider is skipped and shows up as a rejection reason, never as a
  guess. Adder verification falls back to 256 seeded operand vectors and the
  finding then says `heuristic` with the vector count.
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
  `sbox.py`, `shiftreg.py`, `arx.py`, `permutation.py`, `ntt.py`,
  `classify.py` (aggregation rules and every findings builder),
  `hal_adapter.py` (only module needing `hal_py`).
- Fixtures: `tools/hal_crypto/fixtures/` — **synthesized shapes, not vendor
  exports**, generated by `synth.py`; `GROUND_TRUTH.md` states the exact
  expected result for each and `MANIFEST.json` pins their hashes.
- Tests: `python -m unittest discover -s tools/hal_crypto -t tools -p "test_*.py"`
  (no HAL, 61 tests, ~20 s), registered with ctest as
  `runTest-hal_crypto_standalone`.
- Reader and primitive semantics come from `tools/hal_agilex` and are not
  duplicated here.
