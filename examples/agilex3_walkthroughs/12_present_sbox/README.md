# 12_present_sbox — reading a published S-box out of the silicon

Open **[`guide.html`](guide.html)** in a browser. It works offline from a
checkout: inline CSS, relative image paths, no external requests.

A PRESENT-80 encryption datapath (64-bit block, 80-bit key, 31 rounds, one round
per clock, key schedule included, encrypt only) synthesized for Agilex 3, and the
walk back from the export to the cipher: the 4-bit S-box read out of the LUT
cones and matched against the published table, the bit permutation recovered from
which cone drives which flip-flop — by hand, and now by the permutation pass's
cone-support tier, the key schedule classified bit by bit, the
round count measured, and the four published test vectors reproduced by
simulating the netlist itself.

| file | what it is |
| --- | --- |
| `guide.html` | the walkthrough: synthesis, first contact, the RE steps, the verdict, the proof |
| `design.v` | the original RTL (the answer key) |
| `spec.md` | what the design was supposed to do |
| `quartus/` | the Quartus Prime Pro project (`.qpf`, `.qsf`, `build.tcl`) |
| `present_sbox.vo` | the Quartus post-synthesis Verilog export |
| `netlist.hal.v` | the same netlist, rewritten by `tools/hal_agilex import` so HAL can read it |
| `analysis.py` | the recovery: cones, substitution groups, the permutation, the key schedule, the round count, the test vectors |
| `check.py` | re-derives every number the guide quotes and asserts it (CI smoke, no HAL needed) |
| `run_analysis.sh` | every analysis command in the guide, in order |
| `recovered.v` | the RTL as reconstructed from the netlist alone |
| `recovered_reference.py` | the same reconstruction as a behavioural model, checked against the export |
| `variants/` | two counterfactual exports of the same cipher: what the RTL decides about visibility |
| `images/` | every diagram in the guide (all generated, none drawn by hand) |
| `artifacts/` | findings documents, the analysis JSON, the run transcript, the HTML report |

## The netlist as a graph

Nodes are gates, edges are nets. The datapath register feeds itself, but every
cycle in a synchronous design passes through a register, so cutting the edges
that land on a flip-flop pin leaves a DAG — the combinational core — which
`hal_viz dag` levels topologically and draws left to right (`images/dag.svg`,
plus the standalone `images/dag.html`).

**3 levels**, 379 gates, 1495 edges, 601 cut at a register. Three levels over 226
combinational cells is the substitution-permutation signature: level 0 is the 151
flip-flops, level 1 is **153** cells reading them directly, level 2 is 73 more,
and that is the end. Wide and shallow, because sixteen 4-bit S-boxes are sixteen
independent functions with no carry between them — the opposite of walkthrough
01's 24-level ripple or walkthrough 06's ten-cell carry chain. This drawing is the
one in the series that uses `--const-hub`: with one tie-off stub per consuming pin
the constants add 2292 nodes and 2.5&times; the file size, which at 379 gates buys
nothing.

`run_analysis.sh` regenerates it as step 2c, and step 2d joins a 34-cycle trace
to the same drawing: `images/dag_interactive.html` steps one whole encryption of
the first published test vector — clear, load, 31 rounds, stop — a clock at a
time, with every net coloured by its value.

## Running it

Everything except the two pictures is pure standard library:

```bash
python3 examples/agilex3_walkthroughs/12_present_sbox/analysis.py
python3 examples/agilex3_walkthroughs/12_present_sbox/check.py
```

The full run, in a container with a built HAL and Graphviz:

```bash
HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib PYTHONPATH=/work/build/lib \
    bash examples/agilex3_walkthroughs/12_present_sbox/run_analysis.sh
```

Re-synthesize from the RTL (needs Quartus Prime Pro 26.1 and an Agilex 3
licence): copy `design.v` next to `quartus/build.tcl` into a scratch directory and
follow the three commands in section 1 of the guide.
