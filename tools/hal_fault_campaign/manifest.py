"""The campaign manifest: what ran, against what, and with which verdict.

A campaign manifest is a superset of what :mod:`hal_runner.manifest` records for
a step, and it is built on the same primitives (``hal_runner.hashing``) on
purpose -- one digest vocabulary across the repository, with ``sha256`` reserved
for things ``sha256sum`` can reproduce and our own constructions named
(``sha256-tree/1``, ``sha256-json/1``).

What makes it a *replay* manifest rather than a log is that it records the
resolved configuration and the **fully enumerated fault list**, not just the
seed.  Replay therefore does not have to reproduce the sampler to reproduce the
campaign: it re-injects exactly the faults the manifest names, at exactly the
cycles it names, into a netlist whose content digest it re-checks first.  The
seed and the sampling algorithm are recorded as well, so that the enumeration
*can* be re-derived and cross-checked -- and ``replay --verify-enumeration``
does exactly that.

``manifest_digest`` hashes the manifest with everything unreproducible removed
(timestamps, durations, absolute paths, host details), so two runs of the same
campaign against the same HAL share a digest.
"""

import copy
import json
import os
import platform
import sys

from hal_runner.hashing import ALGORITHM_JSON, json_digest

from . import MANIFEST_VERSION, __version__
from . import campaign as campaign_module

__all__ = [
    "MANIFEST_VERSION",
    "SUPPORTED_MANIFEST_VERSIONS",
    "ManifestError",
    "build",
    "write",
    "read",
    "manifest_digest",
    "faults_of",
    "resolved_config_of",
    "source_config_of",
    "summarize_text",
]

SUPPORTED_MANIFEST_VERSIONS = ("1.0.0",)

#: Fields dropped before digesting: they are true, and they are not reproducible.
_VOLATILE_KEYS = (
    "started_at",
    "finished_at",
    "duration_s",
    "generated_at",
    "paths",
    "logs",
    "host",
    "output_dir",
    "resolved_path",
    "hal_binary_path",
    "command",
)


class ManifestError(ValueError):
    """Raised when a manifest cannot be read or does not fit this build."""


def _host():
    return {
        "python": "{}.{}.{}".format(*sys.version_info[:3]),
        "platform": platform.platform(),
    }


def build(config, inputs, tool, enumeration, sites, faults, results, summary,
          status, engine, instrumentation=None, skipped=(), artifacts=(), logs=None,
          exit_code=0, started_at=None, finished_at=None, duration_s=None,
          diagnostics=None, output_dir=None):
    """Assemble the manifest. Pure: everything it records is passed in."""
    resolved = config.resolved()
    fault_records = []
    for fault in faults:
        record = dict(fault)
        result = (results or {}).get(fault["id"])
        if result:
            record.update(
                {
                    "classification": result["classification"],
                    "detection_latency_cycles": result["detection_latency_cycles"],
                    "divergence_latency_cycles": result["divergence_latency_cycles"],
                    "window": result["window"]["cycles"],
                    "observed_cycles": result["window"]["observed_cycles"],
                    "detection_cycles": result["detection_cycles"],
                    "divergence_cycles": result["divergence_cycles"][:32],
                    "indeterminate": result["indeterminate"],
                }
            )
        fault_records.append(record)

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "campaign": {
            "name": config.name,
            "description": config.description,
            "status": status,
            "exit_code": int(exit_code),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_s": None if duration_s is None else round(float(duration_s), 3),
        },
        "tool": dict(tool),
        "host": _host(),
        "engine": engine,
        "inputs": list(inputs),
        "config": resolved,
        # The configuration exactly as written, so that replay can re-run the campaign
        # without having to invert the resolution above.
        "config_source": copy.deepcopy(config.document),
        "config_digest": json_digest(resolved),
        "config_digest_algorithm": ALGORITHM_JSON,
        "enumeration": dict(enumeration),
        "enumeration_digest": json_digest([_fault_key(f) for f in fault_records]),
        "instrumentation": dict(instrumentation or {}),
        "sites": list(sites),
        "skipped_sites": list(skipped),
        "faults": fault_records,
        "summary": dict(summary or {}),
        "artifacts": list(artifacts),
        "logs": dict(logs or {}),
    }
    if output_dir:
        manifest["output_dir"] = output_dir
    if diagnostics:
        manifest["diagnostics"] = list(diagnostics)
    return manifest


def _fault_key(fault):
    return {
        "id": fault["id"],
        "site": fault["site"],
        "cycle": fault["cycle"],
        "hold_cycles": fault["hold_cycles"],
    }


def write(manifest, path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n")
    return path


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except OSError as exc:
        raise ManifestError("could not read the manifest at {}: {}".format(path, exc))
    except ValueError as exc:
        raise ManifestError("the manifest at {} is not valid JSON: {}".format(path, exc))
    version = manifest.get("manifest_version")
    if version not in SUPPORTED_MANIFEST_VERSIONS:
        raise ManifestError(
            "the manifest at {} declares manifest_version {!r}; this build "
            "({}) reads {}".format(path, version, __version__,
                                   ", ".join(SUPPORTED_MANIFEST_VERSIONS))
        )
    return manifest


def _strip(value):
    if isinstance(value, dict):
        return {
            key: _strip(item)
            for key, item in value.items()
            if key not in _VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_strip(item) for item in value]
    return value


def manifest_digest(manifest):
    """Digest of the reproducible part of a manifest."""
    reduced = _strip(copy.deepcopy(manifest))
    reduced.pop("host", None)
    return json_digest(reduced)


def faults_of(manifest):
    """The manifest's fault list as :class:`~hal_fault_campaign.campaign.FaultSpec`."""
    return [campaign_module.fault_from_dict(entry) for entry in manifest.get("faults", [])]


def resolved_config_of(manifest):
    return manifest.get("config", {})


def source_config_of(manifest):
    """The configuration as originally written, ready to re-parse for a replay."""
    document = manifest.get("config_source")
    if not document:
        raise ManifestError(
            "the manifest carries no 'config_source', so the campaign it describes "
            "cannot be re-run from it"
        )
    return copy.deepcopy(document)


def summarize_text(manifest):
    """A human summary of a manifest, for ``hal_fault_campaign manifest``."""
    lines = []
    campaign = manifest.get("campaign", {})
    lines.append(
        "campaign {!r}: {} (exit {})".format(
            campaign.get("name"), campaign.get("status"), campaign.get("exit_code")
        )
    )
    lines.append("  engine          : {}".format(manifest.get("engine")))
    lines.append("  manifest digest : {}".format(manifest_digest(manifest)))
    lines.append("  config digest   : {}".format(manifest.get("config_digest")))
    for entry in manifest.get("inputs", []):
        lines.append(
            "  input {:<10}: {} {}".format(
                entry.get("role", "?"),
                entry.get("algorithm", "sha256"),
                entry.get("digest") or entry.get("sha256", "-"),
            )
        )
    enumeration = manifest.get("enumeration", {})
    lines.append(
        "  enumeration     : {} of {} pair(s), mode {}{}".format(
            enumeration.get("selected"),
            enumeration.get("grid_size"),
            enumeration.get("mode"),
            ", seed {}".format(enumeration["seed"]) if "seed" in enumeration else "",
        )
    )
    summary = manifest.get("summary", {})
    counts = summary.get("counts", {})
    for name in sorted(counts):
        lines.append("  {:<16}: {}".format(name, counts[name]))
    latency = summary.get("detection_latency_cycles")
    if latency:
        lines.append(
            "  detect latency  : min {} / median {} / max {} cycle(s)".format(
                latency["min"], latency["median"], latency["max"]
            )
        )
    divergence = summary.get("divergence_latency_cycles")
    if divergence:
        lines.append(
            "  diverge latency : min {} / median {} / max {} cycle(s)".format(
                divergence["min"], divergence["median"], divergence["max"]
            )
        )
    if manifest.get("skipped_sites"):
        lines.append(
            "  coverage gap    : {} sequential gate(s) could not be instrumented".format(
                len(manifest["skipped_sites"])
            )
        )
    return "\n".join(lines)
