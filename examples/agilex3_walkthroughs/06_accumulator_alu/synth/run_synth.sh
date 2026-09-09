#!/usr/bin/env bash
# Regenerate netlist/accumulator_alu.vo and netlist/netlist.hal.v from design.v.
#
# Needs a Quartus Prime Pro 26.1 installation with the Agilex 3 device support
# installed.  Run from this directory:
#
#     QUARTUS_BIN=/c/altera_pro/26.1/quartus/bin64 ./run_synth.sh
#
# Everything Quartus writes lands in ./work, which is scratch and is deleted at
# the end; only the two netlist files are kept.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/.." && pwd)"
repo="$(cd "$root/../../.." && pwd)"
QUARTUS_BIN="${QUARTUS_BIN:-/c/altera_pro/26.1/quartus/bin64}"

rm -rf "$here/work"
mkdir -p "$here/work" "$root/netlist"
cp "$root/design.v" "$here/work/"
cp "$here/accumulator_alu.qpf" "$here/accumulator_alu.qsf" "$here/work/"

cd "$here/work"
"$QUARTUS_BIN/quartus_syn" accumulator_alu -c accumulator_alu
"$QUARTUS_BIN/quartus_eda" --simulation --format=verilog --tool=questasim \
    --output_directory=simulation accumulator_alu -c accumulator_alu

cp simulation/accumulator_alu.vo "$root/netlist/accumulator_alu.vo"

# The .vo is not directly readable by HAL's Verilog front end; hal_agilex
# rewrites it (tri1 -> wire+assign, pin inversions absorbed into lut_mask,
# unconnected pins dropped).  Identical rewrite as the hal_agilex fixtures use.
python "$repo/tools/hal_agilex" import "$root/netlist/accumulator_alu.vo" \
    -o "$root/netlist/netlist.hal.v"
python "$repo/tools/hal_agilex" inventory "$root/netlist/accumulator_alu.vo" \
    -o "$root/artifacts/inventory.json" --strict

cd "$here"
rm -rf "$here/work"
