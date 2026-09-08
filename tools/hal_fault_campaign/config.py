"""Loading, validating and *resolving* a campaign configuration.

Two layers, mirroring :mod:`hal_runner.config`:

1. **Schema** -- structure and vocabularies, checked with
   :mod:`hal_findings.jsonschema_mini` against the versioned file in ``schema/``.
2. **Semantics** -- what JSON Schema cannot see: that the clock period lands on
   the cycle grid, that stimulus cycles are inside the workload, that a random
   sample carries a seed, that an absolute window is not empty, that the same
   net is not both an observed output and a detection signal.

Nothing here touches HAL, and nothing here checks whether the netlist exists --
so ``validate`` works on a configuration written for another machine.

The *resolved* configuration is what everything downstream uses and what the
manifest pins by digest.  Resolution fills in every default explicitly, so a
later release that changes a default changes the digest instead of silently
changing the meaning of an old manifest.
"""

import json
import os
import re

from hal_findings import jsonschema_mini

from . import CONFIG_VERSION
from .faultmodel import MODEL_ID
from .workload import ClockGrid, WorkloadError, resolve_stimulus

__all__ = [
    "CONFIG_VERSION",
    "SUPPORTED_CONFIG_VERSIONS",
    "SCHEMA_DIR",
    "DEFAULT_ENGINE",
    "ConfigError",
    "CampaignConfig",
    "schema_path",
    "load_schema",
    "collect_errors",
    "parse",
    "load",
]

SUPPORTED_CONFIG_VERSIONS = ("1.0.0",)

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema")

#: HAL's built-in event-driven engine: in-process, no external tool required.
#: 'verilator' is the other registered engine and needs the verilator binary.
DEFAULT_ENGINE = "hal_simulator"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")

_SCHEMA_CACHE = {}


class ConfigError(ValueError):
    """Raised when a campaign configuration is unusable; carries every problem."""

    def __init__(self, errors, path=None):
        self.errors = list(errors)
        self.path = path
        where = " in {}".format(path) if path else ""
        ValueError.__init__(
            self,
            "invalid campaign configuration{}:\n  - {}".format(
                where, "\n  - ".join(self.errors)
            ),
        )


def schema_path(version=CONFIG_VERSION):
    return os.path.join(SCHEMA_DIR, "campaign-{}.schema.json".format(version))


def load_schema(version=CONFIG_VERSION):
    if version not in _SCHEMA_CACHE:
        with open(schema_path(version), "r", encoding="utf-8") as handle:
            _SCHEMA_CACHE[version] = json.load(handle)
    return _SCHEMA_CACHE[version]


def _schema_errors(document):
    version = document.get("config_version")
    if version not in SUPPORTED_CONFIG_VERSIONS:
        return [
            "config_version {!r} is not supported; this build reads {}".format(
                version, ", ".join(SUPPORTED_CONFIG_VERSIONS)
            )
        ]
    schema = load_schema(version)
    return [str(error) for error in jsonschema_mini.iter_errors(document, schema)]


def _semantic_errors(document):
    errors = []

    name = document.get("name", "")
    if not _IDENTIFIER.match(str(name)):
        errors.append(
            "name {!r} is not usable as a directory name; use letters, digits, '.', "
            "'_' and '-'".format(name)
        )

    clock = document.get("clock") or {}
    period = clock.get("period_ps")
    if isinstance(period, int) and not isinstance(period, bool):
        try:
            ClockGrid(period)
        except WorkloadError as exc:
            errors.append(str(exc))

    workload = document.get("workload") or {}
    cycles = workload.get("cycles")
    if isinstance(cycles, int) and not isinstance(cycles, bool):
        try:
            resolve_stimulus(workload.get("stimulus"), cycles)
        except WorkloadError as exc:
            errors.append(str(exc))

    observation = document.get("observation") or {}
    outputs = set(observation.get("outputs") or [])
    detection = set(observation.get("detection_signals") or [])
    overlap = sorted(outputs & detection)
    if overlap:
        errors.append(
            "{} appear(s) both as an observed output and as a detection signal; a signal "
            "cannot be evidence of corruption and of detection at once".format(
                ", ".join(repr(name) for name in overlap)
            )
        )
    if clock.get("net") in outputs | detection:
        errors.append(
            "the clock net {!r} cannot be an observed output or a detection "
            "signal".format(clock.get("net"))
        )

    window = observation.get("window") or {}
    if window.get("mode") == "absolute":
        start = window.get("start_cycle", 0)
        end = window.get("end_cycle")
        if end is not None and end < start:
            errors.append(
                "the absolute observation window ends (cycle {}) before it starts "
                "(cycle {})".format(end, start)
            )
        if end is not None and isinstance(cycles, int) and start >= cycles:
            errors.append(
                "the absolute observation window starts at cycle {}, outside the {} "
                "simulated cycles".format(start, cycles)
            )
    elif window.get("mode") == "relative":
        if "length" not in window:
            errors.append("a relative observation window needs a 'length'")

    faults = document.get("faults") or {}
    if faults.get("model") not in (None, MODEL_ID):
        errors.append(
            "fault model {!r} is not implemented; this build offers {!r}".format(
                faults.get("model"), MODEL_ID
            )
        )
    sampling = faults.get("sampling") or {}
    if sampling.get("mode") == "random":
        if "count" not in sampling:
            errors.append("random sampling needs a 'count'")
        if "seed" not in sampling:
            errors.append(
                "random sampling needs a 'seed'; an unseeded campaign cannot be replayed"
            )
    if sampling.get("mode") == "exhaustive" and ("count" in sampling or "seed" in sampling):
        errors.append(
            "exhaustive sampling takes neither 'count' nor 'seed'; remove them or switch "
            "to mode 'random'"
        )

    fault_cycles = faults.get("cycles")
    if isinstance(fault_cycles, dict) and isinstance(cycles, int):
        start = fault_cycles.get("from", 0)
        end = fault_cycles.get("to", cycles - 1)
        if end < start:
            errors.append(
                "faults.cycles ends (cycle {}) before it starts (cycle {})".format(end, start)
            )
    if isinstance(fault_cycles, list) and isinstance(cycles, int):
        outside = sorted(value for value in fault_cycles if value >= cycles)
        if outside:
            errors.append(
                "faults.cycles names cycle(s) {} outside the {} simulated cycles".format(
                    ", ".join(str(value) for value in outside), cycles
                )
            )

    return errors


def collect_errors(document):
    """Every problem with ``document``: schema first, then semantics."""
    errors = _schema_errors(document)
    if errors:
        return errors
    return _semantic_errors(document)


class CampaignConfig(object):
    """A validated campaign configuration with every path and default resolved."""

    def __init__(self, document, path=None):
        self.document = document
        self.path = os.path.abspath(path) if path else None
        self.base_dir = os.path.dirname(self.path) if self.path else os.getcwd()

        self.name = document["name"]
        self.description = document.get("description", "")
        self.netlist = self._resolve(document["netlist"])
        self.gate_library = (
            self._resolve(document["gate_library"]) if document.get("gate_library") else None
        )
        self.output_dir = self._resolve(
            document.get("output_dir") or os.path.join(self.base_dir, self.name)
        )
        self.hal_binary = document.get("hal_binary") or None
        self.hal_args = list(document.get("hal_args") or [])
        self.engine = document.get("engine") or DEFAULT_ENGINE

        limits = document.get("limits") or {}
        self.timeout_s = limits.get("timeout_s")
        self.memory_mb = limits.get("memory_mb")

        self.clock_net = document["clock"]["net"]
        self.clock_period_ps = int(document["clock"]["period_ps"])
        self.grid = ClockGrid(self.clock_period_ps)

        workload = document["workload"]
        self.cycles = int(workload["cycles"])
        self.workload_description = workload.get("description", "")
        self.stimulus = resolve_stimulus(workload.get("stimulus"), self.cycles)

        observation = document["observation"]
        self.outputs = list(observation["outputs"])
        self.detection_signals = list(observation.get("detection_signals") or [])
        self.detection_active_value = int(observation.get("detection_active_value", 1))
        self.record_signals = list(observation.get("record_signals") or [])
        self.window = dict(
            observation.get("window") or {"mode": "absolute", "start_cycle": 0,
                                          "end_cycle": self.cycles - 1}
        )
        self.window.setdefault("mode", "absolute")
        if self.window["mode"] == "absolute":
            self.window.setdefault("start_cycle", 0)
            self.window.setdefault("end_cycle", self.cycles - 1)
        else:
            self.window.setdefault("start_offset", 0)

        faults = document["faults"]
        self.fault_model = faults.get("model", MODEL_ID)
        self.hold_cycles = int(faults.get("hold_cycles", 1))
        sites = faults.get("sites") or {}
        self.site_include = list(sites.get("include") or ["*"])
        self.site_exclude = list(sites.get("exclude") or [])
        self.fault_cycles = faults.get("cycles")
        self.sampling = dict(faults.get("sampling") or {"mode": "exhaustive"})

    def _resolve(self, path):
        path = os.path.expanduser(str(path))
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(self.base_dir, path))

    @property
    def recorded_signals(self):
        """Every signal the traces must contain, in a stable order."""
        seen = []
        for name in list(self.outputs) + list(self.detection_signals) + list(
            self.record_signals
        ):
            if name not in seen:
                seen.append(name)
        return seen

    def resolved(self):
        """The configuration with every default made explicit.

        This is what the manifest digests.  Absolute paths are deliberately
        *excluded*: they differ between machines, and the inputs are pinned by
        content hash anyway.
        """
        return {
            "config_version": CONFIG_VERSION,
            "name": self.name,
            "engine": self.engine,
            "clock": {"net": self.clock_net, "period_ps": self.clock_period_ps},
            "grid": self.grid.as_dict(),
            "workload": {
                "cycles": self.cycles,
                "stimulus": [
                    {"cycle": cycle, "inputs": dict(sorted(assignments.items()))}
                    for cycle, assignments in sorted(self.stimulus.items())
                ],
            },
            "observation": {
                "outputs": list(self.outputs),
                "detection_signals": list(self.detection_signals),
                "detection_active_value": self.detection_active_value,
                "record_signals": list(self.record_signals),
                "window": dict(self.window),
            },
            "faults": {
                "model": self.fault_model,
                "hold_cycles": self.hold_cycles,
                "sites": {"include": list(self.site_include),
                          "exclude": list(self.site_exclude)},
                "cycles": self.fault_cycles,
                "sampling": dict(self.sampling),
            },
        }


def parse(document, path=None):
    """Validate ``document`` and return a :class:`CampaignConfig`."""
    errors = collect_errors(document)
    if errors:
        raise ConfigError(errors, path)
    try:
        return CampaignConfig(document, path)
    except WorkloadError as exc:
        raise ConfigError([str(exc)], path)


def load(path):
    """Read, validate and resolve the configuration at ``path``."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise ConfigError(["could not read the configuration: {}".format(exc)], path)
    except ValueError as exc:
        raise ConfigError(["not valid JSON: {}".format(exc)], path)
    if not isinstance(document, dict):
        raise ConfigError(["the configuration must be a JSON object"], path)
    return parse(document, path)
