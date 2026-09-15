# 05_lfsr_prng — from a synthesized Agilex 3 netlist back to the LFSR

Open **[`guide.html`](guide.html)** in a browser. It works offline from a
checkout: inline CSS, relative image paths, no external requests.

| file | what it is |
| --- | --- |
| `guide.html` | the walkthrough: synthesis, first contact, the RE steps, the reconstruction |
| `design.v` | the original RTL (the answer key) |
| `spec.md` | what the design was supposed to do |
| `quartus/` | the Quartus Prime Pro project (`.qpf`, `.qsf`) and `build.sh` |
| `lfsr_prng.vo` | the Quartus post-synthesis Verilog export |
| `netlist.hal.v` | the same netlist, rewritten by `tools/hal_agilex import` so HAL can read it |
| `check.py` | re-derives the whole result from the netlist and asserts it (CI smoke) |
| `scc.py` | strongly connected components via the `graph_algorithm` plugin |
| `run_analysis.sh` | every analysis command in the guide, in order |
| `recovered.v` | the RTL as reconstructed from the netlist alone |
| `recovered_model.py` | the same reconstruction as a behavioural model, checked against the export |
| `images/` | every diagram in the guide (all generated, none drawn by hand) |
| `artifacts/` | findings documents, next-state functions, traces, reports |

## The netlist as a graph

Nodes are gates, edges are nets. A shift register is a cycle, but every cycle
in a synchronous design passes through a register, so cutting the edges that
land on a flip-flop pin leaves a DAG — the combinational core —
which `hal_viz dag` levels topologically and draws left to right
(`images/dag.svg`, plus the standalone `images/dag.html`).

**2 levels**, 33 gates, 298 edges, 128 cut at a register. Two levels is the
whole point: after the cut, every combinational cell here is one LUT away from
a register — no ripple, no chain, maximal parallelism. Level 0 is exactly the
16 flip-flops. Of the 16 cut edges that start at a gate, seven go
register→register directly and eight through a single one-input cell; the
sixteenth comes from the one cell with four drawn inputs — `state[3]`,
`state[12]`, `state[14]`, `state[15]` — which drives the bottom of the chain.
Those four positions are the taps, visible from connectivity alone. 262 of the
298 edges are constant tie-offs, drawn as one `0`/`1` circle per consuming pin
rather than a shared GND/VCC hub.

`run_analysis.sh` regenerates it as step 3b.

Reproduce the analysis in a container with a built HAL and Graphviz:

```bash
HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib PYTHONPATH=/work/build/lib \
    bash examples/agilex3_walkthroughs/05_lfsr_prng/run_analysis.sh
```

Re-synthesize from the RTL (needs Quartus Prime Pro 26.1 and an Agilex 3
licence): `cd quartus && sh build.sh`.
