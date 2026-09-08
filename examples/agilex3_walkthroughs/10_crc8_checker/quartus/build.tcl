# Create the Quartus Prime Pro project for the walkthrough and synthesise it.
#
#   quartus_sh -t build.tcl
#
# Run from a scratch directory that contains a copy of design.v.  This script
# only *creates* the project and writes the .qsf; synthesis and the EDA netlist
# export are the two command-line steps documented in guide.html, kept separate
# so that each one's log can be shown:
#
#   quartus_syn crc8_checker -c crc8_checker
#   quartus_eda --simulation --format=verilog --tool=questasim \
#               --output_directory=simulation crc8_checker -c crc8_checker
#
# Device and flow are taken verbatim from tools/hal_agilex/fixtures/*/MANIFEST.json
# so that the export lands inside the validated AGILEX_TENNM coverage.

package require ::quartus::project

if {[project_exists crc8_checker]} {
    project_open crc8_checker -revision crc8_checker
} else {
    project_new crc8_checker -revision crc8_checker
}

set_global_assignment -name FAMILY "Agilex 3"
set_global_assignment -name DEVICE A3CW135BM16AE6S
set_global_assignment -name TOP_LEVEL_ENTITY crc8_checker
set_global_assignment -name VERILOG_FILE design.v
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output_files

project_close
