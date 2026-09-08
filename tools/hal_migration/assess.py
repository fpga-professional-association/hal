"""Turn a source inventory plus a target catalogue into a findings document.

The output is a ``hal_findings`` document, so the one rule that matters is
enforced by the shared schema rather than by this module's prose: an assessment
can only ever be a *structural, heuristic* claim.

* a proposed mapping (``supported`` or ``candidate``) is a ``heuristic``
  finding produced by a ``structural`` method -- the schema forbids a heuristic
  from carrying a formal method or claiming unbounded validity, so no consumer
  can render it as a proof;
* an unresolved primitive is an ``unsupported`` finding naming the gate type,
  its count and example gates, so a gap is a first-class result and not an
  absence;
* timing closure, resource fit and whole-design equivalence are emitted as
  ``unknown`` findings on every run, so a report can never be produced that
  quietly omits them.

Nothing here converts anything, and no status in the output means "converted".
"""

import re

from hal_findings import model
from hal_findings.adapters import common
from hal_findings.serialize import sha256_file

from . import __version__
from . import catalogue as catalogue_module
from . import obligations as obligations_module

__all__ = [
    "METHOD_NAME",
    "ENTRY_POINT",
    "build_document",
    "summarize",
]

METHOD_NAME = "source/target primitive inventory comparison"
ENTRY_POINT = "hal_migration.assess.build_document"

_METHOD_DESCRIPTION = (
    "Each source gate type from the inventory is looked up in a manually reviewed "
    "target capability catalogue. The comparison is structural and by gate type: it "
    "reads no Boolean function, simulates nothing and proves nothing. A 'supported' "
    "mapping means a reviewer proposed it and the inventory carried the metadata the "
    "mapping requires -- it does not mean the primitive was converted, that the "
    "behaviour matches, or that the design would meet timing in the target device."
)

_SEVERITY = {"supported": "info", "candidate": "medium", "unresolved": "high"}

_CATALOGUE_ASSUMPTION = model.assumption(
    "catalogue.reviewed",
    "The target capability catalogue is accurate for the target family and "
    "toolchain version it names. This tool does not check the catalogue against any "
    "vendor documentation or device model.",
    kind="user_provided",
    discharged=False,
)

_NO_CONVERSION_ASSUMPTION = model.assumption(
    "assessment.no-conversion",
    "No netlist was converted, rewritten or re-synthesised. Every statement is about "
    "the source inventory and a reviewed table, not about a migrated design.",
    kind="tool",
    discharged=False,
)


def _identifier(text):
    """Make ``text`` usable as a findings identifier without losing readability."""
    cleaned = re.sub(r"[^A-Za-z0-9_.:/-]", "-", str(text))
    cleaned = cleaned.lstrip("-.:/") or "unnamed"
    return cleaned[:96]


def _technology_label(technology):
    """"<vendor> <family>", without repeating a vendor the family already names."""
    vendor = (technology or {}).get("vendor") or ""
    family = (technology or {}).get("family") or ""
    if family and vendor and not family.startswith(vendor):
        return "{} {}".format(vendor, family)
    return family or vendor or "<unknown technology>"


def _gate_refs(primitive, artifact_id):
    refs = []
    for entry in primitive.get("example_gates") or []:
        module = {"name": entry["module"]} if entry.get("module") else None
        refs.append(
            model.gate_ref(
                artifact_id,
                entry["id"],
                entry.get("name", ""),
                gate_type=primitive.get("gate_type"),
                module=module,
            )
        )
    return refs


def _net_refs(signals, artifact_id, role):
    return [
        model.net_ref(artifact_id, signal["net_id"], signal.get("net_name", ""), role=role)
        for signal in signals
    ]


def _artifacts(inventory, catalogue, catalogue_path):
    source = inventory.get("source") or {}
    artifact_id = source.get("artifact_id", "source_netlist")

    description_parts = []
    if source.get("vendor") or source.get("family") or source.get("device"):
        description_parts.append(
            "source technology: {}".format(
                " / ".join(
                    str(source[key])
                    for key in ("vendor", "family", "device")
                    if source.get(key)
                )
            )
        )
    tool = source.get("tool") or {}
    if tool:
        description_parts.append(
            "produced by {} {}".format(tool.get("name", "<unnamed tool>"), tool.get("version", "<unknown version>"))
        )
    else:
        description_parts.append("producing tool and version not stated")

    netlist_artifact = model.artifact(
        artifact_id,
        kind="netlist",
        path=source.get("path"),
        sha256=source.get("sha256"),
        unhashed_reason=source.get("unhashed_reason"),
        size_bytes=source.get("size_bytes"),
        design_name=source.get("design_name"),
        device_name=source.get("device_name"),
        netlist_id=source.get("netlist_id"),
        gate_count=source.get("gate_count"),
        net_count=source.get("net_count"),
        gate_library=source.get("gate_library"),
        description="; ".join(description_parts),
    )

    catalogue_id = _identifier(catalogue.get("catalogue_id", "target_catalogue"))
    sha256 = None
    unhashed_reason = None
    if catalogue_path:
        try:
            sha256 = sha256_file(catalogue_path)
        except OSError:
            sha256 = None
    if sha256 is None:
        unhashed_reason = "the catalogue was supplied in memory and has no file to hash"

    target = catalogue.get("target") or {}
    toolchain = target.get("toolchain") or {}
    catalogue_artifact = model.artifact(
        catalogue_id,
        kind="other",
        path=catalogue_path,
        sha256=sha256,
        unhashed_reason=unhashed_reason,
        description=(
            "target capability catalogue revision {}, target {}{}; reviewed by {} "
            "on {}".format(
                catalogue.get("revision"),
                _technology_label(target),
                " with {} {}".format(toolchain.get("name"), toolchain.get("version"))
                if toolchain
                else "",
                (catalogue.get("review") or {}).get("reviewed_by", "<unknown>"),
                (catalogue.get("review") or {}).get("reviewed_at", "<unknown date>"),
            )
        ),
    )
    return artifact_id, catalogue_id, [netlist_artifact, catalogue_artifact]


def _mapping_assumptions(mapping):
    assumptions = [_CATALOGUE_ASSUMPTION, _NO_CONVERSION_ASSUMPTION]
    for entry in (mapping or {}).get("assumptions") or []:
        assumptions.append(
            model.assumption(
                _identifier(entry["id"]),
                entry["description"],
                kind=entry.get("kind"),
                discharged=False,
            )
        )
    return assumptions


def _primitive_finding(primitive, mapping, resolution, obligation_records, artifact_id,
                       catalogue_id, method, catalogue):
    gate_type = primitive["gate_type"]
    category = resolution["category"]
    count = primitive.get("count", 0)

    data = {
        "source": {
            "gate_type": gate_type,
            "category": primitive.get("category"),
            "properties": primitive.get("properties", []),
            "instances": count,
        },
        "mapping": {
            "category": category,
            "declared_category": resolution.get("declared_category"),
            "catalogue_id": catalogue.get("catalogue_id"),
            "catalogue_revision": catalogue.get("revision"),
        },
        "obligations": obligation_records,
    }
    if mapping:
        data["mapping"]["target_primitive"] = mapping.get("target_primitive")
        data["mapping"]["target_capability"] = mapping.get("target_capability")
        data["mapping"]["rationale"] = mapping.get("rationale")
        if mapping.get("notes"):
            data["mapping"]["notes"] = mapping["notes"]
    if resolution.get("reasons"):
        data["mapping"]["reasons"] = resolution["reasons"]
    if resolution.get("missing_metadata"):
        data["mapping"]["missing_metadata"] = resolution["missing_metadata"]
    if resolution.get("downgraded"):
        data["mapping"]["downgraded_from"] = resolution.get("declared_category")
    if primitive.get("metadata_gaps"):
        data["metadata_gaps"] = primitive["metadata_gaps"]
    if primitive.get("metadata"):
        data["metadata"] = primitive["metadata"]

    scope = model.scope(
        [artifact_id, catalogue_id],
        description="all {} instance(s) of source gate type {}".format(count, gate_type),
        gates=_gate_refs(primitive, artifact_id) or None,
        gate_types=[gate_type],
    )
    metrics = {
        "instances": count,
        "obligations": len(obligation_records),
        "open_obligations": len(obligation_records),
    }
    tags = ["migration", "mapping", category]

    finding_id = "migration/primitive/{}".format(_identifier(gate_type))

    if category == "unresolved":
        reasons = resolution.get("reasons") or ["no reviewed mapping is available"]
        primitives = [
            model.unsupported_primitive(
                gate_type,
                " ".join(reasons),
                count=count or None,
                properties=primitive.get("properties") or None,
                gate_library=((primitive.get("gate_library")) or None),
                example_gates=_gate_refs(primitive, artifact_id) or None,
            )
        ]
        return model.finding(
            finding_id,
            "Unresolved source primitive {} ({} instance(s))".format(gate_type, count),
            model.STATUS_UNSUPPORTED,
            method,
            scope,
            summary=(
                "No mapping for {} can be proposed against catalogue {} revision {}. "
                "{}".format(
                    gate_type,
                    catalogue.get("catalogue_id"),
                    catalogue.get("revision"),
                    " ".join(reasons),
                )
            ),
            severity=_SEVERITY["unresolved"],
            assumptions=_mapping_assumptions(mapping),
            unsupported_dict=model.unsupported(
                "primitive",
                "the target capability catalogue proposes no usable mapping for this "
                "source primitive; the migration effort for it is unquantified",
                primitives,
            ),
            metrics=metrics,
            data=data,
            tags=tags + ["unresolved"],
        )

    target = mapping.get("target_primitive") or mapping.get("target_capability") or "<unnamed>"
    summary = (
        "Catalogue {} revision {} proposes {} -> {} as {}. Structural, by gate type: "
        "{} verification obligation(s) remain open and nothing was converted or "
        "verified.".format(
            catalogue.get("catalogue_id"),
            catalogue.get("revision"),
            gate_type,
            target,
            category,
            len(obligation_records),
        )
    )
    return model.finding(
        finding_id,
        "{} mapping {} -> {} ({} instance(s))".format(
            category.capitalize(), gate_type, target, count
        ),
        model.STATUS_HEURISTIC,
        method,
        scope,
        summary=summary,
        severity=_SEVERITY[category],
        assumptions=_mapping_assumptions(mapping),
        metrics=metrics,
        data=data,
        tags=tags,
    )


def _signal_finding(finding_id, title, signals, role, artifact_id, catalogue_id, method,
                    summary, obligation_records, tags):
    return model.finding(
        finding_id,
        title,
        model.STATUS_HEURISTIC,
        method,
        model.scope(
            [artifact_id, catalogue_id],
            description=title,
            nets=_net_refs(signals, artifact_id, role) or None,
        ),
        summary=summary,
        severity="medium",
        assumptions=[_CATALOGUE_ASSUMPTION, _NO_CONVERSION_ASSUMPTION],
        metrics={"signals": len(signals), "obligations": len(obligation_records)},
        data={"signals": signals, "obligations": obligation_records},
        tags=tags,
    )


def build_document(
    inventory,
    catalogue,
    catalogue_path=None,
    generated_at=None,
    producer_command=None,
    duration_s=None,
):
    """Assess ``inventory`` against ``catalogue`` and return a findings document.

    :param inventory: a validated source inventory document.
    :param catalogue: a validated target capability catalogue.
    :param catalogue_path: path the catalogue was read from (hashed into the
        artifact list so the assessment can be traced to the exact table).
    :returns: a findings document; validate it with
        ``hal_findings.validate.validate_document``.
    """
    artifact_id, catalogue_id, artifacts = _artifacts(inventory, catalogue, catalogue_path)
    templates = catalogue_module.template_index(catalogue)
    mappings = catalogue_module.mappings_by_type(catalogue)

    method = model.method(
        METHOD_NAME,
        "structural",
        False,
        description=_METHOD_DESCRIPTION,
        parameters={
            "catalogue_id": catalogue.get("catalogue_id"),
            "catalogue_revision": catalogue.get("revision"),
            "inventory_version": inventory.get("inventory_version"),
            "catalogue_version": catalogue.get("catalogue_version"),
        },
    )

    findings = []
    counts = {"supported": 0, "candidate": 0, "unresolved": 0}
    instance_counts = {"supported": 0, "candidate": 0, "unresolved": 0}
    downgraded = []
    uncatalogued = []

    for primitive in inventory.get("primitives") or []:
        gate_type = primitive["gate_type"]
        mapping = mappings.get(gate_type)
        resolution = catalogue_module.resolve_mapping(primitive, mapping)
        category = resolution["category"]

        origin = {}
        obligation_ids = set(obligations_module.derive_for_primitive(primitive))
        for obligation_id in obligation_ids:
            origin[obligation_id] = "derived from the inventory"
        for obligation_id in (mapping or {}).get("obligations") or []:
            obligation_ids.add(obligation_id)
            origin[obligation_id] = "required by the catalogue mapping"
        if category == "unresolved":
            obligation_ids.add("semantic/functional-equivalence")
            origin.setdefault("semantic/functional-equivalence", "derived from the inventory")
        obligation_records = obligations_module.resolve(
            sorted(obligation_ids), templates, origin=origin
        )

        counts[category] += 1
        instance_counts[category] += primitive.get("count", 0)
        if resolution.get("downgraded"):
            downgraded.append(gate_type)
        if resolution.get("uncatalogued"):
            uncatalogued.append(gate_type)

        findings.append(
            _primitive_finding(
                primitive,
                mapping,
                resolution,
                obligation_records,
                artifact_id,
                catalogue_id,
                method,
                catalogue,
            )
        )

    # ---- design-level structure: clocks, resets, I/O -----------------------
    clock_signals = inventory.get("clock_signals") or []
    reset_signals = inventory.get("reset_signals") or []
    io_ports = inventory.get("io_ports") or []

    if clock_signals:
        records = obligations_module.resolve(
            ["physical/clock-network-and-buffering"]
            + (["semantic/clock-domain-crossings"] if len(clock_signals) > 1 else []),
            templates,
        )
        findings.append(
            _signal_finding(
                "migration/clocking/signals",
                "Clock signals to re-plan in the target device",
                clock_signals,
                "clock",
                artifact_id,
                catalogue_id,
                method,
                "{} net(s) drive clock pins in the source design. Clock resources, "
                "buffering and skew are device properties and are not assessed here; "
                "the number of nets is not the number of clock domains.".format(
                    len(clock_signals)
                ),
                records,
                ["migration", "clocking"],
            )
        )
    if reset_signals:
        records = obligations_module.resolve(
            ["semantic/async-set-reset-priority", "semantic/power-up-and-init-state"],
            templates,
        )
        findings.append(
            _signal_finding(
                "migration/reset/signals",
                "Reset and set signals whose semantics must be re-checked",
                reset_signals,
                "reset_or_set",
                artifact_id,
                catalogue_id,
                method,
                "{} net(s) drive reset or set pins. Polarity, synchronicity, priority "
                "and the power-up state are technology properties and are carried "
                "over as obligations, not as results.".format(len(reset_signals)),
                records,
                ["migration", "reset"],
            )
        )
    if io_ports:
        records = obligations_module.resolve(
            [
                "physical/io-standard-and-drive",
                "physical/io-pin-assignment",
                "semantic/io-registered-path",
            ],
            templates,
        )
        findings.append(
            model.finding(
                "migration/io/ports",
                "Top-level I/O ports and their constraints",
                model.STATUS_HEURISTIC,
                method,
                model.scope(
                    [artifact_id, catalogue_id],
                    description="top-level ports of the source design",
                    nets=[
                        model.net_ref(
                            artifact_id, port["net_id"], port.get("net_name", ""),
                            role=port["direction"],
                        )
                        for port in io_ports
                    ],
                ),
                summary=(
                    "{} top-level port(s). I/O standards, drive strength, termination "
                    "and pin assignment are not in the netlist and must be migrated "
                    "from the constraint files.".format(len(io_ports))
                ),
                severity="medium",
                assumptions=[_CATALOGUE_ASSUMPTION, _NO_CONVERSION_ASSUMPTION],
                metrics={"ports": len(io_ports), "obligations": len(records)},
                data={"ports": io_ports, "obligations": records},
                tags=["migration", "io"],
            )
        )

    # ---- metadata gaps -----------------------------------------------------
    gaps = inventory.get("metadata_gaps") or []
    if gaps:
        findings.append(
            model.finding(
                "migration/metadata/gaps",
                "Metadata the source netlist and gate library do not provide",
                model.STATUS_UNSUPPORTED,
                method,
                model.scope(
                    [artifact_id, catalogue_id],
                    description="information required for a migration decision that "
                    "could not be read from the source",
                    gate_types=sorted(
                        {gap["scope"] for gap in gaps if gap.get("scope") != "design"}
                    )
                    or None,
                ),
                summary=(
                    "{} metadata gap(s). Where a gap blocks a mapping, the affected "
                    "primitive is reported as unresolved rather than assessed on "
                    "incomplete information.".format(len(gaps))
                ),
                severity="medium",
                unsupported_dict=model.unsupported(
                    "configuration",
                    "the source netlist and gate library do not describe these "
                    "properties, so they cannot be compared against any target",
                ),
                assumptions=[_NO_CONVERSION_ASSUMPTION],
                metrics={"gaps": len(gaps)},
                data={"metadata_gaps": gaps},
                tags=["migration", "metadata", "coverage"],
            )
        )

    # ---- catalogue applicability ------------------------------------------
    mismatch = catalogue_module.library_mismatch(inventory, catalogue)
    if mismatch:
        findings.append(
            model.finding(
                "migration/catalogue/applicability",
                "Catalogue applicability to this netlist is not established",
                model.STATUS_UNKNOWN,
                method,
                model.scope(
                    [artifact_id, catalogue_id],
                    description="the gate library the catalogue was written for",
                ),
                summary=mismatch,
                severity="high",
                assumptions=[_CATALOGUE_ASSUMPTION],
                data={"catalogue_id": catalogue.get("catalogue_id")},
                tags=["migration", "catalogue"],
            )
        )

    # ---- always-open obligations ------------------------------------------
    design_records = obligations_module.resolve(
        obligations_module.derive_design_level(inventory), templates
    )
    findings.append(
        model.finding(
            "migration/obligations/design-level",
            "Design-level verification obligations (none of them assessed here)",
            model.STATUS_UNKNOWN,
            method,
            model.scope(
                [artifact_id, catalogue_id],
                description="the design as a whole",
            ),
            summary=(
                "{} design-level obligation(s) are open, including timing closure, "
                "resource fit and whole-design functional equivalence. This tool "
                "performs no conversion, no synthesis and no timing analysis, so none "
                "of them can be discharged by anything in this document.".format(
                    len(design_records)
                )
            ),
            severity="high",
            assumptions=[_NO_CONVERSION_ASSUMPTION],
            metrics={"obligations": len(design_records)},
            data={"obligations": design_records},
            tags=["migration", "obligations"],
        )
    )

    # ---- inventory summary -------------------------------------------------
    findings.append(
        model.finding(
            "migration/summary/inventory",
            "Source inventory assessed against catalogue {} revision {}".format(
                catalogue.get("catalogue_id"), catalogue.get("revision")
            ),
            model.STATUS_HEURISTIC,
            method,
            model.scope(
                [artifact_id, catalogue_id],
                description="every gate type of the source design",
                gate_types=[
                    primitive["gate_type"] for primitive in inventory.get("primitives") or []
                ]
                or None,
            ),
            summary=(
                "{} gate type(s), {} gate instance(s): {} supported, {} candidate, {} "
                "unresolved by gate type. Categories describe reviewed mapping "
                "proposals, not conversions.".format(
                    sum(counts.values()),
                    sum(instance_counts.values()),
                    counts["supported"],
                    counts["candidate"],
                    counts["unresolved"],
                )
            ),
            severity="info",
            assumptions=[_CATALOGUE_ASSUMPTION, _NO_CONVERSION_ASSUMPTION],
            metrics={
                "gate_types": sum(counts.values()),
                "gate_instances": sum(instance_counts.values()),
                "supported_types": counts["supported"],
                "candidate_types": counts["candidate"],
                "unresolved_types": counts["unresolved"],
                "supported_instances": instance_counts["supported"],
                "candidate_instances": instance_counts["candidate"],
                "unresolved_instances": instance_counts["unresolved"],
            },
            data={
                "by_category": counts,
                "instances_by_category": instance_counts,
                "downgraded_by_missing_metadata": sorted(downgraded),
                "not_in_catalogue": sorted(uncatalogued),
                "source_tool": (inventory.get("source") or {}).get("tool"),
                "catalogue_review": catalogue.get("review"),
            },
            tags=["migration", "summary"],
        )
    )

    analysis = {
        "plugin": {
            "name": "hal_migration",
            "version": __version__,
            "description": "FPGA vendor migration assessment (a tool, not a HAL plugin)",
        },
        "entry_point": ENTRY_POINT,
        "configuration": {
            "catalogue_id": catalogue.get("catalogue_id"),
            "catalogue_revision": catalogue.get("revision"),
            "catalogue_version": catalogue.get("catalogue_version"),
            "inventory_version": inventory.get("inventory_version"),
            "target": catalogue.get("target"),
            "source_tool": (inventory.get("source") or {}).get("tool"),
        },
    }
    hal = (inventory.get("source") or {}).get("hal")
    if hal:
        analysis["hal"] = hal
    if duration_s is not None:
        analysis["duration_s"] = float(duration_s)

    producer = {"name": "hal_migration.assess", "version": __version__}
    if producer_command:
        producer["command"] = list(producer_command)

    notes = [
        "no netlist was converted: this document compares a source inventory with a "
        "reviewed target capability table",
        "'supported' means a reviewer proposed the mapping and the required source "
        "metadata was present; it is not a claim that behaviour matches",
        "timing closure and resource fit are not assessed by any part of this tool",
        "catalogue {} revision {} reviewed by {} on {}".format(
            catalogue.get("catalogue_id"),
            catalogue.get("revision"),
            (catalogue.get("review") or {}).get("reviewed_by", "<unknown>"),
            (catalogue.get("review") or {}).get("reviewed_at", "<unknown date>"),
        ),
    ]
    if uncatalogued:
        notes.append(
            "{} gate type(s) are absent from the catalogue and are reported as "
            "unresolved: {}".format(len(uncatalogued), ", ".join(sorted(uncatalogued)))
        )
    if downgraded:
        notes.append(
            "{} mapping(s) were downgraded to unresolved because the inventory lacked "
            "the metadata they require: {}".format(
                len(downgraded), ", ".join(sorted(downgraded))
            )
        )

    return model.document(
        producer,
        artifacts,
        analysis,
        findings,
        generated_at=generated_at if generated_at is not None else common.utc_now(),
        notes=notes,
    )


def summarize(document):
    """Reduce an assessment document to the counters a caller needs.

    Reads only the summary finding's metrics and the per-finding statuses, so a
    consumer never has to re-derive the categories.
    """
    summary = {
        "supported": 0,
        "candidate": 0,
        "unresolved": 0,
        "obligations": 0,
        "unresolved_types": [],
        "downgraded_types": [],
        "not_in_catalogue": [],
    }
    seen_obligations = set()
    for finding in document.get("findings", []):
        data = finding.get("data") or {}
        for obligation in data.get("obligations") or []:
            seen_obligations.add(obligation["id"])
        mapping = data.get("mapping")
        if mapping and finding["id"].startswith("migration/primitive/"):
            category = mapping.get("category")
            if category in summary:
                summary[category] += 1
            if category == "unresolved":
                summary["unresolved_types"].append(data["source"]["gate_type"])
            if mapping.get("downgraded_from"):
                summary["downgraded_types"].append(data["source"]["gate_type"])
        if finding["id"] == "migration/summary/inventory":
            summary["not_in_catalogue"] = list(data.get("not_in_catalogue") or [])
    summary["obligations"] = len(seen_obligations)
    summary["unresolved_types"].sort()
    summary["downgraded_types"].sort()
    return summary
