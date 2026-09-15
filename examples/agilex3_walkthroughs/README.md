# Agilex 3 reverse-engineering walkthroughs

Eight self-contained exercises, each recovering the RTL of a small design from
a real Quartus Prime Pro post-synthesis export for Agilex 3, using only this
fork's headless HAL toolchain. Every walkthrough ships the original design (the
answer key), the exported netlist, the analysis scripts that produced every
number its guide quotes, a `check.py` that re-asserts the guide's claims, and a
`guide.html` you can open straight from a checkout — offline, no scripts.

Recommended order — each introduces one or two techniques the later ones lean on:

| # | design | what it teaches |
| --- | --- | --- |
| [01_blinky_counter](01_blinky_counter/) | 24-bit free-running counter | the standard opening moves: census, register bank signatures, SCCs, the carry chain as bit order; why a ripple-carry counter is 24 bit-sized loops, not one |
| [02_traffic_fsm](02_traffic_fsm/) | traffic-light Moore FSM + dwell counter | splitting state register from datapath by enables and output reachability; toggle counts as bit order; product-machine recovery; un-blinding |
| [03_uart_tx](03_uart_tx/) | 8N1 UART transmitter | structure and behaviour as two independent halves; black-box probing (bit period, frame, payload order); a Quartus re-synthesis round trip |
| [04_pwm_generator](04_pwm_generator/) | 8-bit PWM with bus write | two carry chains with different jobs (increment vs compare); reading propagate/generate semantics from lut_masks; a write-enable decode |
| [05_lfsr_prng](05_lfsr_prng/) | 16-bit Fibonacci LFSR | the one-big-SCC signature of word-level feedback; taps and polynomial from next-state algebra; inverted-storage polarity and the seed |
| [06_accumulator_alu](06_accumulator_alu/) | 8-bit accumulator ALU | opcode sweeps; an enable decoded from control bits; an honest null result from word-level recognition |
| [08_shift_debouncer](08_shift_debouncer/) | button debouncer | designs with no chain, no comparator cells and no enables; bit order from the state orbit when structure is symmetric; negative controls that expose coverage holes |
| [10_crc8_checker](10_crc8_checker/) | serial CRC-8 checker | LFSR-with-input recovery; the polynomial from GF(2) linearity; proving the recovered CRC against a corrupted codeword |

07 and 09 do not exist; the numbering is the series' history, not a promise.

## The netlist as a graph

Every guide now carries a "The netlist as a graph" section built on
`images/dag.svg` — the design drawn as a directed graph (nodes = gates, edges =
nets) with the feedback cut at every flip-flop, so the combinational core is a
DAG and can be levelled topologically, level 0 on the left. The level count is
the design's combinational depth between registers, and it is a measurement of
the circuit rather than of its size:

| walkthrough | gates | edges | levels | what the shape is |
| --- | --- | --- | --- | --- |
| 01_blinky_counter | 48 | 456 | 24 | one carry cell per level: a 23-gate ripple |
| 02_traffic_fsm | 22 | 207 | 3 | condition layer, then a one-hot next-state cone |
| 03_uart_tx | 40 | 370 | 3 | a shift chain of eight two-input cells; one gate sets the depth |
| 04_pwm_generator | 34 | 305 | 8 | two carry chains side by side: increment and compare |
| 05_lfsr_prng | 33 | 298 | 2 | a shift ring; one wide cell over four taps |
| 06_accumulator_alu | 31 | 255 | 12 | a ten-cell carry chain with an output rank behind it |
| 08_shift_debouncer | 14 | 131 | 2 | no chain at all: one LUT per counter bit |
| 10_crc8_checker | 13 | 111 | 3 | a shift ring with feedback into three positions |

Each also writes `images/dag.dot` and a standalone `images/dag.html` (inline
SVG, legend, counts). Constants are drawn as one `0`/`1` tie-off stub per
consuming pin rather than a shared GND/VCC hub — which is why the leftmost
column is tall: between 68% and 88% of the edges in these drawings are
constant tie-offs.

Every walkthrough reruns end to end inside the build container:

```
HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib PYTHONPATH=/work/build/lib \
    bash examples/agilex3_walkthroughs/<walkthrough>/run_all.sh   # or run_analysis.sh / run_analyses.sh
python3 examples/agilex3_walkthroughs/<walkthrough>/check.py     # asserts everything the guide claims
```

`tools/hal_agilex` documents the validated Agilex 3 primitive coverage all of
this rests on; [`CAPABILITIES.md`](../../CAPABILITIES.md) keeps parsing,
simulation and recognition support separate per device family.
