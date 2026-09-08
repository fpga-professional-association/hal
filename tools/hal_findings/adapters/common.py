"""Duck-typed helpers shared by the analysis adapters.

Nothing here imports ``hal_py``.  Every accessor is called defensively so the
helpers work against real bindings and against test stubs alike, and so a
binding that is missing in an older HAL build degrades to a missing field
rather than a crash.
"""

import datetime
import os

from .. import model
from ..serialize import sha256_file

__all__ = [
    "utc_now",
    "call",
    "gate_type_name",
    "gate_type_properties",
    "netlist_artifact",
    "gate_reference",
    "net_reference",
    "module_reference",
    "sequential_gate_types",
]


def utc_now():
    """Current UTC time as an RFC 3339 string with second resolution."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def call(obj, name, *args, **kwargs):
    """Call ``obj.name(*args)`` if it exists, else return ``default``.

    ``default`` may be passed as a keyword; it defaults to ``None``.  Errors
    raised by the accessor are swallowed for the same reason: an adapter must
    never turn a missing optional detail into a failed analysis.
    """
    default = kwargs.pop("default", None)
    method = getattr(obj, name, None)
    if method is None:
        return default
    try:
        value = method(*args, **kwargs)
    except Exception:
        return default
    return default if value is None else value


def _enum_name(value):
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    text = str(value)
    return text.rsplit(".", 1)[-1]


def gate_type_name(gate):
    """Name of a gate's type, or ``None`` if the accessor chain is unavailable."""
    gate_type = call(gate, "get_type")
    if gate_type is None:
        return None
    return call(gate_type, "get_name")


def gate_type_properties(gate_type):
    """Sorted property names of a gate type (``ff``, ``ram``, ``sequential``, ...)."""
    properties = call(gate_type, "get_property_list", default=None)
    if properties is None:
        properties = call(gate_type, "get_properties", default=[])
    return sorted({_enum_name(entry) for entry in properties})


def netlist_artifact(netlist, artifact_id, path=None, description=None):
    """Describe a loaded netlist as an artifact, hashing its source file.

    A netlist that has no readable source file on disk is recorded with an
    explicit ``unhashed_reason`` instead of silently looking reproducible.
    """
    source = path if path is not None else call(netlist, "get_input_filename", default="")
    source = str(source) if source else ""

    sha256 = None
    size_bytes = None
    unhashed_reason = None
    if source and os.path.isfile(source):
        sha256 = sha256_file(source)
        size_bytes = os.path.getsize(source)
    elif source and os.path.isdir(source):
        unhashed_reason = (
            "input is a HAL project directory ({}); hash the archive it was "
            "extracted from to pin it".format(os.path.basename(source))
        )
    else:
        unhashed_reason = (
            "netlist has no readable source file (in-memory or modified netlist)"
        )

    library = call(netlist, "get_gate_library")
    gate_library = None
    if library is not None:
        library_path = call(library, "get_path", default="")
        library_path = str(library_path) if library_path else None
        gate_library = {}
        library_name = call(library, "get_name")
        if library_name:
            gate_library["name"] = library_name
        if library_path:
            gate_library["path"] = library_path
            if os.path.isfile(library_path):
                gate_library["sha256"] = sha256_file(library_path)
        if not gate_library:
            gate_library = None

    gates = call(netlist, "get_gates", default=None)
    nets = call(netlist, "get_nets", default=None)

    return model.artifact(
        artifact_id,
        kind="netlist",
        path=source or None,
        sha256=sha256,
        unhashed_reason=unhashed_reason,
        size_bytes=size_bytes,
        design_name=call(netlist, "get_design_name") or None,
        device_name=call(netlist, "get_device_name") or None,
        netlist_id=call(netlist, "get_id"),
        gate_count=len(gates) if gates is not None else None,
        net_count=len(nets) if nets is not None else None,
        gate_library=gate_library,
        description=description,
    )


def gate_reference(gate, artifact_id, with_module=True):
    """Build a schema gate reference from a (real or stub) gate object."""
    module = None
    if with_module:
        module_object = call(gate, "get_module")
        if module_object is not None:
            module = {}
            module_id = call(module_object, "get_id")
            module_name = call(module_object, "get_name")
            if module_id is not None:
                module["id"] = module_id
            if module_name is not None:
                module["name"] = module_name
            if not module:
                module = None

    return model.gate_ref(
        artifact_id,
        call(gate, "get_id"),
        call(gate, "get_name", default=""),
        gate_type=gate_type_name(gate),
        module=module,
    )


def net_reference(net, artifact_id, role=None):
    """Build a schema net reference from a (real or stub) net object."""
    return model.net_ref(
        artifact_id, call(net, "get_id"), call(net, "get_name", default=""), role=role
    )


def module_reference(module, artifact_id):
    """Build a schema module reference from a (real or stub) module object."""
    return model.module_ref(
        artifact_id, call(module, "get_id"), call(module, "get_name", default="")
    )


def sequential_gate_types(netlist):
    """Map gate type name -> ``{"count", "properties", "gates"}`` for sequential gates.

    "Sequential" is decided by the gate type's own ``sequential`` property, the
    same predicate ``z3_utils::compare_netlists`` and dataflow analysis use, so
    the adapters never guess at primitive semantics.
    """
    summary = {}
    for gate in call(netlist, "get_gates", default=[]) or []:
        gate_type = call(gate, "get_type")
        if gate_type is None:
            continue
        properties = gate_type_properties(gate_type)
        if "sequential" not in properties:
            continue
        name = call(gate_type, "get_name", default="<unnamed>")
        entry = summary.setdefault(name, {"count": 0, "properties": properties, "gates": []})
        entry["count"] += 1
        if len(entry["gates"]) < 3:
            entry["gates"].append(gate)
    return summary
