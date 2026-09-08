# agilex3_counter_adder — expected results

Device **A3CW135BM16AE6S** (Agilex 3, package MBGA896), Quartus Prime Pro
**26.1.0 Build 110 03/26/2026**. Exact commands and file hashes: `MANIFEST.json`.

## The design

`counter_adder.v` is an 8-bit accumulator: `count <= count + addend` while `en`
is high, cleared asynchronously by the active-low `rst_n`. `carry_out` is bit 8
of `count + addend`, i.e. the carry out of the chain, and is combinational.

## What the export contains

| primitive | count |
| --- | --- |
| `tennm_lcell_comb` | 9 |
| `tennm_ff` | 8 |

The nine ALM cells are one carry chain in this order:

```
add_0~6 → add_0~11 → add_0~16 → add_0~21 → add_0~26 → add_0~31 → add_0~36 → add_0~41 → add_0~1
```

The first eight are the adder bit slices, all with
`lut_mask = 64'h00000000000F0FF0`, `extended_lut = "off"`, `shared_arith =
"off"`, `datac = ~count[i]`, `datad = ~addend[i]`. The last one
(`lut_mask = 0`) only taps the carry: `sumout = cin`, and it drives
`carry_out`.

Every `tennm_ff` has `clk = clk`, `ena = en`, `clrn = rst_n`, `asdata = vcc`
and `aload = sload = sclr = sclr1 = gnd`, which is exactly the configuration
`hal_agilex` models.

## Expected findings

Running `python tools/hal_agilex fixture tools/hal_agilex/fixtures/agilex3_counter_adder`:

| step | status | claim |
| --- | --- | --- |
| inventory | `proven_under_assumptions` | all 17 instances are covered primitives in a validated configuration |
| behaviour | `proven_bounded` (200 cycles) | the export matches `reference.py` for 200 pseudo-random cycles including two asynchronous clears |
| recognition | `heuristic` | the chain is an 8-bit ripple-carry accumulator/counter |
| recognition | `proven_under_assumptions` | the chain computes `A + B` for all 65 536 operand pairs |

`reference.py` is the ground-truth behaviour model. The numbers above are
properties of this exported file: if a rerun of Quartus produces a different
packing, the fixture and this file have to be regenerated together.

## Mask decoding, from the vendor's own equations

`quartus_eda` writes its equations into the `.vo` next to each instance. For
`add_0~6` it emits

```
sumout = ( !count[0] ^ (!addend[0]) ) ^ (!VCC )
cout   = ( !count[0] ^ (!addend[0]) ) & !VCC
       | !( !count[0] ^ (!addend[0]) ) & ( (count[0] & addend[0]) )
```

which is the propagate/generate form `hal_agilex.primitives` implements:
`f0 = lut_mask[a+2b+4c+8d]`, `f1 = lut_mask[16+a+2b+4c+8d]`,
`sumout = f0 ^ cin`, `cout = f0 ? cin : f1`.
