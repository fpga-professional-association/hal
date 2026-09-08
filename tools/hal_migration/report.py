"""Render an assessment findings document as a Markdown report.

The report is the deliverable of this tool, so it is written to be hard to
misread:

* it opens with what the assessment is *not* -- no conversion, no timing;
* every mapping row carries its category and the number of obligations that
  are still open, so a "supported" row never appears without its remaining
  work;
* unresolved primitives get their own section with the reason each one is
  unresolved, including the ones that were downgraded because the source
  metadata was missing;
* the obligation section lists every check that has to be performed, grouped
  into semantic and physical, with the target assumption each one rests on.

Input is the findings document produced by :mod:`hal_migration.assess`; the
inventory is optional and only used for the source-side counts.
"""

__all__ = ["render_markdown"]

_BANNER = (
    "> **This is an assessment, not a conversion.** No netlist was converted, "
    "re-synthesised or simulated. Mapping categories are structural proposals from a "
    "manually reviewed catalogue; none of them is evidence of behavioural equivalence, "
    "resource fit or timing closure."
)

_CATEGORY_ORDER = ("supported", "candidate", "unresolved")


def _escape(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def _table(header, rows):
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_escape(cell) for cell in row) + " |")
    return lines


def _artifact_lines(document):
    lines = []
    for artifact in document.get("artifacts", []):
        pin = artifact.get("sha256")
        pin = "sha256 `{}`".format(pin[:16]) if pin else "not hashed: {}".format(
            artifact.get("unhashed_reason", "reason not recorded")
        )
        detail = [
            "- **{}** ({}) - {}".format(
                artifact["artifact_id"], artifact.get("kind", "?"), pin
            )
        ]
        if artifact.get("path"):
            detail.append("  - path: `{}`".format(artifact["path"]))
        if artifact.get("design_name"):
            detail.append("  - design: `{}`".format(artifact["design_name"]))
        if artifact.get("device_name"):
            detail.append("  - device: `{}`".format(artifact["device_name"]))
        if artifact.get("gate_library"):
            library = artifact["gate_library"]
            detail.append(
                "  - gate library: `{}`{}".format(
                    library.get("name", "?"),
                    " (sha256 `{}`)".format(library["sha256"][:16])
                    if library.get("sha256")
                    else "",
                )
            )
        if artifact.get("description"):
            detail.append("  - {}".format(artifact["description"]))
        lines.extend(detail)
    return lines


def _primitive_findings(document):
    return [
        finding
        for finding in document.get("findings", [])
        if finding["id"].startswith("migration/primitive/")
    ]


def _finding_by_id(document, finding_id):
    for finding in document.get("findings", []):
        if finding["id"] == finding_id:
            return finding
    return None


def render_markdown(document, inventory=None, title=None):
    """Render ``document`` (an assessment findings document) as Markdown text."""
    analysis = document.get("analysis", {})
    configuration = analysis.get("configuration", {})
    summary_finding = _finding_by_id(document, "migration/summary/inventory")
    metrics = (summary_finding or {}).get("metrics", {})
    summary_data = (summary_finding or {}).get("data", {})

    target = configuration.get("target") or {}
    target_name = " ".join(
        part
        for part in (target.get("vendor"), target.get("family"))
        if part and not (target.get("family") or "").startswith(target.get("vendor") or "\0")
    ) or (target.get("family") or "target")
    lines = [
        "# {}".format(
            title
            or "FPGA migration assessment: {} -> {}".format(
                ((document.get("artifacts") or [{}])[0]).get("design_name")
                or "source design",
                target_name,
            ).strip()
        ),
        "",
        _BANNER,
        "",
        "## Inputs and versions",
        "",
        "- findings schema: `{}`".format(document.get("schema_version")),
        "- inventory format: `{}`".format(configuration.get("inventory_version")),
        "- catalogue format: `{}`, catalogue `{}` revision `{}`".format(
            configuration.get("catalogue_version"),
            configuration.get("catalogue_id"),
            configuration.get("catalogue_revision"),
        ),
        "- produced by `{}` {}".format(
            (document.get("producer") or {}).get("name"),
            (document.get("producer") or {}).get("version"),
        ),
    ]
    source_tool = configuration.get("source_tool")
    if source_tool:
        lines.append(
            "- source netlist produced by: `{} {}` (stated by the caller)".format(
                source_tool.get("name", "?"), source_tool.get("version", "?")
            )
        )
    else:
        lines.append(
            "- source netlist producing tool and version: **not stated** - the "
            "assessment cannot be tied to a synthesis tool version"
        )
    if analysis.get("hal"):
        lines.append("- HAL: `{}`".format(analysis["hal"].get("version", "?")))
    review = summary_data.get("catalogue_review") or {}
    if review:
        lines.append(
            "- catalogue reviewed by {} on {} ({})".format(
                review.get("reviewed_by", "?"),
                review.get("reviewed_at", "?"),
                review.get("method", "review method not stated"),
            )
        )
    lines.append("")
    lines.extend(_artifact_lines(document))
    lines.append("")

    lines += ["## Summary", ""]
    if metrics:
        lines.extend(
            _table(
                ["category", "gate types", "gate instances"],
                [
                    [
                        category,
                        metrics.get("{}_types".format(category), 0),
                        metrics.get("{}_instances".format(category), 0),
                    ]
                    for category in _CATEGORY_ORDER
                ],
            )
        )
        lines.append("")
    if inventory is not None:
        totals = inventory.get("totals", {})
        lines.append(
            "Source design: {} gate(s), {} net(s), {} gate type(s), {} clock net(s), "
            "{} reset/set net(s), {} top-level port(s).".format(
                totals.get("gates", "?"),
                totals.get("nets", "?"),
                totals.get("gate_types", "?"),
                totals.get("clock_signals", 0),
                totals.get("reset_signals", 0),
                totals.get("io_ports", 0),
            )
        )
        lines.append("")

    lines += ["## Primitive mappings", ""]
    rows = []
    for finding in _primitive_findings(document):
        data = finding.get("data", {})
        mapping = data.get("mapping", {})
        source = data.get("source", {})
        rows.append(
            [
                "`{}`".format(source.get("gate_type")),
                source.get("instances", 0),
                source.get("category", "?"),
                mapping.get("category", "?"),
                "`{}`".format(mapping.get("target_primitive"))
                if mapping.get("target_primitive")
                else "-",
                len(data.get("obligations") or []),
            ]
        )
    rows.sort(key=lambda row: (_CATEGORY_ORDER.index(row[3]) if row[3] in _CATEGORY_ORDER else 9, row[0]))
    lines.extend(
        _table(
            ["source primitive", "instances", "source role", "category", "target", "open obligations"],
            rows,
        )
    )
    lines.append("")
    lines.append(
        "`supported` = a reviewer proposed the mapping and the inventory carried the "
        "metadata it requires. It is not a claim that the behaviour matches."
    )
    lines.append("")

    unresolved = [
        finding
        for finding in _primitive_findings(document)
        if (finding.get("data", {}).get("mapping", {}).get("category")) == "unresolved"
    ]
    lines += ["## Unresolved primitives", ""]
    if not unresolved:
        lines.append("None: every source gate type has a reviewed mapping proposal.")
        lines.append("")
    else:
        for finding in unresolved:
            data = finding.get("data", {})
            mapping = data.get("mapping", {})
            source = data.get("source", {})
            lines.append(
                "### `{}` - {} instance(s)".format(
                    source.get("gate_type"), source.get("instances", 0)
                )
            )
            lines.append("")
            if mapping.get("downgraded_from"):
                lines.append(
                    "Downgraded from `{}`: the source metadata this mapping requires "
                    "({}) is not in the inventory.".format(
                        mapping["downgraded_from"],
                        ", ".join(mapping.get("missing_metadata") or []),
                    )
                )
                lines.append("")
            for reason in mapping.get("reasons") or []:
                lines.append("- {}".format(reason))
            for gap in data.get("metadata_gaps") or []:
                lines.append("- metadata gap: {}".format(gap))
            lines.append("")

    lines += ["## Verification obligations", ""]
    obligations = {}
    for finding in document.get("findings", []):
        source_type = (finding.get("data", {}).get("source") or {}).get("gate_type")
        for obligation in finding.get("data", {}).get("obligations") or []:
            entry = obligations.setdefault(
                obligation["id"], {"obligation": obligation, "from": set()}
            )
            entry["from"].add(source_type or "design")
    for kind in ("semantic", "physical"):
        selected = [
            entry
            for entry in obligations.values()
            if entry["obligation"].get("kind") == kind
        ]
        lines.append("### {} obligations".format(kind.capitalize()))
        lines.append("")
        if not selected:
            lines.append("None.")
            lines.append("")
            continue
        for entry in sorted(selected, key=lambda item: item["obligation"]["id"]):
            obligation = entry["obligation"]
            lines.append(
                "- **{}** (`{}`, severity {})".format(
                    obligation.get("title"), obligation["id"], obligation.get("severity", "?")
                )
            )
            lines.append("  - check: {}".format(obligation.get("check")))
            if obligation.get("target_assumption"):
                lines.append(
                    "  - target assumption while open: {}".format(
                        obligation["target_assumption"]
                    )
                )
            lines.append(
                "  - applies to: {}".format(", ".join(sorted(entry["from"])))
            )
        lines.append("")

    gaps_finding = _finding_by_id(document, "migration/metadata/gaps")
    lines += ["## Metadata the source does not provide", ""]
    if gaps_finding is None:
        lines.append("None recorded.")
        lines.append("")
    else:
        for gap in gaps_finding.get("data", {}).get("metadata_gaps") or []:
            lines.append(
                "- `{}`{}: {}".format(
                    gap.get("scope"),
                    " ({})".format(gap["field"]) if gap.get("field") else "",
                    gap.get("detail"),
                )
            )
        lines.append("")

    lines += ["## What this report does not say", ""]
    for note in document.get("notes") or []:
        lines.append("- {}".format(note))
    lines.append(
        "- no finding in this report has a status other than `heuristic`, "
        "`unsupported` or `unknown`; nothing here is a proof"
    )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"
