# Agilex 3 walkthrough 03 — uart_tx

Recovering a minimal 8N1 UART transmitter from a blinded Quartus Prime Pro
post-synthesis netlist, using only this fork's headless HAL toolchain.

The design is taken apart **twice, independently**: once structurally with HAL
(`analyze.py`), once behaviourally by driving the netlist as a black box
(`probe.py`). Neither half reads the other's output, and where they overlap they
have to agree — most sharply on the transmission order of the eight payload bits,
which one half gets from wiring and the other from timing.

**Start with [`guide.html`](guide.html)** — open it in a browser straight from a
checkout. It is offline-safe: inline CSS, relative image paths, no scripts and no
external requests.

## What is here

| file | role |
| --- | --- |
| `design.v` | the original RTL, the thing being recovered |
| `spec.md` | what it was supposed to do, written before synthesis — the answer key |
| `quartus/` | two Quartus projects: `uart_tx` (the original) and `recovered` (the round trip) |
| `uart_tx.vo` | the Quartus Prime Pro 26.1 post-synthesis export |
| `netlist.hal.v` | the same netlist rewritten for HAL's Verilog parser |
| `anonymize.py` | strips every design-derived name from it |
| `netlist_anon.hal.v` | the blinded netlist the whole walk is done on |
| `netlist_anon.hal.v.map.json` | the rename map; opened only after the analysis |
| `analyze.py` | the structural half: census, clock/reset, SCCs, register graph, counters, LUT masks |
| `probe.py` | the behavioural half: black-box probing of the frame |
| `reference.py` | executable model of `spec.md`, for the netlist-vs-model check |
| `recovered.v` | the RTL as reconstructed from the netlist alone |
| `recovered.vo` | that reconstruction, re-synthesized by Quartus — the structural round trip |
| `recovered.hal.v` | the round-trip export, rewritten for HAL |
| `check.py` | headless re-run of every claim the guide makes |
| `artifacts/` | the step outputs the guide quotes, plus `register_graph.svg` and `07_frame.svg` |
| `guide.html` | **the walkthrough** |

## Reproducing

Synthesis (Windows, Quartus Prime Pro 26.1 — same flow as
`tools/hal_agilex/README.md`):

```
cd quartus
quartus_syn uart_tx -c uart_tx
quartus_eda --simulation --format=verilog --tool=questasim \
            --output_directory=simulation uart_tx -c uart_tx

# and, for the round trip, the same two commands with -c recovered
```

Import and blind (Linux):

```
python3 tools/hal_agilex import \
    examples/agilex3_walkthroughs/03_uart_tx/uart_tx.vo \
    -o examples/agilex3_walkthroughs/03_uart_tx/netlist.hal.v

python3 examples/agilex3_walkthroughs/03_uart_tx/anonymize.py \
    examples/agilex3_walkthroughs/03_uart_tx/netlist.hal.v \
    -o examples/agilex3_walkthroughs/03_uart_tx/netlist_anon.hal.v
```

The structural half (needs a built HAL; Graphviz for the register graph):

```
export HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib
PYTHONPATH=/work/build/lib python3 examples/agilex3_walkthroughs/03_uart_tx/analyze.py \
    --repo . -o examples/agilex3_walkthroughs/03_uart_tx/artifacts
```

The behavioural half (plain Python — no HAL build, no Quartus, no Graphviz):

```
python3 examples/agilex3_walkthroughs/03_uart_tx/probe.py \
    --netlist examples/agilex3_walkthroughs/03_uart_tx/netlist_anon.hal.v \
    -o examples/agilex3_walkthroughs/03_uart_tx/artifacts
```

## Checking

`check.py` re-derives every load-bearing claim in the guide and exits non-zero if
one stops holding. It is split into two tiers because they cost very different
things to run:

```
python3 check.py                                          # tier 1
HAL_PY_PATH=/work/build/lib python3 check.py --with-hal   # tier 1 + tier 2
```

* **Tier 1 — always.** Pure Python plus `tools/hal_agilex`. Asserts that the
  export contains only modelled primitives (18 `tennm_ff`, 22 `tennm_lcell_comb`,
  inventory reporting no gap); that the blinded netlist is behaviourally identical
  to the import over 400 cycles including asynchronous clears; that the export
  agrees with `reference.py` over 1200 pseudo-random cycles; and that the probe
  recovers the clock, reset, request, both outputs, the 16-cycle bit period, the
  10-slot frame, start bit 0 / stop bit 1, the LSB-first payload order and the
  161-cycle busy pulse.
* **Tier 2 — with a built HAL.** Runs `analyze.py` and asserts the structural
  conclusions: exactly one clock net and one clear net across all 18 flip-flops;
  the 9 control / 9 datapath split; one 9-deep shift chain that is
  `shreg[8]`…`shreg[0]` in order; exactly one flop carrying all the feedback, and
  that it is `busy`; that the gating counter is `baud_cnt[0..3]` and the gated one
  `bit_cnt[0..3]`, LSB first; and that the gate-level SCC leaves 7 of the 9 chain
  flops outside feedback.

Tier 2 is the only place the rename map is used: it translates the recovered gate
names back to the originals and requires them to line up exactly.
