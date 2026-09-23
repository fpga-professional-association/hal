#!/usr/bin/env bash
# Regenerate Act 2 of this example -- the HAL-native analysis and every picture
# the README's "Act 2" section embeds.  Run from the repository root, inside the
# build container (HAL has no Windows build):
#
#   export HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib
#   export PYTHONPATH=/work/build/lib
#   bash examples/janestreet_asic_2026/tools/run_hal_analysis.sh
#
# Needs artifacts/puzzle_netlist.{v,json} (gitignored -- rebuild with
# tools/gds2netlist.py) and Graphviz.  Everything it writes lands under the
# example's artifacts/ and images/.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLE="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$EXAMPLE/../.." && pwd)"
GL="$REPO/plugins/gate_libraries/definitions/SKY130_FD_SC_HD.hgl"
NET="$EXAMPLE/artifacts/puzzle_netlist.v"
IMG="$EXAMPLE/images"
ART="$EXAMPLE/artifacts"
SUCCESS_FF="dfrtp_2_172040_280160"   # the flop that drives the `success` port

mkdir -p "$IMG" "$ART"
cd "$REPO"

echo "== 0. the gate library really loads both extracted netlists"
python3 "$HERE/hal_load_check.py" --output "$ART/hal_load_check.txt" > /dev/null

echo "== 1. hal_fsm: is any of this a state machine? (read back by step 1-7 below)"
python3 tools/hal_fsm analyze "$NET" --gate-library "$GL" \
    --targets 2 -o "$ART/hal_fsm" > /dev/null

echo "== 2. census, FF signatures, SCCs, register graph, DANA, success cone, FSM"
python3 "$HERE/hal_walkthrough.py" --quiet

echo "== 3. DANA's own register-group drawing"
# hal_viz dataflow writes a *directory*; only its rendered graph is committed,
# the group listing is already artifacts/hal_dataflow.txt from step 2.
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
python3 tools/hal_viz dataflow "$NET" -g "$GL" -o "$SCRATCH/dataflow" \
    --min-group-size 2
cp -f "$SCRATCH/dataflow/graph.svg" "$IMG/hal_dataflow.svg"
cp -f "$SCRATCH/dataflow/graph.dot" "$IMG/hal_dataflow.dot"

echo "== 4. the clock tree (16 leaf nets, one root)"
# clock_tree_extractor logs one `invalid coordinate format: stoi` error per gate
# here -- it wants a placed netlist's generic X/Y data, which a GDS extraction
# does not carry.  The tree it emits is correct; the errors are noise.
python3 tools/hal_viz clock_tree "$NET" -g "$GL" -o "$IMG/hal_clock_tree.svg" \
    2>&1 | grep -v "invalid coordinate format"

echo "== 5. the success cone, levelled"
python3 tools/hal_viz dag "$NET" -g "$GL" --gate "$SUCCESS_FF" \
    --depth 6 --direction predecessors -o "$IMG/hal_success_cone.svg"
python3 tools/hal_viz netlist_graph "$NET" -g "$GL" --gate "$SUCCESS_FF" \
    --depth 2 --show-boundary --pin-labels -o "$IMG/hal_success_closeup.svg"

echo "== 6. the flip-flop dependency graph: DANA boxes, flop_map colours"
# fdp, not dot: with 35 clusters `dot` lays this out as a 1312x5257pt sliver.
fdp -Tsvg "$IMG/hal_register_graph.dot" -o "$IMG/hal_register_graph.svg"

echo "== 7. hal_fsm's recovered state diagram"
dot -Tsvg "$ART/hal_fsm/state-diagram-machine01.dot" -o "$IMG/hal_fsm_machine01.svg"

echo "== 8. the whole-netlist levelled DAG -- counts only, see the README"
# The drawing is 4063x77164pt and unreadable: 880 of the 1612 nodes are
# tap/decap fill with degree zero, and they stack into one level-0 column.
python3 tools/hal_viz dag "$NET" -g "$GL" -o "$SCRATCH/hal_dag.svg" \
    -f none --max-gates 2000 | grep -E "level\(s\)" || true

echo "done -- images in $IMG, artifacts in $ART"
