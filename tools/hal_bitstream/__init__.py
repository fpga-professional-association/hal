"""hal_bitstream -- open a device bitstream with the open-source converters, then hand it to HAL.

Reverse engineering a real device starts at a bitstream, but HAL ingests netlists. The
bitstream-documentation projects (IceStorm, Trellis, Oxide, Apicula, X-Ray) each ship their own
tools, with their own names, their own file formats and their own idea of what "unpack" means.
This tool is the missing, boring layer in front of them:

* it *detects* the family from the file itself rather than from its extension,
* it *runs* the family's converter chain when that toolchain is installed -- for iCE40 that is
  IceStorm's ``iceunpack`` and ``icebox_vlog`` followed by ``yosys -p synth_ice40``, because
  ``icebox_vlog`` writes behavioural Verilog and HAL's parser reads cell netlists,
* it *refuses* to guess when one is not, with an error that names the program, the project that
  ships it and how to point at a copy that is not on ``PATH``,
* it names the gate library the resulting netlist has to be read with, and
* it hands the netlist to ``hal_py`` (through ``hal_viz.halenv``, so HAL's parser plugins are
  loaded) or writes a HAL project directory for the rest of the tool family.

What it never does is produce a netlist that is not the converter's output: no heuristics, no
partial recovery, no silent fallback. A bitstream this fork cannot open is an error, and the error
says what is missing.

Layout::

    registry.py   the families, their converter chains and their gate libraries
    detect.py     which family a file belongs to, from its magic bytes
    convert.py    resolving converters, running the chain, recording provenance
    cli.py        ``python tools/hal_bitstream ...``
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
