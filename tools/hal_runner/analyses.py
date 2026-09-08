"""The registry of analyses a run configuration may name.

A step does not name a Python callable; it names an entry in this registry.
That indirection buys three things the runner needs:

* the configuration of a step can be *validated before anything runs* -- an
  unknown option is a typo that would otherwise silently do nothing, so it is
  rejected at configuration time rather than ignored at analysis time;
* the runner knows, without importing ``hal_py``, what kind of claim the
  analysis produces (``method_kind``), which is what the diagnostic finding for
  a timed-out or failed step has to state honestly;
* the configuration digest that keys the checkpoint cache is taken over the
  *resolved* configuration, defaults included, so adding a default in a later
  release invalidates cached results instead of silently reusing them.

Adding an analysis means adding an entry here and a module under
``hal_runner.steps`` exposing ``run(hal_py, netlist, config, context)``.
"""

from . import __version__

__all__ = [
    "Analysis",
    "Option",
    "ANALYSES",
    "names",
    "get",
    "resolve_config",
    "UnknownAnalysis",
    "AnalysisConfigError",
]


class UnknownAnalysis(ValueError):
    """Raised when a step names an analysis that is not registered."""


class AnalysisConfigError(ValueError):
    """Raised when a step's ``config`` block does not fit the analysis."""


class Option(object):
    """One configuration option of an analysis: its type, default and meaning."""

    def __init__(self, name, types, default, description, choices=None, minimum=None):
        self.name = name
        self.types = types if isinstance(types, tuple) else (types,)
        self.default = default
        self.description = description
        self.choices = choices
        self.minimum = minimum

    def check(self, value):
        """Return a list of problems with ``value`` (empty when it is acceptable)."""
        problems = []
        # bool is a subclass of int in Python; an analysis that wants a number must
        # not silently accept True.
        if isinstance(value, bool) and bool not in self.types:
            problems.append("{!r} is a boolean, expected {}".format(value, self._type_names()))
            return problems
        if not isinstance(value, self.types):
            problems.append(
                "{!r} has type {}, expected {}".format(
                    value, type(value).__name__, self._type_names()
                )
            )
            return problems
        if self.choices is not None and value not in self.choices:
            problems.append("{!r} is not one of {}".format(value, list(self.choices)))
        if self.minimum is not None and isinstance(value, (int, float)) and value < self.minimum:
            problems.append("{!r} is below the minimum {}".format(value, self.minimum))
        return problems

    def _type_names(self):
        return "/".join(
            "null" if entry is type(None) else entry.__name__ for entry in self.types
        )


class Analysis(object):
    """A registered analysis: what it is, how it is called, what it may claim."""

    def __init__(
        self,
        name,
        module,
        plugin,
        entry_point,
        method_kind,
        method_name,
        description,
        options,
        deterministic,
        artifact_roles,
    ):
        self.name = name
        #: dotted module under ``hal_runner.steps`` implementing ``run()``.
        self.module = module
        #: The HAL plugin the step needs, by the name it registers itself under --
        #: which is both ``hal_plugins.<plugin>`` and the key
        #: ``plugin_manager.get_plugin_instance`` takes, and is *not* always the
        #: directory name under ``plugins/`` (DANA lives in
        #: ``plugins/dataflow_analysis`` but calls itself ``dataflow``).
        #: A missing plugin is a step failure, never a skip.
        self.plugin = plugin
        self.entry_point = entry_point
        self.method_kind = method_kind
        self.method_name = method_name
        self.description = description
        self.options = {option.name: option for option in options}
        #: True when two runs on identical inputs must produce identical findings.
        self.deterministic = deterministic
        self.artifact_roles = tuple(artifact_roles)

    def defaults(self):
        return {name: option.default for name, option in self.options.items()}


_COMPONENTS = Analysis(
    name="graph_algorithm.connected_components",
    module="hal_runner.steps.graph_algorithm_components",
    plugin="graph_algorithm",
    entry_point="graph_algorithm.get_connected_components",
    method_kind="structural",
    method_name="connected components of the netlist graph (igraph)",
    description=(
        "Build the netlist graph with graph_algorithm.NetlistGraph.from_netlist and "
        "report its (strongly) connected components. Purely structural and fully "
        "deterministic: the same netlist always yields the same components, which is "
        "why this is the analysis the first documented workflow uses."
    ),
    options=[
        Option(
            "strong",
            bool,
            True,
            "compute strongly connected components (feedback loops) rather than weakly "
            "connected ones",
        ),
        Option(
            "min_size",
            int,
            2,
            "ignore components smaller than this; 0 reports every component",
            minimum=0,
        ),
        Option(
            "max_components",
            int,
            64,
            "emit at most this many component findings, largest first; the total count "
            "is always reported on the summary finding",
            minimum=1,
        ),
        Option(
            "max_gates_per_component",
            int,
            512,
            "list at most this many gates per component finding; a truncated listing is "
            "marked as such rather than silently shortened",
            minimum=1,
        ),
        Option(
            "create_dummy_vertices",
            bool,
            False,
            "create dummy vertices for nets missing a source or destination "
            "(NetlistGraph.from_netlist's own option)",
        ),
    ],
    deterministic=True,
    artifact_roles=("findings",),
)

_DATAFLOW = Analysis(
    name="dataflow.groups",
    module="hal_runner.steps.dataflow_groups",
    # plugins/dataflow_analysis registers itself as "dataflow" (DataflowPlugin::get_name)
    # and its bindings are PYBIND11_MODULE(dataflow, ...); the directory name is not it.
    plugin="dataflow",
    entry_point="dataflow.analyze",
    method_kind="heuristic",
    method_name="dataflow analysis (DANA)",
    description=(
        "Run DANA word-level register recovery and record every candidate group as a "
        "heuristic finding through hal_findings.adapters.dataflow. DANA reconstructs "
        "registers from structural evidence and proves nothing, so its findings can "
        "never be more than heuristic."
    ),
    options=[
        Option(
            "min_group_size",
            (int, type(None)),
            None,
            "minimum register group size; null leaves DANA's own default in place",
            minimum=1,
        ),
        Option(
            "expected_sizes",
            list,
            [],
            "group sizes to prioritize, e.g. [8, 16, 32]",
        ),
        Option(
            "stage_identification", bool, False, "enable DANA's stage identification"
        ),
        Option(
            "type_consistency",
            bool,
            False,
            "require all gates of a group to share a gate type",
        ),
        Option("write_dot", bool, True, "also write DANA's own graph.dot export"),
        Option("write_txt", bool, True, "also write DANA's own groups.txt listing"),
    ],
    deterministic=False,
    artifact_roles=("findings", "dot", "report"),
)

#: Every analysis a run configuration may name, keyed by its configuration name.
ANALYSES = {analysis.name: analysis for analysis in (_COMPONENTS, _DATAFLOW)}

#: Bumped when the meaning of a resolved configuration changes; part of every
#: cache key so that a semantic change can never reuse an old result.
REGISTRY_VERSION = "1.0.0"


def names():
    """Registered analysis names, sorted."""
    return sorted(ANALYSES)


def get(name):
    """Look up an analysis, raising :class:`UnknownAnalysis` with the alternatives."""
    try:
        return ANALYSES[name]
    except KeyError:
        raise UnknownAnalysis(
            "unknown analysis {!r}; hal_runner {} knows {}".format(
                name, __version__, ", ".join(names())
            )
        )


def config_errors(analysis, config):
    """Return every problem with a step's raw ``config`` block."""
    errors = []
    if config is None:
        return errors
    if not isinstance(config, dict):
        return ["config must be an object, got {}".format(type(config).__name__)]
    for key, value in sorted(config.items()):
        option = analysis.options.get(key)
        if option is None:
            errors.append(
                "unknown option {!r} for analysis {!r}; supported options are {}".format(
                    key, analysis.name, ", ".join(sorted(analysis.options)) or "(none)"
                )
            )
            continue
        for problem in option.check(value):
            errors.append("option {!r}: {}".format(key, problem))
    return errors


def resolve_config(analysis, config):
    """Merge ``config`` onto the analysis defaults, rejecting anything unknown.

    The result is what the step actually runs with *and* what the configuration
    digest is taken over, so a default that changes in a later release changes
    the cache key too.
    """
    errors = config_errors(analysis, config)
    if errors:
        raise AnalysisConfigError(
            "invalid configuration for analysis {!r}:\n  - {}".format(
                analysis.name, "\n  - ".join(errors)
            )
        )
    resolved = analysis.defaults()
    resolved.update(config or {})
    return resolved
