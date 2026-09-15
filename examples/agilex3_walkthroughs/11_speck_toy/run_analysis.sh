#!/bin/sh
# Every command the walkthrough runs, in the order the guide runs them.
#
# Run from the repository root, in an environment with a built HAL and the
# Graphviz `dot` binary:
#
#     HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib \
#     PYTHONPATH=/work/build/lib \
#         sh examples/agilex3_walkthroughs/11_speck_toy/run_analysis.sh
#
# Outputs land in the example's images/ and artifacts/ directories.
set -e

EX=examples/agilex3_walkthroughs/11_speck_toy
GL=plugins/gate_libraries/definitions/AGILEX_TENNM.hgl
SCRATCH="${TMPDIR:-/tmp}/speck_toy_controls"
mkdir -p "$EX/images" "$EX/artifacts" "$SCRATCH"

echo "== 0. is the export inside the validated primitive coverage? ========"
python3 tools/hal_agilex --strict inventory "$EX/speck_toy.vo" \
    -o "$EX/artifacts/inventory.findings.json"

echo "== 1. does the export behave like the RTL it came from? ============="
# 2000 cycles: with `start` pseudo-random and an encryption uninterruptible
# once begun, that is 85 accepted starts and 81 completed 23-cycle
# encryptions, 91% of the run spent busy, with the two asynchronous clears
# (cycles 666 and 1333) both landing mid-encryption.
python3 tools/hal_agilex behavior "$EX/speck_toy.vo" \
    --reference "$EX/reference.py" --cycles 2000 \
    -o "$EX/artifacts/behavior_design.findings.json"

echo "== 2. the pictures every walkthrough starts with ===================="
# --const-hub throughout: this design's constant fan-out is 1514 consuming
# pins, and one small 0/1 stub per pin (the default, and what walkthroughs
# 01-10 use) turns the drawing into a wall of circles -- 1.7 MB of SVG and
# eight minutes of Graphviz for the DAG alone.  At this size the shared
# GND/VCC hub is the readable choice; section 4 of the guide says so and
# prints the command for the other spelling.
python3 tools/hal_viz module_tree "$EX/netlist.hal.v" -g "$GL" \
    -o "$EX/images/module_tree.svg" -q
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --module top_module --show-boundary --const-hub \
    -o "$EX/images/netlist_graph.svg" -q

echo "== 2b. the same graph, levelled: feedback cut at the flops =========="
python3 tools/hal_viz dag "$EX/netlist.hal.v" -g "$GL" --const-hub \
    -o "$EX/images/dag.svg" --html -q

echo "== 2b2. ... and the same graph with values, one clock at a time ====="
# 25 cycles: the asynchronous clear, the load, the 22 rounds and the cycle
# `done` comes up on.  start/pt/key are held at the published Speck32/64 test
# vector, so the last frame's `ct` is that vector's ciphertext.  Needs no HAL.
python3 tools/hal_agilex trace "$EX/speck_toy.vo" \
    --reference "$EX/recovered_reference.py" --cycles 25 \
    --hold start=1 --hold pt=0x6574694c --hold key=0x1918111009080100 \
    -o "$EX/artifacts/dag_trace.json"
python3 tools/hal_viz clock_step "$EX/images/dag.svg" \
    --trace "$EX/artifacts/dag_trace.json" \
    -o "$EX/images/dag_interactive.html" -q

echo "== 2c. one slice of the data-path carry chain, close up ============="
# where alpha lives: slice i of the chain reads x[(i+7) mod 16]
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 'add_0~41' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/alpha_slice.svg" -q

echo "== 2d. one next-state cell of the y bank, close up =================="
# where beta lives: bit 0's next-state cell reads y[14] = y[(0-2) mod 16]
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 'i264~0' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/beta_cell.svg" -q

echo "== 3. the structural steps ========================================="
python3 "$EX/analysis.py" all --with-hal -o "$EX/artifacts" \
    > "$EX/artifacts/analysis_all.txt"

echo "== 4. what does the crypto identifier make of it? =================="
python3 tools/hal_crypto identify "$EX/speck_toy.vo" \
    -o "$EX/artifacts/identify.findings.json"

echo "== 5. does the *recovered* model reproduce the netlist? ============"
python3 tools/hal_agilex behavior "$EX/speck_toy.vo" \
    --reference "$EX/recovered_reference.py" --cycles 2000 \
    -o "$EX/artifacts/behavior_recovered.findings.json"

# Two negative controls, so that "it passed" means something.  Each moves one
# recovered rotation constant by one and MUST be caught; the run records at
# which cycle.  A bounded agreement with no negative control beside it says
# nothing about whether the bound was long enough.
sed 's/^ALPHA = 7$/ALPHA = 8/' "$EX/recovered_reference.py" \
    > "$SCRATCH/control_alpha8.py"
python3 tools/hal_agilex behavior "$EX/speck_toy.vo" \
    --reference "$SCRATCH/control_alpha8.py" --cycles 2000 \
    -o "$EX/artifacts/behavior_negative_control_alpha8.findings.json"
sed 's/^BETA = 2$/BETA = 3/' "$EX/recovered_reference.py" \
    > "$SCRATCH/control_beta3.py"
python3 tools/hal_agilex behavior "$EX/speck_toy.vo" \
    --reference "$SCRATCH/control_beta3.py" --cycles 2000 \
    -o "$EX/artifacts/behavior_negative_control_beta3.findings.json"

echo "== 6. one page over every findings document ========================"
python3 tools/hal_viz report "$EX"/artifacts/*.findings.json \
    --artifact "$EX/images/dag.svg" \
    --title "Agilex 3 walkthrough 11 - speck_toy" \
    -o "$EX/artifacts/report.html"

echo "== 7. the smoke check =============================================="
python3 "$EX/check.py" --with-hal | tee "$EX/artifacts/check.txt"

echo "done"
