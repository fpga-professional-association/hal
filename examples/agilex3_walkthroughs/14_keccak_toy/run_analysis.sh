#!/bin/sh
# Every command the walkthrough runs, in the order the guide runs them.
#
# Run from the repository root, in an environment with a built HAL and the
# Graphviz `dot` binary:
#
#     HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib \
#     PYTHONPATH=/work/build/lib \
#         sh examples/agilex3_walkthroughs/14_keccak_toy/run_analysis.sh
#
# Outputs land in the example's images/ and artifacts/ directories.
set -e

EX=examples/agilex3_walkthroughs/14_keccak_toy
GL=plugins/gate_libraries/definitions/AGILEX_TENNM.hgl
SCRATCH="${TMPDIR:-/tmp}/keccak_toy_controls"
mkdir -p "$EX/images" "$EX/artifacts" "$SCRATCH"

echo "== 0. are both exports inside the validated primitive coverage? ====="
python3 tools/hal_agilex --strict inventory "$EX/keccak_toy.vo" \
    -o "$EX/artifacts/inventory.findings.json"
python3 tools/hal_agilex --strict inventory "$EX/keccak_retimed.vo" \
    -o "$EX/artifacts/inventory_retimed.findings.json"

echo "== 1. does the export behave like the RTL it came from? ============="
# 600 cycles: `start` is pseudo-random and a permutation is uninterruptible
# once begun, so that is roughly thirty complete 19-cycle permutations, with
# the asynchronous clear (cycle 200) landing mid-permutation.  Section 7 says
# why the bound is 600 and not 40.
python3 tools/hal_agilex behavior "$EX/keccak_toy.vo" \
    --reference "$EX/reference.py" --cycles 600 \
    -o "$EX/artifacts/behavior_design.findings.json"

echo "== 2. the pictures every walkthrough starts with ===================="
# --const-hub throughout: this design's constant fan-out is 5405 consuming pins
# (207 flip-flops with five tied control pins each, plus every ALM's unused data
# pins), and one small 0/1 stub per pin -- the default, and what walkthroughs
# 01-10 use -- turns the drawing into a wall of circles.
#
# The gate graph is *scoped*, as 12 and 13 scope theirs: at 868 gates the
# unlevelled whole-netlist drawing is not a diagram (the graph is cyclic, so
# `dot` cannot layer it).  One chi cell with its neighbours says what the
# picture is for; the levelled DAG below is the whole-netlist view.
python3 tools/hal_viz module_tree "$EX/netlist.hal.v" -g "$GL" \
    -o "$EX/images/module_tree.svg" -q
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 's[0]' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/netlist_graph.svg" -q

echo "== 2b. the same graph, levelled: feedback cut at the flops =========="
python3 tools/hal_viz dag "$EX/netlist.hal.v" -g "$GL" --const-hub --max-gates 900 \
    -o "$EX/images/dag.svg" --html -q

echo "== 2b2. ... and the same graph with values, one clock at a time ====="
# The window is one whole permutation and then some: 32 cycles from the load
# that starts the published all-zero-state vector, so the counter runs 0..17,
# `busy` falls, `done` rises and `dout` carries 3C 28 26 ...  --hold din=0
# --hold start=1 makes the load happen on the first cycle out of the clear.
python3 tools/hal_agilex trace "$EX/keccak_toy.vo" \
    --reference "$EX/recovered_reference.py" --cycles 32 \
    --hold start=1 --hold din=0 \
    -o "$EX/artifacts/dag_trace.json"
python3 tools/hal_viz clock_step "$EX/images/dag.svg" \
    --trace "$EX/artifacts/dag_trace.json" \
    -o "$EX/images/dag_interactive.html" -q

echo "== 2c. one column parity cell, close up ============================"
# theta: five flip-flops, one ALM, and the five are one column of the 5 x 5
# array bit-sliced -- which is the whole of how the grid is recovered.
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 'cpar[0]' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/parity_cell.svg" -q

echo "== 2d. one chi cell, close up ======================================"
# chi: three theta nets into one ALM, and the 5-bit substitution lives in the
# lut_masks of five of these, not in any gate called AND.  --depth 1, not 2:
# at depth 2 the scope is 401 gates, because each theta net feeds five chi
# cells and each of those feeds a multiplexer.
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 'chi[0]' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/chi_cell.svg" -q

echo "== 2e. the round-constant lookup, close up ========================="
# iota: the five counter flip-flops into one ALM per lane bit, and out into the
# next-state multiplexer of one bit of one lane.
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 'rc_lut[0]' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/iota_cell.svg" -q

echo "== 3. the structural steps ========================================="
python3 "$EX/analysis.py" all --with-hal -o "$EX/artifacts" \
    > "$EX/artifacts/analysis_all.txt"

echo "== 4. what does the crypto identifier make of each export? =========="
python3 tools/hal_crypto identify "$EX/keccak_toy.vo" \
    -o "$EX/artifacts/identify.findings.json"
python3 tools/hal_crypto identify "$EX/keccak_retimed.vo" \
    -o "$EX/artifacts/identify_retimed.findings.json"

echo "== 5. does the *recovered* model reproduce the netlist? ============="
python3 tools/hal_agilex behavior "$EX/keccak_toy.vo" \
    --reference "$EX/recovered_reference.py" --cycles 600 \
    -o "$EX/artifacts/behavior_recovered.findings.json"

# Three negative controls, so that "it passed" means something.  The first
# moves one rho offset by one bit; the second replaces chi's AND with an XOR,
# which makes the whole permutation linear over GF(2) and is the counterfactual
# the walkthrough is about; the third changes the *last* round's constant, and
# it is the one that decides the bound -- see section 7.
sed 's/^    (6, 6, 3, 7, 5),$/    (6, 6, 3, 6, 5),/' \
    "$EX/recovered_reference.py" > "$SCRATCH/control_rho.py"
python3 tools/hal_agilex behavior "$EX/keccak_toy.vo" \
    --reference "$SCRATCH/control_rho.py" --cycles 600 \
    -o "$EX/artifacts/behavior_negative_control_rho.findings.json"
sed 's/^NONLINEAR = 1$/NONLINEAR = 0/' \
    "$EX/recovered_reference.py" > "$SCRATCH/control_linear.py"
python3 tools/hal_agilex behavior "$EX/keccak_toy.vo" \
    --reference "$SCRATCH/control_linear.py" --cycles 600 \
    -o "$EX/artifacts/behavior_negative_control_linear.findings.json"
sed 's/0x02, 0x80,$/0x02, 0x81,/' \
    "$EX/recovered_reference.py" > "$SCRATCH/control_rc.py"
python3 tools/hal_agilex behavior "$EX/keccak_toy.vo" \
    --reference "$SCRATCH/control_rc.py" --cycles 600 \
    -o "$EX/artifacts/behavior_negative_control_rc.findings.json"

echo "== 6. one page over every findings document ========================"
python3 tools/hal_viz report "$EX"/artifacts/*.findings.json \
    --artifact "$EX/images/dag.svg" \
    --title "Agilex 3 walkthrough 14 - keccak_toy" \
    -o "$EX/artifacts/report.html"

echo "== 7. the smoke check =============================================="
python3 "$EX/check.py" --with-hal | tee "$EX/artifacts/check.txt"

echo "done"
