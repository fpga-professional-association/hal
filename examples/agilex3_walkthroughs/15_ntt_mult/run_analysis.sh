#!/bin/sh
# Every command the walkthrough runs, in the order the guide runs them.
#
# Run from the repository root, in an environment with a built HAL and the
# Graphviz `dot` binary:
#
#     HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib \
#     PYTHONPATH=/work/build/lib \
#         sh examples/agilex3_walkthroughs/15_ntt_mult/run_analysis.sh
#
# Outputs land in the example's images/ and artifacts/ directories.
set -e

EX=examples/agilex3_walkthroughs/15_ntt_mult
GL=plugins/gate_libraries/definitions/AGILEX_TENNM.hgl
SCRATCH="${TMPDIR:-/tmp}/ntt_mult_controls"
mkdir -p "$EX/images" "$EX/artifacts" "$SCRATCH"

echo "== 0. is the export inside the validated primitive coverage? ========"
# The one that matters for this design: no tennm_mac.  A 9 x 9 multiply is
# exactly what Quartus puts in a DSP block unless told not to, and a DSP is
# not a primitive hal_agilex models.
python3 tools/hal_agilex --strict inventory "$EX/ntt_mult.vo" \
    -o "$EX/artifacts/inventory.findings.json"
# ... and the counterfactual that did not say so, which is *not* --strict
# because the whole point is that it comes back `unsupported`.
python3 tools/hal_agilex inventory "$EX/ntt_dsp.vo" \
    -o "$EX/artifacts/inventory_dsp.findings.json"

echo "== 1. does the export behave like the RTL it came from? ============="
# 600 cycles: one product takes 144, `start` is pseudo-random and a product
# once begun is uninterruptible, and the asynchronous clears land at 200 and
# 400 -- so the first product completes before anything is disturbed.  Section
# 7 says why the bound is 600 and not 200; one of the three negative controls
# passes a 200-cycle run clean.
python3 tools/hal_agilex behavior "$EX/ntt_mult.vo" \
    --reference "$EX/reference.py" --cycles 600 \
    -o "$EX/artifacts/behavior_design.findings.json"

echo "== 2. the pictures every walkthrough starts with ===================="
# --const-hub throughout: 299 flip-flops with five tied control pins each plus
# every ALM's unused data pins is a wall of little circles otherwise.
#
# The gate graph is *scoped*, as 12, 13 and 14 scope theirs: at 1209 gates the
# unlevelled whole-netlist drawing is not a diagram (the graph is cyclic, so
# `dot` cannot layer it).  The levelled DAG below is the whole-netlist view.
python3 tools/hal_viz module_tree "$EX/netlist.hal.v" -g "$GL" \
    -o "$EX/images/module_tree.svg" -q
#
# --depth 1, not 2: with --const-hub the shared GND/VCC node is a neighbour of
# every cell, so *any* depth-2 scope is the whole netlist.  Walkthrough 14 hit
# the same 401-gate wall from its chi cell.
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 'u[0]' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/netlist_graph.svg" -q

echo "== 2b. the whole netlist, levelled: feedback cut at the flops ======="
# 43 levels over 1211 nodes and 5913 edges -- the deepest in the series by a
# factor of two and a half, because a 9 x 9 array multiplier and two modular
# corrections sit between one register bank and the next.  Speck's ARX round is
# 18 levels; PRESENT's SPN round is 3.
#
# `-f none`: the counts are computed and committed as dag.dot and dag.html, but
# the drawing is not.  Graphviz's `dot` needs **over an hour** on this graph and
# still does not finish -- 256 input-port bits feed cells at level 42, and a
# long edge costs a dummy node on every rank it crosses.  Walkthrough 13 makes
# the neighbouring point about unlevelled graphs; this is the first design where
# even the levelled whole-netlist view is beyond a layout engine.
python3 tools/hal_viz dag "$EX/netlist.hal.v" -g "$GL" --const-hub --max-gates 1300 \
    -f none -o "$EX/images/dag.svg" --html -q

echo "== 2b2. the datapath cone, levelled and drawn ======================"
# What the guide embeds: everything the modular sum depends on, eight hops back.
# 901 of the 1211 gates and 34 of the 43 levels -- the multiplier, both
# reductions, the butterfly and the read multiplexers -- in about a minute.
python3 tools/hal_viz dag "$EX/netlist.hal.v" -g "$GL" --const-hub --max-gates 900 \
    --gate 'sum_mod[0]' --depth 8 --direction predecessors --render-timeout 900 \
    -o "$EX/images/dag_datapath.svg" --html -q

echo "== 2b3. ... and the same drawing with values, one clock at a time ==="
# 32 cycles: the clear, the load, then thirty butterflies -- stage 0 and most of
# stage 1 of the first forward transform.  --hold start=1 makes the load happen
# on the first cycle out of the clear.
python3 tools/hal_agilex trace "$EX/ntt_mult.vo" \
    --reference "$EX/recovered_reference.py" --cycles 32 \
    --hold start=1 \
    -o "$EX/artifacts/dag_trace.json"
python3 tools/hal_viz clock_step "$EX/images/dag_datapath.svg" \
    --trace "$EX/artifacts/dag_trace.json" \
    -o "$EX/images/dag_interactive.html" -q

echo "== 2c. the butterfly, close up ====================================="
# One slice of the adder and one of the subtracter, over the same two operand
# nets.  That the two chains read the *same* vectors is the whole claim.
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 'add_2~1' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/butterfly_cell.svg" -q

echo "== 2d. the modular correction, close up ============================"
# Where the modulus lives: a multiplexer picking between the raw sum and the
# sum minus q, on the sign bit of that subtraction.  No constant operand
# anywhere, because 257 is cheap.
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 'sum_mod[0]' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/reduction_cell.svg" -q

echo "== 2e. one twiddle-table cell, close up ============================"
# Five counter flip-flops into one ALM, nine of them: a 32-entry table of
# nine-bit constants, and nothing that looks like a constant.
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 'tw_fwd_w[0]' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/twiddle_cell.svg" -q

echo "== 3. the structural steps ========================================="
python3 "$EX/analysis.py" all --with-hal -o "$EX/artifacts" \
    > "$EX/artifacts/analysis_all.txt"

echo "== 4. what does the crypto identifier make of each export? =========="
python3 tools/hal_crypto identify "$EX/ntt_mult.vo" \
    -o "$EX/artifacts/identify.findings.json"
python3 tools/hal_crypto identify "$EX/ntt_dsp.vo" \
    -o "$EX/artifacts/identify_dsp.findings.json"

echo "== 5. does the *recovered* model reproduce the netlist? ============="
python3 tools/hal_agilex behavior "$EX/ntt_mult.vo" \
    --reference "$EX/recovered_reference.py" --cycles 600 \
    -o "$EX/artifacts/behavior_recovered.findings.json"

# Three negative controls, so that "it passed" means something.  The first
# moves the last forward twiddle by one; the second moves the modulus from 257
# to 256, which is the difference between a ring and a truncation; the third
# moves one post-scale constant, and it is the one that decides the bound --
# see section 7.
sed 's/^    15, 240, 197, 68, 34, 30, 121, 137,$/    15, 240, 197, 68, 34, 30, 121, 136,/' \
    "$EX/recovered_reference.py" > "$SCRATCH/control_twiddle.py"
python3 tools/hal_agilex behavior "$EX/ntt_mult.vo" \
    --reference "$SCRATCH/control_twiddle.py" --cycles 600 \
    -o "$EX/artifacts/behavior_negative_control_twiddle.findings.json"
sed 's/^MODULUS = 257$/MODULUS = 256/' \
    "$EX/recovered_reference.py" > "$SCRATCH/control_modulus.py"
python3 tools/hal_agilex behavior "$EX/ntt_mult.vo" \
    --reference "$SCRATCH/control_modulus.py" --cycles 600 \
    -o "$EX/artifacts/behavior_negative_control_modulus.findings.json"
sed 's/^    241, 256, 4, 193, 129, 249, 32, 2,$/    240, 256, 4, 193, 129, 249, 32, 2,/' \
    "$EX/recovered_reference.py" > "$SCRATCH/control_post.py"
python3 tools/hal_agilex behavior "$EX/ntt_mult.vo" \
    --reference "$SCRATCH/control_post.py" --cycles 600 \
    -o "$EX/artifacts/behavior_negative_control_post.findings.json"

echo "== 6. one page over every findings document ========================"
python3 tools/hal_viz report "$EX"/artifacts/*.findings.json \
    --artifact "$EX/images/dag_datapath.svg" \
    --title "Agilex 3 walkthrough 15 - ntt_mult" \
    -o "$EX/artifacts/report.html"

echo "== 7. the smoke check =============================================="
python3 "$EX/check.py" --with-hal | tee "$EX/artifacts/check.txt"

echo "done"
