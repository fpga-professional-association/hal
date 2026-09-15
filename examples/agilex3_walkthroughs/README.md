# Agilex 3 reverse-engineering walkthroughs

Self-contained exercises, each recovering the RTL of a small design from
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
| [11_speck_toy](11_speck_toy/) | Speck32/64 ARX block cipher | the first cryptographic design: recovering rotation constants that are not signals (from carry-chain operand order and register-bank fan-in) and an XOR layer whose cells are all multiplexers; SCCs that split key schedule from data path; `hal_crypto identify` |
| [12_present_sbox](12_present_sbox/) | PRESENT-80 encryption datapath | the S-box read out of LUT cones and matched to a published table (and what a match tier means); a bit permutation no pass can see, recovered by hand; a key schedule classified bit by bit; published test vectors out of the netlist; two counterfactual exports showing that the RTL, not the algorithm, decides what survives synthesis |

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
| 11_speck_toy | 243 | 432 | 18 | two carry chains in lockstep, six cells per level |
| 12_present_sbox | 379 | 1495 | 3 | wide and shallow: 153 independent cells in one rank, the SPN signature |

Each also writes `images/dag.dot` and a standalone `images/dag.html` (inline
SVG, legend, counts). Constants are drawn as one `0`/`1` tie-off stub per
consuming pin rather than a shared GND/VCC hub — which is why the leftmost
column is tall: between 68% and 88% of the edges in these drawings are
constant tie-offs. The exceptions are 11_speck_toy and 12_present_sbox, both an
order of magnitude bigger than the rest and both drawn with `--const-hub`: at
1514 and 2292 consuming pins the stubs are most of the file and none of the
information (for 11 they also cost eight minutes of Graphviz). Their rows above
therefore count the hub's edges rather than the stubs'.

Every walkthrough reruns end to end inside the build container:

```
HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib PYTHONPATH=/work/build/lib \
    bash examples/agilex3_walkthroughs/<walkthrough>/run_all.sh   # or run_analysis.sh / run_analyses.sh
python3 examples/agilex3_walkthroughs/<walkthrough>/check.py     # asserts everything the guide claims
```

`tools/hal_agilex` documents the validated Agilex 3 primitive coverage all of
this rests on; [`CAPABILITIES.md`](../../CAPABILITIES.md) keeps parsing,
simulation and recognition support separate per device family.
