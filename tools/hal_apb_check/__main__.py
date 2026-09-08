"""Entry point so the directory can be run directly.

``python tools/hal_apb_check ...`` and ``python -m hal_apb_check ...`` both work;
the first only needs ``tools`` on ``sys.path``, which this file arranges.
"""

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from hal_apb_check.cli import main
else:
    from .cli import main

if __name__ == "__main__":
    sys.exit(main())
