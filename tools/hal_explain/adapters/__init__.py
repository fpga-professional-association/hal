"""Adapters from existing analysis results into block contributions.

Every adapter reads a *findings document* -- the shared ``tools/hal_findings``
contract that dataflow analysis, module identification and ``hal_fsm`` already
write -- and returns :class:`hal_explain.adapters.common.Contribution` objects.
None of them re-derives anything from the netlist: if a claim is not in a
findings document, this tool does not make it.

``module_identification`` is the one analysis that has no findings adapter in
``tools/hal_findings`` yet, so
:mod:`hal_explain.adapters.module_identification` provides one as well as the
block mapping.  It is written against the plugin's Python bindings but imports
nothing from HAL, so it is unit-testable against stubs; see the README for the
one-line move into ``hal_findings/adapters/`` that would make it shared.
"""

from . import common, dataflow, fsm, module_identification

__all__ = ["common", "dataflow", "fsm", "module_identification", "ADAPTERS", "select_adapter"]

#: adapter name -> module.  ``select_adapter`` uses this and the order matters
#: only for the auto-detection fallback.
ADAPTERS = {
    "dataflow": dataflow,
    "module_identification": module_identification,
    "fsm": fsm,
}


def select_adapter(document, name=None):
    """Pick the adapter for a findings document.

    An explicit ``name`` always wins.  Otherwise the choice is made from
    ``analysis.plugin.name``, which every adapter in this repository sets --
    never from the file name, and never by sniffing finding IDs first, because a
    document that merely mentions ``fsm`` in a title is not an FSM document.
    """
    if name:
        if name not in ADAPTERS:
            raise KeyError(
                "unknown adapter {!r}; known adapters are {}".format(
                    name, ", ".join(sorted(ADAPTERS))
                )
            )
        return name, ADAPTERS[name]

    plugin = (document.get("analysis") or {}).get("plugin") or {}
    plugin_name = plugin.get("name") or ""
    for adapter_name, module in sorted(ADAPTERS.items()):
        if plugin_name in module.PLUGIN_NAMES:
            return adapter_name, module

    # Fall back on the finding-ID prefixes each adapter publishes.  This is the
    # last resort and it is reported as such by the caller.
    finding_ids = [entry.get("id", "") for entry in document.get("findings", [])]
    for adapter_name, module in sorted(ADAPTERS.items()):
        if any(
            finding_id.startswith(prefix)
            for finding_id in finding_ids
            for prefix in module.FINDING_PREFIXES
        ):
            return adapter_name, module

    raise KeyError(
        "cannot tell which analysis produced this document: analysis.plugin.name is "
        "{!r} and no finding ID matches a known prefix. Pass --adapter "
        "<{}> explicitly.".format(plugin_name, "|".join(sorted(ADAPTERS)))
    )
