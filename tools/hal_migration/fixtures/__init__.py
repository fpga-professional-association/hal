"""Fixtures for the migration assessment tool.

``stub_netlist`` builds a netlist-shaped object out of a real HAL gate library
file plus an instance list, so the inventory extractor can be exercised (and
the checked-in inventory fixtures regenerated) on a machine without a built
HAL. ``_generate.py`` writes the fixtures; ``README.md`` states the ground
truth the container test asserts against the *real* bindings.
"""
