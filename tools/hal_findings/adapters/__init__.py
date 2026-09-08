"""Adapters that wrap existing HAL analysis results in the findings schema.

The adapters never change plugin APIs: each one takes whatever the plugin's
Python bindings already return and maps it onto findings.  They only use
duck-typed accessors (``get_id``, ``get_name``, ``get_groups``, ...), so they
can be unit tested against stub objects on a machine with no HAL build --
exactly like ``hal_viz.extract``.

Available adapters:

``hal_findings.adapters.dataflow``
    ``dataflow.analyze()`` register groups -> heuristic findings.
``hal_findings.adapters.netlist_comparison``
    ``z3_utils.compare_netlists()`` -> proof / counterexample / unknown /
    timeout / error findings, with the preconditions of that check recorded as
    assumptions.
"""

__all__ = ["common", "dataflow", "netlist_comparison"]
