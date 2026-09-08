"""Turn a recovered register map into a ``hal_findings`` document.

The register map already carries a per-field confidence; this module maps it
onto the shared status vocabulary without softening it:

===============================  ============================================
register-map confidence          findings status
===============================  ============================================
``proven_under_assumptions``     ``proven_under_assumptions`` (unbounded, with
                                 the mapping's assumptions attached)
``proven_bounded``               ``proven_bounded``, ``cycle_bound`` 1 -- one
                                 APB transfer, checked in two environments
``heuristic``                    ``heuristic`` (never a proof)
``unknown``                      ``unknown`` -- listed, not guessed
===============================  ============================================

A register whose bits fall into several tiers becomes several findings, one per
tier, because a finding carries exactly one status and a register that is
proven in 15 bits and merely checked in the 16th is neither.
"""

import os
import sys

from . import __version__
from .recover import SIDE_EFFECT_TEMPLATES

_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from hal_findings import model, serialize  # noqa: E402
from hal_findings import __version__ as findings_version  # noqa: E402
from hal_findings.adapters.common import utc_now  # noqa: E402

__all__ = ["build_document", "ANALYSIS_NAME", "ENTRY_POINT"]

ANALYSIS_NAME = "hal_apb_recover"
ENTRY_POINT = "hal_apb_recover.recover.recover"

_METHOD_ABSTRACT = (
    "Three-valued evaluation of the combinational next-state and read-mux functions "
    "for one APB access. Every input, state bit and data bit that is not being "
    "characterised is left unconstrained, so a definite result holds for all of them."
)
_METHOD_BOUNDED = (
    "The same evaluation with the non-bus inputs pinned to their declared quiescent "
    "value and the remaining state and data bits pinned to 0 and to 1. Agreement of "
    "both environments checks one transfer; it does not prove the behaviour."
)
_METHOD_STRUCTURAL = (
    "Structural comparison of recovered behaviour across addresses and of the gate "
    "types the netlist uses."
)


def _method(kind):
    if kind == "abstract":
        return model.method(
            "APB access probing (unconstrained environment)",
            "symbolic",
            False,
            description=_METHOD_ABSTRACT,
        )
    if kind == "bounded":
        return model.method(
            "APB access probing (pinned environment)",
            "simulation",
            True,
            description=_METHOD_BOUNDED,
        )
    return model.method(
        "APB register-map structure comparison",
        "structural",
        False,
        description=_METHOD_STRUCTURAL,
    )


def _assumptions(document):
    return [
        model.assumption(
            entry["id"], entry["description"], kind=entry["kind"], discharged=False
        )
        for entry in document["assumptions"]
    ]


def _gate_refs(document, artifact_id, storage_keys):
    refs = []
    storage = document.get("storage", {})
    for key in sorted(set(storage_keys)):
        entry = storage.get(key)
        if not entry or entry.get("uid") is None:
            continue
        refs.append(
            model.gate_ref(
                artifact_id, entry["uid"], entry.get("name") or key, gate_type=entry.get("type")
            )
        )
    return refs


def _status_for(confidence):
    return {
        "proven_under_assumptions": model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
        "proven_bounded": model.STATUS_PROVEN_BOUNDED,
        "heuristic": model.STATUS_HEURISTIC,
        "unknown": model.STATUS_UNKNOWN,
    }[confidence]


def _bounds_for(confidence):
    if confidence == "proven_under_assumptions":
        return model.unbounded(
            "the probe left every unrelated input, state bit and data bit "
            "unconstrained, so the result holds in every cycle the assumptions hold"
        )
    return model.bounded(
        1,
        description="one APB access; no multi-cycle behaviour was analysed",
    )


def _method_for(confidence):
    if confidence == "proven_under_assumptions":
        return _method("abstract")
    if confidence == "proven_bounded":
        return _method("bounded")
    return _method("bounded" if confidence == "heuristic" else "structural")


def _register_findings(document, artifact_id):
    findings = []
    assumptions = _assumptions(document)

    for register in sorted(document["registers"], key=lambda item: item["address"]):
        by_confidence = {}
        for field in register["fields"]:
            by_confidence.setdefault(field.get("confidence", "unknown"), []).append(field)

        for confidence, fields in sorted(by_confidence.items()):
            bits = sorted(field["bit"] for field in fields)
            templates = sorted({field.get("template") for field in fields if field.get("template")})
            storage_keys = [field["storage"] for field in fields if field.get("storage")]
            status = _status_for(confidence)
            finding_id = "apb/register/{:08x}/{}".format(register["address"], confidence)

            data = {
                "address": register["address"],
                "address_hex": register["address_hex"],
                "register_access": register["access"],
                "reset_value": register["reset_value"],
                "aliases": register.get("aliases") or [],
                "bits": bits,
                "templates": templates,
                "fields": [
                    {
                        "bit": field["bit"],
                        "kind": field.get("kind"),
                        "access": field.get("access"),
                        "template": field.get("template"),
                        "reset_value": field.get("reset_value"),
                        "read_bit": field.get("read_bit"),
                        "strobe_lanes": field.get("strobe_lanes"),
                        "write_requires": field.get("write_requires"),
                        "next_state_table": field.get("next_state_table"),
                        "guarded_by": field.get("guarded_by"),
                    }
                    for field in sorted(fields, key=lambda item: item["bit"])
                ],
            }

            summary = (
                "{} bit(s) of the register at {} behave as {} ({}). Recovered from "
                "structure alone; the netlist gives these bits no names.".format(
                    len(bits),
                    register["address_hex"],
                    ", ".join(templates) or "a constant read",
                    register["access"],
                )
            )

            findings.append(
                model.finding(
                    finding_id,
                    "Register {} bits {} ({})".format(
                        register["address_hex"],
                        _compress(bits),
                        ", ".join(templates) or "constant",
                    ),
                    status,
                    _method_for(confidence),
                    model.scope(
                        [artifact_id],
                        description="the storage bits and read-mux path of one APB address",
                        gates=_gate_refs(document, artifact_id, storage_keys) or None,
                    ),
                    summary=summary,
                    severity="info",
                    assumptions=assumptions,
                    bounds_dict=_bounds_for(confidence),
                    metrics={"bit_count": len(bits)},
                    data=data,
                    tags=["apb", "register-map", register["access"]],
                )
            )
    return findings


def _compress(bits):
    if not bits:
        return "none"
    ranges = []
    start = previous = bits[0]
    for bit in bits[1:]:
        if bit == previous + 1:
            previous = bit
            continue
        ranges.append((start, previous))
        start = previous = bit
    ranges.append((start, previous))
    return ", ".join(
        str(low) if low == high else "{}..{}".format(low, high) for low, high in ranges
    )


def _alias_findings(document, artifact_id):
    findings = []
    by_address = {register["address"]: register for register in document["registers"]}
    for index, entry in enumerate(document.get("alias_classes", [])):
        confidences = [
            by_address[address]["confidence"]
            for address in entry["addresses"]
            if address in by_address
        ]
        order = ["proven_under_assumptions", "proven_bounded", "heuristic", "unknown"]
        confidence = max(confidences, key=order.index) if confidences else "unknown"
        # The alias claim is a comparison of two recovered behaviours, so it can
        # never be stronger than the weaker of the two, and the comparison
        # itself was only made over the probed accesses.
        if confidence == "proven_under_assumptions":
            confidence = "proven_bounded"
        findings.append(
            model.finding(
                "apb/alias/{:04d}".format(index),
                "Addresses {} are aliases of one register".format(
                    ", ".join("0x{:02x}".format(address) for address in entry["addresses"])
                ),
                _status_for(confidence),
                _method("structural") if confidence in ("heuristic", "unknown") else _method("bounded"),
                model.scope(
                    [artifact_id],
                    description="two or more APB addresses that reach the same storage",
                ),
                summary=(
                    "{} addresses produced identical write associations, next-state "
                    "templates and read-mux associations at every probed access, so "
                    "they address the same register. {}".format(
                        len(entry["addresses"]), entry["evidence"]
                    )
                ),
                severity="info",
                assumptions=_assumptions(document),
                bounds_dict=_bounds_for(confidence),
                data={
                    "addresses": entry["addresses"],
                    "canonical_address": entry["canonical_address"],
                },
                tags=["apb", "alias"],
            )
        )
    return findings


def _side_effect_findings(document, artifact_id):
    findings = []
    for register in sorted(document["registers"], key=lambda item: item["address"]):
        grouped = {}
        for field in register["fields"]:
            template = field.get("template")
            if template in SIDE_EFFECT_TEMPLATES:
                grouped.setdefault((template, field.get("confidence", "unknown")), []).append(field)
        for (template, confidence), fields in sorted(grouped.items()):
            bits = sorted(field["bit"] for field in fields)
            findings.append(
                model.finding(
                    "apb/side-effect/{:08x}/{}".format(register["address"], template),
                    "Write side effect at {}: {} on bits {}".format(
                        register["address_hex"], template.replace("_", " "), _compress(bits)
                    ),
                    _status_for(confidence),
                    _method_for(confidence),
                    model.scope(
                        [artifact_id],
                        description="storage bits a write modifies without storing the "
                        "written value",
                        gates=_gate_refs(
                            document,
                            artifact_id,
                            [field["storage"] for field in fields if field.get("storage")],
                        )
                        or None,
                    ),
                    summary=(
                        "A write at {} does not store PWDATA into these bits: the next "
                        "state follows the '{}' rule. Software that writes back a value "
                        "it just read will change the register.".format(
                            register["address_hex"], template
                        )
                    ),
                    severity="info",
                    assumptions=_assumptions(document),
                    bounds_dict=_bounds_for(confidence),
                    data={
                        "address": register["address"],
                        "template": template,
                        "bits": bits,
                        "next_state_tables": {
                            str(field["bit"]): field.get("next_state_table")
                            for field in fields
                        },
                    },
                    tags=["apb", "side-effect", template.replace("_", "-")],
                )
            )
    return findings


def _unmapped_finding(document, artifact_id):
    if not document["unmapped_addresses"]:
        return None
    addresses = [entry["address"] for entry in document["unmapped_addresses"]]
    return model.finding(
        "apb/coverage/unmapped-addresses",
        "{} probed address(es) carry no recovered register".format(len(addresses)),
        model.STATUS_HEURISTIC,
        _method("bounded"),
        model.scope([artifact_id], description="probed addresses with no observed effect"),
        summary=(
            "At {} no probe changed a modelled state bit and every PRDATA bit read a "
            "constant 0. Absence of an observed effect is not proof that no register "
            "exists there: a register the probes do not reach, or one behind an "
            "unmodelled primitive, would look the same.".format(
                ", ".join("0x{:02x}".format(address) for address in addresses)
            )
        ),
        severity="info",
        assumptions=_assumptions(document),
        bounds_dict=_bounds_for("proven_bounded"),
        data={"addresses": addresses},
        tags=["apb", "coverage"],
    )


def _unresolved_findings(document, artifact_id):
    if not document.get("unresolved_fields"):
        return []
    grouped = {}
    for entry in document["unresolved_fields"]:
        grouped.setdefault(entry["address"], []).append(entry)
    findings = []
    for address, entries in sorted(grouped.items()):
        findings.append(
            model.finding(
                "apb/unresolved/{:08x}".format(address),
                "{} storage bit(s) at {} could not be characterised".format(
                    len(entries), entries[0]["address_hex"]
                ),
                model.STATUS_UNKNOWN,
                _method("bounded"),
                model.scope(
                    [artifact_id],
                    description="storage bits that react to a write with no consistent "
                    "next-state rule",
                    gates=_gate_refs(
                        document, artifact_id, [entry["storage"] for entry in entries]
                    )
                    or None,
                ),
                summary=(
                    "A write at {} changes these storage bits, but no probed "
                    "environment produced a next-state table matching any modelled "
                    "access rule. They are reported unresolved rather than assigned a "
                    "plausible access type.".format(entries[0]["address_hex"])
                ),
                severity="medium",
                assumptions=_assumptions(document),
                bounds_dict=_bounds_for("unknown"),
                data={
                    "address": address,
                    "entries": [
                        {
                            "data_bit": entry["data_bit"],
                            "reason": entry["reason"],
                            "environments": entry.get("environments"),
                        }
                        for entry in entries
                    ],
                },
                tags=["apb", "unresolved"],
            )
        )
    return findings


def _unsupported_findings(document, artifact_id):
    findings = []
    coverage = document["coverage"]

    primitives = coverage.get("unsupported_gate_types") or []
    if primitives:
        entries = []
        for primitive in primitives:
            entries.append(
                model.unsupported_primitive(
                    primitive["gate_type"],
                    primitive["reason"],
                    count=primitive["count"],
                    example_gates=[
                        model.gate_ref(
                            artifact_id,
                            example["uid"],
                            example.get("name") or example["gate"],
                            gate_type=primitive["gate_type"],
                        )
                        for example in primitive.get("examples", [])
                        if example.get("uid") is not None
                    ]
                    or None,
                )
            )
        findings.append(
            model.finding(
                "apb/coverage/unsupported-primitives",
                "{} gate type(s) the recovery does not model".format(len(entries)),
                model.STATUS_UNSUPPORTED,
                _method("structural"),
                model.scope(
                    [artifact_id],
                    description="gate types outside the recovery's primitive coverage",
                    gate_types=[primitive["gate_type"] for primitive in primitives],
                ),
                summary=(
                    "The netlist uses {} gate type(s) the recovery does not model. Any "
                    "register behind them is invisible to this analysis, so the "
                    "register map below is not a complete interface description.".format(
                        len(entries)
                    )
                ),
                severity="medium",
                assumptions=_assumptions(document),
                unsupported_dict=model.unsupported(
                    "primitive",
                    "the recovery models combinational cells and edge-triggered "
                    "flip-flops; every other primitive evaluates as unconstrained",
                    entries,
                ),
                tags=["apb", "coverage"],
            )
        )

    mapping = document["mapping"]
    window = mapping["address_window"]
    findings.append(
        model.finding(
            "apb/coverage/address-window",
            "Only {} address(es) were analysed".format(window["count"]),
            model.STATUS_UNSUPPORTED,
            _method("structural"),
            model.scope([artifact_id], description="the analysed address window"),
            summary=(
                "The recovery enumerated {} address(es) from 0x{:x} with stride {}. "
                "Addresses outside the window, and the byte offsets the stride skips, "
                "were never driven, so this register map must not be read as a "
                "complete programming interface.".format(
                    window["count"], window["base"], window["stride"]
                )
            ),
            severity="info",
            assumptions=_assumptions(document),
            unsupported_dict=model.unsupported(
                "configuration",
                "automatic interface detection and full address-space exploration are "
                "outside this analysis; the address window is user-supplied",
            ),
            data={
                "address_window": window,
                "address_bits": document["coverage"]["address_bits"],
                "limits": document["coverage"]["limits"],
            },
            tags=["apb", "coverage"],
        )
    )

    clocking = document["coverage"].get("clocking") or {}
    if clocking.get("checked") and (clocking.get("foreign") or clocking.get("unknown")):
        findings.append(
            model.finding(
                "apb/coverage/clocking",
                "{} flip-flop(s) do not match the declared clock assumption".format(
                    len(clocking.get("foreign", [])) + len(clocking.get("unknown", []))
                ),
                model.STATUS_UNKNOWN,
                _method("structural"),
                model.scope([artifact_id], description="flip-flop clocking"),
                summary=(
                    "The mapping declares one clock for the whole interface, but some "
                    "flip-flops are clocked elsewhere or expose no typed clock pin. "
                    "Recovered behaviour for those bits rests on an assumption the "
                    "netlist does not support."
                ),
                severity="medium",
                assumptions=_assumptions(document),
                bounds_dict=_bounds_for("unknown"),
                data=clocking,
                tags=["apb", "coverage", "clocking"],
            )
        )
    return findings


def build_document(register_map, artifact_id="netlist", netlist_path=None,
                   generated_at=None, producer_command=None, hal_version=None):
    """Build a validated-shape findings document from a register map."""
    netlist = register_map["netlist"]
    source = netlist_path if netlist_path is not None else netlist.get("source")

    sha256 = None
    size_bytes = None
    unhashed_reason = None
    if source and os.path.isfile(source):
        sha256 = serialize.sha256_file(source)
        size_bytes = os.path.getsize(source)
    elif source and os.path.isdir(source):
        unhashed_reason = (
            "input is a HAL project directory ({}); hash the archive it was extracted "
            "from to pin it".format(os.path.basename(source))
        )
    else:
        unhashed_reason = "netlist has no readable source file on this machine"

    statistics = netlist["statistics"]
    artifact = model.artifact(
        artifact_id,
        kind="netlist",
        path=source or None,
        sha256=sha256,
        unhashed_reason=unhashed_reason,
        size_bytes=size_bytes,
        design_name=register_map.get("design"),
        gate_count=statistics.get("gates"),
        net_count=statistics.get("nets"),
        gate_library={"name": netlist["gate_library"]} if netlist.get("gate_library") else None,
        description="flattened netlist the APB register map was recovered from",
    )

    findings = []
    findings.extend(_register_findings(register_map, artifact_id))
    findings.extend(_alias_findings(register_map, artifact_id))
    findings.extend(_side_effect_findings(register_map, artifact_id))
    unmapped = _unmapped_finding(register_map, artifact_id)
    if unmapped is not None:
        findings.append(unmapped)
    findings.extend(_unresolved_findings(register_map, artifact_id))
    findings.extend(_unsupported_findings(register_map, artifact_id))

    analysis = {
        "plugin": {
            "name": ANALYSIS_NAME,
            "version": __version__,
            "description": "APB register-map recovery from a flattened netlist",
        },
        "entry_point": ENTRY_POINT,
        "configuration": register_map["mapping"],
        "duration_s": register_map["metrics"]["duration_s"],
    }
    if hal_version:
        analysis["hal"] = {"version": str(hal_version)}

    producer = {"name": "hal_apb_recover.findings", "version": findings_version}
    if producer_command:
        producer["command"] = list(producer_command)

    notes = [
        "storage bits are referenced by the gate that holds them; the netlist gives "
        "them no meaningful names, so the register and field names in this document "
        "are generated from addresses, not recovered from the design",
        "every claim is about the combinational next-state and read functions of a "
        "single APB access; no multi-cycle or wait-state behaviour was analysed",
    ]
    if unhashed_reason:
        notes.append(
            "the analysed netlist could not be hashed: {}".format(unhashed_reason)
        )

    return model.document(
        producer,
        [artifact],
        analysis,
        findings,
        generated_at=generated_at if generated_at is not None else utc_now(),
        notes=notes,
    )
