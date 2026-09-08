"""Validated Altera Agilex primitive support for HAL.

The package is deliberately import-light: everything except :mod:`hal_adapter`
runs on a plain CPython 3 interpreter with the standard library only, so the
primitive semantics, the netlist reader, the reference simulator, the coverage
inventory and the arithmetic recognition adapter can all be tested without a
built HAL and without the vendor tool.

See ``README.md`` for the exact device, Quartus version and export commands the
shipped fixtures were produced with.
"""

__all__ = [
    "primitives",
    "vo_netlist",
    "vo_import",
    "simulate",
    "inventory",
    "recognize",
]
