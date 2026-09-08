"""Wrap ``dataflow.analyze()`` results (DANA) in the findings schema.

What the plugin actually returns
--------------------------------
``hal_plugins.dataflow.analyze(config)`` returns a ``dataflow.Result`` (or
``None`` on failure).  The Python-visible accessors used here are the ones
declared in ``plugins/dataflow_analysis/python/python_bindings.cpp``::

    Result.get_netlist()                      -> hal_py.Netlist
    Result.get_groups()                       -> dict[int, set[hal_py.Gate]]
    Result.get_group_successors(group_id)     -> set[int]   or None
    Result.get_group_predecessors(group_id)   -> set[int]   or None
    Result.get_group_control_nets(gid, type)  -> set[Net]   or None

Why every group is a *heuristic* finding
----------------------------------------
DANA reconstructs word-level registers from structural evidence -- shared
control signals, common predecessors/successors, expected group sizes.  It
proves nothing about the design, so its output can never be more than
``heuristic``.  Encoding that in the status is the whole point of the schema:
downstream reports cannot accidentally present a register grouping next to an
equivalence proof as if they carried the same weight.
"""

from .. import model
from .. import __version__
from . import common

__all__ = ["PLUGIN_NAME", "ENTRY_POINT", "configuration_to_json", "build_document"]

PLUGIN_NAME = "dataflow_analysis"
ENTRY_POINT = "dataflow.analyze"

_METHOD_DESCRIPTION = (
    "DANA groups sequential gates into word-level registers from structural evidence "
    "(shared control nets, common predecessors and successors, expected group sizes). "
    "It is a reconstruction heuristic: a group is a candidate register, not a proven one."
)

_CONFIG_FIELDS = (
    "min_group_size",
    "expected_sizes",
    "enable_stages",
    "enforce_type_consistency",
)


def _json_safe(value):
    """Convert binding objects (enums, gate types, ...) into JSON values."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted((_json_safe(item) for item in value), key=repr)
    name = common.call(value, "get_name")
    if isinstance(name, str):
        return name
    enum_name = getattr(value, "name", None)
    if isinstance(enum_name, str):
        return enum_name
    return str(value)


def configuration_to_json(configuration):
    """Serialize a ``dataflow.Configuration`` (or any stub of it) to JSON values."""
    if configuration is None:
        return None
    config = {}
    for field in _CONFIG_FIELDS:
        if hasattr(configuration, field):
            config[field] = _json_safe(getattr(configuration, field))
    if hasattr(configuration, "gate_types"):
        config["gate_types"] = _json_safe(configuration.gate_types)
    if hasattr(configuration, "control_pin_types"):
        config["control_pin_types"] = _json_safe(configuration.control_pin_types)
    return config or None


def _group_finding(result, artifact_id, group_id, gates, control_pin_types, method):
    gate_refs = [common.gate_reference(gate, artifact_id) for gate in gates]
    gate_types = sorted({ref["type"] for ref in gate_refs if ref.get("type")})

    data = {}
    successors = common.call(result, "get_group_successors", group_id)
    if successors is not None:
        data["successor_groups"] = sorted(int(entry) for entry in successors)
    predecessors = common.call(result, "get_group_predecessors", group_id)
    if predecessors is not None:
        data["predecessor_groups"] = sorted(int(entry) for entry in predecessors)

    control_nets = []
    for label, pin_type in control_pin_types or ():
        nets = common.call(result, "get_group_control_nets", group_id, pin_type)
        for net in nets or []:
            control_nets.append(common.net_reference(net, artifact_id, role=label))
    if control_nets:
        data["control_net_roles"] = sorted({ref["role"] for ref in control_nets})

    data["group_id"] = int(group_id)
    data["group_id_note"] = (
        "dataflow group IDs are internal to this analysis run and unrelated to any HAL ID"
    )

    return model.finding(
        "dataflow/group/{:04d}".format(int(group_id)),
        "Candidate register group {} ({} sequential gates)".format(group_id, len(gate_refs)),
        model.STATUS_HEURISTIC,
        method,
        model.scope(
            [artifact_id],
            description="sequential gates grouped into one candidate register",
            gates=gate_refs,
            nets=control_nets or None,
            gate_types=gate_types or None,
        ),
        summary=(
            "Dataflow analysis grouped {} sequential gates of type {} into one candidate "
            "word-level register. Structural evidence only.".format(
                len(gate_refs), ", ".join(gate_types) if gate_types else "unknown"
            )
        ),
        severity="info",
        metrics={"group_size": len(gate_refs)},
        data=data,
        tags=["dataflow", "register-candidate"],
    )


def _coverage_finding(netlist, result, artifact_id, covered_types, method):
    """Report sequential gate types this run did not group, as unsupported."""
    summary = common.sequential_gate_types(netlist)
    uncovered = {
        name: entry for name, entry in summary.items() if name not in covered_types
    }
    if not uncovered:
        return None

    primitives = []
    for name, entry in sorted(uncovered.items()):
        primitives.append(
            model.unsupported_primitive(
                name,
                "sequential gate type present in the netlist but not grouped by this "
                "dataflow run; word-level recovery does not cover it",
                count=entry["count"],
                properties=entry["properties"],
                example_gates=[
                    common.gate_reference(gate, artifact_id) for gate in entry["gates"]
                ],
            )
        )

    return model.finding(
        "dataflow/coverage/unsupported-primitives",
        "Sequential gate types not covered by this dataflow run",
        model.STATUS_UNSUPPORTED,
        method,
        model.scope(
            [artifact_id],
            description="sequential gate types of the analysed netlist",
            gate_types=sorted(uncovered),
        ),
        summary=(
            "{} sequential gate type(s) were not grouped: {}. Absence of a register "
            "group for these gates is not evidence that no register exists.".format(
                len(uncovered), ", ".join(sorted(uncovered))
            )
        ),
        severity="info",
        unsupported_dict=model.unsupported(
            "primitive",
            "dataflow analysis only groups the sequential gate types it was configured "
            "for; the types below carry the 'sequential' property but did not end up in "
            "any group",
            primitives,
        ),
        tags=["coverage", "dataflow"],
    )


def build_document(
    result,
    artifact_id="netlist",
    configuration=None,
    plugin_version="unknown",
    hal_version=None,
    control_pin_types=(),
    netlist_path=None,
    duration_s=None,
    generated_at=None,
    producer_command=None,
    evidence_list=None,
):
    """Build a findings document from a ``dataflow.Result``.

    :param result: the object returned by ``hal_plugins.dataflow.analyze()``.
    :param artifact_id: ID the netlist is registered (and referenced) under.
    :param configuration: the ``dataflow.Configuration`` that was used, if any.
    :param control_pin_types: iterable of ``(label, hal_py.PinType)`` pairs; for
        each pair the group's control nets of that pin type are recorded.  Left
        empty by default so this module never has to import ``hal_py``.
    :returns: a findings document (validate it with
        :func:`hal_findings.validate.validate_document`).
    """
    netlist = common.call(result, "get_netlist")
    artifact = common.netlist_artifact(netlist, artifact_id, path=netlist_path)

    method = model.method(
        "dataflow analysis (DANA)",
        "heuristic",
        False,
        description=_METHOD_DESCRIPTION,
        parameters=configuration_to_json(configuration),
    )

    groups = common.call(result, "get_groups", default={}) or {}
    findings = []
    covered_types = set()
    for group_id in sorted(groups, key=int):
        gates = sorted(
            groups[group_id], key=lambda gate: (common.call(gate, "get_id", default=0))
        )
        for gate in gates:
            type_name = common.gate_type_name(gate)
            if type_name:
                covered_types.add(type_name)
        findings.append(
            _group_finding(result, artifact_id, group_id, gates, control_pin_types, method)
        )

    coverage = _coverage_finding(netlist, result, artifact_id, covered_types, method)
    notes = []
    if coverage is not None:
        findings.append(coverage)
    else:
        notes.append(
            "every sequential gate type of artifact {!r} ended up in a register "
            "group; no unsupported primitives to report".format(artifact_id)
        )
    notes.append(
        "dataflow group IDs are local to this run and are not HAL object IDs; gate "
        "references are scoped to artifact {!r}".format(artifact_id)
    )

    analysis = {
        "plugin": {
            "name": PLUGIN_NAME,
            "version": str(plugin_version),
            "description": "word-level structure recovery (DANA)",
        },
        "entry_point": ENTRY_POINT,
    }
    configuration_json = configuration_to_json(configuration)
    if configuration_json:
        analysis["configuration"] = configuration_json
    if hal_version:
        analysis["hal"] = {"version": str(hal_version)}
    if duration_s is not None:
        analysis["duration_s"] = float(duration_s)

    if evidence_list:
        for finding in findings:
            finding.setdefault("evidence", []).extend(evidence_list)

    producer = {"name": "hal_findings.adapters.dataflow", "version": __version__}
    if producer_command:
        producer["command"] = list(producer_command)

    return model.document(
        producer,
        [artifact],
        analysis,
        findings,
        generated_at=generated_at if generated_at is not None else common.utc_now(),
        notes=notes,
    )
