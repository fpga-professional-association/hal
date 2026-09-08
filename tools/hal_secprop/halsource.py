"""The ``hal_py`` front end: a real netlist -> the same transition system.

This module deliberately contains almost no logic. ``hal_apb_check.netlist``
already turns a ``hal_py`` netlist into a
:class:`hal_apb_check.system.TransitionSystem` -- splitting gates by the gate
type's own ``sequential`` property, requiring an ``FFComponent``, building each
register's next state from the type's ``next_state``/``async_reset``/
``async_set`` functions and every combinational net's function from
``SubgraphNetlistDecorator`` -- and that model is exactly the one this analysis
needs. Re-deriving it here would mean two implementations of the same
primitive semantics, drifting apart in the container where neither is easy to
run.

What is added is the translation of its ``UnsupportedPrimitives`` into ours, so
that a latch in the design produces ``unsupported`` findings on both paths
instead of a stack trace on one and a report on the other.
"""

import os

from .errors import DesignError, UnsupportedPrimitives

__all__ = ["load", "import_hal"]


def _front_end():
    from hal_apb_check import netlist as front_end

    return front_end


def import_hal(hal_libs=()):
    """Import ``hal_py``, with an actionable message when it is missing."""
    front_end = _front_end()
    try:
        return front_end.import_hal(hal_libs)
    except front_end.NetlistFrontendError as error:
        raise DesignError(str(error))


def load(netlist_path=None, project_path=None, gate_library=None, hal_libs=()):
    """Load a design through ``hal_py``. Returns ``(system, artifact)``."""
    front_end = _front_end()
    try:
        system, artifact = front_end.load(
            netlist_path=netlist_path,
            project_path=project_path,
            gate_library=gate_library,
            hal_libs=hal_libs,
        )
    except front_end.UnsupportedPrimitives as error:
        raise UnsupportedPrimitives(str(error), error.primitives)
    except front_end.NetlistFrontendError as error:
        raise DesignError(str(error))
    artifact = dict(artifact)
    artifact["front_end"] = "hal_py"
    artifact["description"] = (
        "gate-level design loaded through hal_py; gate and net identifiers are HAL ids "
        "scoped to this artifact"
    )
    if gate_library and os.path.isfile(gate_library):
        artifact.setdefault("gate_library", {"path": os.path.abspath(gate_library)})
    return system, artifact
