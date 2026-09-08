"""The templated explanatory report.

This is a *template*, not a writer.  Every sentence in the output is assembled
from fields of the recovered-block document, and every functional claim is
printed together with the two things that make it checkable:

* the **gate set** it is about (names and IDs, truncation always marked), and
* the **finding** it came from -- ``source_id#finding_id``, its status, its
  method, the document it lives in, and the assumptions that finding did not
  discharge.

There is no natural-language generation anywhere in this module and no model is
consulted.  ``hal_explain prose`` exists as a separate, optional command for
people who want an LLM to paraphrase; it consumes this same JSON, and nothing in
the pipeline below depends on it.

Verified and heuristic material is separated at three levels -- section, badge
and wording -- because a reader who skims must not be able to come away with a
heuristic register grouping in their head as a fact.
"""

from . import model

__all__ = ["ReportOptions", "build_report", "write_report", "BADGES"]

#: confidence -> the badge printed next to every block and claim.
BADGES = {
    model.CONFIDENCE_VERIFIED: "VERIFIED",
    model.CONFIDENCE_HEURISTIC: "HEURISTIC",
    model.CONFIDENCE_UNKNOWN: "INCONCLUSIVE",
    model.CONFIDENCE_REFUTED: "REFUTED",
}

_SECTIONS = (
    (
        model.CONFIDENCE_VERIFIED,
        "Verified blocks",
        "A decision procedure proved these. Each one still rests on the assumptions "
        "printed with it -- read them before quoting the claim.",
    ),
    (
        model.CONFIDENCE_HEURISTIC,
        "Heuristic blocks",
        "Structural evidence only. These are candidates: plausible groupings that no "
        "solver confirmed. Do not cite them as design facts.",
    ),
    (
        model.CONFIDENCE_REFUTED,
        "Refuted blocks",
        "An analysis produced a counterexample against a claim about these gates.",
    ),
    (
        model.CONFIDENCE_UNKNOWN,
        "Inconclusive blocks",
        "These gate sets were examined and no verdict was reached. An inconclusive "
        "result is not a negative result: it says nothing about the design.",
    ),
)


class ReportOptions(object):
    """Presentation limits, all of them visible in the output when they bite."""

    def __init__(self, max_gate_names=24, max_port_names=12, include_evidence_index=True):
        self.max_gate_names = int(max_gate_names)
        self.max_port_names = int(max_port_names)
        self.include_evidence_index = bool(include_evidence_index)


def _names(refs, limit):
    names = ["{} (#{})".format(ref.get("name", ""), ref.get("id")) for ref in refs]
    if len(names) <= limit:
        return ", ".join(names) if names else "none"
    return "{}, ... (+{} more, {} total)".format(
        ", ".join(names[:limit]), len(names) - limit, len(names)
    )


def _gate_ids(ids, limit):
    ids = sorted(ids)
    if len(ids) <= limit:
        return ", ".join(str(entry) for entry in ids)
    return "{}, ... (+{} more)".format(
        ", ".join(str(entry) for entry in ids[:limit]), len(ids) - limit
    )


def _evidence_line(evidence):
    method = evidence.get("method") or {}
    parts = [
        "`{}#{}`".format(evidence.get("source_id"), evidence.get("finding_id")),
        "status `{}`".format(evidence.get("status")),
    ]
    if method.get("name"):
        parts.append(
            "method {} ({}{})".format(
                method["name"],
                method.get("kind", "?"),
                ", bounded" if method.get("bounded") else "",
            )
        )
    if evidence.get("cycle_bound") is not None:
        parts.append("cycle bound {}".format(evidence["cycle_bound"]))
    if evidence.get("document"):
        parts.append("in `{}`".format(evidence["document"]))
    return " - ".join(parts)


def _claim_lines(claim, options):
    lines = []
    badge = BADGES.get(claim.get("confidence"), "?")
    bound = ""
    if claim.get("bounded"):
        bound = (
            " (bounded: holds up to {} cycles)".format(claim["cycle_bound"])
            if claim.get("cycle_bound") is not None
            else " (bounded)"
        )
    lines.append("- **[{}]**{} {}".format(badge, bound, claim.get("text", "")))
    lines.append(
        "  - gates ({}): {}".format(
            len(claim.get("gates", [])), _gate_ids(claim.get("gates", []), options.max_gate_names)
        )
    )
    for evidence in claim.get("evidence", []):
        lines.append("  - evidence: {}".format(_evidence_line(evidence)))
        if evidence.get("open_assumptions"):
            lines.append(
                "    - assumptions NOT discharged: {}".format(
                    ", ".join(evidence["open_assumptions"])
                )
            )
        for artifact in evidence.get("artifacts") or []:
            lines.append("    - evidence file: `{}`".format(artifact))
    return lines


def _block_lines(block, options):
    lines = []
    lines.append(
        "### `{}` - {} [{}]".format(
            block["block_id"], block["label"], BADGES.get(block["confidence"], "?")
        )
    )
    lines.append("")
    lines.append("- kind: `{}`".format(block["kind"]))
    if block.get("contested"):
        lines.append(
            "- **contested**: at least one analysis refuted a claim about this block"
        )
    lines.append(
        "- gates ({}): {}".format(
            len(block.get("gates", [])), _names(block.get("gates", []), options.max_gate_names)
        )
    )
    ports = block.get("ports") or {}
    for key, caption in (("inputs", "inputs"), ("control", "control"), ("outputs", "outputs")):
        if ports.get(key):
            lines.append(
                "- {} ({}): {}".format(
                    caption, len(ports[key]), _names(ports[key], options.max_port_names)
                )
            )
    attributes = block.get("attributes") or {}
    if attributes:
        lines.append(
            "- attributes: {}".format(
                ", ".join(
                    "{}={}".format(key, value) for key, value in sorted(attributes.items())
                )
            )
        )
    for note in block.get("notes") or []:
        lines.append("- note: {}".format(note))
    lines.append("")
    lines.append("Claims:")
    lines.append("")
    for claim in block.get("claims", []):
        lines.extend(_claim_lines(claim, options))
    lines.append("")
    return lines


def _coverage_table(coverage):
    rows = [
        ("gates in the netlist", coverage.get("gates_total")),
        ("gates in a block", coverage.get("gates_in_blocks")),
        ("gates in no block (unknown regions)", coverage.get("gates_unclassified")),
        ("gates covered by a verified claim", coverage.get("gates_verified")),
        ("gates covered only by a heuristic", coverage.get("gates_heuristic")),
        ("gates covered only by an inconclusive claim", coverage.get("gates_unknown_confidence")),
        ("gates claimed by more than one block", coverage.get("gates_overlapping")),
        ("sequential gates in the netlist", coverage.get("sequential_gates_total")),
        ("sequential gates in no block", coverage.get("sequential_gates_unclassified")),
        ("blocks", coverage.get("block_count")),
        ("unknown regions", coverage.get("unknown_region_count")),
        ("claims", coverage.get("claim_count")),
    ]
    lines = ["| measure | value |", "| --- | ---: |"]
    for label, value in rows:
        if value is None:
            continue
        lines.append("| {} | {} |".format(label, value))
    return lines


def build_report(document, options=None, title=None, diagram_path=None):
    """Render the whole report as Markdown text."""
    options = options or ReportOptions()
    design = document.get("design") or {}
    coverage = document.get("coverage") or {}
    name = design.get("design_name") or design.get("artifact_id") or "design"

    lines = []
    lines.append("# {}".format(title or "Recovered block model: {}".format(name)))
    lines.append("")
    lines.append(
        "Produced by `{}` {} from {} findings document(s). Every claim below names "
        "the gates it is about and the finding it came from; nothing here was "
        "written by a language model.".format(
            (document.get("producer") or {}).get("name", "hal_explain"),
            (document.get("producer") or {}).get("version", "?"),
            len(document.get("sources", [])),
        )
    )
    lines.append("")
    lines.append("## What this is, and what it is not")
    lines.append("")
    lines.append(
        "- This is a **composition of existing analysis results**, not a new "
        "analysis. If an analysis did not claim something, this report does not "
        "either."
    )
    lines.append(
        "- **Verified** means a decision procedure proved the claim, under the "
        "assumptions printed with it. **Heuristic** means structural evidence and "
        "nothing more. **Inconclusive** means the question was asked and not "
        "answered - it is never evidence of absence."
    )
    lines.append(
        "- Gates that no analysis claimed are listed under *Unclassified regions*. "
        "They are part of the design and their function is unknown."
    )
    lines.append("")

    lines.append("## Design")
    lines.append("")
    lines.append("- artifact id: `{}`".format(design.get("artifact_id")))
    if design.get("design_name"):
        lines.append("- design name: `{}`".format(design["design_name"]))
    if design.get("device_name"):
        lines.append("- device: `{}`".format(design["device_name"]))
    if design.get("path"):
        lines.append("- source: `{}`".format(design["path"]))
    if design.get("sha256"):
        lines.append("- sha256: `{}`".format(design["sha256"]))
    elif design.get("unhashed_reason"):
        lines.append("- **not pinned by hash**: {}".format(design["unhashed_reason"]))
    library = design.get("gate_library") or {}
    if library:
        lines.append(
            "- gate library: {}".format(
                " ".join(
                    "`{}`".format(library[key]) for key in ("name", "path") if library.get(key)
                )
            )
        )
    lines.append("- gates: {}, nets: {}".format(design.get("gate_count"), design.get("net_count")))
    if diagram_path:
        lines.append("- block diagram: `{}` (render with `dot -Tsvg`)".format(diagram_path))
    lines.append("")

    lines.append("## Coverage")
    lines.append("")
    lines.extend(_coverage_table(coverage))
    lines.append("")
    total = coverage.get("gates_total") or 0
    unclassified = coverage.get("gates_unclassified") or 0
    if total and unclassified:
        lines.append(
            "**{} of {} gates ({:.0%}) are not explained by any analysis in this "
            "model.**".format(unclassified, total, float(unclassified) / float(total))
        )
        lines.append("")

    lines.append("## Evidence sources")
    lines.append("")
    lines.append("| source | adapter | analysis | findings | statuses | document |")
    lines.append("| --- | --- | --- | ---: | --- | --- |")
    for source in document.get("sources", []):
        analysis = source.get("analysis") or {}
        plugin = (analysis.get("plugin") or {}).get("name", "?")
        statuses = ", ".join(
            "{} {}".format(count, status)
            for status, count in sorted((source.get("status_counts") or {}).items())
        )
        lines.append(
            "| `{}` | `{}` | `{}` | {} | {} | `{}` |".format(
                source.get("source_id"),
                source.get("adapter"),
                plugin,
                source.get("finding_count"),
                statuses or "-",
                source.get("path") or "-",
            )
        )
    if not document.get("sources"):
        lines.append("| _none_ | | | | | |")
    lines.append("")
    for source in document.get("sources", []):
        if source.get("unresolved_gates"):
            lines.append(
                "- **`{}` has {} unresolved gate reference(s)**: {}. The blocks built "
                "from this source are incomplete.".format(
                    source["source_id"],
                    len(source["unresolved_gates"]),
                    "; ".join(source["unresolved_gates"][:8]),
                )
            )
    lines.append("")

    blocks_by_confidence = {}
    for block in document.get("blocks", []):
        blocks_by_confidence.setdefault(block["confidence"], []).append(block)

    for confidence, heading, caption in _SECTIONS:
        blocks = blocks_by_confidence.get(confidence, [])
        lines.append("## {} ({})".format(heading, len(blocks)))
        lines.append("")
        lines.append(caption)
        lines.append("")
        if not blocks:
            lines.append("_None in this model._")
            lines.append("")
            continue
        for block in blocks:
            lines.extend(_block_lines(block, options))

    regions = document.get("unknown_regions", [])
    lines.append("## Unclassified regions ({})".format(len(regions)))
    lines.append("")
    lines.append(
        "Gates that no analysis in this model claimed. They are drawn in the block "
        "diagram too. Their function is unknown - not trivial, not absent, unknown."
    )
    lines.append("")
    if not regions:
        lines.append("_Every gate of this netlist is in a block._")
        lines.append("")
    for region in regions:
        lines.append("### `{}` - {}".format(region["region_id"], region["label"]))
        lines.append("")
        lines.append("- reason: {}".format(region["reason"]))
        lines.append(
            "- gates ({}): {}".format(
                len(region.get("gates", [])),
                _names(region.get("gates", []), options.max_gate_names),
            )
        )
        if region.get("gate_types"):
            lines.append(
                "- gate types: {}".format(
                    ", ".join(
                        "{} x{}".format(key, value)
                        for key, value in sorted(region["gate_types"].items())
                    )
                )
            )
        if region.get("sequential_gate_count"):
            lines.append(
                "- **{} sequential gate(s) in this region hold state nothing here "
                "explains**".format(region["sequential_gate_count"])
            )
        lines.append("")

    overlaps = document.get("overlaps") or []
    if overlaps:
        lines.append("## Overlapping claims ({})".format(len(overlaps)))
        lines.append("")
        lines.append(
            "These gates are claimed by more than one block. That is not resolved "
            "here: two analyses disagreeing about a boundary is a result, and the "
            "`owner` column only says which block the diagram draws the gate in."
        )
        lines.append("")
        lines.append("| gate | blocks | diagram owner |")
        lines.append("| --- | --- | --- |")
        for overlap in overlaps:
            gate = overlap.get("gate") or {}
            lines.append(
                "| `{}` (#{}) | {} | `{}` |".format(
                    gate.get("name"),
                    gate.get("id"),
                    ", ".join("`{}`".format(entry) for entry in overlap.get("block_ids", [])),
                    overlap.get("owner"),
                )
            )
        lines.append("")

    lines.append("## Limitations and notes")
    lines.append("")
    notes = document.get("notes") or []
    if not notes:
        lines.append("_No composition notes were recorded._")
    for note in notes:
        lines.append("- {}".format(note))
    lines.append("")

    if options.include_evidence_index:
        lines.append("## Evidence index")
        lines.append("")
        lines.append("| finding | status | block | source document |")
        lines.append("| --- | --- | --- | --- |")
        rows = []
        for block in document.get("blocks", []):
            for claim in block.get("claims", []):
                for evidence in claim.get("evidence", []):
                    rows.append(
                        (
                            "{}#{}".format(
                                evidence.get("source_id"), evidence.get("finding_id")
                            ),
                            evidence.get("status"),
                            block["block_id"],
                            evidence.get("document") or "-",
                        )
                    )
        for row in sorted(set(rows)):
            lines.append("| `{}` | `{}` | `{}` | `{}` |".format(*row))
        if not rows:
            lines.append("| _none_ | | | |")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def write_report(document, path, **kwargs):
    """Write the Markdown report to ``path`` and return the path."""
    text = build_report(document, **kwargs)
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return str(path)
