"""hal_capabilities - what a HAL plugin needs before it can answer anything.

A plugin being *built* says nothing about whether it can say anything about the
netlist in front of you.  ``dataflow`` needs sequential gate types; a Xilinx
utility needs a Xilinx library; ``hawkeye`` needs ``graph_algorithm`` loaded
first.  Today that knowledge lives in READMEs and in the heads of the people
who wrote the plugins, so the failure mode is an analysis that runs, finds
nothing, and is read as "nothing there".

This package makes it declarative.  Each plugin ships a
``capabilities.json`` -- validated against a versioned schema -- naming its
plugin dependencies, the gate-type properties and pin types it needs, the gate
libraries it has been exercised against, and what it costs to run.  The
discovery command then answers four separate questions instead of one:

    declared   the plugin declares its capabilities in the source tree
    built      its shared object exists in this build
    loadable   plugin_manager could actually instantiate it
    supported  it can say something about *this* netlist -- or exactly why not

Layout (mirrors ``tools/hal_findings``: only ``discover.probe_loadable`` and the
CLI's netlist loading need a built HAL)::

    hal_capabilities.schema       schema file lookup, CAPABILITIES_VERSION
    hal_capabilities.validate     schema + closed-vocabulary validation
    hal_capabilities.support      capability declaration x netlist -> verdict
    hal_capabilities.discover     declared / built / loadable, drift, dependencies
    hal_capabilities.cli          ``python tools/hal_capabilities list ...``

Run the unit tests with a plain interpreter from the repository root::

    python -m unittest discover -s tools/hal_capabilities -t tools -p "test_*.py"
"""

from .schema import (
    CAPABILITIES_VERSION,
    SUPPORTED_CAPABILITIES_VERSIONS,
    load_schema,
    schema_path,
)
from .validate import (
    CapabilitiesValidationError,
    collect_errors,
    is_valid,
    validate_document,
)

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "CAPABILITIES_VERSION",
    "SUPPORTED_CAPABILITIES_VERSIONS",
    "load_schema",
    "schema_path",
    "CapabilitiesValidationError",
    "collect_errors",
    "is_valid",
    "validate_document",
]
