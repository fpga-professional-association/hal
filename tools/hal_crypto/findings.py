"""Shared ``hal_findings`` plumbing for the crypto passes.

Every pass in this package emits into the same document format the rest of the
repository uses, for the same reason: a claim that is not machine readable is a
claim nobody can check later.  The pieces that all of them share -- the input
artifact with its sha256, the method descriptors, the document envelope -- are
here so that no pass gets to describe itself as more certain than the others by
accident.

Three method descriptors cover everything the package does, and the difference
between them is the difference between the claims:

``STRUCTURAL``
    reading the netlist graph.  Never bounded, never a proof of behaviour.
``EXACT_EVALUATION``
    enumerating a cone's whole input space and reading its truth table off.  A
    statement obtained this way holds for every input of that cone, which is
    why the S-box and feedback-polynomial findings can be
    ``proven_under_assumptions``.
``SAMPLED_EVALUATION``
    the same evaluation on a fixed, seeded sample because the operand space is
    too large.  Whatever comes out of it is ``heuristic``, always.
"""

import os

from hal_agilex.inventory import export_metadata

from hal_findings import model as findings_model
from hal_findings import serialize
from hal_findings.adapters.common import utc_now

__all__ = [
    "PLUGIN",
    "STRUCTURAL",
    "EXACT_EVALUATION",
    "SAMPLED_EVALUATION",
    "PRIMITIVE_SEMANTICS_ASSUMPTION",
    "artifact_for",
    "document",
    "sampled_method",
]

PLUGIN = {"name": "hal_crypto", "version": "1.0.0"}

STRUCTURAL = findings_model.method(
    "structural crypto pattern match",
    "structural",
    False,
    description=(
        "Walks the netlist graph produced by hal_agilex.vo_netlist: register "
        "chains, carry chains, pure-wire bit maps and LUT cones. No solver and "
        "no simulation."
    ),
)

EXACT_EVALUATION = findings_model.method(
    "exhaustive cone evaluation",
    "simulation",
    False,
    description=(
        "Enumerates the whole input space of a combinational cone using only the "
        "tennm_lcell_comb semantics in hal_agilex.primitives, and reads the truth "
        "table off it. Exhaustive, so the resulting table is the function, not a "
        "sample of it."
    ),
)


def sampled_method(vectors):
    return findings_model.method(
        "sampled operand evaluation",
        "simulation",
        True,
        description=(
            "Drives the recognized datapath on a fixed, seeded sample of operand "
            "vectors because the operand space is too large to enumerate."
        ),
        parameters={"vectors": vectors},
    )


SAMPLED_EVALUATION = sampled_method(0)

PRIMITIVE_SEMANTICS_ASSUMPTION = findings_model.assumption(
    "primitive-semantics",
    "tennm_lcell_comb and tennm_ff behave as documented in "
    "hal_agilex.primitives, which was validated by simulating real Quartus "
    "exports against the RTL they were synthesised from.",
    kind="library",
)

READER_ASSUMPTION = findings_model.assumption(
    "reader-completeness",
    "The .vo reader refuses Verilog it does not understand, so no instance can "
    "have been dropped silently before the analysis ran.",
    kind="tool",
    discharged=True,
)


def artifact_for(netlist, path, artifact_id=None):
    """The findings artifact for a ``.vo`` export, with its sha256."""
    metadata = export_metadata(path)
    return findings_model.artifact(
        artifact_id or netlist.name,
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


def document(producer, artifact, findings, entry_point, generated_at=None, notes=None):
    return findings_model.document(
        producer,
        [artifact],
        {"entry_point": entry_point, "plugin": PLUGIN},
        findings,
        generated_at=generated_at or utc_now(),
        notes=notes or None,
    )
