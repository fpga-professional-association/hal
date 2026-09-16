# Regenerate trivium_stream.vo from design.v.
#
#   cd <a scratch directory>
#   cp <example>/design.v .
#   cp <example>/quartus/trivium_stream.qpf <example>/quartus/trivium_stream.qsf .
#   quartus_sh -t build.tcl
#
# This script only writes the project files and reports the environment; the
# two flow steps themselves are run as separate executables so that their exit
# codes and reports are the ordinary Quartus ones:
#
#   quartus_syn trivium_stream -c trivium_stream
#   quartus_eda --simulation --format=verilog --tool=questasim \
#               --output_directory=simulation trivium_stream -c trivium_stream
#
# Then, from the repository root:
#
#   python tools/hal_agilex import simulation/trivium_stream.vo -o netlist.hal.v

package require ::quartus::project
package require ::quartus::flow

set revision trivium_stream

if {[project_exists $revision]} {
    project_open $revision -revision $revision
} else {
    project_new $revision -revision $revision -overwrite
}

set_global_assignment -name FAMILY "Agilex 3"
set_global_assignment -name DEVICE A3CW135BM16AE6S
set_global_assignment -name TOP_LEVEL_ENTITY trivium_stream
set_global_assignment -name VERILOG_FILE design.v
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output_files

# See trivium_stream.qsf for why these two are here.
set_global_assignment -name ALLOW_SYNCH_CTRL_USAGE OFF
set_global_assignment -name AUTO_SHIFT_REGISTER_RECOGNITION OFF

export_assignments

puts "quartus version : $quartus(version)"
puts "family list     : [get_family_list]"
puts "revision        : $revision"

project_close
