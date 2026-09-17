# Regenerate one mystery core's .vo from its design.
#
# Five revisions share this script because the five cores share a flow; the
# only thing that differs is the revision name and the two or three synthesis
# assignments each design needs to stay inside hal_agilex's validated primitive
# coverage.  Those are in the table below and, identically, in each .qsf.
#
#   cd <a scratch directory>
#   cp <example>/ground_truth/designs/core_a.v .
#   cp <example>/ground_truth/quartus/core_a.qpf <example>/ground_truth/quartus/core_a.qsf .
#   quartus_sh -t build.tcl core_a
#
# This script only writes the project files and reports the environment; the
# two flow steps themselves are run as separate executables so that their exit
# codes and reports are the ordinary Quartus ones:
#
#   quartus_syn core_a -c core_a
#   quartus_eda --simulation --format=verilog --tool=questasim \
#               --output_directory=simulation core_a -c core_a
#
# Then, from the repository root:
#
#   python tools/hal_agilex import simulation/core_a.vo \
#       -o <example>/ground_truth/exports/core_a.hal.v
#   python <example>/anonymize.py \
#       <example>/ground_truth/exports/core_a.hal.v \
#       <example>/cores/core_a.anon.hal.v \
#       <example>/ground_truth/maps/core_a.map.json

package require ::quartus::project
package require ::quartus::flow

set revision [lindex $quartus(args) 0]
if {$revision eq ""} {
    puts stderr "usage: quartus_sh -t build.tcl <core_a|core_b|core_c|core_d|core_e>"
    exit 2
}

# Per-core synthesis assignments; see each .qsf for why each one is there.
array set extra {
    core_a {{ALLOW_SYNCH_CTRL_USAGE OFF}}
    core_b {{ALLOW_SYNCH_CTRL_USAGE OFF} {AUTO_SHIFT_REGISTER_RECOGNITION OFF}}
    core_c {{ALLOW_SYNCH_CTRL_USAGE OFF} {AUTO_SHIFT_REGISTER_RECOGNITION OFF}}
    core_d {{ALLOW_SYNCH_CTRL_USAGE OFF}}
    core_e {{ALLOW_SYNCH_CTRL_USAGE OFF} {AUTO_DSP_RECOGNITION OFF} \
            {DSP_BLOCK_BALANCING {LOGIC ELEMENTS}}}
}

if {![info exists extra($revision)]} {
    puts stderr "unknown revision $revision"
    exit 2
}

if {[project_exists $revision]} {
    project_open $revision -revision $revision
} else {
    project_new $revision -revision $revision -overwrite
}

set_global_assignment -name FAMILY "Agilex 3"
set_global_assignment -name DEVICE A3CW135BM16AE6S
set_global_assignment -name TOP_LEVEL_ENTITY $revision
set_global_assignment -name VERILOG_FILE $revision.v
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output_files

foreach assignment $extra($revision) {
    set_global_assignment -name [lindex $assignment 0] [lindex $assignment 1]
}

export_assignments

puts "quartus version : $quartus(version)"
puts "family list     : [get_family_list]"
puts "revision        : $revision"

project_close
