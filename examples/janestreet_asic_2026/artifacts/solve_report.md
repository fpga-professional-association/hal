# Key recovery -- symbolic simulation + SAT

Tool: `tools/solve_key.py`.  Netlist: `artifacts/puzzle_netlist.json`
(extracted from `puzzle.gds` by `tools/gds2netlist.py`).  Simulator:
`tools/sc_sim.py` -- the same one that replayed `example_inputs.vcd`
with 0 mismatches -- lifted to z3 booleans.

## Answer

```
121-bit payload (transmission order, one bit per enable=1 rising edge):

0000000101010000100000000000010101010000000000001010000001000001000000100000101000010000000100000010000010010001010000000

success : asserted at clock edge 125 of the attempt, held to the end
O stream: 28 2A 20 54 57 4F 20 53 54 41 52 53 20 2A 29 00  |(* TWO STARS *)\0|
message : (* TWO STARS *)
```

**The key is not an ASCII string.**  It is an 11 x 11 bitmap -- the
solution of a 2-star *Star Battle*, which is what the chip announces
when it opens:

```
.......*.*.
*....*.....
.......*.*.
*.*........
....*.*....
..*.....*..
....*.....*
.*....*....
...*......*
.....*..*..
.*.*.......
```

Bit *k* of the stream is row `k // 11`, column `k mod 11`.  Row-major
is a convention: the transpose (`k mod 11` as the row) is an equally
valid Star Battle, and nothing in the netlist distinguishes the two.
The 121-bit stream above is what the chip accepts either way.

| property | value |
| --- | --- |
| stars total | 22 |
| stars per row | [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2] |
| stars per column | [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2] |
| touching pairs (incl. diagonal) | 0 |
| star columns per row | [[7, 9], [0, 5], [7, 9], [0, 2], [4, 6], [2, 8], [4, 10], [1, 6], [3, 10], [5, 8], [1, 3]] |

Those three properties alone do **not** pin the grid down: at least 5
distinct 11 x 11 grids have two stars per row and column with none
touching, while the chip accepts exactly one.  So the netlist also
encodes the puzzle's region partition, and it checks it *online* --
121 payload bits arrive but the design has only 92 flip-flops, too few
to store the grid, so the constraints are accumulated while shifting.

Decoded with the framing the recorded attempts use (11 chars x 8 data
bits LSB-first + 3 pad bits) the payload would read `\x80!\x80\x05P\x04\x10B\x08 \x0a`
with pad bits `010000010000000100001000001100000`.  The pad bits are *not* zero, so that framing is
not what the chip checks, and no rotation or bit order of the 121 bits
yields printable ASCII: the "11-character string" reading of the
puzzle's flavour text does not survive contact with the netlist.

## Protocol driven

| edges | rst_n | enable | I |
| --- | --- | --- | --- |
| 0..2 | 0 | 0 | 0 |
| 3 | 1 | 0 | 0 |
| 4..124 | 1 | 1 | key bit k |
| 125..155 | 1 | 0 | 0 |

Identical, edge for edge, to each of the two attempts in
`example_inputs.vcd` (31 trailing edges).  `solve_key.py` proves that
rather than assuming it: replaying its own `attempt_stimulus()` with
each recorded payload reproduces the recorded chip response exactly.

* attempt at edges (0, 155), payload `The night s`: 312 comparisons, **0 mismatches**
* attempt at edges (156, 311), payload `ky awaits  `: 312 comparisons, **0 mismatches**

The engine self-test is the other half: the *symbolic* engine fed the
same concrete payload agrees with `sc_sim.Simulator` on all 9 outputs
and all 92 flip-flop states at all 156 edges (15756 comparisons, 0
mismatches).

## Method

1. `SymSimulator` subclasses `sc_sim.Simulator` and overrides only the
   three methods that touch a *value* (`set_inputs`, `eval_comb`,
   `_apply_async`).  Levelization, driver resolution, clock detection,
   the flip-flop model and the `step()` event order are inherited, so
   the symbolic run *is* the validated simulator, not a copy of it.
2. Every combinational cell type in the netlist is **proved** equivalent
   between the symbolic path and the concrete `sc_sim.CELLS` truth
   table by z3 -- 62 (cell, output pin) pairs, 0 mismatches -- before
   any simulation runs (`--check-cells`).  Cells whose Python function
   branches on a value (`mux2` here) are in `SYM_OVERRIDES`; `Bit` has
   no `__bool__`, so a missing override raises instead of silently
   picking a branch.
3. Driving the symbolic engine with the *concrete* bits of a recorded
   attempt reproduces the concrete simulator exactly at all 156 edges
   (all 9 outputs and all 92 flip-flop states) -- a self-check that the
   value lifting changed nothing.
4. One attempt unrolled over 156 rising edges; the 121 serial bits are
   z3 booleans, everything else concrete.  `success` is constant 0 at
   edges 0..124 and, from edge 125 on, one latched formula of all 121
   variables (identical expression at every later edge).
5. Solve `Or(success_e)` over that window, then enumerate further
   models with blocking clauses over the whole 121-bit vector.

## Uniqueness

The enumeration returned **1 solution**, and the next solver call was
UNSAT: over the full 2^121 input space **exactly one payload raises
`success`**.  There are no don't-care bits, no free pad bits and no
alternative key -- all 121 variables occur in the `success` formula and
every one of them is pinned.

## Concrete verification (independent code path)

Re-run through plain `sc_sim.Simulator` with 0/1 values only:

```
recovered key
  success  : True, edges 125..155
  O stream : 28 2A 20 54 57 4F 20 53 54 41 52 53 20 2A 29 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00  |(* TWO STARS *)\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0|
  message  : '(* TWO STARS *)' then NUL padding
  n278 (the one undriven net) forced to 1 instead of 0: same result: True

negative control -- recorded VCD attempt 1 payload 'The night s'
  success  : False
  O stream : 54 52 59 20 41 47 41 49 4E 00 00 00 00 00 00 00  |TRY AGAIN\0\0\0\0\0\0\0|
negative control -- recorded VCD attempt 2 payload 'ky awaits  '
  success  : False
  O stream : 54 52 59 20 41 47 41 49 4E 00 00 00 00 00 00 00  |TRY AGAIN\0\0\0\0\0\0\0|
negative control -- recovered key with bit 0 flipped
  success  : False
  O stream : 54 52 59 20 41 47 41 49 4E 00 00 00 00 00 00 00  |TRY AGAIN\0\0\0\0\0\0\0|
negative control -- recovered key with bit 120 flipped
  success  : False
  O stream : 54 52 59 20 41 47 41 49 4E 00 00 00 00 00 00 00  |TRY AGAIN\0\0\0\0\0\0\0|
```

The two recorded attempts reproduce the recorded `TRY AGAIN` message,
and flipping a single bit of the key at either end of the stream loses
`success` and falls back to `TRY AGAIN` -- the check reads the whole
payload.

## Raw log

```
cell-equivalence proof (symbolic path vs sc_sim truth tables)
  62 (cell, output pin) pairs checked by z3, 0 mismatches

engine self-test: symbolic engine driven with the concrete bits of
the recorded attempt 1 payload, compared against sc_sim.Simulator
  15756 comparisons over 156 edges (9 outputs + 92 flop states each), 0 mismatches

protocol cross-check: this module's attempt_stimulus() replayed
against example_inputs.vcd
  attempt (0, 155) payload 'The night s': 156 edges, 312 comparisons, 0 mismatches
  attempt (156, 311) payload 'ky awaits  ': 156 edges, 312 comparisons, 0 mismatches

symbolic unroll of one attempt: 156 rising edges, 121 input variables
  built in 2.1 s
  success is constant 0 at 125 edges (0..124)
  success depends on the key at edges 125..155
  the formula is identical at every one of those edges: True

SAT: is there a 121-bit payload with success high at any edge?
  solutions found: 1  (search EXHAUSTED -- the answer is unique, 0.0 s)
  key (121 bits, in transmission order):
    0000000101010000100000000000010101010000000000001010000001000001000000100000101000010000000100000010000010010001010000000

decoding
  as 11 chars x (8 data LSB-first + 3 pad) -- the framing the two
  recorded VCD attempts use:
    chars : \x80!\x80\x05P\x04\x10B\x08 \x0a
    pads  : 010000010000000100001000001100000
    printable ASCII: False
  -> not a character string: the pad bits are not zero, so this is
     not the framing the chip checks.
  as an 11 x 11 grid (bit k = row k//11, column k mod 11):
    .......*.*.
    *....*.....
    .......*.*.
    *.*........
    ....*.*....
    ..*.....*..
    ....*.....*
    .*....*....
    ...*......*
    .....*..*..
    .*.*.......
    total 1 bits    : 22
    ones per row    : [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]
    ones per column : [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]
    touching pairs (incl. diagonal): 0
    -> two per row, two per column, none touching:
       a 2-star Star Battle solution: True
    star columns per row: [[7, 9], [0, 5], [7, 9], [0, 2], [4, 6], [2, 8], [4, 10], [1, 6], [3, 10], [5, 8], [1, 3]]
    grids meeting only row/column/adjacency: >=5, so the chip must
    also encode the puzzle's region map (it accepts exactly one grid)

concrete verification -- plain sc_sim.Simulator, 0/1 values only
  recovered key:
    success asserted : True, first at edge 125, held through edge 155
    O stream from edge 125 (enable=0 window):
      28 2A 20 54 57 4F 20 53 54 41 52 53 20 2A 29 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00  |(* TWO STARS *)\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0\0|
    message: '(* TWO STARS *)'
    re-run with the undriven net n278 forced to 1: identical: True

negative controls
  recorded VCD attempt 1 payload 'The night s'
    success : False
    O stream: 54 52 59 20 41 47 41 49 4E 00 00 00 00 00 00 00  |TRY AGAIN\0\0\0\0\0\0\0|
  recorded VCD attempt 2 payload 'ky awaits  '
    success : False
    O stream: 54 52 59 20 41 47 41 49 4E 00 00 00 00 00 00 00  |TRY AGAIN\0\0\0\0\0\0\0|
  recovered key with bit 0 flipped
    success : False
    O stream: 54 52 59 20 41 47 41 49 4E 00 00 00 00 00 00 00  |TRY AGAIN\0\0\0\0\0\0\0|
  recovered key with bit 120 flipped
    success : False
    O stream: 54 52 59 20 41 47 41 49 4E 00 00 00 00 00 00 00  |TRY AGAIN\0\0\0\0\0\0\0|

VERDICT: KEY CONFIRMED
```
