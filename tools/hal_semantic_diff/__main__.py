"""Allow both ``python -m hal_semantic_diff`` and ``python tools/hal_semantic_diff``.

When the directory is executed directly, Python puts the package directory
itself on ``sys.path`` and the package is not importable by name, so add the
parent directory before importing.
"""

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_semantic_diff.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
