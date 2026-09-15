"""Allow both ``python -m hal_crypto`` and ``python tools/hal_crypto``.

When the directory is executed directly, Python puts the package directory
itself on ``sys.path`` and neither ``hal_crypto`` nor its siblings
``hal_agilex``/``hal_findings`` are importable by name, so add the parent
directory first.
"""

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_crypto.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
