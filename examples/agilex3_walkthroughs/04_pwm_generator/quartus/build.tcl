# Regenerate netlist/pwm_generator.vo from ../design.v.
#
#   cd examples/agilex3_walkthroughs/04_pwm_generator/quartus
#   <quartus>/bin64/quartus_sh -t build.tcl
#
# Requires Quartus Prime Pro Edition 26.1 with the Agilex 3 device support
# installed. Synthesis only: the fitter is never run, so the export is the
# post-synthesis snapshot and carries no placement noise.

set rev pwm_generator

# quartus_syn / quartus_eda are separate executables; call them the same way the
# tools/hal_agilex fixtures were made.
if {[catch {exec quartus_syn $rev -c $rev} out]} { puts $out ; exit 1 } else { puts $out }
if {[catch {exec quartus_eda --simulation --format=verilog --tool=questasim \
                --output_directory=simulation $rev -c $rev} out]} { puts $out ; exit 1 } else { puts $out }

puts "wrote simulation/$rev.vo"
