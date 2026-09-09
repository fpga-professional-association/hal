# hal_bitstream — from a device bitstream to a HAL netlist

HAL ingests netlists. Reverse engineering a device starts at a bitstream. The open
bitstream-documentation projects bridge that gap — IceStorm for iCE40, Trellis for ECP5, Oxide for
Nexus, Apicula for Gowin, X-Ray for Xilinx 7-series — but every one of them has its own tool names,
its own intermediate formats and its own idea of what "unpack" means, and none of them tells you
which HAL gate library the result has to be read with.

`hal_bitstream` is that missing layer, and nothing more:

* **detects** the family from the file's magic bytes (`.bit` is Xilinx *and* ECP5 *and* Nexus;
  `.bin` is iCE40 *and* Xilinx — the extension decides nothing),
* **runs** the family's converter chain when the toolchain is installed,
* **refuses** to guess when it is not, with an error naming the missing program, the project that
  ships it, the package to install and the flag that points at a copy elsewhere,
* **names** the gate library the produced netlist must be read with, and
* **hands off** to `hal_py` — loading the netlist in-process, and optionally writing a HAL project
  directory that the rest of the tool family (`hal_viz`, `hal_runner`, `hal_explain`, …) consumes.

It never invents a netlist. A family without an open converter is an error that says so.

## Commands

```bash
# what this fork knows, and which converters are installed right now
python tools/hal_bitstream families

# what is this file?
python tools/hal_bitstream detect blinky.bin

# bitstream -> Verilog netlist (+ a provenance manifest next to it)
python tools/hal_bitstream convert blinky.bin -o blinky.v

# bitstream -> netlist -> hal_py, printing a summary and saving a HAL project
python tools/hal_bitstream load blinky.bin \
    --output-summary blinky.summary.json \
    --project-dir build/blinky_project
```

Exit codes: `0` the command did what it was asked, `1` the result is negative (a netlist with no
gate), `2` the command could not run — unknown family, missing converter, converter failure, no
built HAL. Exit 2 is never a statement about the design.

## Families

| key | device family | toolchain | converter chain | gate library | status |
| --- | --- | --- | --- | --- | --- |
| `ice40` | Lattice iCE40 | Project IceStorm | `iceunpack` → `icebox_vlog` → `yosys` | `ice40ultra.hgl` | verified |
| `ecp5` | Lattice ECP5 | Project Trellis | `ecpunpack` → `ecp_vlog` → `yosys` | — (pass `--gate-library`) | unverified |
| `nexus` | Lattice Nexus | Project Oxide | — | — | no converter |
| `gowin` | Gowin LittleBee | Project Apicula | — | — | no converter |
| `xilinx7` | Xilinx 7-series | Project X-Ray | — | `XILINX_UNISIM.hgl` | no converter |

**verified** means exercised end to end in this fork: a bitstream built with the open toolchain,
converted, and loaded into `hal_py` (`tests/headless_smoke/bitstream_smoke.py`).
**unverified** means the chain is implemented from the converter's own documentation but has not
been run here, because the toolchain is not installed in this fork's containers — the command lines
live in `registry.py` and are one edit away if a real run disagrees.
**no converter** means the format is documented by an open project but nothing open writes a
netlist from it yet; detection still works, and the error says exactly that.

### Why yosys is in the chain

IceStorm's `icebox_vlog` does not write a *netlist*: it writes behavioural Verilog — one LUT
equation or `always @(posedge …)` block per configured cell — and HAL's Verilog parser is a netlist
parser, so it cannot read that. The last step of the chain,
`yosys -p "read_verilog …; synth_ice40 -top chip; write_verilog -noattr -noexpr …"`, maps that logic
back onto the `SB_LUT4`/`SB_DFF*`/`SB_CARRY` primitives `ice40ultra.hgl` defines.

That is a re-mapping, and the tool says so rather than pretending otherwise: the logic is the
bitstream's, but cell count and instance names come from yosys, not from the bitstream's placement.
Run with `--keep-intermediates` to keep `icebox_vlog`'s own output next to the netlist and compare
the two.

## Adding a family or fixing a converter

Everything family-specific is data in `registry.py`. In-tree, add a `Family` there. Out of tree
(or in a notebook, while you are working out a new converter's command line):

```python
from hal_bitstream import registry

registry.register(
    registry.Family(
        key="ice40_experimental",
        name="Lattice iCE40 (experimental chain)",
        vendor="Lattice",
        toolchain="local build",
        extensions=(".bin",),
        magic=(registry.Magic(b"\x7e\xaa\x99\x7e", "the iCE40 sync word"),),
        steps=(
            registry.ConverterStep(
                program="my_ice_vlog",
                arguments=("{input}", "-o", "{output}"),
                output_suffix=".v",
                purpose="write a Verilog netlist",
            ),
        ),
        gate_library="ice40ultra.hgl",
    )
)
```

`ConverterStep.input_suffixes` is what lets a chain accept both a packed bitstream and an already
unpacked ASCII file: the iCE40 chain skips `iceunpack` when it is handed a `.asc`.
`capture_stdout=True` covers converters that write the netlist to standard output (`icebox_vlog`
does).

## Provenance

Every conversion writes `<netlist>.bitstream.json` next to the netlist: the bitstream's path, size
and sha256, how the family was determined and on what evidence, the resolved path and version of
every converter, the literal command lines, and the gate library. A netlist recovered from a
bitstream without that record is an unciteable claim — and `hal_findings` documents that cite the
netlist can point at the manifest.

## What it does not do

* It does not reconstruct the designer's names. `icebox_vlog` names nets after their position in
  the fabric (`io_7_0_1`, `n17`) and yosys numbers the cells it creates; that *is* the information
  the bitstream contains.
* It does not claim a one-to-one correspondence with the bitstream's cells: the iCE40 chain ends in
  a yosys re-mapping (see above), so the netlist is the bitstream's logic, not its floorplan.
* It does not infer a gate library for families this fork ships none for (ECP5, Nexus, Gowin) —
  pass `--gate-library`.
* It does not analyse. Once the netlist is in HAL, that is `hal_explain`, `hal_cdc`, `hal_fsm`,
  `hal_runner` and the plugins.

## Tests

```bash
# stub converters, no toolchain, no HAL needed
python -m unittest discover -s tools/hal_bitstream -t tools -p "test_*.py"

# the real thing: IceStorm + yosys + a built HAL
HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib \
    python3 tests/headless_smoke/bitstream_smoke.py --work-dir <build>/bitstream_smoke --keep
```

`fixtures/README.md` documents how the checked-in bitstream was built and how to rebuild it.
