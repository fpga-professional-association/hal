"""``python tools/hal_apb_recover ...`` entry point."""

import os
import sys

if __package__ in (None, ""):  # executed as a directory, not as a module
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from hal_apb_recover.cli import main
else:
    from .cli import main

if __name__ == "__main__":
    sys.exit(main())
