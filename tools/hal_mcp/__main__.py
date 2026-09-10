"""Allow both ``python -m hal_mcp`` and ``python tools/hal_mcp``.

When the directory is executed directly, Python puts the package directory
itself on ``sys.path`` and the package is not importable by name, so add the
parent directory (``tools/``) before importing.  ``tools/`` on ``sys.path`` is
also what makes ``hal_viz.halenv`` -- the HAL import and netlist loading this
server reuses -- importable.
"""

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
