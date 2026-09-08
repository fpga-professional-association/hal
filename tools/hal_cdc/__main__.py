"""``python tools/hal_cdc ...`` entry point."""

import os
import sys

if __package__ in (None, ""):  # run as a directory/script, not as a module
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from hal_cdc.cli import main
else:
    from .cli import main

if __name__ == "__main__":
    sys.exit(main())
