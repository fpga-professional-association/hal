#!/bin/sh
# Every HAL command the walkthrough runs, in the order the guide runs them.
#
# Run from the repository root, in an environment with a built HAL and the
# Graphviz `dot` binary:
#
#     HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib \
#     PYTHONPATH=/work/build/lib \
#         sh examples/agilex3_walkthroughs/01_blinky_counter/run_analyses.sh
#
# Outputs land in the example's images/ and artifacts/ directories.
set -e

EX=examples/agilex3_walkthroughs/01_blinky_counter
GL=plugins/gate_libraries/definitions/AGILEX_TENNM.hgl
mkdir -p "$EX/images" "$EX/artifacts"

echo "== 0. import-time coverage and elaboration =========================="
python3 tools/hal_agilex inventory "$EX/blinky_counter.vo" \
    -o "$EX/artifacts/hal_agilex_inventory.json"
python3 tools/hal_agilex elaborate "$EX/netlist.hal.v" \
    --gate-library "$GL" --hal-lib "${HAL_PY_PATH:-/work/build/lib}" \
    > "$EX/artifacts/hal_agilex_elaborate.json"

echo "== 1. the two pictures every walkthrough starts with ================"
python3 tools/hal_viz module_tree "$EX/netlist.hal.v" -g "$GL" \
    -o "$EX/images/module_tree.svg" -q
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --module top_module --show-boundary -o "$EX/images/netlist_graph.svg" -q

echo "== 2. one bit slice, close up ======================================="
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 'count[7]' --depth 2 --show-boundary --pin-labels \
    -o "$EX/images/slice_count7.svg" -q

echo "== 3. the clock tree ==============================================="
python3 tools/hal_viz clock_tree "$EX/netlist.hal.v" -g "$GL" \
    -o "$EX/images/clock_tree.svg" -q || echo "(clock_tree unavailable)"

echo "== 4. DANA register grouping ======================================="
# NOTE: -f svg is not optional here.  `hal_viz dataflow` leaves --format at None
# when it is not given and then does "graph." + args.format, which raises
# TypeError after the analysis has already succeeded.  The other subcommands
# default to svg; this one does not.
python3 tools/hal_viz dataflow "$EX/netlist.hal.v" -g "$GL" \
    -o "$EX/images/dataflow" -f svg -q

echo "== 5. the structural steps ========================================="
python3 "$EX/analysis.py" all -o "$EX/artifacts" \
    --dot "$EX/images/carry_chain.dot" > "$EX/artifacts/analysis_all.txt"
dot -Tsvg "$EX/images/carry_chain.dot" -o "$EX/images/carry_chain.svg"

echo "== 6. what the other tools say about this design ==================="
python3 tools/hal_agilex recognize "$EX/blinky_counter.vo" \
    -o "$EX/artifacts/hal_agilex_recognize.json"
python3 tools/hal_cdc discover "$EX/netlist.hal.v" --gate-library "$GL" \
    -o "$EX/artifacts/cdc_declarations.json"
python3 tools/hal_cdc audit "$EX/netlist.hal.v" --gate-library "$GL" \
    -d "$EX/artifacts/cdc_declarations.json" \
    -o "$EX/artifacts/cdc.findings.json" \
    --dot "$EX/images/cdc_domains.dot" || true
[ -f "$EX/images/cdc_domains.dot" ] && \
    dot -Tsvg "$EX/images/cdc_domains.dot" -o "$EX/images/cdc_domains.svg"

echo "== 7. does the recovered model reproduce the netlist? =============="
# Bounded netlist-vs-model simulation using the validated primitive semantics.
python3 tools/hal_agilex behavior "$EX/blinky_counter.vo" \
    --reference "$EX/recovered_reference.py" --cycles 1200 \
    -o "$EX/artifacts/hal_agilex_behavior.json"

# Two negative controls, so that "it passed" means something.  The first is a
# wrong increment and IS caught; the second is a wrong width and is NOT, because
# the top bits never move inside any simulatable bound.  Both outcomes are the
# point.
sed 's/+ 1) & MASK/+ 2) \& MASK/' "$EX/recovered_reference.py" > /tmp/ctl_plus2.py
python3 tools/hal_agilex behavior "$EX/blinky_counter.vo" \
    --reference /tmp/ctl_plus2.py --cycles 200 \
    -o "$EX/artifacts/hal_agilex_behavior_negative_control_plus2.json" || true
sed 's/^WIDTH = 24/WIDTH = 23/' "$EX/recovered_reference.py" > /tmp/ctl_w23.py
python3 tools/hal_agilex behavior "$EX/blinky_counter.vo" \
    --reference /tmp/ctl_w23.py --cycles 1200 \
    -o "$EX/artifacts/hal_agilex_behavior_negative_control_width23.json" || true

echo "== 8. the smoke check =============================================="
python3 "$EX/check.py" | tee "$EX/artifacts/check.txt"

echo "done"
