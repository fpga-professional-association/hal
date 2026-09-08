# agilex3_lut_logic — expected results

Device **A3CW135BM16AE6S** (Agilex 3), Quartus Prime Pro **26.1.0 Build 110
03/26/2026**. Exact commands and file hashes: `MANIFEST.json`.

## The design

Three purely combinational functions of the same six inputs `a..f`
(`lut_logic.v`). Nothing is registered, so the export is normal-mode ALM LUTs
only — this fixture exists to pin the `lut_mask` decoding of `combout`
independently of the arithmetic path.

## What the export contains

| primitive | count |
| --- | --- |
| `tennm_lcell_comb` | 3 |

| instance | `lut_mask` | drives |
| --- | --- | --- |
| `y0~1` | `64'h11F1FFFFFFFF11F1` | `y0` |
| `y1~1` | `64'h6969006969690069` | `y1` |
| `y2~1` | `64'h040404FFAEAEAEAE` | `y2` |

All three connect their data inputs inverted (`.dataa(~a)` and so on) and have
`extended_lut = "off"`, `shared_arith = "off"`, `datag = datah = cin = sharein
= gnd`, and no `sumout`/`cout`.

## Expected findings

`python tools/hal_agilex fixture tools/hal_agilex/fixtures/agilex3_lut_logic`:

| step | status | claim |
| --- | --- | --- |
| inventory | `proven_under_assumptions` | all 3 instances are covered primitives in a validated configuration |
| behaviour | `proven_under_assumptions` | all 64 input assignments match `reference.py` |
| recognition | `unknown` | no carry chain, so no adder was recognized — and nothing is claimed about what the design does |

The behaviour check is exhaustive: six inputs, 64 vectors, every one compared
against the reference model of the RTL.

## Import rewrite

`counter_adder` shows the arithmetic path; this fixture is where the inversion
absorption is most visible. `y0~1` has all six inputs inverted, so
`64'h11F1FFFFFFFF11F1` becomes `64'h8F88FFFFFFFF8F88` in
`lut_logic.hal.v` — the same function of the non-inverted signals. The test
suite re-simulates both forms and requires identical outputs on all 64 vectors.
