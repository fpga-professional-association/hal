"""Register-map document handling: validation, Markdown, canonical summary.

The recovery emits a versioned JSON document (``fpgapa.apb-register-map``).
This module keeps it honest -- :func:`validate_document` refuses a document
that claims more than the schema allows -- renders it as Markdown for humans,
and reduces it to a *behavioural summary* that contains no netlist-local
identifiers.

The summary is what makes the two code paths comparable: gate keys differ
between a netlist read offline and the same netlist loaded through ``hal_py``
(instance names versus HAL object ids), so the fixture test compares what was
recovered, not what it was called.
"""

import json

from . import REGISTER_MAP_SCHEMA, REGISTER_MAP_SCHEMA_VERSION

__all__ = [
    "RegisterMapError",
    "validate_document",
    "behavioural_summary",
    "to_markdown",
    "write_document",
    "read_document",
    "dumps",
]

_CONFIDENCES = (
    "proven_under_assumptions",
    "proven_bounded",
    "heuristic",
    "unknown",
)

_CONFIDENCE_LABEL = {
    "proven_under_assumptions": "proven under assumptions",
    "proven_bounded": "bounded check (1 transfer)",
    "heuristic": "inferred",
    "unknown": "unresolved",
}


class RegisterMapError(RuntimeError):
    """The register-map document is malformed."""


def validate_document(document):
    """Raise :class:`RegisterMapError` unless ``document`` is well formed."""
    if document.get("schema") != REGISTER_MAP_SCHEMA:
        raise RegisterMapError(
            "document schema is {!r}, expected {!r}".format(
                document.get("schema"), REGISTER_MAP_SCHEMA
            )
        )
    if document.get("schema_version") != REGISTER_MAP_SCHEMA_VERSION:
        raise RegisterMapError(
            "document schema_version is {!r}; this build reads {!r}".format(
                document.get("schema_version"), REGISTER_MAP_SCHEMA_VERSION
            )
        )
    for key in ("registers", "unmapped_addresses", "assumptions", "coverage", "mapping"):
        if key not in document:
            raise RegisterMapError("document has no {!r}".format(key))

    seen = set()
    for register in document["registers"]:
        address = register.get("address")
        if address is None:
            raise RegisterMapError("a register has no address")
        if address in seen:
            raise RegisterMapError("address {} appears twice".format(address))
        seen.add(address)
        if register.get("confidence") not in _CONFIDENCES:
            raise RegisterMapError(
                "register {} has confidence {!r}".format(address, register.get("confidence"))
            )
        bits = set()
        for field in register.get("fields", []):
            bit = field.get("bit")
            if bit is None:
                raise RegisterMapError("a field of register {} has no bit".format(address))
            if bit in bits:
                raise RegisterMapError(
                    "register {} has two fields for bit {}".format(address, bit)
                )
            bits.add(bit)
            if field.get("confidence") not in _CONFIDENCES:
                raise RegisterMapError(
                    "field {}[{}] has confidence {!r}".format(
                        address, bit, field.get("confidence")
                    )
                )
            if field.get("kind") == "storage" and not field.get("storage"):
                raise RegisterMapError(
                    "field {}[{}] is a storage field without a storage bit".format(
                        address, bit
                    )
                )
            if field.get("kind") == "constant" and field.get("constant_value") is None:
                raise RegisterMapError(
                    "field {}[{}] is a constant field without a value".format(address, bit)
                )
    for address in document["unmapped_addresses"]:
        if address.get("address") in seen:
            raise RegisterMapError(
                "address {} is reported both as a register and as unmapped".format(
                    address.get("address")
                )
            )
    return document


# ---------------------------------------------------------------------------
# canonical summary
# ---------------------------------------------------------------------------


def _storage_index(document):
    """Map every storage key to the ``(canonical address, bit)`` it belongs to."""
    index = {}
    for register in document["registers"]:
        if register.get("is_alias"):
            continue
        for field in register["fields"]:
            storage = field.get("storage")
            if storage and storage not in index:
                index[storage] = (register["address"], field["bit"])
    return index


def behavioural_summary(document):
    """Reduce ``document`` to what it says about the design, with no local ids."""
    index = _storage_index(document)

    def resolve(storage):
        return list(index.get(storage, ("?", "?")))

    registers = []
    for register in sorted(document["registers"], key=lambda item: item["address"]):
        fields = []
        for field in sorted(register["fields"], key=lambda item: item["bit"]):
            entry = {
                "bit": field["bit"],
                "kind": field.get("kind"),
                "template": field.get("template"),
                "access": field.get("access"),
                "confidence": field.get("confidence"),
                "reset_value": field.get("reset_value"),
                "read_bit": field.get("read_bit"),
                "strobe_lanes": field.get("strobe_lanes"),
                "write_requires": field.get("write_requires"),
                "constant_value": field.get("constant_value"),
            }
            guard = field.get("guarded_by")
            if guard:
                entry["guarded_by"] = sorted(
                    (resolve(candidate["storage"]), candidate["enabling_value"])
                    for candidate in guard.get("candidates", [])
                )
            fields.append(entry)
        registers.append(
            {
                "address": register["address"],
                "access": register.get("access"),
                "confidence": register.get("confidence"),
                "reset_value": register.get("reset_value"),
                "aliases": sorted(register.get("aliases") or []),
                "canonical_address": register.get("canonical_address"),
                "fields": fields,
            }
        )

    return {
        "registers": registers,
        "unmapped_addresses": sorted(
            entry["address"] for entry in document["unmapped_addresses"]
        ),
        "alias_classes": sorted(
            (entry["canonical_address"], sorted(entry["addresses"]))
            for entry in document.get("alias_classes", [])
        ),
        "unresolved_fields": sorted(
            (entry["address"], entry["data_bit"], resolve(entry["storage"])[0])
            for entry in document.get("unresolved_fields", [])
        ),
        "unsupported_gate_types": sorted(
            entry["gate_type"]
            for entry in document["coverage"].get("unsupported_gate_types", [])
        ),
        "flip_flops_total": document["coverage"].get("flip_flops_total"),
        "flip_flops_unassociated": len(
            document["coverage"].get("flip_flops_unassociated", [])
        ),
        "address_bits_without_effect": sorted(
            document["coverage"]["address_bits"].get("no_effect_at_probe_address", [])
        ),
    }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _format_lanes(field):
    lanes = field.get("strobe_lanes")
    if lanes is None:
        return "--"
    if not lanes:
        return "none"
    return ", ".join(str(lane) for lane in lanes)


def _format_reset(field):
    value = field.get("reset_value")
    return "?" if value is None else str(value)


def to_markdown(document):
    """Render the register map as Markdown."""
    lines = []
    design = document.get("design") or document["netlist"]["name"]
    lines.append("# APB register map: {}".format(design))
    lines.append("")
    lines.append(
        "Recovered by `hal_apb_recover` {} from a netlist whose internal names carry "
        "no meaning. Every row states how strongly the claim is supported; nothing "
        "below is a specification of the hardware, it is what the netlist was "
        "observed to do under the assumptions listed at the end.".format(
            document["generated_by"]["version"]
        )
    )
    lines.append("")

    netlist = document["netlist"]
    statistics = netlist["statistics"]
    lines.append("| netlist | value |")
    lines.append("| --- | --- |")
    lines.append("| source | `{}` |".format(netlist.get("source") or "in memory"))
    lines.append("| gate library | {} |".format(netlist.get("gate_library") or "unknown"))
    lines.append("| gates / nets | {} / {} |".format(statistics["gates"], statistics["nets"]))
    lines.append("| flip-flops | {} |".format(statistics["flip_flops"]))
    lines.append(
        "| data / address width | {} / {} bits |".format(
            document["mapping"]["data_width"], document["mapping"]["address_width"]
        )
    )
    lines.append("| byte strobe lanes | {} |".format(document["mapping"]["strobe_lanes"]))
    lines.append("")

    lines.append("## Confidence vocabulary")
    lines.append("")
    lines.append("| value | meaning |")
    lines.append("| --- | --- |")
    lines.append(
        "| proven under assumptions | the next-state or read function was definite "
        "with every other input, state bit and data bit left unconstrained, so it "
        "holds for all of them -- given the assumptions below |"
    )
    lines.append(
        "| bounded check (1 transfer) | definite only once the non-bus inputs and the "
        "other state bits were pinned; verified for one APB transfer in two "
        "environments, not proven |"
    )
    lines.append(
        "| inferred | the environments disagreed; the behaviour shown is the one that "
        "writes, and the state bits that gate it are named |"
    )
    lines.append("| unresolved | no environment gave a definite answer; see *Unresolved* |")
    lines.append("")

    lines.append("## Registers")
    lines.append("")
    for register in sorted(document["registers"], key=lambda item: item["address"]):
        title = "### {} `{}`".format(register["name"], register["address_hex"])
        if register.get("is_alias"):
            title += " (alias)"
        lines.append(title)
        lines.append("")
        summary = [
            "access: **{}**".format(register["access"]),
            "confidence: {}".format(
                _CONFIDENCE_LABEL.get(register["confidence"], register["confidence"])
            ),
            "reset/constant value: `{}`".format(register["reset_value"]["value"]),
        ]
        if not register["reset_value"]["complete"]:
            summary.append(
                "known reset bits: `{}`".format(register["reset_value"]["known_bits"])
            )
        if register.get("aliases"):
            summary.append(
                "aliased at: {}".format(
                    ", ".join("`0x{:02x}`".format(alias) for alias in register["aliases"])
                )
            )
        lines.append("- " + "\n- ".join(summary))
        lines.append("")
        lines.append("| bit | kind | access | reset | read bit | strobe lane(s) | confidence | notes |")
        lines.append("| ---: | --- | --- | ---: | ---: | --- | --- | --- |")
        for field in sorted(register["fields"], key=lambda item: item["bit"], reverse=True):
            notes = list(field.get("notes") or [])
            guard = field.get("guarded_by")
            if guard:
                names = ", ".join(
                    "`{}`={}".format(candidate["storage"], candidate["enabling_value"])
                    for candidate in guard.get("candidates", [])
                )
                notes.append(
                    "write only happens when {} (otherwise: {})".format(
                        names or "an unidentified state bit is set",
                        guard.get("alternative_template"),
                    )
                )
                if guard.get("truncated"):
                    notes.append("guard scan was truncated; more guards may exist")
            if field.get("strobe_note"):
                notes.append(field["strobe_note"])
            lines.append(
                "| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                    field["bit"],
                    field.get("kind", "?"),
                    field.get("access", "?"),
                    _format_reset(field),
                    "--" if field.get("read_bit") is None else field["read_bit"],
                    _format_lanes(field),
                    _CONFIDENCE_LABEL.get(field.get("confidence"), field.get("confidence")),
                    "; ".join(notes) or "",
                )
            )
        if register.get("unresolved_read_bits"):
            lines.append("")
            lines.append(
                "> PRDATA bits {} could not be attributed at this address.".format(
                    ", ".join(str(bit) for bit in register["unresolved_read_bits"])
                )
            )
        lines.append("")

    if document.get("alias_classes"):
        lines.append("## Address aliases")
        lines.append("")
        lines.append("| addresses | evidence |")
        lines.append("| --- | --- |")
        for entry in document["alias_classes"]:
            lines.append(
                "| {} | {} |".format(
                    ", ".join("`0x{:02x}`".format(address) for address in entry["addresses"]),
                    entry["evidence"],
                )
            )
        lines.append("")

    lines.append("## Addresses with no register")
    lines.append("")
    if document["unmapped_addresses"]:
        lines.append("| address | evidence |")
        lines.append("| --- | --- |")
        for entry in document["unmapped_addresses"]:
            lines.append("| `{}` | {} |".format(entry["address_hex"], entry["evidence"]))
    else:
        lines.append("Every probed address carried a register.")
    lines.append("")

    if document.get("unresolved_fields"):
        lines.append("## Unresolved")
        lines.append("")
        lines.append(
            "These storage bits react to a write but no environment produced a "
            "consistent next-state table. They are listed rather than guessed at.")
        lines.append("")
        lines.append("| address | data bit | storage | reason |")
        lines.append("| --- | ---: | --- | --- |")
        for entry in document["unresolved_fields"]:
            lines.append(
                "| `{}` | {} | `{}` | {} |".format(
                    entry["address_hex"], entry["data_bit"], entry["storage"], entry["reason"]
                )
            )
        lines.append("")

    coverage = document["coverage"]
    lines.append("## Coverage and limits")
    lines.append("")
    lines.append(
        "- {} of {} flip-flops were attributed to a register.".format(
            coverage["flip_flops_associated"], coverage["flip_flops_total"]
        )
    )
    if coverage["flip_flops_unassociated"]:
        lines.append(
            "- {} flip-flop(s) were not attributed to any address: `{}`.".format(
                len(coverage["flip_flops_unassociated"]),
                "`, `".join(coverage["flip_flops_unassociated"][:12]),
            )
        )
    for entry in coverage["unsupported_gate_types"]:
        lines.append(
            "- **unsupported primitive**: {} x `{}` -- {}".format(
                entry["count"], entry["gate_type"], entry["reason"]
            )
        )
    address_bits = coverage["address_bits"]
    if address_bits.get("never_varied"):
        lines.append(
            "- PADDR bits {} were never varied by the enumeration ({}).".format(
                ", ".join(str(bit) for bit in address_bits["never_varied"]),
                address_bits["never_varied_note"],
            )
        )
    if address_bits.get("no_effect_at_probe_address"):
        lines.append(
            "- PADDR bits {} had no effect at `0x{:02x}` ({}).".format(
                ", ".join(str(bit) for bit in address_bits["no_effect_at_probe_address"]),
                address_bits["probe_address"],
                address_bits["no_effect_note"],
            )
        )
    clocking = coverage["clocking"]
    if clocking.get("checked"):
        if clocking["foreign"]:
            lines.append(
                "- {} flip-flop(s) are clocked by a net other than the declared "
                "clock; their recovered behaviour rests on a clock assumption that "
                "does not hold.".format(len(clocking["foreign"]))
            )
        if clocking["unknown"]:
            lines.append(
                "- {} flip-flop(s) have no identifiable clock pin.".format(
                    len(clocking["unknown"])
                )
            )
    for note in coverage.get("reset_notes", []):
        lines.append("- {}".format(note))
    for limit in coverage["limits"]:
        lines.append("- {}".format(limit))
    lines.append("")

    lines.append("## Assumptions")
    lines.append("")
    lines.append("| id | kind | assumption |")
    lines.append("| --- | --- | --- |")
    for assumption in document["assumptions"]:
        lines.append(
            "| `{}` | {} | {} |".format(
                assumption["id"], assumption["kind"], assumption["description"]
            )
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# io
# ---------------------------------------------------------------------------


def dumps(document):
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_document(document, path):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps(document))
    return path


def read_document(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
