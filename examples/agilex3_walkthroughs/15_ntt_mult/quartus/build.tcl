# Regenerate ntt_mult.vo from design.v.
#
#   cd <a scratch directory>
#   cp <example>/design.v .
#   cp <example>/quartus/ntt_mult.qpf <example>/quartus/ntt_mult.qsf .
#   quartus_sh -t build.tcl
#
# This script only writes the project files and reports the environment; the
# two flow steps themselves are run as separate executables so that their exit
# codes and reports are the ordinary Quartus ones:
#
#   quartus_syn ntt_mult -c ntt_mult
#   quartus_eda --simulation --format=verilog --tool=questasim \
#               --output_directory=simulation ntt_mult -c ntt_mult
#
# Then, from the repository root:
#
#   python tools/hal_agilex import simulation/ntt_mult.vo -o netlist.hal.v
#
# The counterfactual export of section 8.1 is the same three commands with the
# revision name `ntt_dsp` and no change to design.v at all; see
# quartus/ntt_dsp.qsf, which defines ALLOW_DSP and drops the two DSP
# assignments.  Check the result with
#
#   grep -c tennm_mac simulation/ntt_dsp.vo     # 1
#   grep -c tennm_mac simulation/ntt_mult.vo    # 0

package require ::quartus::project
package require ::quartus::flow

set revision ntt_mult

if {[project_exists $revision]} {
    project_open $revision -revision $revision
} else {
    project_new $revision -revision $revision -overwrite
}

set_global_assignment -name FAMILY "Agilex 3"
set_global_assignment -name DEVICE A3CW135BM16AE6S
set_global_assignment -name TOP_LEVEL_ENTITY ntt_mult
set_global_assignment -name VERILOG_FILE design.v
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output_files

# See ntt_mult.qsf for why these three are here.
set_global_assignment -name ALLOW_SYNCH_CTRL_USAGE OFF
set_global_assignment -name AUTO_DSP_RECOGNITION OFF
set_global_assignment -name DSP_BLOCK_BALANCING "LOGIC ELEMENTS"

export_assignments

puts "quartus version : $quartus(version)"
puts "family list     : [get_family_list]"
puts "revision        : $revision"

project_close
