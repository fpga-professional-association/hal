"""Allow both ``python -m hal_agilex`` and ``python tools/hal_agilex``.

When the directory is executed directly, Python puts the package directory
itself on ``sys.path`` and neither ``hal_agilex`` nor its sibling
``hal_findings`` is importable by name, so add the parent directory first.
"""

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_agilex.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
