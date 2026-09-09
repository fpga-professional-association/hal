"""Modifies the netlist HAL loaded, to check what happens to the modification after the run.

The new name comes from ``--python-args`` so that one fixture can serve both the run whose change has
to be written back to the project and the ``--volatile-mode`` run whose change must not be.
"""

import sys

print("NETLIST=present")

new_name = sys.argv[0] if (sys.argv and sys.argv[0]) else "renamed_by_the_script"

netlist.get_top_module().set_name(new_name)

print("TOP_MODULE={}".format(netlist.get_top_module().get_name()))
