#!/bin/bash
# Every analysis command the 12_present_sbox walkthrough shows, in order.
#
# Run it from the repository root inside a container that has a built HAL and
# the Graphviz `dot` binary:
#
#     HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib \
#     PYTHONPATH=/work/build/lib \
#         bash examples/agilex3_walkthroughs/12_present_sbox/run_analysis.sh
#
# Steps 1, 4-9 need neither HAL nor Graphviz -- tools/hal_agilex and
# tools/hal_crypto are plain standard library -- so they also run on a bare
# Python 3.  Only the pictures (steps 2 and 3) need a build.
#
# It writes into the walkthrough's own images/ and artifacts/ directories.
set -u

EX=examples/agilex3_walkthroughs/12_present_sbox
GL=plugins/gate_libraries/definitions/AGILEX_TENNM.hgl
VO=$EX/present_sbox.vo
NET=$EX/netlist.hal.v
export PYTHONPATH="${PYTHONPATH:-}:$(pwd)/tools"

mkdir -p "$EX/images" "$EX/artifacts" "$EX/artifacts/variants"

# keep a transcript next to the artifacts: the guide quotes it
exec > >(tee "$EX/artifacts/run_analysis.log") 2>&1

echo "== 1. coverage inventory -- is the export inside the modelled primitives?"
python3 tools/hal_agilex --strict inventory "$VO" \
    -o "$EX/artifacts/inventory.findings.json" >/dev/null
echo "-- inventory exit: $?"

echo "== 1b. the same export, rewritten so HAL can read it"
python3 tools/hal_agilex import "$VO" -o /tmp/netlist.hal.v >/dev/null
diff -q /tmp/netlist.hal.v "$NET" && echo "-- netlist.hal.v is up to date"

echo "== 2. module hierarchy"
python3 tools/hal_viz module_tree "$NET" -g "$GL" -o "$EX/images/module_tree.svg" -q

echo "== 2b. the gate-level graph around one datapath flip-flop"
# NOT the whole graph: at 379 gates Graphviz's `dot` did not finish the
# unscoped drawing in 600 s, and a 379-node hairball teaches nothing.  Depth 2
# around a datapath flop is already the whole design (the guide says why), so
# this is depth 1.  (`--engine sfdp --const-hub` does render the whole thing,
# in about four minutes, if you want to see the hairball for yourself.)
python3 tools/hal_viz netlist_graph "$NET" -g "$GL" --gate 'state[0]' --depth 1 \
    --show-boundary --pin-labels -o "$EX/images/netlist_graph.svg" -q

echo "== 2c. the whole graph levelled, feedback cut at the flops"
# --const-hub: 2292 per-pin tie-off stubs is most of this drawing's weight, and
# the one-hub form is 2.5x smaller for the same circuit.
python3 tools/hal_viz dag "$NET" -g "$GL" --const-hub -o "$EX/images/dag.svg" --html -q

echo "== 2d. ... and the same graph with values, one clock at a time"
# One whole encryption of the first published test vector: start held high,
# plaintext and key held at zero, 34 cycles = the clear, the load, 31 rounds and
# the stop.  Needs no HAL: clock_step joins the committed dag.svg to the trace.
python3 tools/hal_agilex trace "$VO" \
    --reference "$EX/recovered_reference.py" --cycles 34 \
    --hold start=1 --hold plaintext=0 --hold key_in=0 \
    -o "$EX/artifacts/dag_trace.json"
python3 tools/hal_viz clock_step "$EX/images/dag.svg" \
    --trace "$EX/artifacts/dag_trace.json" \
    -o "$EX/images/dag_interactive.html" -q

echo "== 3. one S-box cone, close up"
python3 tools/hal_viz netlist_graph "$NET" -g "$GL" --gate 'subs[0]' --depth 1 \
    --show-boundary --pin-labels -o "$EX/images/sbox_cone.svg" -q

echo "== 4. the crypto identification, all six passes"
python3 -m hal_crypto identify "$VO" -o "$EX/artifacts/identify.findings.json" >/dev/null
python3 -m hal_crypto sbox "$VO" -o "$EX/artifacts/sbox.findings.json" >/dev/null
python3 -m hal_crypto permutation "$VO" \
    -o "$EX/artifacts/permutation.findings.json" >/dev/null

echo "== 5. the same question of the two counterfactual exports in variants/"
python3 -m hal_crypto identify "$EX/variants/present_nokeep.vo" \
    -o "$EX/artifacts/variants/nokeep.identify.findings.json" >/dev/null
python3 -m hal_crypto identify "$EX/variants/present_textbook.vo" \
    -o "$EX/artifacts/variants/textbook.identify.findings.json" >/dev/null

echo "== 6. the reverse-engineering result itself"
python3 "$EX/analysis.py" --json "$EX/artifacts/analysis.json"

echo "== 7. does the reconstruction behave like the netlist? (600 cycles)"
python3 tools/hal_agilex behavior "$VO" \
    --reference "$EX/recovered_reference.py" --cycles 600 \
    -o "$EX/artifacts/behavior.findings.json" >/dev/null

echo "== 8. two negative controls, so that step 7 passing means something"
# one entry of the recovered substitution table, S[0xA]: F -> E
sed 's/    0x3, 0xE, 0xF, 0x8,/    0x3, 0xE, 0xE, 0x8,/' \
    "$EX/recovered_reference.py" > /tmp/control_sbox.py
python3 tools/hal_agilex behavior "$VO" --reference /tmp/control_sbox.py --cycles 200 \
    -o "$EX/artifacts/behavior_negative_control_sbox.findings.json" >/dev/null || true
sed 's/^ROUNDS = 31/ROUNDS = 30/' "$EX/recovered_reference.py" > /tmp/control_rounds.py
python3 tools/hal_agilex behavior "$VO" --reference /tmp/control_rounds.py --cycles 200 \
    -o "$EX/artifacts/behavior_negative_control_rounds.findings.json" >/dev/null || true

echo "== 9. one HTML page over every finding"
python3 tools/hal_viz report \
    "$EX/artifacts/inventory.findings.json" \
    "$EX/artifacts/identify.findings.json" \
    "$EX/artifacts/behavior.findings.json" \
    "$EX/artifacts/behavior_negative_control_sbox.findings.json" \
    "$EX/artifacts/behavior_negative_control_rounds.findings.json" \
    --artifact "$EX/images/sbox_cone.svg" \
    --artifact "$EX/images/netlist_graph.svg" \
    --title "12_present_sbox -- findings" \
    -o "$EX/artifacts/findings_report.html"

echo "== 10. the smoke check"
python3 "$EX/check.py"

echo "done"
