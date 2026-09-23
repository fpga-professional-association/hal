# GDS extraction report -- `04_final.gds`

Produced by `tools/gds2netlist.py` (geometric extraction, no names read
from the GDS other than standard-cell pin labels and top-level port labels).

## Summary

| item | value |
| --- | --- |
| top cell | `adder_demo` |
| die area | 100.00 x 100.00 um |
| standard-cell instances | 230 |
| ... of which physical-only (decap/tap/fill) | 151 |
| distinct cell types | 18 |
| conductor rectangles analysed | 5476 |
| via instances | 869 |
| signal nets | 84 |
| top-level ports | 6 |

## Method and validation

Connectivity is rebuilt from geometry alone:

1. every `sky130_fd_sc_hd__*` cell's pin shapes are derived from its own
   pin labels (texttype 5) plus the conductor shapes under them, following
   intra-cell `mcon`/`via*` cuts -- several cells (`dfrtp_2`'s `RESET_B`,
   for one) present their pin on met1, not li1, and the router uses that;
2. top-cell routing is read from polygons **and** GDS PATH records on
   li1/met1..met5, and every `VIA_*` reference shorts its two landing pads;
3. a union-find over axis-aligned rectangles (spatial hash, exact integer-nm
   overlap test) produces the net partition;
4. nets are named from the top cell's port labels, constants from `conb_1`.

`tools/validate_warmup.py` runs the same extractor on `warmup/04_final.gds`
and compares it against the ground truth in
`warmup/03_post_place_and_route.def` and `warmup/01_netlist.v`:
230/230 instances matched by cell type, placement *and* orientation;
285/285 signal pins land in a net partition that is exactly isomorphic to
the DEF's; all 6 signal ports get their correct name. No exceptions.

## Ports

| port | direction | terminals |
| --- | --- | --- |
| `A` | input | 1 |
| `B` | input | 1 |
| `S` | output | 1 |
| `clk` | input | 1 |
| `en` | input | 16 |
| `rst_n` | input | 16 |

## Cell census

### Sequential (16 instances, 1 types)

| cell | count |
| --- | --- |
| `sky130_fd_sc_hd__dfrtp_2` | 16 |

### Clock tree (3 instances, 1 types)

| cell | count |
| --- | --- |
| `sky130_fd_sc_hd__clkbuf_16` | 3 |

### Combinational (60 instances, 14 types)

| cell | count |
| --- | --- |
| `sky130_fd_sc_hd__a21bo_2` | 1 |
| `sky130_fd_sc_hd__a21boi_2` | 1 |
| `sky130_fd_sc_hd__a21o_2` | 1 |
| `sky130_fd_sc_hd__a31o_2` | 5 |
| `sky130_fd_sc_hd__and2_2` | 7 |
| `sky130_fd_sc_hd__and3_2` | 1 |
| `sky130_fd_sc_hd__and4bb_2` | 2 |
| `sky130_fd_sc_hd__mux2_1` | 16 |
| `sky130_fd_sc_hd__nand2_2` | 4 |
| `sky130_fd_sc_hd__nor2_2` | 8 |
| `sky130_fd_sc_hd__o21bai_2` | 1 |
| `sky130_fd_sc_hd__or2_2` | 5 |
| `sky130_fd_sc_hd__xnor2_2` | 3 |
| `sky130_fd_sc_hd__xor2_2` | 5 |

### Physical only (151 instances, 2 types)

| cell | count |
| --- | --- |
| `sky130_fd_sc_hd__decap_3` | 58 |
| `sky130_fd_sc_hd__tapvpwrvgnd_1` | 93 |

## Connectivity health

| check | count |
| --- | --- |
| pins with no net at all | 0 |
| nets with more than one driver | 0 |
| nets with no driver and no port | 0 |
| single-terminal (dangling) nets | 0 |
| constant nets from `conb_1` | 0 |

### Floating conductor geometry

Metal that belongs to no cell pin, no port and no supply -- fill, dummy
metal or decoration.  Listed so it is visible that nothing was dropped.

| clusters | rectangles |
| --- | --- |
| 3 | 1366 |

| rectangles | bounding box (nm) |
| --- | --- |
| 596 | (65900, 66200) .. (83000, 83300) |
| 470 | (67700, 68000) .. (81200, 81500) |
| 300 | (69800, 70100) .. (79100, 79400) |

## Warnings

None.
