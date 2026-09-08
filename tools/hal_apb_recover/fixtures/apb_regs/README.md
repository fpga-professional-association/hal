# `apb_regs` — an APB peripheral with its internal names stripped

A 224-gate APB3 peripheral, generated from `generate.py` against
`plugins/gate_libraries/definitions/NangateOpenCellLibrary.hgl`. Nothing in the
netlist says what anything is: every top-level port is `io_NN`, every internal
net `nNNN`, every instance `gNNN`. `mapping.json` is the analyst's input — the
one place the bus is named — and `ground_truth.json` is what the recovery has to
reproduce from the netlist and that mapping alone.

Regenerate all three files after editing the description in `generate.py`:

```bash
python tools/hal_apb_recover/fixtures/apb_regs/generate.py
```

`test_apb_recover.py::FixtureTest` fails if the committed files and the
generator have drifted apart.

## What the peripheral is

APB3, 5 address bits (`PADDR[4:2]` decoded, `PADDR[1:0]` a byte offset), 16 data
bits, two byte-strobe lanes of 8 bits.

| address | register | access | reset / value | why it is in the fixture |
| --- | --- | --- | --- | --- |
| `0x00` | CTRL | read-write | `0x0000` | the ordinary case; bits 0–14 are provable with everything else unconstrained |
| `0x04` | STATUS | write-one-to-clear (bits 0–7) | `0x0000` | the modelled side effect; bits 8–15 have no storage and read 0 |
| `0x08` | CTRL again | read-write | `0x0000` | **the intentional alias**: slots 0 and 2 share one decode line |
| `0x0C` | ID | read-only | `0xa53c` | a constant with no storage at all |
| `0x10` | SCRATCH | read-write **while CTRL[0] is 1** | `0xbeef` | a guarded (locked) write, and a non-zero reset value built from `DFFS`/`DFFR` |
| `0x14`, `0x18`, `0x1C` | — | — | — | decoded by nothing: writes change nothing, reads return 0 |

Three deliberate complications make the confidence tiers observable:

* **CTRL[15]** is also cleared by `io_53`, a non-bus input. With that input left
  unconstrained its next state is `X`, so the bit drops from *proven under
  assumptions* to a *bounded check* — the tier boundary made visible.
* **STATUS** bits are set by the event inputs `io_45..io_52`, so the same thing
  happens to the whole register: write-one-to-clear is only definite once the
  event inputs are pinned.
* **SCRATCH** writes are gated by CTRL[0]. The two pinned environments disagree
  (hold versus write), so the recovery must report a *guarded write* naming
  CTRL[0] rather than calling the register read-only or read-write.

`PREADY` is driven by a `DLH_X1` transparent latch. The recovery models only
edge-triggered state, so it has to report that latch as an unsupported
primitive instead of assuming it is a flip-flop.

One gate exists purely so the netlist survives a round trip through HAL: an
`XOR2_X1` fed by `PADDR[1:0]` whose output goes nowhere. HAL's Verilog parser
deletes any net with neither a source nor a destination, and without that gate
the two undecoded address bits would not exist as nets for `mapping.json` to
name. It drives nothing and changes no register.

## Expected result

| property | value |
| --- | --- |
| registers | 5 (one of them an alias) |
| flip-flops | 40, all attributed |
| alias classes | 1 (`0x00` ≡ `0x08`) |
| unmapped addresses | `0x14`, `0x18`, `0x1C` |
| fields proven under assumptions | 54 |
| fields bounded-checked | 10 (CTRL[15] twice — it is readable at both addresses — plus STATUS[0..7]) |
| fields inferred | 16 (all of SCRATCH, guarded by CTRL[0]) |
| unresolved fields | 0 |
| unsupported gate types | `DLH_X1` × 1 |
| PADDR bits with no effect at `0x00` | 0, 1 (byte offset) and 3 (the alias) |

## Port map (only `mapping.json` knows this)

| ports | role |
| --- | --- |
| `io_00`, `io_01` | PCLK, PRESETn (active low, asynchronous) |
| `io_02`–`io_04` | PSEL, PENABLE, PWRITE |
| `io_05` | PREADY (out, through the latch) |
| `io_06`–`io_10` | PADDR[0..4] |
| `io_11`–`io_26` | PWDATA[0..15] |
| `io_27`–`io_42` | PRDATA[0..15] (out) |
| `io_43`, `io_44` | PSTRB[0..1] |
| `io_45`–`io_52` | event inputs that set STATUS (not part of the bus) |
| `io_53` | hardware clear for CTRL[15] (not part of the bus) |

## Running it

Offline, no HAL build:

```bash
python tools/hal_apb_recover recover \
    tools/hal_apb_recover/fixtures/apb_regs/apb_regs.v \
    tools/hal_apb_recover/fixtures/apb_regs/mapping.json \
    --gate-library plugins/gate_libraries/definitions/NangateOpenCellLibrary.hgl \
    -o /tmp/apb_regs
```

Through `hal_py`, against a build:

```bash
python tools/hal_apb_recover recover \
    tools/hal_apb_recover/fixtures/apb_regs/apb_regs.v \
    tools/hal_apb_recover/fixtures/apb_regs/mapping.json \
    --source hal --hal-lib "$HAL_BUILD/lib" \
    --gate-library plugins/gate_libraries/definitions/NangateOpenCellLibrary.hgl \
    -o /tmp/apb_regs_hal
```

Both must produce the same register map; `test_apb_recover_hal.py` asserts it.
