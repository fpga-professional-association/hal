#!/bin/sh
# Every command the walkthrough runs, in the order the guide runs them.
#
# Run from the repository root, in an environment with a built HAL and the
# Graphviz `dot` binary:
#
#     HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib \
#     PYTHONPATH=/work/build/lib \
#         sh examples/agilex3_walkthroughs/13_trivium_stream/run_analysis.sh
#
# Outputs land in the example's images/ and artifacts/ directories.
set -e

EX=examples/agilex3_walkthroughs/13_trivium_stream
GL=plugins/gate_libraries/definitions/AGILEX_TENNM.hgl
SCRATCH="${TMPDIR:-/tmp}/trivium_stream_controls"
mkdir -p "$EX/images" "$EX/artifacts" "$SCRATCH"

echo "== 0. is the export inside the validated primitive coverage? ========"
python3 tools/hal_agilex --strict inventory "$EX/trivium_stream.vo" \
    -o "$EX/artifacts/inventory.findings.json"

echo "== 1. does the export behave like the RTL it came from? ============="
# 3600 cycles: with `start` pseudo-random and a warm-up uninterruptible once
# begun, that is 6 accepted loads and 3 completed 1152-step warm-ups, with the
# two asynchronous clears (cycles 1200 and 2400) both landing mid-warm-up.
# Shorter runs do not reach `ks_valid` at all -- the guide says so rather than
# quietly picking a number that looks thorough.
python3 tools/hal_agilex behavior "$EX/trivium_stream.vo" \
    --reference "$EX/reference.py" --cycles 3600 \
    -o "$EX/artifacts/behavior_design.findings.json"

echo "== 2. the pictures every walkthrough starts with ===================="
# --const-hub throughout: this design's constant fan-out is 4377 consuming pins
# (301 flip-flops with five tied control pins each, plus every ALM's unused data
# pins), and one small 0/1 stub per pin -- the default, and what walkthroughs
# 01-10 use -- turns the drawing into a wall of circles.  At this size the
# shared GND/VCC hub is the readable choice; section 4 of the guide says so and
# prints the command for the other spelling.
#
# The gate graph is *scoped*, the way walkthrough 12 scopes its own: at 613
# gates the unlevelled whole-netlist drawing is not a diagram.  `dot` did not
# finish it in ten minutes (the graph is cyclic, so it cannot be layered) and
# `--engine sfdp` produced a hairball rather than something to read gate names
# off.  One shift stage with its neighbours says what the picture is for; the
# levelled DAG below is the whole-netlist view, and it *is* layered, so `dot`
# draws all 613 nodes of it in seconds.
python3 tools/hal_viz module_tree "$EX/netlist.hal.v" -g "$GL" \
    -o "$EX/images/module_tree.svg" -q
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 's[92]' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/netlist_graph.svg" -q

echo "== 2b. the same graph, levelled: feedback cut at the flops =========="
python3 tools/hal_viz dag "$EX/netlist.hal.v" -g "$GL" --const-hub --max-gates 700 \
    -o "$EX/images/dag.svg" --html -q

echo "== 2b2. ... and the same graph with values, one clock at a time ====="
# The window is the end of the warm-up: 32 cycles around the moment the counter
# reaches 63/17, `busy` falls, `ks_valid` rises and `ks` carries z1 of the
# published all-zero-key vector.  --skip walks the same run forward without
# recording, so this is a window of one 1152-step warm-up, not a second short
# run.  Needs no HAL.
python3 tools/hal_agilex trace "$EX/trivium_stream.vo" \
    --reference "$EX/recovered_reference.py" --cycles 32 --skip 1140 \
    --hold start=1 --hold key=0 --hold iv=0 \
    -o "$EX/artifacts/dag_trace.json"
python3 tools/hal_viz clock_step "$EX/images/dag.svg" \
    --trace "$EX/artifacts/dag_trace.json" \
    -o "$EX/images/dag_interactive.html" -q

echo "== 2c. one segment head, close up =================================="
# where the nonlinearity lives: five state bits into one ALM, and the AND is
# inside its lut_mask rather than being a gate of its own.
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 't3' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/feedback_cell.svg" -q

echo "== 2d. the output function, close up ==============================="
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 'ks~0' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/keystream_cell.svg" -q

echo "== 3. the structural steps ========================================="
python3 "$EX/analysis.py" all --with-hal -o "$EX/artifacts" \
    > "$EX/artifacts/analysis_all.txt"

echo "== 4. what does the crypto identifier make of it? =================="
python3 tools/hal_crypto identify "$EX/trivium_stream.vo" \
    -o "$EX/artifacts/identify.findings.json"

echo "== 5. does the *recovered* model reproduce the netlist? ============"
python3 tools/hal_agilex behavior "$EX/trivium_stream.vo" \
    --reference "$EX/recovered_reference.py" --cycles 3600 \
    -o "$EX/artifacts/behavior_recovered.findings.json"

# Two negative controls, so that "it passed" means something.  The first moves
# one feedback tap by one stage; the second replaces the three AND terms with
# XORs, which turns the cipher into a (coupled, linear) feedback register and is
# the counterfactual the whole walkthrough is about.  Each MUST be caught, and
# the run records at which cycle -- not cycle 1: the wrong bit enters at the
# head of a 93-stage segment and has to shift 65 stages before the output
# function reads it.
sed 's/^T3_TAPS = (242, 287, 68)$/T3_TAPS = (242, 287, 69)/' \
    "$EX/recovered_reference.py" > "$SCRATCH/control_tap.py"
python3 tools/hal_agilex behavior "$EX/trivium_stream.vo" \
    --reference "$SCRATCH/control_tap.py" --cycles 3600 \
    -o "$EX/artifacts/behavior_negative_control_tap.findings.json"
sed 's/^NONLINEAR = 1$/NONLINEAR = 0/' \
    "$EX/recovered_reference.py" > "$SCRATCH/control_linear.py"
python3 tools/hal_agilex behavior "$EX/trivium_stream.vo" \
    --reference "$SCRATCH/control_linear.py" --cycles 3600 \
    -o "$EX/artifacts/behavior_negative_control_linear.findings.json"

echo "== 6. one page over every findings document ========================"
python3 tools/hal_viz report "$EX"/artifacts/*.findings.json \
    --artifact "$EX/images/dag.svg" \
    --title "Agilex 3 walkthrough 13 - trivium_stream" \
    -o "$EX/artifacts/report.html"

echo "== 7. the smoke check =============================================="
python3 "$EX/check.py" --with-hal | tee "$EX/artifacts/check.txt"

echo "done"
