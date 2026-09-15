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
# the .vo is passed repo-relative so the findings documents record a stable path
# and the digest of the repository file, not of a per-machine absolute copy
REL="${HERE#"$REPO"/}"
python3 tools/hal_agilex --strict inventory "$REL/netlist/crc8_checker.vo" \
    -o "$ART/inventory.findings.json" > /dev/null
python3 tools/hal_agilex --strict behavior "$REL/netlist/crc8_checker.vo" \
    --reference "$HERE/reference.py" -o "$ART/behavior.findings.json" > /dev/null

echo "== 1. first contact: module tree and gate-level graph"
python3 tools/hal_viz module_tree "$NET" --gate-library "$GL" -o "$IMG/module_tree.svg"
python3 tools/hal_viz netlist_graph "$NET" --gate-library "$GL" \
    --module top --pin-labels -o "$IMG/netlist_graph.svg"
# ...and the same graph levelled, feedback cut at the flops: 3 levels.
python3 tools/hal_viz dag "$NET" --gate-library "$GL" \
    -o "$IMG/dag.svg" --html
# ...and the same levelled graph with values on it, one clock at a time.  The
# drawing is of the anonymised netlist, so the join needs the map anonymize.py
# wrote next to it; the page says which side each name comes from.
# The export and the model are passed repo-relative for the same reason step 0
# does it: the trace records the path it read, and it has to be a stable one.
python3 tools/hal_agilex trace "$REL/netlist/crc8_checker.vo" \
    --reference "$REL/reference.py" --cycles 32 --hold en=1 \
    -o "$ART/dag_trace.json" > /dev/null
python3 tools/hal_viz clock_step "$IMG/dag.svg" --trace "$ART/dag_trace.json" \
    --name-map "$HERE/netlist/anonymize_map.json" \
    -o "$IMG/dag_interactive.html"

echo "== 2. which port is the clock, which is the reset"
python3 tools/hal_cdc discover "$NET" --gate-library "$GL" -o "$ART/clocks.json"

echo "== 3. register grouping, as DANA sees it"
python3 tools/hal_viz dataflow "$NET" --gate-library "$GL" -o "$ART/dataflow" \
    --min-group-size 2 --expected-size 8 || true
cp -f "$ART/dataflow/graph.svg" "$IMG/dataflow.svg" 2>/dev/null || true

echo "== 4-6. the walk itself"
python3 "$HERE/re_walk.py" all -o "$ART" | tee "$ART/re_walk.txt"
dot -Tsvg "$ART/recovered_lfsr.dot" -o "$IMG/recovered_lfsr.svg"

echo "== 7. the assertions, headless"
python3 "$HERE/check.py"

echo "== 8. one page over every findings document"
python3 tools/hal_viz report "$ART"/*.findings.json \
    --artifact "$IMG/recovered_lfsr.svg" --artifact "$IMG/netlist_graph.svg" \
    -o "$ART/report.html"
