#!/bin/sh
# Every command walkthrough 16 runs, in the order the guide runs them.
#
# Run from the repository root, in an environment with a built HAL and the
# Graphviz `dot` binary:
#
#     HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib \
#     PYTHONPATH=/work/build/lib \
#         sh examples/agilex3_walkthroughs/16_mystery_cores/run_analysis.sh
#
# Outputs land in the example's images/ and artifacts/ directories.
#
# Sections 0-4 are the *blind* half and touch nothing but cores/.  Sections 5-7
# are the reveal and are the first place ground_truth/ is opened.
set -e

EX=examples/agilex3_walkthroughs/16_mystery_cores
GL=plugins/gate_libraries/definitions/AGILEX_TENNM.hgl
SCRATCH="${TMPDIR:-/tmp}/mystery_cores_controls"
mkdir -p "$EX/images" "$EX/artifacts" "$SCRATCH"

echo "== 0. is every blinded export inside the validated coverage? ========"
# --strict on all five: a core that came back `unsupported` would mean the
# exercise was set with a netlist the tools cannot read, which is a different
# experiment from the one this walkthrough is running.
for core in core_a core_b core_c core_d core_e; do
    python3 tools/hal_agilex --strict inventory "$EX/cores/$core.anon.hal.v" \
        -o "$EX/artifacts/inventory_$core.findings.json"
done

echo "== 1. what does the tool alone say about each? ======================"
# Recorded before the method runs, and deliberately not read by steps 1-4.
for core in core_a core_b core_c core_d core_e; do
    python3 tools/hal_crypto identify "$EX/cores/$core.anon.hal.v" \
        -o "$EX/artifacts/identify_$core.findings.json"
done

echo "== 2. the picture the census is read off: one DAG per core =========="
# --const-hub on the three big ones, for the reason the series README gives:
# at 866, 611 and 1209 consuming pins the per-pin tie-off stubs are most of the
# file and none of the information.  core_e is `-f none`: at 1209 nodes and 43
# levels `dot` does not finish (walkthrough 15 measured over an hour), so the
# counts and the .dot are committed and the drawing is not.
python3 tools/hal_viz dag "$EX/cores/core_a.anon.hal.v" -g "$GL" --const-hub \
    --max-gates 900 -o "$EX/images/dag_core_a.svg" --html -q
python3 tools/hal_viz dag "$EX/cores/core_b.anon.hal.v" -g "$GL" \
    --max-gates 400 -o "$EX/images/dag_core_b.svg" --html -q
python3 tools/hal_viz dag "$EX/cores/core_c.anon.hal.v" -g "$GL" --const-hub \
    --max-gates 700 -o "$EX/images/dag_core_c.svg" --html -q
python3 tools/hal_viz dag "$EX/cores/core_d.anon.hal.v" -g "$GL" --const-hub \
    --max-gates 400 -o "$EX/images/dag_core_d.svg" --html -q
python3 tools/hal_viz dag "$EX/cores/core_e.anon.hal.v" -g "$GL" --const-hub \
    --max-gates 1300 -f none -o "$EX/images/dag_core_e.svg" --html -q

echo "== 3. the three close-ups the guide argues from ====================="
# core_b's counter slice and core_d's adder slice are the same object at this
# magnification -- an arithmetic-mode ALM with a cout -> cin wire.  The
# difference is one operand, and that is the point of both pictures.
python3 tools/hal_viz netlist_graph "$EX/cores/core_b.anon.hal.v" -g "$GL" \
    --gate 'u94' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/cell_counter_core_b.svg" -q
python3 tools/hal_viz netlist_graph "$EX/cores/core_d.anon.hal.v" -g "$GL" \
    --gate 'u19' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/cell_adder_core_d.svg" -q
# ... and one of core_a's forty parity cells: five register outputs, one XOR.
python3 tools/hal_viz netlist_graph "$EX/cores/core_a.anon.hal.v" -g "$GL" \
    --gate 'u728' --depth 1 --show-boundary --pin-labels --const-hub \
    -o "$EX/images/cell_parity_core_a.svg" -q
# The module tree of the smallest core, to show what a flat export looks like.
python3 tools/hal_viz module_tree "$EX/cores/core_b.anon.hal.v" -g "$GL" \
    -o "$EX/images/module_tree_core_b.svg" -q

echo "== 4. the blind pass: six steps per core, then the call ============="
python3 "$EX/analysis.py" blind -o "$EX/artifacts" \
    > "$EX/artifacts/blind_all.txt"

echo "== 5. THE REVEAL: does the named export behave like the answer key? ="
# This is the first command in the file that opens ground_truth/.  Each core is
# simulated against its family's reference model; the bound per core is the one
# its own walkthrough established, except core_b, which is new.
python3 tools/hal_agilex behavior "$EX/ground_truth/exports/core_a.vo" \
    --reference "$EX/ground_truth/references/core_a_reference.py" --cycles 600 \
    -o "$EX/artifacts/behavior_core_a.findings.json"
python3 tools/hal_agilex behavior "$EX/ground_truth/exports/core_b.vo" \
    --reference "$EX/ground_truth/references/core_b_reference.py" --cycles 6000 \
    -o "$EX/artifacts/behavior_core_b.findings.json"
python3 tools/hal_agilex behavior "$EX/ground_truth/exports/core_c.vo" \
    --reference "$EX/ground_truth/references/core_c_reference.py" --cycles 3600 \
    -o "$EX/artifacts/behavior_core_c.findings.json"
python3 tools/hal_agilex behavior "$EX/ground_truth/exports/core_d.vo" \
    --reference "$EX/ground_truth/references/core_d_reference.py" --cycles 2000 \
    -o "$EX/artifacts/behavior_core_d.findings.json"
python3 tools/hal_agilex behavior "$EX/ground_truth/exports/core_e.vo" \
    --reference "$EX/ground_truth/references/core_e_reference.py" --cycles 600 \
    -o "$EX/artifacts/behavior_core_e.findings.json"

echo "== 5b. ... and six negative controls, so that 'it agreed' means =====
==     something.  Five are caught.  The sixth is not, on purpose."
sed 's/^    (1, 44, 10, 45, 2),$/    (1, 44, 10, 45, 3),/' \
    "$EX/ground_truth/references/core_a_reference.py" > "$SCRATCH/control_a.py"
python3 tools/hal_agilex behavior "$EX/ground_truth/exports/core_a.vo" \
    --reference "$SCRATCH/control_a.py" --cycles 600 \
    -o "$EX/artifacts/behavior_control_core_a.findings.json"
sed 's/^DELIMITER = 0x7E$/DELIMITER = 0x7F/' \
    "$EX/ground_truth/references/core_b_reference.py" > "$SCRATCH/control_b.py"
python3 tools/hal_agilex behavior "$EX/ground_truth/exports/core_b.vo" \
    --reference "$SCRATCH/control_b.py" --cycles 6000 \
    -o "$EX/artifacts/behavior_control_core_b.findings.json"
# The one that is *not* caught: a frame cannot last 65536 cycles while bits keep
# arriving, so the watchdog is unreachable and its constant is unobservable.
# A clean pass here is the coverage hole, stated rather than hidden.
sed 's/^AGE_LIMIT = 0xFFFF$/AGE_LIMIT = 0xFFFE/' \
    "$EX/ground_truth/references/core_b_reference.py" > "$SCRATCH/control_b2.py"
python3 tools/hal_agilex behavior "$EX/ground_truth/exports/core_b.vo" \
    --reference "$SCRATCH/control_b2.py" --cycles 6000 \
    -o "$EX/artifacts/behavior_control_unreachable_core_b.findings.json"
sed 's/\^ _s(s, 171)$/^ _s(s, 170)/' \
    "$EX/ground_truth/references/core_c_reference.py" > "$SCRATCH/control_c.py"
python3 tools/hal_agilex behavior "$EX/ground_truth/exports/core_c.vo" \
    --reference "$SCRATCH/control_c.py" --cycles 3600 \
    -o "$EX/artifacts/behavior_control_core_c.findings.json"
sed 's/^ALPHA = 7$/ALPHA = 8/' \
    "$EX/ground_truth/references/core_d_reference.py" > "$SCRATCH/control_d.py"
python3 tools/hal_agilex behavior "$EX/ground_truth/exports/core_d.vo" \
    --reference "$SCRATCH/control_d.py" --cycles 2000 \
    -o "$EX/artifacts/behavior_control_core_d.findings.json"
sed 's/^Q = 257$/Q = 251/' \
    "$EX/ground_truth/references/core_e_reference.py" > "$SCRATCH/control_e.py"
python3 tools/hal_agilex behavior "$EX/ground_truth/exports/core_e.vo" \
    --reference "$SCRATCH/control_e.py" --cycles 600 \
    -o "$EX/artifacts/behavior_control_core_e.findings.json"

echo "== 6. the score table, and the named-versus-blinded control ========="
python3 "$EX/analysis.py" reveal -o "$EX/artifacts" \
    > "$EX/artifacts/reveal.txt"

echo "== 7. one page over every findings document ========================="
python3 tools/hal_viz report "$EX"/artifacts/*.findings.json \
    --artifact "$EX/images/dag_core_a.svg" \
    --title "Agilex 3 walkthrough 16 - mystery cores" \
    -o "$EX/artifacts/report.html"

echo "== 8. the smoke check =============================================="
python3 "$EX/check.py" | tee "$EX/artifacts/check.txt"

echo "done"
