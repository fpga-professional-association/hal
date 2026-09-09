"""Reports what HAL put in front of the script, so the driver can check the loaded netlist.

Prints one KEY=VALUE line per property. Exits nonzero when there is no netlist at all, which is what
this fixture is run to rule out.
"""

import sys

import hal_py

if "netlist" not in globals():
    print("NETLIST=absent")
    sys.exit(3)

print("NETLIST=present")
print("NETLIST_TYPE={}".format(type(netlist).__name__))
print("IS_HAL_NETLIST={}".format(isinstance(netlist, hal_py.Netlist)))
print("GATE_COUNT={}".format(len(netlist.get_gates())))
print("NET_COUNT={}".format(len(netlist.get_nets())))
print("TOP_MODULE={}".format(netlist.get_top_module().get_name()))
print("GATE_LIBRARY={}".format(netlist.get_gate_library().get_name()))
print("DESIGN_NAME={}".format(netlist.get_design_name()))
print("ARGV={}".format(",".join(sys.argv)))
