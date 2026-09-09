"""Checks that a script started without any project argument still gets a bare interpreter.

Nothing is loaded for a plain ``hal --python-script``, so the name ``netlist`` must not exist -- a
script that does its own loading has to be able to bind that name itself.
"""

import sys

if "netlist" in globals():
    print("NETLIST=present")
    sys.exit(3)

print("NETLIST=absent")
