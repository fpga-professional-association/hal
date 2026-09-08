#!/usr/bin/env bash
# Regenerate every image and artifact of this walkthrough, in the order
# guide.html presents them.  Run from the repository root, inside the build
# container (or anywhere with a built HAL and Graphviz).
#
#   export HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib
#   export PYTHONPATH=/work/build/lib
#   bash examples/agilex3_walkthroughs/10_crc8_checker/run_all.sh
#
# Everything it writes lands under the walkthrough's images/ and artifacts/.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
GL="$REPO/plugins/gate_libraries/definitions/AGILEX_TENNM.hgl"
NET="$HERE/netlist/netlist.anon.hal.v"
IMG="$HERE/images"
ART="$HERE/artifacts"

mkdir -p "$IMG" "$ART"
cd "$REPO"

echo "== 0. the export is inside the validated Agilex coverage, and matches the RTL"
python tools/hal_agilex --strict inventory "$HERE/netlist/crc8_checker.vo" \
    -o "$ART/inventory.findings.json" > /dev/null
python tools/hal_agilex --strict behavior "$HERE/netlist/crc8_checker.vo" \
    --reference "$HERE/reference.py" -o "$ART/behavior.findings.json" > /dev/null

echo "== 1. first contact: module tree and gate-level graph"
python tools/hal_viz module_tree "$NET" --gate-library "$GL" -o "$IMG/module_tree.svg"
python tools/hal_viz netlist_graph "$NET" --gate-library "$GL" \
    --module top --pin-labels -o "$IMG/netlist_graph.svg"

echo "== 2. which port is the clock, which is the reset"
python tools/hal_cdc discover "$NET" --gate-library "$GL" -o "$ART/clocks.json"

echo "== 3. register grouping, as DANA sees it"
python tools/hal_viz dataflow "$NET" --gate-library "$GL" -o "$ART/dataflow" \
    --min-group-size 2 --expected-size 8 || true
cp -f "$ART/dataflow/graph.svg" "$IMG/dataflow.svg" 2>/dev/null || true

echo "== 4-6. the walk itself"
python "$HERE/re_walk.py" all -o "$ART" | tee "$ART/re_walk.txt"
dot -Tsvg "$ART/recovered_lfsr.dot" -o "$IMG/recovered_lfsr.svg"

echo "== 7. the assertions, headless"
python "$HERE/check.py"

echo "== 8. one page over every findings document"
python tools/hal_viz report "$ART"/*.findings.json \
    --artifact "$IMG/recovered_lfsr.svg" --artifact "$IMG/netlist_graph.svg" \
    -o "$ART/report.html"
