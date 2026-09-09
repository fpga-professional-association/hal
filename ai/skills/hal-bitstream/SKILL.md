---
name: hal-bitstream
description: Detect an FPGA bitstream's family from its magic bytes, convert it to a netlist with an open-source toolchain (IceStorm for iCE40, Trellis for ECP5), or load the result straight into hal_py. Use when the starting artifact is a raw bitstream file, not a netlist.
---

# hal_bitstream — from a device bitstream to a HAL netlist

## When to use
- You have a raw bitstream file (`.bit`/`.bin`) and need to know its family
  before anything else — the file extension alone is ambiguous (`.bit` is
  Xilinx *and* ECP5 *and* Nexus; `.bin` is iCE40 *and* Xilinx).
- You need that bitstream turned into a netlist HAL can load, and you want the
  provenance (converter versions, exact command lines, digests) recorded
  alongside it.
- You do **not** have a bitstream and are working from a Quartus `.vo` export
  instead — that's `hal_agilex`, not this tool (see Pitfalls).

## Quickstart
```bash
# what does this fork know, and what's actually installed right now
python tools/hal_bitstream families

# what is this file?
python tools/hal_bitstream detect blinky.bin

# bitstream -> Verilog netlist, + a provenance manifest next to it
python tools/hal_bitstream convert blinky.bin -o blinky.v

# bitstream -> netlist -> hal_py, in one step, saving a HAL project too
HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib PYTHONPATH=/work/build/lib \
    python tools/hal_bitstream load blinky.bin \
    --output-summary blinky.summary.json \
    --project-dir build/blinky_project
```

## The commands that matter

| command | does | needs HAL |
| --- | --- | --- |
| `families [--json] [--converter NAME=PATH]` | list known families + installed converters | no |
| `detect FILE [--family F] [--json]` | identify (or verify) the family from magic bytes | no |
| `convert FILE -o OUT [--family F] [--gate-library F] [--keep-intermediates] [--timeout S] [--manifest F]` | run the converter chain → Verilog netlist + `<netlist>.bitstream.json` | no |
| `load FILE -o OUT [--project-dir D] [--output-summary F] [--hal-lib D] ...` | `convert`, then hand the result to `hal_py` | **yes** |

`--converter NAME=PATH` (repeatable) overrides a converter lookup instead of
searching `$PATH`. Exit codes: `0` did what it was asked, `1` negative result
(netlist with no gates), `2` could not run (unknown family, missing
converter, converter failure, no built HAL for `load`) — exit 2 is never a
statement about the design itself.

| key | family | converter chain | gate library | status |
| --- | --- | --- | --- | --- |
| `ice40` | Lattice iCE40 | `iceunpack` → `icebox_vlog` → `yosys` | `ice40ultra.hgl` | verified end-to-end |
| `ecp5` | Lattice ECP5 | `ecpunpack` → `ecp_vlog` → `yosys` | none — pass `--gate-library` | unverified (chain implemented from docs, not run in this fork) |
| `nexus`/`gowin`/`xilinx7` | Oxide/Apicula/X-Ray | — | — | no converter; detection only |

## Pitfalls
- **This is not how the Agilex walkthroughs got their netlists.**
  `examples/agilex3_walkthroughs/*` netlists came from Quartus `.vo` exports
  (`hal_agilex`), not from a bitstream — Agilex has no key in this tool's
  registry at all. Don't reach for `hal_bitstream` on a `.vo` file, and don't
  expect an Agilex `Family` entry here.
- Only iCE40 and ECP5 have a converter chain at all; everything else
  (`nexus`, `gowin`, `xilinx7`) is **detection-only** — `convert`/`load` on
  those families is a documented error naming the missing open project, not a
  crash or a guess.
- The iCE40 chain ends in a **yosys re-mapping**, not a 1:1 bitstream
  readout: `icebox_vlog` writes behavioral Verilog (LUT equations, `always`
  blocks), which HAL's netlist parser cannot read, so `yosys -p "synth_ice40
  ..."` remaps it onto `SB_LUT4`/`SB_DFF*`/`SB_CARRY`. Cell count and instance
  names come from yosys's synthesis, not the bitstream's placement — use
  `--keep-intermediates` to keep `icebox_vlog`'s raw output next to the
  netlist if you need to compare the two.
- ECP5 ships **no** gate library in this fork — you must pass
  `--gate-library` yourself, or `convert`/`load` will not know what to read
  the result with.
- Every conversion writes `<netlist>.bitstream.json` (bitstream path/size/
  sha256, how the family was determined, resolved converter paths+versions,
  the literal command lines, the gate library). Treat a netlist without that
  manifest sitting next to it as unciteable — `hal_findings` documents that
  reference the netlist should point at the manifest.

## Where things live
- Tool: `tools/hal_bitstream/` — `registry.py` (all family-specific data:
  `Family`, `Magic`, `ConverterStep`; add families here or via
  `registry.register(...)` out of tree), `cli.py`.
- Tests: `python -m unittest discover -s tools/hal_bitstream -t tools -p "test_*.py"`
  (stub converters, no toolchain, no HAL). Real end-to-end:
  `HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib python3
  tests/headless_smoke/bitstream_smoke.py --work-dir <build>/bitstream_smoke --keep`
  (needs IceStorm + yosys + a built HAL).
- `tools/hal_bitstream/fixtures/README.md` documents how the checked-in test
  bitstream was built and how to rebuild it.
- Downstream once you have a netlist: `hal_viz`, `hal_agilex` (for Agilex-
  family imports only), `hal_capabilities`/`hal_findings` for what plugins can
  say about the result.
