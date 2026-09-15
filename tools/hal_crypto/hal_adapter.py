"""The one module in this package that may import ``hal_py``.

Everything else here runs on a plain CPython interpreter, and that is a
property worth protecting: the recognizers can be developed and regression
tested on a machine with no HAL build, and CI can run them before the C++ build
starts.  So the HAL side is confined to this file, and it is deliberately thin.

It does *not* re-implement the passes against HAL's object model.  It loads a
netlist through ``hal_agilex.hal_adapter`` (which is where the ``NetlistFactory``
call and the mandatory ``load_all_plugins()`` already live, audited by
``tools/hal_viz/test_halenv.py``), reports what HAL sees, and then runs the
stdlib passes over the corresponding ``.vo``.  A second implementation of
S-box extraction on top of ``hal_py`` would be a second thing to keep correct
and a second thing to disagree with the first.
"""

import os

from hal_agilex import hal_adapter as agilex_adapter

from hal_findings import model

from . import classify, findings

__all__ = [
    "PRODUCER",
    "import_hal",
    "load_netlist",
    "build_document",
]

PRODUCER = {"name": "hal_crypto.hal_adapter", "version": "1.0.0"}


def import_hal(library_directories=()):
    """Import ``hal_py`` with HAL's plugins loaded, via ``hal_agilex``."""
    return agilex_adapter.import_hal(library_directories)


def load_netlist(hal_py, netlist_path, gate_library_path):
    """Load a netlist through ``hal_agilex.hal_adapter.load_netlist``."""
    return agilex_adapter.load_netlist(hal_py, netlist_path, gate_library_path)


def source_export(netlist_path):
    """The ``.vo`` next to an imported ``netlist.hal.v``, when there is one.

    The imported Verilog is what HAL reads; the vendor export is what the
    stdlib passes read.  They describe the same design, and the walkthroughs
    keep them side by side, so this looks for the sibling rather than making
    the caller pass both.
    """
    directory = os.path.dirname(os.path.abspath(str(netlist_path)))
    candidates = sorted(
        name for name in os.listdir(directory) if name.endswith(".vo")
    )
    if not candidates:
        return None
    return os.path.join(directory, candidates[0])


def build_document(hal_py, netlist, netlist_path, artifact_id=None, export=None):
    """Report the loaded netlist and run the crypto passes over its ``.vo``.

    When no ``.vo`` sits next to the imported netlist the document contains a
    single ``unsupported`` finding saying so -- the passes are written against
    the vendor export and are not going to be guessed at from a partially
    elaborated HAL netlist.
    """
    from hal_agilex import vo_netlist

    export = export or source_export(netlist_path)
    if export is None:
        artifact = model.artifact(
            artifact_id or netlist.get_design_name(),
            kind="netlist",
            path=str(netlist_path),
            unhashed_reason="the loaded netlist is the imported Verilog, not the "
            "vendor export the passes read",
            design_name=netlist.get_design_name(),
            gate_count=len(netlist.get_gates()),
        )
        finding = model.finding(
            "hal_crypto/hal/no-export",
            "No .vo export next to the imported netlist",
            model.STATUS_UNSUPPORTED,
            findings.STRUCTURAL,
            model.scope([artifact["artifact_id"]]),
            summary=(
                "hal_crypto reads the Quartus .vo export, because that is what "
                "carries the lut_mask parameters the cone evaluation needs. None "
                "was found next to {}.".format(netlist_path)
            ),
            unsupported_dict=model.unsupported(
                "format",
                "the crypto passes consume a .vo export; pass --export or place "
                "the export next to the imported netlist",
            ),
            tags=["crypto"],
        )
        return findings.document(
            PRODUCER, artifact, [finding], "hal_crypto.hal_adapter.build_document"
        )

    parsed = vo_netlist.parse_file(export)
    artifact = findings.artifact_for(parsed, export, artifact_id)
    document = classify.build_document(parsed, artifact)
    document["notes"] = list(document.get("notes") or []) + [
        "the netlist was also loaded into HAL from {} ({} gates); the crypto "
        "passes read the vendor export {}".format(
            netlist_path, len(netlist.get_gates()), export
        )
    ]
    document["producer"] = PRODUCER
    return document
