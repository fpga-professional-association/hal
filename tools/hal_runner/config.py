"""Loading and validating a run configuration.

A run configuration is plain JSON on purpose.  YAML would read slightly nicer
and would drag a third-party parser into a tool whose whole point is to be
runnable inside a bare HAL build container; the standard library reads JSON.

Validation happens in two layers, mirroring :mod:`hal_findings.validate`:

1. **Schema** -- structure and vocabularies, checked with
   :mod:`hal_findings.jsonschema_mini` against the versioned schema file in
   ``schema/``.
2. **Semantics** -- everything JSON Schema cannot see: that step IDs are unique
   and usable as directory names, that every step names a registered analysis,
   and that each step's ``config`` block only uses options that analysis has.
   A misspelled option is rejected here rather than being ignored at run time.

Nothing in this module touches the filesystem beyond reading the configuration
itself; whether the netlist exists is the runner's business, so that
``validate`` can check a configuration written for another machine.
"""

import json
import os
import re

from hal_findings import jsonschema_mini

from . import analyses

__all__ = [
    "CONFIG_VERSION",
    "SUPPORTED_CONFIG_VERSIONS",
    "SCHEMA_DIR",
    "ConfigError",
    "StepConfig",
    "RunConfig",
    "schema_path",
    "load_schema",
    "collect_errors",
    "parse",
    "load",
]

#: The configuration version this build writes.
CONFIG_VERSION = "1.0.0"

#: Every configuration version this build can read.
SUPPORTED_CONFIG_VERSIONS = ("1.0.0",)

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema")

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")

_SCHEMA_CACHE = {}


class ConfigError(ValueError):
    """Raised when a run configuration is unusable; carries every problem found."""

    def __init__(self, errors, path=None):
        self.errors = list(errors)
        self.path = path
        ValueError.__init__(
            self,
            "{} is not a valid run configuration ({} problem{}):\n  - {}".format(
                path or "the run configuration",
                len(self.errors),
                "" if len(self.errors) == 1 else "s",
                "\n  - ".join(self.errors),
            ),
        )


def schema_path(version=CONFIG_VERSION):
    """Absolute path of the schema file for ``version``."""
    if version not in SUPPORTED_CONFIG_VERSIONS:
        raise ValueError(
            "unsupported run configuration version {!r}; this build of hal_runner "
            "understands {}".format(version, ", ".join(SUPPORTED_CONFIG_VERSIONS))
        )
    return os.path.join(SCHEMA_DIR, "run-config-{}.schema.json".format(version))


def load_schema(version=CONFIG_VERSION):
    """Load (and cache) the run configuration schema."""
    if version not in _SCHEMA_CACHE:
        with open(schema_path(version), "r", encoding="utf-8") as handle:
            _SCHEMA_CACHE[version] = json.load(handle)
    return _SCHEMA_CACHE[version]


def schema_errors(document):
    """Structural problems, as human readable strings."""
    version = document.get("config_version") if isinstance(document, dict) else None
    if not isinstance(document, dict):
        return ["a run configuration must be a JSON object, got {}".format(type(document).__name__)]
    if version is None:
        return [
            "no 'config_version'; add \"config_version\": \"{}\"".format(CONFIG_VERSION)
        ]
    if version not in SUPPORTED_CONFIG_VERSIONS:
        return [
            "unsupported config_version {!r}; this build understands {}".format(
                version, ", ".join(SUPPORTED_CONFIG_VERSIONS)
            )
        ]
    return [str(error) for error in jsonschema_mini.iter_errors(document, load_schema(version))]


def semantic_errors(document):
    """Problems JSON Schema cannot express: duplicate IDs, unknown analyses, bad options."""
    errors = []
    seen = set()
    for index, step in enumerate(document.get("steps", [])):
        if not isinstance(step, dict):
            continue
        step_id = step.get("id")
        where = "step {} ({!r})".format(index, step_id)
        if step_id in seen:
            errors.append(
                "duplicate step id {!r}; step IDs name the per-step output directory "
                "and must be unique".format(step_id)
            )
        seen.add(step_id)
        if isinstance(step_id, str) and not _IDENTIFIER.match(step_id):
            errors.append(
                "{}: step IDs must match {} so they are safe as directory "
                "names".format(where, _IDENTIFIER.pattern)
            )

        name = step.get("analysis")
        try:
            analysis = analyses.get(name)
        except analyses.UnknownAnalysis as exc:
            errors.append("{}: {}".format(where, exc))
            continue
        for problem in analyses.config_errors(analysis, step.get("config")):
            errors.append("{}: {}".format(where, problem))
    return errors


def collect_errors(document):
    """Every problem with ``document``, structural ones first."""
    errors = schema_errors(document)
    if errors:
        # The semantic checks assume a structurally valid document.
        return errors
    return semantic_errors(document)


class StepConfig(object):
    """One resolved step: what to run, with which options and which limits."""

    def __init__(self, index, document, defaults):
        self.index = index
        self.id = document["id"]
        self.analysis_name = document["analysis"]
        self.analysis = analyses.get(self.analysis_name)
        self.description = document.get("description")
        self.config = analyses.resolve_config(self.analysis, document.get("config"))
        self.timeout_s = _first_set(document.get("timeout_s"), defaults.get("timeout_s"))
        self.memory_mb = _first_set(document.get("memory_mb"), defaults.get("memory_mb"))
        self.continue_on_failure = bool(document.get("continue_on_failure", False))

    def as_json(self):
        """The step exactly as the manifest and the cache key record it."""
        return {
            "id": self.id,
            "analysis": self.analysis_name,
            "config": self.config,
            "timeout_s": self.timeout_s,
            "memory_mb": self.memory_mb,
            "continue_on_failure": self.continue_on_failure,
        }


def _first_set(*values):
    for value in values:
        if value is not None:
            return value
    return None


class RunConfig(object):
    """A validated run configuration with every path resolved."""

    def __init__(self, document, path=None, base_dir=None):
        errors = collect_errors(document)
        if errors:
            raise ConfigError(errors, path)

        self.document = document
        self.path = os.path.abspath(path) if path else None
        self.base_dir = os.path.abspath(base_dir or (os.path.dirname(self.path) if self.path else os.getcwd()))

        self.config_version = document["config_version"]
        self.name = document["name"]
        self.description = document.get("description")
        self.netlist = self._resolve(document["netlist"])
        self.gate_library = self._resolve(document.get("gate_library"))
        self.output_dir = self._resolve(document.get("output_dir")) or os.path.join(
            self.base_dir, self.name
        )
        self.hal_binary = document.get("hal_binary") or None
        self.hal_args = list(document.get("hal_args", []))
        self.stop_on_failure = bool(document.get("stop_on_failure", True))
        defaults = document.get("defaults", {}) or {}
        self.default_timeout_s = defaults.get("timeout_s")
        self.default_memory_mb = defaults.get("memory_mb")
        self.steps = [
            StepConfig(index, step, defaults)
            for index, step in enumerate(document["steps"])
        ]

    def _resolve(self, value):
        if not value:
            return None
        value = os.path.expanduser(str(value))
        if os.path.isabs(value):
            return os.path.normpath(value)
        return os.path.normpath(os.path.join(self.base_dir, value))

    def step_by_id(self, step_id):
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(
            "no step {!r} in run {!r}; it has {}".format(
                step_id, self.name, ", ".join(step.id for step in self.steps)
            )
        )

    def as_json(self):
        """The normalized configuration recorded in the manifest.

        Paths appear as written in the configuration file (portable, and stable
        across machines, so the manifest digest is too); the absolute paths they
        resolved to are recorded separately under ``inputs``.
        """
        return {
            "config_version": self.config_version,
            "name": self.name,
            "description": self.description,
            "netlist": self.document["netlist"],
            "gate_library": self.document.get("gate_library"),
            "output_dir": self.document.get("output_dir"),
            "hal_args": self.hal_args,
            "stop_on_failure": self.stop_on_failure,
            "defaults": {
                "timeout_s": self.default_timeout_s,
                "memory_mb": self.default_memory_mb,
            },
            "steps": [step.as_json() for step in self.steps],
        }


def parse(document, path=None, base_dir=None):
    """Validate ``document`` and return a :class:`RunConfig`."""
    return RunConfig(document, path=path, base_dir=base_dir)


def load(path):
    """Read and validate a run configuration file."""
    path = os.path.abspath(os.path.expanduser(str(path)))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise ConfigError(["could not read the configuration: {}".format(exc)], path)
    except ValueError as exc:
        raise ConfigError(["not valid JSON: {}".format(exc)], path)
    return RunConfig(document, path=path)
