#!/bin/sh
# Every command the walkthrough runs, in the order guide.html runs them.
#
# Run from the repository root, in an environment with a built HAL and the
# Graphviz `dot` binary:
#
#     HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib \
#     PYTHONPATH=/work/build/lib:tools \
#         sh examples/agilex3_walkthroughs/08_shift_debouncer/run_analysis.sh
#
# Outputs land in the example's images/ and artifacts/ directories.
set -e

EX=examples/agilex3_walkthroughs/08_shift_debouncer
GL=plugins/gate_libraries/definitions/AGILEX_TENNM.hgl
mkdir -p "$EX/images" "$EX/artifacts"

echo "== 0. what is in the export at all ================================="
# NOTE: the committed inventory/recognize/behavior findings are NOT rewritten
# here.  They record the sha256 of the .vo as it was when they were made, and a
# checkout with CRLF line endings hashes differently.  Regenerate them
# deliberately, not as a side effect of re-running the analysis.
python3 tools/hal_agilex inventory "$EX/shift_debouncer.vo" -o /tmp/inventory_rerun.json
head -40 /tmp/inventory_rerun.json

echo "== 1. the two pictures every walkthrough starts with ==============="
python3 tools/hal_viz module_tree "$EX/netlist.hal.v" -g "$GL" \
    -o "$EX/images/module_tree.svg" -q
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --show-boundary --pin-labels -o "$EX/images/netlist_graph.svg" -q

echo "== 2. two close-ups: one counter bit slice, and the hysteresis bit =="
# NOTE: --depth 2 is useless on a design this small -- the depth-2
# neighbourhood of any flop here is all 16 gates, i.e. the whole netlist.
# Depth 1 around a next-state cell is the real close-up: 7 gates each.
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 'cnt[3]~0' --depth 1 --show-boundary --pin-labels \
    -o "$EX/images/counter_cone.svg" -q
python3 tools/hal_viz netlist_graph "$EX/netlist.hal.v" -g "$GL" \
    --gate 'i38~0' --depth 1 --show-boundary --pin-labels \
    -o "$EX/images/hysteresis_cone.svg" -q

echo "== 3. the structural steps ========================================="
python3 "$EX/analyze.py" --repo . -o "$EX/artifacts" > "$EX/artifacts/analyze_stdout.txt"

echo "== 4. the behavioural probe ========================================"
python3 "$EX/probe.py" -o "$EX/artifacts" --images "$EX/images" > /dev/null

echo "== 5. netlist versus the RTL reference ============================="
python3 tools/hal_agilex behavior "$EX/shift_debouncer.vo" \
    --reference "$EX/reference.py" --cycles 200 -o /tmp/behavior_rerun.json

# Two negative controls, so that "it passed" means something.
#  (a) the saturation clamp removed -- caught almost immediately;
#  (b) the clamp kept but placed one count early -- NOT caught by the 200-cycle
#      bound the shipped findings document uses, and caught at cycle 280 by a
#      longer run.  Both outcomes are the point.
sed 's/ and not at_max:/:/; s/ and not at_min:/:/' "$EX/reference.py" > /tmp/ctl_nosat.py
python3 tools/hal_agilex behavior "$EX/shift_debouncer.vo" \
    --reference /tmp/ctl_nosat.py --cycles 200 \
    -o "$EX/artifacts/09_negative_control_nosat.json"
sed 's/^CNT_MAX = 0xF/CNT_MAX = 0xE/' "$EX/reference.py" > /tmp/ctl_max14.py
python3 tools/hal_agilex behavior "$EX/shift_debouncer.vo" \
    --reference /tmp/ctl_max14.py --cycles 200 \
    -o "$EX/artifacts/09_negative_control_max14_200.json"
python3 tools/hal_agilex behavior "$EX/shift_debouncer.vo" \
    --reference /tmp/ctl_max14.py --cycles 2000 \
    -o "$EX/artifacts/09_negative_control_max14_2000.json"

echo "== 6. the smoke check =============================================="
python3 "$EX/check.py" --with-hal | tee "$EX/artifacts/check.txt"

echo "done"
