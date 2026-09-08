"""hal_cdc -- structural clock-domain and reset-domain audit for HAL netlists.

This is a *screening* tool.  It reads a gate-level netlist, combines the user's
clock/reset declarations with a structurally recovered clock tree, propagates
clock domains forward through combinational logic, enumerates the paths that
leave one domain and enter another, and classifies each of them conservatively
as a recognised two-flop synchroniser, an unsynchronised crossing candidate, a
waived crossing, or -- most often when a design is only partly declared --
``unknown``.

It is **not** timing or metastability sign-off.  Every claim it emits carries
``status: heuristic`` or ``status: unknown`` in the ``hal_findings`` schema, and
the report always contains an explicit ``unsupported`` finding listing what the
analysis does not cover (multi-bit coherency, reconvergence, gated-clock phase
relationships, black boxes, and all physical timing).

Layout::

    netlist_view.py     plain-data view of a netlist (hal_py in one place)
    declarations.py     user clock/reset/input declarations and scoped waivers
    domains.py          clock-source resolution and forward domain propagation
    crossings.py        crossing enumeration with supporting gate paths
    patterns.py         two-flop synchroniser classification
    resets.py           reset fan-out and reset-release classification
    clock_tree.py       optional clock_tree_extractor cross-check
    audit.py            the analysis entry point (``run_audit``)
    report.py           findings document and Graphviz output
    fixture_netlist.py  dependency-free loader for the .v/.hgl fixtures
    cli.py              ``python tools/hal_cdc ...``
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
