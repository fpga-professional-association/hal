# GDS extraction report -- `puzzle.gds`

Produced by `tools/gds2netlist.py` (geometric extraction, no names read
from the GDS other than standard-cell pin labels and top-level port labels).

## Summary

| item | value |
| --- | --- |
| top cell | `puzzle` |
| die area | 200.00 x 300.00 um |
| standard-cell instances | 1618 |
| ... of which physical-only (decap/tap/fill) | 880 |
| distinct cell types | 69 |
| conductor rectangles analysed | 38161 |
| via instances | 8221 |
| signal nets | 739 |
| top-level ports | 13 |

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
| `I` | input | 45 |
| `O[0]` | output | 1 |
| `O[1]` | output | 1 |
| `O[2]` | output | 1 |
| `O[3]` | output | 1 |
| `O[4]` | output | 1 |
| `O[5]` | output | 1 |
| `O[6]` | output | 1 |
| `O[7]` | output | 1 |
| `clk` | input | 1 |
| `enable` | input | 2 |
| `rst_n` | input | 88 |
| `success` | output | 5 |

## Cell census

### Sequential (92 instances, 3 types)

| cell | count |
| --- | --- |
| `sky130_fd_sc_hd__dfrtp_2` | 84 |
| `sky130_fd_sc_hd__dfstp_2` | 4 |
| `sky130_fd_sc_hd__dfxtp_2` | 4 |

### Clock tree (32 instances, 3 types)

| cell | count |
| --- | --- |
| `sky130_fd_sc_hd__clkbuf_16` | 1 |
| `sky130_fd_sc_hd__clkbuf_4` | 15 |
| `sky130_fd_sc_hd__clkbuf_8` | 16 |

### Tie cells (6 instances, 1 types)

| cell | count |
| --- | --- |
| `sky130_fd_sc_hd__conb_1` | 6 |

### Antenna diodes (10 instances, 1 types)

| cell | count |
| --- | --- |
| `sky130_fd_sc_hd__diode_2` | 10 |

### Combinational (598 instances, 59 types)

| cell | count |
| --- | --- |
| `sky130_fd_sc_hd__a2111oi_2` | 1 |
| `sky130_fd_sc_hd__a211o_2` | 5 |
| `sky130_fd_sc_hd__a211oi_2` | 3 |
| `sky130_fd_sc_hd__a21bo_2` | 2 |
| `sky130_fd_sc_hd__a21boi_2` | 4 |
| `sky130_fd_sc_hd__a21o_2` | 19 |
| `sky130_fd_sc_hd__a21oi_2` | 20 |
| `sky130_fd_sc_hd__a221o_2` | 6 |
| `sky130_fd_sc_hd__a221oi_2` | 1 |
| `sky130_fd_sc_hd__a22o_2` | 23 |
| `sky130_fd_sc_hd__a22oi_2` | 1 |
| `sky130_fd_sc_hd__a311o_2` | 2 |
| `sky130_fd_sc_hd__a31o_2` | 26 |
| `sky130_fd_sc_hd__a31oi_2` | 1 |
| `sky130_fd_sc_hd__a32o_2` | 5 |
| `sky130_fd_sc_hd__a41oi_2` | 1 |
| `sky130_fd_sc_hd__and2_2` | 17 |
| `sky130_fd_sc_hd__and2b_2` | 30 |
| `sky130_fd_sc_hd__and3_2` | 26 |
| `sky130_fd_sc_hd__and3b_2` | 4 |
| `sky130_fd_sc_hd__and4_2` | 8 |
| `sky130_fd_sc_hd__and4b_2` | 4 |
| `sky130_fd_sc_hd__and4bb_2` | 14 |
| `sky130_fd_sc_hd__buf_2` | 1 |
| `sky130_fd_sc_hd__inv_2` | 25 |
| `sky130_fd_sc_hd__mux2_1` | 21 |
| `sky130_fd_sc_hd__nand2_2` | 39 |
| `sky130_fd_sc_hd__nand2b_2` | 24 |
| `sky130_fd_sc_hd__nand3_2` | 2 |
| `sky130_fd_sc_hd__nand3b_2` | 1 |
| `sky130_fd_sc_hd__nand4_2` | 15 |
| `sky130_fd_sc_hd__nor2_2` | 49 |
| `sky130_fd_sc_hd__nor3_2` | 4 |
| `sky130_fd_sc_hd__nor3b_2` | 5 |
| `sky130_fd_sc_hd__nor4_2` | 2 |
| `sky130_fd_sc_hd__nor4b_2` | 2 |
| `sky130_fd_sc_hd__o211a_2` | 12 |
| `sky130_fd_sc_hd__o211ai_2` | 1 |
| `sky130_fd_sc_hd__o21a_2` | 31 |
| `sky130_fd_sc_hd__o21ai_2` | 6 |
| `sky130_fd_sc_hd__o21ba_2` | 2 |
| `sky130_fd_sc_hd__o21bai_2` | 1 |
| `sky130_fd_sc_hd__o221a_2` | 3 |
| `sky130_fd_sc_hd__o22a_2` | 4 |
| `sky130_fd_sc_hd__o22ai_2` | 2 |
| `sky130_fd_sc_hd__o2bb2a_2` | 1 |
| `sky130_fd_sc_hd__o311a_2` | 2 |
| `sky130_fd_sc_hd__o31a_2` | 11 |
| `sky130_fd_sc_hd__o31ai_2` | 2 |
| `sky130_fd_sc_hd__o32a_2` | 4 |
| `sky130_fd_sc_hd__o32ai_2` | 1 |
| `sky130_fd_sc_hd__or2_2` | 13 |
| `sky130_fd_sc_hd__or3_2` | 18 |
| `sky130_fd_sc_hd__or3b_2` | 1 |
| `sky130_fd_sc_hd__or4_2` | 10 |
| `sky130_fd_sc_hd__or4b_2` | 9 |
| `sky130_fd_sc_hd__or4bb_2` | 1 |
| `sky130_fd_sc_hd__xnor2_2` | 29 |
| `sky130_fd_sc_hd__xor2_2` | 21 |

### Physical only (880 instances, 2 types)

| cell | count |
| --- | --- |
| `sky130_fd_sc_hd__decap_3` | 204 |
| `sky130_fd_sc_hd__tapvpwrvgnd_1` | 676 |

## Connectivity health

| check | count |
| --- | --- |
| pins with no net at all | 0 |
| nets with more than one driver | 0 |
| nets with no driver and no port | 1 |
| single-terminal (dangling) nets | 21 |
| constant nets from `conb_1` | 12 |

### Constant nets

| net | value | terminals |
| --- | --- | --- |
| `TIE0_250` | 0 | 2 |
| `TIE0_307` | 0 | 1 |
| `TIE0_338` | 0 | 2 |
| `TIE0_361` | 0 | 2 |
| `TIE0_410` | 0 | 3 |
| `TIE0_470` | 0 | 2 |
| `TIE1_251` | 1 | 1 |
| `TIE1_306` | 1 | 2 |
| `TIE1_339` | 1 | 1 |
| `TIE1_362` | 1 | 1 |
| `TIE1_411` | 1 | 1 |
| `TIE1_471` | 1 | 1 |

### Dangling nets (single terminal)

Unused tie-cell outputs (a `conb_1` always offers both `HI` and `LO`,
the design usually needs one) and clock buffers whose output drives
nothing -- the dummy load buffers OpenROAD's CTS inserts to balance
clock-tree levels.  Both are expected, not extraction errors.

| net | terminal |
| --- | --- |
| `TIE0_307` | `conb_1_77280_97920`.LO |
| `TIE1_251` | `conb_1_165600_84320`.HI |
| `TIE1_339` | `conb_1_168820_106080`.HI |
| `TIE1_362` | `conb_1_170200_125120`.HI |
| `TIE1_411` | `conb_1_170660_141440`.HI |
| `TIE1_471` | `conb_1_168360_155040`.HI |
| `n236` | `clkbuf_4_97520_70720`.X |
| `n243` | `clkbuf_4_83260_76160`.X |
| `n249` | `clkbuf_4_123740_84320`.X |
| `n273` | `clkbuf_4_126040_89760`.X |
| `n336` | `clkbuf_4_70380_106080`.X |
| `n349` | `clkbuf_4_84640_111520`.X |
| `n404` | `clkbuf_4_74980_141440`.X |
| `n431` | `clkbuf_4_57500_146880`.X |
| `n504` | `clkbuf_4_146280_174080`.X |
| `n518` | `clkbuf_4_155480_176800`.X |
| `n571` | `clkbuf_4_100740_195840`.X |
| `n589` | `clkbuf_4_109480_206720`.X |
| `n642` | `clkbuf_4_156860_223040`.X |
| `n684` | `clkbuf_4_127880_247520`.X |
| `n686` | `clkbuf_4_114540_250240`.X |

### Nets with no driving output pin

Fully routed nets that join inputs to each other but reach no cell output
and no port.  Each one was traced by hand: the routing really does end
there, so these are floating inputs in the source netlist, not missed
connections.

- `n278`: `a31oi_2_172960_89760`.A1, `a311o_2_177560_89760`.A1

### Floating conductor geometry

Metal that belongs to no cell pin, no port and no supply -- fill, dummy
metal or decoration.  Listed so it is visible that nothing was dropped.

| clusters | rectangles |
| --- | --- |
| 3 | 1366 |

| rectangles | bounding box (nm) |
| --- | --- |
| 596 | (34900, 35200) .. (52000, 52300) |
| 470 | (36700, 37000) .. (50200, 50500) |
| 300 | (38800, 39100) .. (48100, 48400) |

## Non-functional annotation cells

These references are **not** part of the netlist: their only geometry is
on GDS layer 200/0, which is not a conductor in the sky130 stack.  They sit
outside the die area and are recorded here for completeness.

| cell | count |
| --- | --- |
| `INTERNAL_3` | 21 |
| `INTERNAL_7` | 15 |

The row at y = -52720 nm is Morse code: the two box widths are in a
1:3 ratio (dot / dash) and the gaps are 1 / 3 / 7 units, i.e. ITU
timing laid out in space.  It reads:

> **PER ARENAM AD ASTRA**

| cell | x (nm) | y (nm) | width (nm) |
| --- | --- | --- | --- |
| `INTERNAL_3` | 1330 | -52720 | 1380 |
| `INTERNAL_7` | 4090 | -52720 | 4140 |
| `INTERNAL_7` | 9610 | -52720 | 4140 |
| `INTERNAL_3` | 15130 | -52720 | 1380 |
| `INTERNAL_3` | 20650 | -52720 | 1380 |
| `INTERNAL_3` | 26170 | -52720 | 1380 |
| `INTERNAL_7` | 28930 | -52720 | 4140 |
| `INTERNAL_3` | 34450 | -52720 | 1380 |
| `INTERNAL_3` | 45490 | -52720 | 1380 |
| `INTERNAL_7` | 48250 | -52720 | 4140 |
| `INTERNAL_3` | 56530 | -52720 | 1380 |
| `INTERNAL_7` | 59290 | -52720 | 4140 |
| `INTERNAL_3` | 64810 | -52720 | 1380 |
| `INTERNAL_3` | 70330 | -52720 | 1380 |
| `INTERNAL_7` | 75850 | -52720 | 4140 |
| `INTERNAL_3` | 81370 | -52720 | 1380 |
| `INTERNAL_3` | 86890 | -52720 | 1380 |
| `INTERNAL_7` | 89650 | -52720 | 4140 |
| `INTERNAL_7` | 97930 | -52720 | 4140 |
| `INTERNAL_7` | 103450 | -52720 | 4140 |
| `INTERNAL_3` | 117250 | -52720 | 1380 |
| `INTERNAL_7` | 120010 | -52720 | 4140 |
| `INTERNAL_7` | 128290 | -52720 | 4140 |
| `INTERNAL_3` | 133810 | -52720 | 1380 |
| `INTERNAL_3` | 136570 | -52720 | 1380 |
| `INTERNAL_3` | 147610 | -52720 | 1380 |
| `INTERNAL_7` | 150370 | -52720 | 4140 |
| `INTERNAL_3` | 158650 | -52720 | 1380 |
| `INTERNAL_3` | 161410 | -52720 | 1380 |
| `INTERNAL_3` | 164170 | -52720 | 1380 |
| `INTERNAL_7` | 169690 | -52720 | 4140 |
| `INTERNAL_3` | 177970 | -52720 | 1380 |
| `INTERNAL_7` | 180730 | -52720 | 4140 |
| `INTERNAL_3` | 186250 | -52720 | 1380 |
| `INTERNAL_3` | 191770 | -52720 | 1380 |
| `INTERNAL_7` | 194530 | -52720 | 4140 |

## Warnings

None.
