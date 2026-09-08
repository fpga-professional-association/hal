"""Entry point so the directory itself is runnable: ``python tools/hal_secprop``."""

import os
import sys

if __package__ in (None, ""):  # invoked as a directory, not as a module
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from hal_secprop.cli import main
else:
    from .cli import main

if __name__ == "__main__":
    sys.exit(main())
