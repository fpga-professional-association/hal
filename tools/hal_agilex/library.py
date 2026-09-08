"""Generator for the ``AGILEX_TENNM`` HGL gate library.

The library file is committed (see ``LIBRARY_PATH``); this module is what
produced it, so the definition can be reviewed as code and a test can assert
that the committed file is exactly what the generator emits.

What the library does and does not say:

* ``tennm_ff`` carries a full ``ff_config``.  Its ``next_state`` is
  ``(ena & d) | (IQ & !ena)`` and its ``clear_on`` is ``!clrn`` -- exactly the
  behaviour validated against the vendor export.  The pins that this package
  does not model (``sclr``, ``sclr1``, ``sload``, ``aload``, ``asdata``) are
  declared so the netlist parses, but they appear in no function: an instance
  that actually uses them is out of coverage and
  :mod:`hal_agilex.inventory` reports it as such.

* ``tennm_lcell_comb`` carries **no** Boolean functions and no ``lut_config``.
  This is deliberate.  HAL derives a LUT function from an init string only for
  gate types with at most six input pins, and it derives exactly one function
  per LUT output; the ALM has ten input pins and three outputs whose meaning
  depends on ``lut_mask`` *and* on the mode.  Declaring a ``lut_config`` here
  would make ``Gate::get_lut_function`` log an error and hand back an empty
  function.  Instead :mod:`hal_agilex.hal_adapter` attaches the per-instance
  functions with ``Gate.add_boolean_function`` after the netlist is loaded, and
  refuses to do so for any instance outside the validated coverage.

* ``HAL_GND``/``HAL_VCC`` are not Quartus atoms.  HAL's Verilog parser needs a
  ground and a power gate type in the library to materialise the ``1'b0``/
  ``1'b1`` literals the export uses; the names are prefixed so they cannot be
  mistaken for something the vendor emitted.
"""

import json
import os

from . import primitives

__all__ = [
    "LIBRARY_NAME",
    "LIBRARY_PATH",
    "GND_GATE_TYPE",
    "VCC_GATE_TYPE",
    "CONSTANT_GATE_TYPES",
    "build_library",
    "dumps",
    "write",
]

LIBRARY_NAME = "AGILEX_TENNM"

#: HAL's own constant gate types.  They are scaffolding for the ``1'b0``/``1'b1``
#: literals in the export, not Quartus atoms, so consumers must not treat them
#: as uncovered vendor primitives.
GND_GATE_TYPE = "HAL_GND"
VCC_GATE_TYPE = "HAL_VCC"
CONSTANT_GATE_TYPES = (GND_GATE_TYPE, VCC_GATE_TYPE)

#: Where the generated library is committed, relative to the repository root.
LIBRARY_PATH = os.path.join(
    "plugins", "gate_libraries", "definitions", "AGILEX_TENNM.hgl"
)

_HGL_VERSION = 4

_FF_NEXT_STATE = "((ena & d) | (IQ & (! ena)))"
_FF_CLEAR_ON = "(! clrn)"


def _pin(name, direction, pin_type="none", function=None):
    entry = {"name": name, "direction": direction, "type": pin_type}
    if function is not None:
        entry["function"] = function
    return entry


def _group(pin_entry):
    return {
        "name": pin_entry["name"],
        "direction": pin_entry["direction"],
        "type": pin_entry["type"],
        "ascending": False,
        "start_index": 0,
        "ordered": False,
        "pins": [pin_entry],
    }


def _lcell():
    pins = []
    for name in primitives.LCELL_DATA_PINS + primitives.LCELL_EXTENDED_PINS:
        pins.append(_pin(name, "input", "data"))
    pins.append(_pin("cin", "input", "carry"))
    pins.append(_pin("sharein", "input", "carry"))
    pins.append(_pin("combout", "output", "none"))
    pins.append(_pin("sumout", "output", "sum"))
    pins.append(_pin("cout", "output", "carry"))
    pins.append(_pin("shareout", "output", "carry"))
    return {
        "name": primitives.LCELL,
        "types": ["combinational", "c_lut", "c_carry"],
        "pin_groups": [_group(entry) for entry in pins],
    }


def _ff():
    pins = [
        _pin("clk", "input", "clock"),
        _pin("d", "input", "data"),
        _pin("asdata", "input", "data"),
        _pin("clrn", "input", "reset"),
        _pin("aload", "input", "control"),
        _pin("sclr", "input", "control"),
        _pin("sload", "input", "control"),
        _pin("ena", "input", "enable"),
        _pin("sclr1", "input", "control"),
        _pin("devclrn", "input", "control"),
        _pin("devpor", "input", "control"),
        _pin("q", "output", "state", function="IQ"),
    ]
    return {
        "name": primitives.FF,
        "types": ["sequential", "ff"],
        "ff_config": {
            "state": "IQ",
            "neg_state": "IQN",
            "next_state": _FF_NEXT_STATE,
            "clocked_on": "clk",
            "clear_on": _FF_CLEAR_ON,
        },
        "pin_groups": [_group(entry) for entry in pins],
    }


def _constant(name, property_name, pin_type):
    return {
        "name": name,
        "types": ["combinational", property_name],
        "pin_groups": [
            _group(
                _pin(
                    "O",
                    "output",
                    pin_type,
                    function="0b0" if property_name == "ground" else "0b1",
                )
            )
        ],
    }


def build_library():
    """Return the library as a plain dict."""
    return {
        "version": _HGL_VERSION,
        "library": LIBRARY_NAME,
        "cells": [
            _constant(GND_GATE_TYPE, "ground", "ground"),
            _constant(VCC_GATE_TYPE, "power", "power"),
            _ff(),
            _lcell(),
        ],
    }


def dumps():
    """Deterministic JSON text of the library, ending in a newline."""
    return json.dumps(build_library(), indent=4) + "\n"


def write(path):
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps())
    return path
