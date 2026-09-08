"""``hal_fsm`` results -> state-machine blocks.

``tools/hal_fsm`` already writes exactly the document this adapter wants, and it
already splits the two claims a recovered FSM really is:

* ``fsm/candidate/NNN`` -- *which flip-flops are the state register* is a
  ``heuristic``;
* ``fsm/<machine>/transitions`` -- *what the transitions are* is
  ``proven_under_assumptions``, with the candidate listed as one of its
  undischarged assumptions.

Both end up on the same block, as two claims with two different confidences,
because that is the honest rendering: the reader can disbelieve the state
register without disbelieving the relation that was derived from it.  Everything
else hal_fsm establishes about the machine -- determinism and totality, the
reachable set, the comparison against a reference, the asynchronous control it
could not model -- becomes an additional claim on the same gate set, so a
``proven`` transition relation and an ``unsupported`` reset gap sit next to each
other instead of only the flattering one surviving into the report.

The state diagram hal_fsm wrote is not redrawn here: it is referenced as
evidence, and the block diagram links to it.
"""

from .. import model
from . import common

__all__ = [
    "PLUGIN_NAMES",
    "FINDING_PREFIXES",
    "ADAPTER_NAME",
    "machine_id_of",
    "contributions",
]

ADAPTER_NAME = "fsm"
PLUGIN_NAMES = ("solve_fsm", "hal_fsm")
FINDING_PREFIXES = ("fsm/",)

#: ``fsm/<segment>/...`` segments that are not machine identifiers.
_NON_MACHINE_SEGMENTS = frozenset(("candidate", "candidates", "coverage", "run"))

#: The per-machine finding whose claim defines the block.
_PRIMARY_SUFFIX = "transitions"


def machine_id_of(finding_id):
    """``fsm/machine01/transitions`` -> ``machine01``; ``None`` when not one."""
    parts = str(finding_id).split("/")
    if len(parts) < 3 or parts[0] != "fsm":
        return None
    if parts[1] in _NON_MACHINE_SEGMENTS:
        return None
    return parts[1]


def _state_diagram(finding):
    for path in common.evidence_artifacts(finding):
        if path.endswith(".dot"):
            return path
    return None


def _transitions_attributes(finding):
    data = finding.get("data") or {}
    metrics = finding.get("metrics") or {}
    attributes = {
        "state_bits": metrics.get("state_bits") or len(data.get("bit_order") or []) or None,
        "state_count": metrics.get("states")
        or (len(data.get("states")) if data.get("states") is not None else None),
        "transition_count": metrics.get("transitions")
        or (len(data.get("transitions")) if data.get("transitions") is not None else None),
        "bit_order": data.get("bit_order") or None,
        "initial_state": data.get("initial_state"),
        "initial_state_bits": data.get("initial_state_bits"),
        "solver_mode": data.get("solver_mode"),
    }
    diagram = _state_diagram(finding)
    if diagram:
        attributes["state_diagram"] = diagram
    return {key: value for key, value in attributes.items() if value is not None}


def _claim_text(finding, gate_ids, inventory):
    status = finding.get("status")
    finding_id = finding.get("id", "")
    metrics = finding.get("metrics") or {}
    names = common.gate_name_list(inventory, gate_ids)

    if finding_id.endswith("/" + _PRIMARY_SUFFIX):
        if status == "proven_under_assumptions":
            return (
                "The state register {} has a recovered transition relation of {} and "
                "{}, derived from the netlist by solve_fsm. It holds in every cycle as "
                "far as the listed assumptions hold -- in particular the choice of "
                "state register, which is a heuristic.".format(
                    names,
                    common.plural(metrics.get("states", 0), "state"),
                    common.plural(metrics.get("transitions", 0), "transition"),
                )
            )
        return (
            "No transition relation was recovered for the candidate state register "
            "{} ({}). solve_fsm returns nothing on failure, so there is no partial "
            "state machine to show.".format(names, status)
        )

    title = finding.get("title") or finding_id
    summary = finding.get("summary") or ""
    text = "{} (about the state register {}).".format(title.rstrip("."), names)
    if summary:
        text += " " + summary
    return text


def _candidate_claim(source, finding, gate_ids, inventory):
    data = finding.get("data") or {}
    confidence = finding.get("confidence")
    text = (
        "{} flip-flop(s) ({}) were proposed as a state register by hal_fsm's feedback "
        "analysis. This is a structural guess and stays one however well it "
        "scores.".format(len(gate_ids), common.gate_name_list(inventory, gate_ids))
    )
    if confidence is not None:
        text += " Candidate score: {}.".format(confidence)
    if data.get("sources"):
        text += " Proposed by: {}.".format(", ".join(sorted(data["sources"])))
    return model.claim(
        "{}/{}".format(source.source_id, finding["id"]),
        text,
        finding.get("status", "heuristic"),
        gate_ids,
        [common.evidence_from_finding(source, finding)],
        metrics=finding.get("metrics"),
    )


def contributions(source, inventory):
    """Return ``(contributions, notes)`` for one hal_fsm findings document."""
    notes = []
    machines = {}
    candidates = []

    for finding in source.findings():
        finding_id = str(finding.get("id", ""))
        machine_id = machine_id_of(finding_id)
        if machine_id is None:
            continue
        gate_ids = common.resolve_gates(
            (finding.get("scope") or {}).get("gates"), inventory, source, finding_id
        )
        machines.setdefault(machine_id, []).append((finding, gate_ids))

    for finding in sorted(
        source.by_prefix("fsm/candidate/"), key=lambda entry: entry.get("id", "")
    ):
        gate_ids = common.resolve_gates(
            (finding.get("scope") or {}).get("gates"),
            inventory,
            source,
            finding.get("id", "?"),
        )
        if gate_ids:
            candidates.append((finding, gate_ids))

    results = []
    claimed_candidate_sets = set()

    for order, machine_id in enumerate(sorted(machines)):
        entries = machines[machine_id]
        primary = None
        for finding, gate_ids in entries:
            if str(finding.get("id", "")).endswith("/" + _PRIMARY_SUFFIX):
                primary = (finding, gate_ids)
                break
        gate_ids = sorted({gid for _finding, ids in entries for gid in ids})
        if not gate_ids:
            notes.append(
                "hal_fsm machine {!r} claims gates that are not in this netlist; it "
                "was skipped".format(machine_id)
            )
            continue

        claims = []
        attributes = {"machine_id": machine_id}
        for finding, finding_gates in sorted(entries, key=lambda item: item[0].get("id", "")):
            target = finding_gates or gate_ids
            claims.append(
                model.claim(
                    "{}/{}".format(source.source_id, finding["id"]),
                    _claim_text(finding, target, inventory),
                    finding.get("status", "unknown"),
                    target,
                    [common.evidence_from_finding(source, finding)],
                    cycle_bound=(finding.get("bounds") or {}).get("cycle_bound"),
                    metrics=finding.get("metrics"),
                )
            )
            if str(finding.get("id", "")).endswith("/" + _PRIMARY_SUFFIX):
                attributes.update(_transitions_attributes(finding))

        # The candidate that produced this machine is the same gate set; fold its
        # heuristic claim in so the block carries both strengths at once.
        for finding, candidate_gates in candidates:
            if set(candidate_gates) == set(gate_ids):
                claims.append(_candidate_claim(source, finding, candidate_gates, inventory))
                claimed_candidate_sets.add(tuple(sorted(candidate_gates)))

        state_count = attributes.get("state_count")
        label = "state machine {}".format(machine_id)
        if state_count:
            label = "state machine {} [{} states]".format(machine_id, state_count)
        results.append(
            common.Contribution(
                "fsm/{}".format(machine_id),
                "state_machine",
                label,
                gate_ids,
                claims,
                source.source_id,
                attributes=attributes,
                order=order,
            )
        )
        if primary is None:
            notes.append(
                "hal_fsm machine {!r} has no '{}' finding; the block carries only the "
                "surrounding claims".format(machine_id, _PRIMARY_SUFFIX)
            )

    for order, (finding, gate_ids) in enumerate(candidates):
        if tuple(sorted(gate_ids)) in claimed_candidate_sets:
            continue
        results.append(
            common.Contribution(
                finding["id"],
                "register",
                "candidate state register [{} bit]".format(len(gate_ids)),
                gate_ids,
                [_candidate_claim(source, finding, gate_ids, inventory)],
                source.source_id,
                attributes={"width": len(gate_ids), "solved": False},
                notes=[
                    "proposed as a state register but no transition relation was "
                    "recovered for it in this run"
                ],
                order=1000 + order,
            )
        )

    for finding in source.findings():
        if finding.get("id") == "fsm/coverage/uncovered-sequential-gates":
            notes.append(
                "hal_fsm left sequential gates outside every solved state register "
                "[{}#{}]".format(source.source_id, finding["id"])
            )

    if not results:
        notes.append(
            "the hal_fsm document {!r} contained no machines or candidates".format(
                source.source_id
            )
        )
    return results, notes
