#!/bin/bash
# Every analysis command the 05_lfsr_prng walkthrough shows, in order.
#
# Run it from the repository root inside a container that has a built HAL and
# the Graphviz `dot` binary:
#
#     HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib \
#     PYTHONPATH=/work/build/lib \
#         bash examples/agilex3_walkthroughs/05_lfsr_prng/run_analysis.sh
#
# It writes into the walkthrough's own images/ and artifacts/ directories.
set -u

EX=examples/agilex3_walkthroughs/05_lfsr_prng
GL=plugins/gate_libraries/definitions/AGILEX_TENNM.hgl
NET=$EX/netlist.hal.v
export PYTHONPATH="${PYTHONPATH:-}:$(pwd)/tools"

mkdir -p "$EX/images" "$EX/artifacts"

# keep a transcript next to the artifacts: the guide quotes it
exec > >(tee "$EX/artifacts/run_analysis.log") 2>&1

echo "== 1. coverage inventory (no HAL needed)"
python3 tools/hal_agilex inventory "$EX/lfsr_prng.vo" \
    -o "$EX/artifacts/inventory.findings.json" >/dev/null

echo "== 2. module hierarchy"
python3 tools/hal_viz module_tree "$NET" -g "$GL" -o "$EX/images/module_tree.svg" -q

echo "== 3. the whole gate-level graph (33 gates -- it fits on one page)"
python3 tools/hal_viz netlist_graph "$NET" -g "$GL" --pin-labels \
    -o "$EX/images/netlist_graph.svg" -q

echo "== 4. the cone around the only wide combinational cell"
python3 tools/hal_viz netlist_graph "$NET" -g "$GL" --gate feedback --depth 2 \
    --show-boundary --pin-labels -o "$EX/images/feedback_cone.svg" -q

echo "== 5. DANA register grouping"
python3 tools/hal_viz dataflow "$NET" -g "$GL" -o "$EX/images/dataflow" -q
echo "-- groups DANA reported:"
cat "$EX/images/dataflow/groups.txt" 2>/dev/null | head -40

echo "== 6. strongly connected components (graph_algorithm / igraph)"
python3 "$EX/scc.py" > "$EX/artifacts/scc.txt"
tail -n 5 "$EX/artifacts/scc.txt"
dot -Tsvg "$EX/artifacts/scc.dot" -o "$EX/images/scc.svg"

echo "== 7. clock/reset domain screening"
python3 tools/hal_cdc discover "$NET" --gate-library "$GL" \
    -o "$EX/artifacts/cdc.declarations.json"
python3 tools/hal_cdc audit "$NET" --gate-library "$GL" \
    --declarations "$EX/artifacts/cdc.declarations.json" \
    -o "$EX/artifacts/cdc.findings.json" --dot "$EX/artifacts/cdc.domains.dot"

echo "== 7b. the wrong tool, on purpose: FSM recovery over a 16-bit state"
python3 tools/hal_fsm analyze "$NET" --gate-library "$GL" \
    -o "$EX/artifacts/hal_fsm" --timeout 180 --print-summary 2>&1 | tail -25
echo "-- hal_fsm exit: $?"

echo "== 8. the reverse-engineering result itself"
python3 "$EX/check.py" --write-artifacts

echo "== 9. render the recovered structure"
dot -Tsvg "$EX/artifacts/recovered_lfsr.dot" -o "$EX/images/recovered_lfsr.svg"

echo "== 10. does the reconstruction behave like the netlist?"
python3 tools/hal_agilex behavior "$EX/lfsr_prng.vo" \
    --reference "$EX/recovered_model.py" --cycles 400 \
    -o "$EX/artifacts/behavior.findings.json" >/dev/null

echo "== 11. one HTML page over every finding"
python3 tools/hal_viz report \
    "$EX/artifacts/inventory.findings.json" \
    "$EX/artifacts/lfsr.findings.json" \
    "$EX/artifacts/behavior.findings.json" \
    "$EX/artifacts/cdc.findings.json" \
    --artifact "$EX/images/recovered_lfsr.svg" \
    --artifact "$EX/images/netlist_graph.svg" \
    --title "05_lfsr_prng -- findings" \
    -o "$EX/artifacts/findings_report.html"

echo "done"
