"""Primitive inventory and coverage report as a ``hal_findings`` document.

This is the step that runs *before* anything else.  It answers one question
about an export -- which primitives does it instantiate, and which of them does
this package actually model -- and answers it in the shared findings format, so
an uncovered RAM block is a machine-readable ``unsupported`` finding rather than
a line in a log that a later analysis quietly ignores.
"""

import os
import re

from . import primitives
from .vo_netlist import Const, parse_file

from hal_findings import model, serialize
from hal_findings.adapters.common import utc_now

__all__ = [
    "PRODUCER",
    "export_metadata",
    "inventory",
    "build_document",
]

PRODUCER = {"name": "hal_agilex.inventory", "version": "1.0.0"}

_DEVICE_RE = re.compile(r"^//\s*Device:\s*(.+?)\s*$", re.MULTILINE)
_VERSION_RE = re.compile(r'^//\s*VERSION\s+"(.+?)"\s*$', re.MULTILINE)
_PROGRAM_RE = re.compile(r'^//\s*PROGRAM\s+"(.+?)"\s*$', re.MULTILINE)
_DATE_RE = re.compile(r'^//\s*DATE\s+"(.+?)"\s*$', re.MULTILINE)


def export_metadata(path):
    """Device, tool and date recorded in the header ``quartus_eda`` writes."""
    with open(str(path), "r", encoding="utf-8", errors="replace") as handle:
        head = handle.read(8192)
    metadata = {}
    for key, pattern in (
        ("device", _DEVICE_RE),
        ("tool_version", _VERSION_RE),
        ("tool", _PROGRAM_RE),
        ("exported_at", _DATE_RE),
    ):
        match = pattern.search(head)
        if match:
            metadata[key] = match.group(1)
    return metadata


def _pin_state(netlist_instance, pins, resolve):
    driven = set()
    constants = {}
    for pin in pins:
        bits = netlist_instance.connections.get(pin)
        if not bits:
            continue
        bit = netlist_instance.single(pin)
        if isinstance(bit, Const):
            constants[pin] = bit.value
            continue
        value = resolve(bit)
        if value is None:
            driven.add(pin)
        else:
            constants[pin] = value
    return driven, constants


def _constant_resolver(netlist):
    aliases = {}
    constants = {}
    for target, source in netlist.assignments:
        for target_bit, source_bit in zip(target, source):
            if isinstance(source_bit, Const):
                constants[target_bit.key] = source_bit.value
            else:
                aliases[target_bit.key] = source_bit

    def resolve(bit, depth=0):
        if depth > 32:
            return None
        key = bit.key
        if key in constants:
            value = constants[key]
            if value is None:
                return None
            return 1 - value if bit.inverted else value
        alias = aliases.get(key)
        if alias is None:
            return None
        value = resolve(alias, depth + 1)
        if value is None:
            return None
        if alias.inverted:
            value = 1 - value
        return 1 - value if bit.inverted else value

    return resolve


def inventory(netlist):
    """Classify every instance of *netlist*.

    Returns a dict with ``covered`` (type -> count), ``uncovered`` (type ->
    ``{"count", "reason", "examples"}``) and ``out_of_coverage`` (a list of
    ``{"instance", "type", "reason"}`` for instances of a covered type whose
    configuration is outside what has been validated).
    """
    resolve = _constant_resolver(netlist)
    covered = {}
    uncovered = {}
    out_of_coverage = []

    for instance in netlist.instances:
        if instance.type not in primitives.COVERED_PRIMITIVES:
            entry = uncovered.setdefault(
                instance.type,
                {
                    "count": 0,
                    "reason": primitives.UNCOVERED_PRIMITIVE_REASONS.get(
                        instance.type,
                        "primitive is not modelled by hal_agilex and its semantics "
                        "have not been established from a vendor-documented source",
                    ),
                    "examples": [],
                },
            )
            entry["count"] += 1
            if len(entry["examples"]) < 3:
                entry["examples"].append(instance.name)
            continue

        covered[instance.type] = covered.get(instance.type, 0) + 1
        try:
            if instance.type == primitives.LCELL:
                driven, constants = _pin_state(
                    instance, primitives.LCELL_INPUT_PINS, resolve
                )
                uses_arithmetic = any(
                    instance.connections.get(pin) for pin in ("sumout", "cout")
                )
                primitives.check_lcell_configuration(
                    instance.parameters, driven, uses_arithmetic, constants
                )
                if instance.connections.get("shareout"):
                    raise primitives.UnsupportedConfiguration(
                        "shareout is connected; the shared arithmetic mode is not modelled"
                    )
            else:
                driven, constants = _pin_state(instance, primitives.FF_INPUT_PINS, resolve)
                primitives.check_ff_configuration(driven, constants)
        except primitives.UnsupportedConfiguration as exc:
            out_of_coverage.append(
                {"instance": instance.name, "type": instance.type, "reason": str(exc)}
            )

    return {
        "covered": covered,
        "uncovered": uncovered,
        "out_of_coverage": out_of_coverage,
        "total": len(netlist.instances),
    }


def _artifact(netlist, path, artifact_id):
    metadata = export_metadata(path)
    return model.artifact(
        artifact_id,
        kind="netlist",
        path=str(path),
        sha256=serialize.sha256_file(str(path)),
        size_bytes=os.path.getsize(str(path)),
        design_name=netlist.name,
        device_name=metadata.get("device"),
        gate_count=len(netlist.instances),
        gate_library={"name": "AGILEX_TENNM"},
        description="Quartus Prime Pro EDA netlist export ({})".format(
            metadata.get("tool_version", "version not recorded in the header")
        ),
    )


def build_document(netlist, path, artifact_id=None, generated_at=None):
    """Build a findings document describing the primitive coverage of *path*."""
    artifact_id = artifact_id or netlist.name
    report = inventory(netlist)
    metadata = export_metadata(path)
    artifact = _artifact(netlist, path, artifact_id)

    structural = model.method(
        "primitive inventory",
        "structural",
        False,
        description=(
            "Enumeration of every instance in the export and of the pin "
            "configuration of each one; no solver and no simulation involved."
        ),
    )

    findings = []

    if report["uncovered"]:
        entries = []
        for gate_type in sorted(report["uncovered"]):
            entry = report["uncovered"][gate_type]
            entries.append(
                model.unsupported_primitive(
                    gate_type,
                    entry["reason"],
                    count=entry["count"],
                    gate_library="AGILEX_TENNM",
                )
            )
        findings.append(
            model.finding(
                "hal_agilex/inventory/uncovered-primitives",
                "Primitives outside the validated Agilex coverage",
                model.STATUS_UNSUPPORTED,
                structural,
                model.scope(
                    [artifact_id],
                    description="every instance of the export",
                    gate_types=sorted(report["uncovered"]),
                ),
                summary=(
                    "{} of {} instances are of a primitive type this package does "
                    "not model; they are reported, never imported with invented "
                    "semantics.".format(
                        sum(entry["count"] for entry in report["uncovered"].values()),
                        report["total"],
                    )
                ),
                severity="high",
                unsupported_dict=model.unsupported(
                    "primitive",
                    "hal_agilex models only {}".format(
                        " and ".join(primitives.COVERED_PRIMITIVES)
                    ),
                    primitives=entries,
                ),
                data={
                    "examples": {
                        gate_type: report["uncovered"][gate_type]["examples"]
                        for gate_type in sorted(report["uncovered"])
                    }
                },
                tags=["coverage", "agilex"],
            )
        )

    if report["out_of_coverage"]:
        entries = []
        by_type = {}
        for item in report["out_of_coverage"]:
            by_type.setdefault(item["type"], []).append(item)
        for gate_type in sorted(by_type):
            items = by_type[gate_type]
            entries.append(
                model.unsupported_primitive(
                    gate_type,
                    "; ".join(sorted({item["reason"] for item in items}))[:900],
                    count=len(items),
                    gate_library="AGILEX_TENNM",
                )
            )
        findings.append(
            model.finding(
                "hal_agilex/inventory/out-of-coverage-configuration",
                "Instances of a covered primitive in an unvalidated configuration",
                model.STATUS_UNSUPPORTED,
                structural,
                model.scope([artifact_id], gate_types=sorted(by_type)),
                summary=(
                    "{} instance(s) use pins or parameters whose behaviour has not "
                    "been validated against the vendor export.".format(
                        len(report["out_of_coverage"])
                    )
                ),
                severity="high",
                unsupported_dict=model.unsupported(
                    "configuration",
                    "the instance uses a mode outside the validated subset",
                    primitives=entries,
                ),
                data={"instances": report["out_of_coverage"][:20]},
                tags=["coverage", "agilex"],
            )
        )

    if not report["uncovered"] and not report["out_of_coverage"]:
        findings.append(
            model.finding(
                "hal_agilex/inventory/fully-covered",
                "Every primitive in the export is inside the validated coverage",
                model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
                structural,
                model.scope([artifact_id], gate_types=sorted(report["covered"])),
                summary=(
                    "All {} instances are {}, each in a configuration this package "
                    "models and has validated against the vendor export.".format(
                        report["total"], " or ".join(sorted(report["covered"]))
                    )
                ),
                bounds_dict=model.unbounded(
                    description="a statement about the netlist text, not about an execution"
                ),
                assumptions=[
                    model.assumption(
                        "reader-completeness",
                        "The .vo reader refuses Verilog it does not understand, so "
                        "an instance cannot have been skipped silently.",
                        kind="tool",
                        discharged=True,
                    ),
                    model.assumption(
                        "coverage-list",
                        "Coverage means the primitive semantics in "
                        "tools/hal_agilex/primitives.py, which were validated by "
                        "simulating this export against the RTL it was synthesised "
                        "from.",
                        kind="library",
                    ),
                ],
                data={"histogram": report["covered"]},
                tags=["coverage", "agilex"],
            )
        )

    notes = []
    if metadata:
        notes.append(
            "export header: "
            + "; ".join("{}={}".format(key, metadata[key]) for key in sorted(metadata))
        )

    return model.document(
        PRODUCER,
        [artifact],
        {
            "entry_point": "hal_agilex.inventory.build_document",
            "plugin": {"name": "hal_agilex", "version": "1.0.0"},
        },
        findings,
        generated_at=generated_at or utc_now(),
        notes=notes or None,
    )


def build_document_for_file(path, artifact_id=None, generated_at=None):
    netlist = parse_file(path)
    return build_document(netlist, path, artifact_id=artifact_id, generated_at=generated_at)
