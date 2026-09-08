"""DANA register groups -> candidate register blocks.

The input is the document ``hal_findings.adapters.dataflow`` writes: one
``heuristic`` finding per group (``dataflow/group/NNNN``) plus, when the run did
not cover every sequential gate type, one ``unsupported`` coverage finding.

Two things this adapter deliberately does *not* do:

* it never promotes a group.  DANA's own adapter already fixed the status at
  ``heuristic``, and the block inherits it, so a register grouping can never be
  drawn the way a verified adder is;
* it never drops the coverage finding.  ``dataflow/coverage/unsupported-primitives``
  becomes a document note, because "no group covers these flip-flops" is a
  statement about the *analysis*, not about the design, and the report has to be
  able to repeat it.
"""

from .. import model
from . import common

__all__ = ["PLUGIN_NAMES", "FINDING_PREFIXES", "ADAPTER_NAME", "contributions"]

ADAPTER_NAME = "dataflow"
PLUGIN_NAMES = ("dataflow_analysis", "dataflow")
FINDING_PREFIXES = ("dataflow/",)

_GROUP_PREFIX = "dataflow/group/"


def _group_claim(source, finding, gate_ids, inventory):
    data = finding.get("data") or {}
    control_roles = data.get("control_net_roles") or []
    text = (
        "{} flip-flop(s) ({}) were grouped into one candidate word-level register "
        "by dataflow analysis (DANA). This is structural evidence -- shared control "
        "signals and common predecessors/successors -- not a proof that the design "
        "treats these bits as one word.".format(
            len(gate_ids), common.gate_name_list(inventory, gate_ids)
        )
    )
    if control_roles:
        text += " Shared control signal role(s): {}.".format(", ".join(sorted(control_roles)))
    return model.claim(
        "{}/{}".format(source.source_id, finding["id"]),
        text,
        finding.get("status", "heuristic"),
        gate_ids,
        [common.evidence_from_finding(source, finding)],
        metrics=finding.get("metrics"),
    )


def contributions(source, inventory):
    """Return ``(contributions, notes)`` for one dataflow findings document."""
    results = []
    notes = []

    for order, finding in enumerate(sorted(source.by_prefix(_GROUP_PREFIX),
                                           key=lambda entry: entry.get("id", ""))):
        scope = finding.get("scope") or {}
        gate_ids = common.resolve_gates(
            scope.get("gates"), inventory, source, finding.get("id", "?")
        )
        if not gate_ids:
            notes.append(
                "dataflow finding {!r} claims gates that are not in this netlist; it "
                "was skipped and its gates are listed under the source's "
                "unresolved_gates".format(finding.get("id"))
            )
            continue
        data = finding.get("data") or {}
        attributes = {
            "width": len(gate_ids),
            "dataflow_group_id": data.get("group_id"),
            "successor_groups": data.get("successor_groups"),
            "predecessor_groups": data.get("predecessor_groups"),
            "control_net_roles": data.get("control_net_roles"),
        }
        attributes = {key: value for key, value in attributes.items() if value is not None}
        results.append(
            common.Contribution(
                finding["id"],
                "register",
                "candidate register [{} bit]".format(len(gate_ids)),
                gate_ids,
                [_group_claim(source, finding, gate_ids, inventory)],
                source.source_id,
                attributes=attributes,
                order=order,
            )
        )

    for finding in source.findings():
        if finding.get("id") == "dataflow/coverage/unsupported-primitives":
            types = ", ".join(sorted((finding.get("scope") or {}).get("gate_types") or []))
            notes.append(
                "dataflow analysis did not group every sequential gate type "
                "({}); their absence from a register block is a limitation of the "
                "analysis, not evidence that no register exists "
                "[{}#{}]".format(types or "see the finding", source.source_id, finding["id"])
            )

    if not results:
        notes.append(
            "the dataflow document {!r} contained no register groups".format(source.source_id)
        )
    return results, notes
