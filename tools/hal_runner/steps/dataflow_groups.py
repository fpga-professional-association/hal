"""DANA register recovery as a runner step.

This step deliberately contains no findings-building logic of its own: the
mapping from a ``dataflow.Result`` to the findings schema already exists in
:mod:`hal_findings.adapters.dataflow`, including the part that matters most --
that every recovered group is ``heuristic`` and that sequential gate types which
ended up in no group are reported as an explicit coverage gap rather than as
absence of evidence.  Duplicating that here would be a second place to get the
status wrong.

What this module owns is the plumbing: turning the step's JSON configuration
into a ``dataflow.Configuration``, running the analysis, and asking DANA's own
writers for the ``.dot`` and ``.txt`` exports so the run has artifacts a human
can open.

Unlike ``graph_algorithm.connected_components`` this analysis is not registered
as deterministic; it is here to show that the runner is not a one-analysis tool
and to give DANA a reproducible harness, not to promise identical output across
HAL versions.
"""

import os

from hal_findings import model
from hal_findings.adapters import dataflow as dataflow_adapter

from .dispatch import StepError, StepOutcome

__all__ = ["NAME", "run"]

NAME = "dataflow.groups"


def _configure(dataflow, netlist, config):
    """Build a ``dataflow.Configuration`` from the step's JSON options."""
    configuration = dataflow.Configuration(netlist).with_flip_flops()
    if config.get("min_group_size") is not None:
        configuration = configuration.with_min_group_size(int(config["min_group_size"]))
    if config.get("expected_sizes"):
        configuration = configuration.with_expected_sizes(
            [int(size) for size in config["expected_sizes"]]
        )
    if config.get("stage_identification"):
        configuration = configuration.with_stage_identification(True)
    if config.get("type_consistency"):
        configuration = configuration.with_type_consistency(True)
    return configuration


def run(hal_py, netlist, config, context):
    """Run DANA and record its groups through the shared findings adapter."""
    dataflow = context.plugin_module

    configuration = _configure(dataflow, netlist, config)
    result = dataflow.analyze(configuration)
    if result is None:
        raise StepError(
            "dataflow.analyze() returned None; see the HAL log for the reason",
            kind="plugin_error",
        )

    artifacts = []
    notes = []
    evidence = []

    if config.get("write_dot", True):
        # DANA writes graph.dot into a directory it is given; use its own exporter
        # rather than re-deriving the grouping graph here.
        if result.write_dot(context.output_dir) and os.path.isfile(
            os.path.join(context.output_dir, "graph.dot")
        ):
            artifacts.append(
                {
                    "path": "graph.dot",
                    "role": "dot",
                    "description": "DANA's own register-group graph export",
                }
            )
            evidence.append(
                model.evidence(
                    "dot",
                    description="DANA's register-group graph for this run",
                    path="graph.dot",
                )
            )
        else:
            notes.append("dataflow result.write_dot() failed; no graph.dot was produced")

    if config.get("write_txt", True):
        txt_path = os.path.join(context.output_dir, "groups.txt")
        if result.write_txt(txt_path) and os.path.isfile(txt_path):
            artifacts.append(
                {
                    "path": "groups.txt",
                    "role": "report",
                    "description": "DANA's own listing of the recovered groups",
                }
            )
            evidence.append(
                model.evidence(
                    "report",
                    description="DANA's listing of the recovered groups",
                    path="groups.txt",
                )
            )
        else:
            notes.append("dataflow result.write_txt() failed; no groups.txt was produced")

    document = dataflow_adapter.build_document(
        result,
        artifact_id=context.artifact_id,
        configuration=configuration,
        plugin_version=context.plugin_version,
        hal_version=context.hal_version,
        netlist_path=context.netlist_path,
        evidence_list=evidence or None,
    )
    if notes:
        document.setdefault("notes", []).extend(notes)

    groups = result.get_groups() or {}
    return StepOutcome(
        document,
        artifacts=artifacts,
        metrics={
            "groups": len(groups),
            "grouped_gates": sum(len(gates) for gates in groups.values()),
        },
        notes=notes,
    )
