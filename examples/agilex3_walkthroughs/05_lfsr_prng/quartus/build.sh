#!/bin/sh
# Regenerate lfsr_prng.vo and netlist.hal.v from design.v.
#
# Quartus Prime Pro Edition 26.1.0 Build 110 03/26/2026 SC, installed at
# C:/altera_pro/26.1 on the machine this walkthrough was made on. Device
# A3CW135BM16AE6S (Agilex 3, package MBGA896) — the same device the fixtures
# in tools/hal_agilex/fixtures use, so the exports are directly comparable.
#
# Run this from THIS directory (examples/agilex3_walkthroughs/05_lfsr_prng/quartus).
# On Windows the binaries are .exe under C:/altera_pro/26.1/quartus/bin64.
set -eu

QUARTUS_BIN=${QUARTUS_BIN:-/c/altera_pro/26.1/quartus/bin64}

# 1. synthesis only — no fitting, so the export carries no placement noise
"$QUARTUS_BIN/quartus_syn" lfsr_prng -c lfsr_prng

# 2. post-synthesis Verilog export (Pro 26.1 has no VQM writer for this family)
"$QUARTUS_BIN/quartus_eda" --simulation --format=verilog --tool=questasim \
    --output_directory=simulation lfsr_prng -c lfsr_prng

# 3. publish the export and rewrite it into something HAL's Verilog parser reads
cp simulation/lfsr_prng.vo ../lfsr_prng.vo
( cd ../../../.. && \
  python tools/hal_agilex import \
      examples/agilex3_walkthroughs/05_lfsr_prng/lfsr_prng.vo \
      -o examples/agilex3_walkthroughs/05_lfsr_prng/netlist.hal.v && \
  python tools/hal_agilex inventory \
      examples/agilex3_walkthroughs/05_lfsr_prng/lfsr_prng.vo \
      -o examples/agilex3_walkthroughs/05_lfsr_prng/artifacts/inventory.findings.json )

# 4. Quartus scratch state is not committed
rm -rf db incremental_db output_files qdb tmp-clearbox simulation
