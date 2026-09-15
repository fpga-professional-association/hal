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
| `sbox.py` | which LUT cones form an *n*-bit bijection, and does it equal a published S-box? | `fixtures/present_sbox_layer.vo` | `fixtures/unknown_sbox_layer.vo`, `fixtures/counter8.vo` |
| `shiftreg.py` | is a register chain closed by feedback; is the feedback linear (polynomial) or not (ANF); Fibonacci or Galois? | `fixtures/lfsr16_fibonacci.vo`, `fixtures/lfsr16_galois.vo` | `fixtures/nlfsr16.vo`, `fixtures/shift16_plain.vo` |
| `arx.py` | are adders, fixed rotations and an XOR layer present **and wired together**? | `fixtures/arx_round8.vo` | `fixtures/counter8.vo`, `fixtures/rotate16.vo` |
| `permutation.py` | which pure-wire bit maps exist, and do they equal a published pLayer or rotation set? | `fixtures/rotate16.vo` | `fixtures/counter8.vo` |
| `ntt.py` | is there an add/subtract butterfly over the same operands, and what modulus does the constant-operand chain reduce by? | `fixtures/ntt_stage13.vo` | `fixtures/butterfly4.vo` (butterfly, no modulus) |
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

76 tests, no HAL, ~5 s. Registered with ctest as
`runTest-hal_crypto_standalone` in `tests/headless_smoke/CMakeLists.txt`. The
end-to-end cases are the acceptance criteria of the issues this package came
from: `05_lfsr_prng` must classify `lfsr-stream` with the polynomial its own
specification states, `01_blinky_counter` must classify `none-detected`, and
`11_speck_toy` must classify `arx` with the published SPECK-32/64 rotation set
quoted and no claim that the design *is* SPECK.

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
