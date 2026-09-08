"""A script whose exception reaches the top level. HAL must not report this run as a success."""

import hal_py

print("regression fixture is about to raise")

raise RuntimeError("deliberate failure from the python shell regression fixture")
