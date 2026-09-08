"""hal_viz - headless, batch visualization for HAL netlist analyses.

The package is deliberately split so that everything which does *not* need a
built HAL can be imported (and unit tested) with a plain Python interpreter:

``hal_viz.dot``
    Dependency-free Graphviz DOT emission (quoting, escaping, clusters).
``hal_viz.extract``
    Turns netlist objects into :class:`~hal_viz.dot.DotGraph` instances.  It
    only ever calls duck-typed accessors, so it can be exercised with stub
    objects instead of real ``hal_py`` ones.
``hal_viz.render``
    Locates and drives the Graphviz ``dot`` binary, writes an HTML index.
``hal_viz.halenv``
    The only module that actually imports ``hal_py``.
``hal_viz.cli``
    Argument parsing and the subcommand implementations.

Run ``python -m hal_viz --help`` (with this directory's parent on
``PYTHONPATH``) or ``python tools/hal_viz --help`` for usage.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
