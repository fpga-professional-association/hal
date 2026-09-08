"""The in-HAL half of hal_runner: the code that runs inside ``hal --python-script``.

Nothing in the rest of ``hal_runner`` imports ``hal_py``; everything here does
(directly or through :mod:`hal_viz.halenv`, which is the repository's one
module for turning a path into a loaded netlist).  The split is what lets the
orchestrator be unit tested on a plain interpreter while the analyses stay real.

``step_runner.py`` is the file HAL is pointed at.  It is deliberately tiny and
deliberately not a package module: ``--python-script`` reads a file and runs its
source, so there is no ``__file__``, no ``sys.argv`` and no package context to
rely on.  It bootstraps ``sys.path`` from the request and hands over to
:mod:`hal_runner.steps.dispatch`.
"""

__all__ = []
