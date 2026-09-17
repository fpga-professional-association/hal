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
| [12_present_sbox](12_present_sbox/) | PRESENT-80 encryption datapath | the S-box read out of LUT cones and matched to a published table (and what a match tier means); a bit permutation the wiring cannot show, recovered from the next-state cone support (and by hand first); a key schedule classified bit by bit; published test vectors out of the netlist; two counterfactual exports showing that the RTL, not the algorithm, decides what survives synthesis |
| [13_trivium_stream](13_trivium_stream/) | Trivium keystream generator | the first *stream* cipher: three coupled nonlinear feedback shift registers that no pass could see at all until a load multiplexer was held, the three AND gates that separate them from walkthrough 05's linear LFSR, a feedback function recovered as an algebraic normal form rather than a polynomial, and a 1152-cycle warm-up read back as 64 x 18 |
| [14_keccak_toy](14_keccak_toy/) | Keccak-f[200] sponge permutation | a 5 x 5 x 8 state grid recovered from XOR fan-in alone; two whole layers of the round (rho, pi) that cost zero cells, so 25 rotation offsets and a lane transposition come out of index arithmetic; one 5-bit substitution found 40 times and matched exactly; and the first verdict in the series that is honestly **`undetermined`** on classical-versus-PQC, because a sponge is SHA-3 *and* the XOF inside ML-KEM/ML-DSA/SPHINCS+. Two exports of the same cipher, differing only by a retiming, get opposite structural verdicts |
| [15_ntt_mult](15_ntt_mult/) | toy negacyclic NTT polynomial multiplier | the first design whose secrets are *numbers*: a modulus that is a constant nowhere in the netlist (`q = 2^8 + 1` is too cheap to need a carry chain, so it is recovered from what the correction *computes* and re-checked on every sum the adder can make), eighty-one twiddle constants read off the multiplier's own operand by holding every coefficient at 1, a root of unity pinned by the address schedule, and a butterfly that no pass could see at all until a *vendor* subtracter — inversion folded into the cell mask, carry-in from a leading seed cell — was recognised. Ends **`pqc-style`**, and spends its last section on what that is not. A DSP counterfactual shows a synthesis setting creating a false positive |

| [16_mystery_cores](16_mystery_cores/) | five anonymized cores, blind | **the capstone, and the series is complete at it.** Five blinded netlists — four of the designs above re-synthesized under neutral names, plus one that is *not* cryptography — run through the whole method with a decision rule fixed in advance, and only then scored against an answer key. The method calls **five of five** families and styles correctly; `hal_crypto identify` on its own called **three of five** on the day, and its two misses were false negatives on real cryptography. The decoy's carry chain is exactly as wide as the block cipher's and its logic exactly as deep; what separates them is that one adds the constant 1. Running the same tool on the *named* and the *blinded* copy of each core makes a miss attributable, and turned one of two identical-looking failures into a filed bug (rotations keyed on vector names) while leaving the other as a documented pipeline property. Both filed bugs (#101, #102) have since been fixed and the page re-derived against the repaired tool: blinding now costs no family at all and the tool alone scores **four of five**, with the method still at five of five |

07 and 09 do not exist; the numbering is the series' history, not a promise.
The series is **complete**: 16 is the capstone, and the method it distils is
`ai/skills/re-walkthrough-method`.

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
| 13_trivium_stream | 613 | 1884 | 3 | wider and shallower still: 301 cells in one rank, because 288 of them are one shift stage each |
| 14_keccak_toy | 868 | 3352 | 5 | one level per step of the round: parity planes, theta, chi, then iota fused with the load multiplexer. No carry chain anywhere |
| 15_ntt_mult | 1211 | 5913 | 43 | the deepest in the series by a factor of two and a half: a 9 x 9 array multiplier *and* two modular corrections sit between one register bank and the next |
| 16_mystery_cores/core_a | 868 | 3346 | 5 | (blinded re-synthesis of 14) |
| 16_mystery_cores/core_b | 120 | 1146 | 18 | the decoy: seventeen of those eighteen levels are one sixteen-cell carry chain, and it is a *counter* |
| 16_mystery_cores/core_c | 613 | 1882 | 3 | (blinded re-synthesis of 13) |
| 16_mystery_cores/core_d | 243 | 942 | 18 | (blinded re-synthesis of 11) — the same profile as core_b above, which is the trap |
| 16_mystery_cores/core_e | 1211 | 5913 | 43 | (blinded re-synthesis of 15) |

The four re-synthesized rows reproduce their originals' shape exactly, which is
the point: blinding removes names, not structure. Only `core_b`'s row is a new
design, and it was built to sit on top of `core_d`'s — same chain width, same
depth, different arithmetic. `core_e` is again `-f none` for walkthrough 15's
reason, and `core_b` is the one drawn with per-pin constant stubs rather than
`--const-hub`, which is why its edge count is mostly tie-offs.

Each also writes `images/dag.dot` and a standalone `images/dag.html` (inline
SVG, legend, counts). Constants are drawn as one `0`/`1` tie-off stub per
consuming pin rather than a shared GND/VCC hub — which is why the leftmost
column is tall: between 68% and 88% of the edges in these drawings are
constant tie-offs. The exceptions are 11_speck_toy and 12_present_sbox, both an
order of magnitude bigger than the rest, 13_trivium_stream, bigger than either,
and 14_keccak_toy, bigger again; all four are drawn with `--const-hub`, because
at 1514, 2292, 4377 and 5979 consuming pins the stubs are most of the file and
none of the information (for 11 they also cost eight minutes of Graphviz). Their
rows above therefore count the hub's edges rather than the stubs'. 13 is also the
design that shows why the *levelled* view earns its place: its 613-node
**unlevelled** gate graph does not render with `dot` at all (ten minutes, no
output — the graph is cyclic, so it cannot be layered), while the same 613 nodes
levelled draw in seconds. Like 12, 13, 14 and 15 all scope
`images/netlist_graph.svg` to one gate's neighbourhood instead.

15_ntt_mult is where that argument runs out: at 1211 nodes and **43** levels the
*levelled* whole-netlist graph does not render either — `dot` runs for over an
hour without finishing, because 256 input-port bits feed cells at level 42 and a
long edge costs a dummy node on every rank it crosses. Its row above is computed
with `-f none` (the counts and `dag.dot` are committed; the drawing is not), and
the figure its guide embeds is `images/dag_datapath.svg`, a levelled drawing of
the 901-gate cone behind the modular sum — 34 of the 43 levels, about a minute.

Every walkthrough reruns end to end inside the build container:

```
HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib PYTHONPATH=/work/build/lib \
    bash examples/agilex3_walkthroughs/<walkthrough>/run_all.sh   # or run_analysis.sh / run_analyses.sh
python3 examples/agilex3_walkthroughs/<walkthrough>/check.py     # asserts everything the guide claims
```

`tools/hal_agilex` documents the validated Agilex 3 primitive coverage all of
this rests on; [`CAPABILITIES.md`](../../CAPABILITIES.md) keeps parsing,
simulation and recognition support separate per device family.
