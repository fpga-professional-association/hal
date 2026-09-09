# Regenerate blinky_counter.vo from design.v.
#
#   cd <a scratch directory>
#   cp <example>/design.v ./blinky_counter.v
#   cp <example>/quartus/blinky_counter.qpf <example>/quartus/blinky_counter.qsf .
#   quartus_sh -t build.tcl
#
# This script only writes the project files and reports the environment; the
# two flow steps themselves are run as separate executables so that their exit
# codes and reports are the ordinary Quartus ones:
#
#   quartus_syn blinky_counter -c blinky_counter
#   quartus_eda --simulation --format=verilog --tool=questasim \
#               --output_directory=simulation blinky_counter -c blinky_counter
#
# Then, from the repository root:
#
#   python tools/hal_agilex import simulation/blinky_counter.vo -o netlist.hal.v

package require ::quartus::project
package require ::quartus::flow

set revision blinky_counter

if {[project_exists $revision]} {
    project_open $revision -revision $revision
} else {
    project_new $revision -revision $revision -overwrite
}

set_global_assignment -name FAMILY "Agilex 3"
set_global_assignment -name DEVICE A3CW135BM16AE6S
set_global_assignment -name TOP_LEVEL_ENTITY blinky_counter
set_global_assignment -name VERILOG_FILE blinky_counter.v
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output_files

export_assignments

puts "quartus version : $quartus(version)"
puts "family list     : [get_family_list]"
puts "revision        : $revision"

project_close
