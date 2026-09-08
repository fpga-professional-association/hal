"""hal_secprop -- interface-to-sensitive-state security property checks.

Given an explicit *policy* (which registers are sensitive, which bit is the
lock, which interface signals an attacker drives, and what the clock/reset
assumptions are), this tool answers one question per policy obligation with a
verdict that can be re-derived: can a run of at most *k* cycles reach a state
the policy forbids, and if so, what transaction sequence gets there?

See ``README.md`` in this directory for the policy format, the property
catalogue, the fixtures and the container commands.

Importing this package puts the repository's ``tools/`` directory on
``sys.path`` when it is not already there, because the analysis is built on
three siblings and none of them is optional:

* ``hal_findings``     -- the shared findings/evidence contract;
* ``hal_apb_check``    -- the Boolean term algebra, the CDCL solver, the
  transition-system unrolling and the ``hal_py`` netlist front end;
* ``hal_apb_recover``  -- the ``.hgl`` reader and the structural Verilog reader
  that make the offline path (and therefore the offline tests) possible.
"""

import os
import sys

VERSION = "1.0.0"
PRODUCER_NAME = "hal_secprop"

#: Version of the policy document format this build reads and writes.
POLICY_SCHEMA = "fpgapa.security-policy"
POLICY_SCHEMA_VERSION = "1.0.0"

__all__ = [
    "VERSION",
    "PRODUCER_NAME",
    "POLICY_SCHEMA",
    "POLICY_SCHEMA_VERSION",
    "tools_dir",
]


def tools_dir():
    """Absolute path of the repository's ``tools/`` directory."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_TOOLS = tools_dir()
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
