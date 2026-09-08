"""hal_fault_campaign -- reproducible register-bit fault-injection campaigns.

The package splits cleanly in two halves, and the split is load bearing:

* everything at the top level is **pure Python** with nothing but the standard
  library (plus ``hal_findings`` / ``hal_runner``, which are equally pure).  It
  parses configurations, enumerates campaigns, classifies traces, builds
  findings and manifests, and can therefore be unit tested on any machine --
  including a Windows checkout with no HAL build in sight.
* ``steps/`` is the only place that imports ``hal_py``.  It runs inside
  ``hal --python-script``, instruments the netlist, drives the simulator and
  writes traces back out as JSON.

That boundary is why a campaign result can be replayed and re-checked without
re-simulating: the classification is a function of the recorded traces, and the
traces are a function of the pinned netlist, the workload and the fault list.
"""

__version__ = "1.0.0"

#: Version of the campaign manifest this build writes.
MANIFEST_VERSION = "1.0.0"

#: Version of the campaign configuration format this build writes.
CONFIG_VERSION = "1.0.0"
