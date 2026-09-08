"""Recover an APB register map from a flattened, name-stripped netlist.

The package is deliberately split into three layers so that the analysis can be
developed and tested without a built HAL:

``circuit``
    a plain-Python, three-valued netlist model (``0``/``1``/``X``) with a
    cached gate evaluator.  Nothing in it knows about HAL or Verilog.
``hgl_library`` / ``verilog_source`` / ``hal_source``
    the three ways to obtain a :class:`circuit.Circuit`.  The first two read a
    ``.hgl`` gate library and a structural Verilog netlist with the standard
    library only (used by the fixtures and by the offline tests); the third
    builds the very same model from a live ``hal_py`` netlist.
``mapping`` / ``recover`` / ``regmap`` / ``findings`` / ``replay``
    the user-supplied APB signal mapping, the recovery itself, and the four
    exports: a machine-readable register map, Markdown, a ``hal_findings``
    document and a replayable transaction list.

Because the analysis only talks to :class:`circuit.Circuit`, the offline path
and the ``hal_py`` path run *identical* code, which is what makes the offline
tests meaningful.
"""

__version__ = "1.0.0"

#: Version of the register-map document this package emits.
REGISTER_MAP_SCHEMA = "fpgapa.apb-register-map"
REGISTER_MAP_SCHEMA_VERSION = "1.0.0"

#: Version of the user-supplied APB mapping document this package reads.
MAPPING_SCHEMA = "fpgapa.apb-signal-mapping"
MAPPING_SCHEMA_VERSION = "1.0.0"
