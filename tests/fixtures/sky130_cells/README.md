# `sky130_cells` — an original sky130 fixture with a documented expected structure

`sky130_cells.v` is a hand-written gate-level netlist over the shipped
`plugins/gate_libraries/definitions/SKY130_FD_SC_HD.hgl`. Nothing in here is
derived from any third-party design: the fixture was written from the library's
own cell definitions so that the gate library can be guarded in CI without
shipping someone else's netlist.

It is consumed by `tests/netlist/gate_library_sky130.cpp`
(`runTest-gate_library_sky130`), which asserts everything tabulated below. The
build copies the file to `<build>/bin/test-files/sky130_cells/sky130_cells.v`.

## Why these cells

The fixture is deliberately not a useful circuit. It is the smallest netlist
that still contains every cell *shape* that an extracted sky130 netlist leans
on, so that a regression in the generated library — a dropped `ff_config`, a
flipped polarity, a lost constant — turns a test red instead of turning up much
later as a wrong analysis.

| shape | cells used | what it guards |
| --- | --- | --- |
| async reset, active low | `dfrtp_2` ×2 | `clear_on = (! RESET_B)` and the `reset` pin type survive the HGL round trip |
| async set, active low | `dfstp_2` ×1 | `preset_on = (! SET_B)` and the `set` pin type |
| plain flop | `dfxtp_2` ×1 | a sequential cell with *neither* — an FF component that must not report a reset or set function |
| constants | `conb_1` ×1 | `HI` is `power`/`1`, `LO` is `ground`/`0`, i.e. HAL can recognise tie cells |
| inverted input pin | `nand2b_1` ×2 | the `A_N` naming convention, `Y = !(!A_N & B)` |
| and/or/invert | `a21oi_1`, `o21ai_1` | the two compound-gate polarities, `c_aoi` / `c_oai` properties |
| xor/xnor | `xor2_1`, `xnor2_1` | both output-pin names (`X` for xor2, `Y` for xnor2) |
| buffer / inverter | `buf_1` ×2, `inv_1` ×1, `and2_1` ×1 | the plain combinational majority of any sky130 netlist |

`conb_1` drives both of its outputs into real consumers, so neither constant is
dangling: `HI` feeds both inputs of `u_tie_nand` (`n_en = !(!1 & 1) = 1`) and
`LO` feeds `u_xnor` (`dout[5] = !(dout[3] ^ 0) = !dout[3]`).

## Expected structure

**15 gates**, by type:

| gate type | count | instances |
| --- | --- | --- |
| `sky130_fd_sc_hd__conb_1` | 1 | `tie_cell` |
| `sky130_fd_sc_hd__nand2b_1` | 2 | `u_din_nand`, `u_tie_nand` |
| `sky130_fd_sc_hd__inv_1` | 1 | `u_inv` |
| `sky130_fd_sc_hd__and2_1` | 1 | `u_and` |
| `sky130_fd_sc_hd__dfrtp_2` | 2 | `state_reg[0]`, `state_reg[1]` |
| `sky130_fd_sc_hd__dfstp_2` | 1 | `state_reg[2]` |
| `sky130_fd_sc_hd__dfxtp_2` | 1 | `state_reg[3]` |
| `sky130_fd_sc_hd__xor2_1` | 1 | `u_xor` |
| `sky130_fd_sc_hd__xnor2_1` | 1 | `u_xnor` |
| `sky130_fd_sc_hd__a21oi_1` | 1 | `u_aoi` |
| `sky130_fd_sc_hd__o21ai_1` | 1 | `u_oai` |
| `sky130_fd_sc_hd__buf_1` | 2 | `buf_aoi_x`, `buf_oai_x` |

All instance names are written as escaped Verilog identifiers (`\u_and `,
`\state_reg[0] `); HAL strips the leading backslash, so the gate names above are
what the netlist reports. The bus-indexed ones (`state_reg[0]` …) keep their
brackets.

**20 nets**: 4 global inputs (`clk`, `rst_n`, `set_n`, `din`), 8 global outputs
(`dout(0)` … `dout(7)` — HAL renames bus bits from `[i]` to `(i)`), and 8
internal wires (`tie_hi`, `tie_lo`, `n_nandb`, `n_inv`, `n_en`, `n_and`,
`n_aoi`, `n_oai`). Every net has exactly one source.

**Connectivity.** The datapath is a four-stage shift register whose first stage
is fed through the nand2b/inv/and chain, with the compound gates hanging off the
state bits:

```
rst_n --A_N--.
             u_din_nand --n_nandb-- u_inv --n_inv--.
din ----B----'                                     u_and --n_and--.
tie_hi --A_N,B-- u_tie_nand --n_en-----------------'              |
                                                                  v
   .--------------------------------------------------- state_reg[0] --> dout(0)
   |   dfrtp_2, RESET_B = rst_n
   '-- dout(0) -> state_reg[1] (dfrtp_2, RESET_B = rst_n) --> dout(1)
                  dout(1) -> state_reg[2] (dfstp_2, SET_B = set_n) --> dout(2)
                  dout(2) -> state_reg[3] (dfxtp_2, no set/reset)  --> dout(3)

u_xor (dout(0), dout(1))    -> dout(4)
u_xnor(dout(3), tie_lo)     -> dout(5)
u_aoi (dout(0), dout(1), dout(5)) -> n_aoi -> buf_aoi_x -> dout(6)
u_oai (dout(2), dout(3), dout(4)) -> n_oai -> buf_oai_x -> dout(7)
```

**Boolean functions** the test re-derives exhaustively from the gate *types*,
not from the fixture text:

| cell | output | function |
| --- | --- | --- |
| `a21oi_1` | `Y` | `!((A1 & A2) \| B1)` |
| `o21ai_1` | `Y` | `!((A1 \| A2) & B1)` |
| `nand2b_1` | `Y` | `!(!A_N & B)` |
| `xor2_1` | `X` | `A ^ B` |
| `xnor2_1` | `Y` | `!(A ^ B)` |
| `conb_1` | `HI` / `LO` | constant `1` / constant `0` |

**Flip-flop configuration**, read off the `FFComponent` of each gate type:

| cell | clock | next state | async reset | async set |
| --- | --- | --- | --- | --- |
| `dfrtp_2` | `CLK` | `D` | `!RESET_B` | — |
| `dfstp_2` | `CLK` | `D` | — | `!SET_B` |
| `dfxtp_2` | `CLK` | `D` | — | — |

## Power pins

The library defines `VPWR`/`VGND`/`VPB`/`VNB` on every cell, and the fixture
connects none of them — exactly like the netlists the sky130 extraction flow
produces. That is intentional: leaving them unconnected is the case that has to
keep working.
