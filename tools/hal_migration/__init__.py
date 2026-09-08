"""hal_migration - assess FPGA vendor migration requirements from a netlist.

A vendor migration starts with two questions that a netlist can actually
answer: *what is in the design*, and *what does the target technology offer*.
This tool answers the first from HAL and takes the second from a manually
reviewed, versioned catalogue, then reports where the two meet -- and, far more
importantly, where they do not.

It converts nothing. Its output is an assessment:

``source inventory``
    primitive counts per gate type with the memory, clock, reset and I/O
    metadata HAL can see, the clock/reset nets, the top-level ports, and an
    explicit list of what could *not* be determined
    (:mod:`hal_migration.inventory`, format ``source-inventory-1.0.0``);
``target capability catalogue``
    a reviewed table of what the target offers per source primitive, with the
    category a human signed off (``supported``/``candidate``/``unresolved``),
    the assumptions it rests on and the obligations it leaves open
    (:mod:`hal_migration.catalogue`, format ``target-catalogue-1.0.0``);
``assessment``
    a ``hal_findings`` document in which every proposed mapping is a
    ``heuristic`` finding, every gap is an ``unsupported`` finding, and timing
    closure, resource fit and whole-design equivalence are ``unknown`` findings
    that are emitted on every single run
    (:mod:`hal_migration.assess`, rendered by :mod:`hal_migration.report`).

Everything except the ``inventory`` step runs on a plain interpreter with the
standard library only; ``inventory`` needs a netlist loaded by a built HAL, but
even that module never imports ``hal_py`` itself -- it goes through the same
duck-typed accessors ``hal_findings`` uses, so it can be driven with stubs.

Run from the repository root::

    python tools/hal_migration inventory examples/uart/ -o uart.inventory.json
    python tools/hal_migration assess --inventory uart.inventory.json \\
        --catalogue ice40ultra-to-generic-fpga -o assessment.json --report report.md

``tools`` must be on ``sys.path`` (``python tools/hal_migration`` and
``python -m hal_migration`` from ``tools/`` both arrange that) because this
package builds on ``hal_findings``.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
