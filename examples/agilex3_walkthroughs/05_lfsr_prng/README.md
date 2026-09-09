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

Reproduce the analysis in a container with a built HAL and Graphviz:

```bash
HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib PYTHONPATH=/work/build/lib \
    bash examples/agilex3_walkthroughs/05_lfsr_prng/run_analysis.sh
```

Re-synthesize from the RTL (needs Quartus Prime Pro 26.1 and an Agilex 3
licence): `cd quartus && sh build.sh`.
