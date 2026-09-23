# Jane Street ASIC puzzle (2026) — full GDS-to-answer walkthrough

Reverse engineering of the [Jane Street ASIC puzzle](https://blog.janestreet.com/can-you-reverse-engineer-an-asic/)
([files](https://github.com/janestreet/asic-puzzle-2026)): a sky130 GDS layout of a chip
with ports `clk, rst_n, enable, I` → `O[7:0], success`, and the question *"what input makes
`success` go high?"*. This example takes the design from raw polygons all the way to the
verified answer, with every step validated against ground truth. It was solved after the
competition deadline (submissions closed 2026-09-04) as an educational exercise.

> **SPOILER WARNING** — this README and `artifacts/` contain the complete solution.

## Quick start

```sh
python tools/fetch_puzzle.py         # download puzzle.gds + warmup chain (not checked in)
pip install gdstk z3-solver
python tools/test_sc_sim.py          # 30 simulator self-tests
python tools/validate_warmup.py      # extractor vs. warm-up DEF ground truth: 100%
python tools/gds2netlist.py          # GDS -> artifacts/puzzle_netlist.{v,json}
python tools/replay_vcd.py           # netlist replays example_inputs.vcd exactly
python tools/solve_key.py            # z3: the unique 121-bit key
python tools/run_attempt.py          # feed the key, watch success go high
```

## Method

1. **Extraction** (`tools/gds2netlist.py`) — purely geometric netlist recovery: std-cell pin
   shapes from the cells' own GDS labels, routing from polygons *and* PATH records, vias from
   `VIA_*` SREFs, all decomposed to integer-nm rectangles and merged with union-find over a
   spatial hash. **Validated to 100%** on the warm-up design against its DEF: 230/230
   instances, 285/285 pins, 84/84 nets bijective (`tools/validate_warmup.py`).
2. **Simulation** (`tools/sc_sim.py`) — dependency-free cycle-based simulator with the full
   sky130_fd_sc_hd cell library (AOI/OAI roster generated from one table so `*oi`/`*ai` are
   complements by construction), async set/reset flops, clock-tree auto-detection.
   Mutation-tested; proves itself on the warm-up (`A+B==496`) netlist.
3. **Ground truth replay** (`tools/replay_vcd.py`) — the extracted netlist driven by the
   stimulus in `example_inputs.vcd` reproduces the recorded outputs at **all 312 clock
   edges**, including the `TRY AGAIN` byte stream. Alignment shifts ±1/±2 all fail, so the
   match is not vacuous.
4. **Solving** (`tools/solve_key.py`) — symbolic simulation of the whole protocol over z3,
   `success == 1` asserted; and independently, **structural RE** (`tools/symbolic.py`,
   `tools/symlift.py`, `tools/flop_map.py`) lifting all 738 logic cells back to boolean
   equations, reading the hidden constant out of the gate cone, and solving it with plain
   backtracking (`tools/starbattle.py`). Both methods produced the identical answer.

## What the chip is

**A Star Battle judge.** There is no stored password and no comparator tree. The 121
`enable`-cycles of serial input are interpreted as an 11×11 grid bitmap (row-major), and the
chip checks five constraints in hardware:

- total population = 22 (8-bit popcount, decoded `== 0b00010110`),
- every row has exactly 2 stars, every column has exactly 2 stars,
- every *region* has exactly 2 stars — the region map is hard-wired as a ~130-gate cone
  mapping the (row, col) counters to a 4-bit region id (`artifacts/region_map.txt`),
- no two stars touch, even diagonally (a 12-deep input delay line checks the −1/−10/−11/−12
  taps).

The flop map (`artifacts/flop_map.md`): row/column counters, popcount, 11×2-bit column
counters, 11×2-bit region counters, delay line + touch flag, output-byte counter, an 8-bit
LFSR (poly x⁸+x⁶+x⁵+x⁴+1, seed 0xA5 — the four `dfstp` set-flops are exactly its 1-bits),
and verdict flops. The "output generator" region from the blog's hint picture holds five
messages: `TRY AGAIN` (fail), `EMPTY SKY` (all-zero input), `BIG BANG` (all-one input), a
near-miss easter egg (`TWO NOT TOUCH`), and the success message — which is **not** a ROM but
`lfsr ⊕ mask`, only decodable by actually solving the puzzle.

## The answer

The region map's Star Battle instance has a **unique** solution (113,758,570 grids satisfy
the counting constraints; the adjacency rule kills all but one — uniqueness confirmed
exhaustively by z3 model enumeration over the full 2^121 input space):

```
. . . . . . . * . * .          6 6 6 6 6 8 8 5 4 4 9
* . . . . * . . . . .          6 6 0 6 6 8 5 5 4 4 9
. . . . . . . * . * .          6 6 0 8 8 8 8 5 5 4 9
* . * . . . . . . . .          6 6 0 8 1 1 1 9 5 5 9
. . . . * . * . . . .          0 6 0 8 1 9 9 9 9 9 9
. . * . . . . . * . .          0 0 0 8 1 1 1 9 2 2 2
. . . . * . . . . . *          8 8 8 8 8 8 1 9 2 A A
. * . . . . * . . . .          8 7 7 7 1 1 1 9 2 A A
. . . * . . . . . . *          8 7 7 3 9 9 9 9 2 A A
. . . . . * . . * . .          8 8 7 3 3 9 9 9 2 2 2
. * . * . . . . . . .          8 7 7 3 9 9 9 9 9 9 9
   solution                       region map
```

121-bit payload (one bit per `enable` rising edge, row-major):

```
0000000101010000100000000000010101010000000000001010000001000001000000100000101000010000000100000010000010010001010000000
```

`success` asserts on the first edge after `enable` drops and `O` streams
**`(* TWO STARS *)`** — an OCaml comment, naturally. (LFSR digest of the winning payload is
0x65; XORed against the hard-wired mask it yields the message.)

The example VCD's "The night sky awaits" / 11-ASCII-char framing is a deliberate red
herring: the winning payload has non-zero "pad" bits and decodes to garbage as ASCII.

## Easter eggs and defects found along the way

- `INTERNAL_3`/`INTERNAL_7` cells on non-fab layer 200, placed *below* the die outline, are
  spatial Morse code (1:3 widths, ITU spacing): **`PER ARENAM AD ASTRA`** — "through the
  sand to the stars" (`artifacts/recon_notes.md`, `artifacts/layer200_markers.png`).
- 1366 floating met2 squares form three concentric rings — decoration (a star chart?).
- One net (`n278`) is genuinely unrouted in the GDS: the near-miss message prints
  `TWO"NOT TOUCH` instead of the intended `TWO NOT TOUCH`. It is unobservable on `success`
  and on every other message (`artifacts/replay_validation.txt`).
- The VCD's `$date` is the 2016-12-31 leap second.

## Files

| | |
|---|---|
| `tools/fetch_puzzle.py` | downloads the upstream puzzle files (not checked in — no license) |
| `tools/gds2netlist.py`, `validate_warmup.py` | extraction + ground-truth validation |
| `tools/sc_sim.py`, `test_sc_sim.py` | gate-level simulator + self-tests |
| `tools/replay_vcd.py`, `vcd_decode.py` | VCD replay validation / stimulus decoding |
| `tools/solve_key.py` | z3 symbolic solve (answer + uniqueness) |
| `tools/symbolic.py`, `symlift.py`, `flop_map.py`, `extract_region_map.py`, `starbattle.py` | structural RE path |
| `tools/run_attempt.py`, `reference.py`, `check_recovered*.py`, `tb_recovered.v` | verification harnesses |
| `recovered.v` | behavioral re-implementation of the whole chip (Icarus-verified vs. gate level) |
| `artifacts/` | reports: extraction, flop map, structure, solve, recon, replays |

Regenerable heavy outputs (`artifacts/puzzle_netlist.*`, `warmup_netlist.*`) are gitignored;
rerun the pipeline above to rebuild them.
