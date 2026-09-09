# Regenerate traffic_fsm.vo from design.v.
#
#   cd examples/agilex3_walkthroughs/02_traffic_fsm/quartus
#   <quartus>/bin64/quartus_sh.exe -t build.tcl
#
# Equivalent to the two commands tools/hal_agilex documents for its fixtures:
#   quartus_syn traffic_fsm -c traffic_fsm
#   quartus_eda --simulation --format=verilog --tool=questasim \
#               --output_directory=simulation traffic_fsm -c traffic_fsm
#
# Afterwards, from the repository root:
#   python tools/hal_agilex import <this dir>/../traffic_fsm.vo \
#          -o <this dir>/../netlist.hal.v

package require ::quartus::flow
package require ::quartus::project

project_open traffic_fsm -revision traffic_fsm

execute_module -tool syn
execute_module -tool eda -args "--simulation --format=verilog --tool=questasim --output_directory=simulation"

project_close
