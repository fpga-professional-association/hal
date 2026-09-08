# crc8_checker — what the export contains

Device **A3CW135BM16AE6S** (Agilex 3, package MBGA896), Quartus Prime Pro
**26.1.0 Build 110 03/26/2026**, post-synthesis snapshot. Exact commands:
`MANIFEST.json`.

This file is the answer key for the walkthrough. Read it *after* `../guide.html`,
not before.

## Primitive inventory

| primitive | count |
| --- | --- |
| `tennm_ff` | 8 |
| `tennm_lcell_comb` | 5 |

No carry chain: `shared_arith = "off"` and `extended_lut = "off"` on every ALM,
none of them drives `sumout`/`cout`, so all five are in normal mode. Every
flip-flop has `clk = clk`, `ena = en`, `clrn = rst_n`, `asdata = vcc` and
`aload = sload = sclr = sclr1 = gnd` — the configuration `hal_agilex` models.

## The five ALMs

| instance (export) | `lut_mask` (export) | `lut_mask` (imported) | what it is |
| --- | --- | --- | --- |
| `feedback` | `64'h6666666666666666` | `64'h6666666666666666` | `crc_r[7] ^ din` |
| `i8` | `64'h6969696969696969` | `64'h9696969696969696` | `crc_r[0] ^ crc_r[7] ^ din` |
| `i9` | `64'h6969696969696969` | `64'h9696969696969696` | `crc_r[1] ^ crc_r[7] ^ din` |
| `reduce_nor_0~0` | `64'h8000800080008000` | `64'h0001000100010001` | `NOR(crc_r[0..3])` |
| `reduce_nor_0` | `64'h0000800000008000` | `64'h0001000000010000` | `NOR(crc_r[4..7]) & datae` |

The mask changes between the two columns because the export writes inversions on
the data pins (`.dataa(~crc_r[0])`) and `tools/hal_agilex import` absorbs them
into the mask — bit `j` moves to `j ^ (1 << i)` for each inverted input `i`,
which is an exact relabelling. `feedback` has an even number of inverted inputs
on an XOR, so its mask is unchanged; `i8`/`i9` have three, so the mask is
complemented.

Decoding rule for normal mode:
`combout = lut_mask[a + 2b + 4c + 8d + 16e + 32f]`.

## The register bank

`crc_r[0]` and `crc_r[1..2]` take their `d` from `feedback`, `i8` and `i9`;
`crc_r[3..7]` take `d` straight from `crc_r[2..6]`. That is a shift register with
the feedback term folded into positions 0, 1 and 2 — the set bits of `8'h07`.

## Expected results of the walk

Run against `netlist.anon.hal.v` (names stripped) or `netlist.hal.v` (as
exported); both must give the same answer.

| step | expected |
| --- | --- |
| port roles | one clock, one active-low asynchronous reset, one clock enable, one data input |
| register banks | 1 (all eight flip-flops share `clk`/`clrn`/`ena`) |
| strongly connected components (size ≥ 2) | 1, covering the eight flip-flops and the three XOR ALMs |
| next-state functions | all eight affine over GF(2), constant term 0 |
| recovered width | 8 |
| recovered taps | positions 0, 1, 2 |
| recovered polynomial | `0x07` — `x^8 + x^2 + x + 1` |
| the second output | truth table over the state word is exactly `state == 0` |
| netlist CRC of `"123456789"` | `0xF4` (the published CRC-8/SMBUS check value) |
| remainder after `"123456789" ‖ 0xF4` | `0x00`, flag high |
| the same with one bit flipped | non-zero, flag low |

`../check.py` asserts every row of that table.

## Findings documents

| document | status |
| --- | --- |
| `../artifacts/inventory.findings.json` | `proven_under_assumptions` — all 13 instances are covered primitives in a validated configuration |
| `../artifacts/behavior.findings.json` | `proven_bounded` — the export matches `../reference.py` for 200 pseudo-random cycles including asynchronous clears |

These are properties of *this* exported file. If a rerun of Quartus packs the
logic differently, this file and the netlists have to be regenerated together.
