# hal_bitstream fixtures

## `blinky.v`

An 8-bit counter whose top bit drives an output. Deliberately sequential: after `synth_ice40` it
contains `SB_LUT4`, `SB_CARRY` and `SB_DFF` cells, so a conversion that drops the sequential
elements cannot pass the end-to-end test.

## The iCE40 bitstream

`ice40_blinky.bin` is a **real iCE40 bitstream**, produced from `blinky.v` with the open toolchain
only — no vendor tools, nothing proprietary, nothing device-specific beyond what yosys and nextpnr
put there:

```bash
yosys -p "synth_ice40 -top blinky -json blinky.json" blinky.v
nextpnr-ice40 --up5k --package sg48 --json blinky.json --asc blinky.asc \
    --pcf-allow-unconstrained --seed 1
icepack blinky.asc ice40_blinky.bin
```

`tests/headless_smoke/bitstream_smoke.py --rebuild-fixture` runs exactly those three commands, so
the recipe cannot drift from the file. On Debian/Ubuntu the three tools are
`yosys`, `nextpnr-ice40` and `fpga-icestorm`.

The bitstream is committed because the smoke test's subject is the *converter* chain
(`iceunpack` → `icebox_vlog` → `yosys` → `hal_py`), not place-and-route: pinning the bitstream keeps
the expected netlist stable and lets the test run wherever IceStorm and yosys are installed, without
nextpnr.

## Recorded converter output

`ice40_blinky.v` is the netlist `hal_bitstream convert` produced from that bitstream on a machine
with IceStorm 2023.02 and Yosys 0.33 installed, and `ice40_blinky.manifest.json` is the provenance
manifest of the same run (bitstream digest, resolved converter paths and versions, literal command
lines). `ice40_blinky.icebox_vlog.v` is the intermediate that IceStorm's `icebox_vlog` wrote — the
behavioural Verilog the yosys step maps onto iCE40 primitives; it is kept so the difference between
"what IceStorm recovers" and "what HAL can read" is visible without running anything.

They exist so that `test_hal_bitstream.py` can check, on any machine and with no FPGA toolchain
installed, the one claim that ties the two halves of this tool together: **every primitive the chain
emits is defined by the gate library the registry names for the family**
(`plugins/gate_libraries/definitions/ice40ultra.hgl`). If the chain starts emitting a cell the
library does not define, that test fails — which is what "produces a Verilog netlist plus the
correct gate library reference" means in practice.

Regenerate all three with:

```bash
python tools/hal_bitstream convert tools/hal_bitstream/fixtures/ice40_blinky.bin \
    -o tools/hal_bitstream/fixtures/ice40_blinky.v \
    --manifest tools/hal_bitstream/fixtures/ice40_blinky.manifest.json \
    --keep-intermediates
```

(the intermediate `ice40_blinky.1.v` that this leaves behind is the one committed as
`ice40_blinky.icebox_vlog.v`; the `.asc` is not committed because `iceunpack` reproduces it from
the bitstream in a second.)
