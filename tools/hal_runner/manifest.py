"""The run manifest: what ran, against what, with which result.

The manifest is the deliverable.  A directory full of ``findings.json`` files
is worth very little on its own -- it does not say which netlist produced them,
which HAL, with what configuration, or whether the run finished.  The manifest
answers exactly those questions, and it is written *whether or not the run
succeeded*, because a failed run needs a record more than a successful one
does.

Determinism is a property of the manifest, not an aspiration: keys are sorted,
floats are rounded to milliseconds, and :func:`digest` hashes the manifest with
the fields that cannot be reproduced (timestamps, durations, absolute paths,
the machine's Python build) removed.  Two runs of the same configuration on the
same inputs against the same HAL therefore share a manifest digest, and CI can
diff runs instead of eyeballing them.
"""

import json
import os
import platform
import subprocess
import sys

from hal_findings.schema import SCHEMA_VERSION as FINDINGS_SCHEMA_VERSION

from . import __version__
from .analyses import REGISTRY_VERSION
from .hashing import json_digest

__all__ = [
    "MANIFEST_VERSION",
    "VOLATILE",
    "dumps",
    "write",
    "read",
    "strip_volatile",
    "digest",
    "hal_version_info",
    "tool_info",
    "summarize",
    "STEP_STATUSES",
    "SUCCESS_STATUSES",
]

#: Version of the manifest format itself.
MANIFEST_VERSION = "1.0.0"

#: Every status a step record can carry.
STEP_STATUSES = ("success", "reused", "failed", "timeout", "skipped")

#: Statuses that do not make the run a failure.
SUCCESS_STATUSES = ("success", "reused")

#: Fields excluded from :func:`digest` because they cannot be reproduced.
#: ``"[]"`` marks a list of objects whose every element is stripped.
VOLATILE = {
    "generated_at": True,
    "run": {"id": True, "started_at": True, "finished_at": True, "duration_s": True},
    "producer": {"command": True},
    "environment": True,
    "tool": {"hal": {"binary": True, "binary_sha256": True}},
    "inputs": ["[]", {"resolved_path": True, "extracted_to": True}],
    "output_dir": True,
    "configuration_file": {"path": True},
    "steps": [
        "[]",
        {
            "started_at": True,
            "finished_at": True,
            "duration_s": True,
            "output_dir": True,
            "command": True,
            "logs": True,
            "execution": {"command": True, "duration_s": True},
            "cache": {"path": True},
        },
    ],
}


def _strip(node, spec):
    """Return ``node`` with everything ``spec`` marks as volatile removed."""
    if spec is True:
        return None
    if isinstance(node, list) and isinstance(spec, list) and spec and spec[0] == "[]":
        return [_strip(item, spec[1]) for item in node]
    if not isinstance(node, dict) or not isinstance(spec, dict):
        return node
    result = {}
    for key, value in node.items():
        if key not in spec:
            result[key] = value
            continue
        if spec[key] is True:
            continue
        result[key] = _strip(value, spec[key])
    return result


def strip_volatile(manifest, spec=None):
    """A copy of ``manifest`` without the fields that differ between machines and runs."""
    return _strip(json.loads(json.dumps(manifest)), VOLATILE if spec is None else spec)


def digest(manifest):
    """SHA-256 over the reproducible part of ``manifest``."""
    return json_digest(strip_volatile(manifest))


def dumps(manifest, indent=2):
    """Serialize deterministically, with a trailing newline."""
    return json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=indent) + "\n"


def write(manifest, path):
    """Write ``manifest`` to ``path`` (UTF-8, LF endings) and return the path."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps(manifest))
    return path


def read(path):
    """Read a manifest from ``path``."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# tool identity
# ---------------------------------------------------------------------------


def _run_capture(command, cwd=None, timeout=30):
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    text = completed.stdout.decode("utf-8", "replace").strip()
    return text or None


def _repo_version(repo_root):
    """Fall back to the checkout's own version when no hal binary can be asked."""
    version = None
    source = None
    if repo_root:
        current = os.path.join(repo_root, "CURRENT_VERSION")
        if os.path.isfile(current):
            try:
                with open(current, "r", encoding="utf-8") as handle:
                    version = handle.read().strip() or None
                source = "CURRENT_VERSION"
            except OSError:
                version = None
        described = _run_capture(["git", "describe", "--tags", "--always", "--dirty"], cwd=repo_root)
        if described:
            version = described
            source = "git describe"
    return version, source


def hal_version_info(hal_binary=None, repo_root=None):
    """Identify the HAL the steps will run against.

    ``hal --version`` prints the git-describe string the binary was built from,
    which is the only answer that describes the *binary* rather than the
    checkout, so it is preferred.  When there is no binary to ask (a
    configuration check, a ``--dry-run``), the checkout's ``CURRENT_VERSION``
    and ``git describe`` stand in -- and ``source`` says which it was, because a
    cache key built on a guess must never be mistaken for one built on the real
    thing.
    """
    info = {"version": "unknown", "source": "unavailable"}
    if hal_binary and os.path.isfile(hal_binary):
        info["binary"] = os.path.abspath(hal_binary)
        reported = _run_capture([hal_binary, "--version"])
        if reported:
            info["version"] = reported.splitlines()[-1].strip()
            info["source"] = "hal --version"
            return info
    version, source = _repo_version(repo_root)
    if version:
        info["version"] = version
        info["source"] = source
    return info


def tool_info(hal_binary=None, repo_root=None, hal_version=None):
    """The full tool identity recorded in the manifest and hashed into cache keys."""
    hal = hal_version if hal_version is not None else hal_version_info(hal_binary, repo_root)
    if hal_binary and "binary" not in hal:
        hal = dict(hal)
        hal["binary"] = os.path.abspath(hal_binary)
    return {
        "hal": hal,
        "hal_runner": {"version": __version__, "analyses": REGISTRY_VERSION},
        "findings_schema_version": FINDINGS_SCHEMA_VERSION,
        "manifest_version": MANIFEST_VERSION,
    }


def environment_info():
    """Machine details that help explain a failure but never key a cache."""
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": os.getcwd(),
    }


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def counts(manifest):
    """Step counts per status, including statuses that did not occur."""
    result = {status: 0 for status in STEP_STATUSES}
    for step in manifest.get("steps", []):
        status = step.get("status")
        result[status] = result.get(status, 0) + 1
    return result


def summarize(manifest):
    """A short human readable report of a run."""
    run = manifest.get("run", {})
    lines = [
        "run {!r}: {}".format(run.get("name"), run.get("status")),
        "  hal          {} ({})".format(
            manifest.get("tool", {}).get("hal", {}).get("version"),
            manifest.get("tool", {}).get("hal", {}).get("source"),
        ),
        "  manifest     digest {}".format(digest(manifest)[:12]),
    ]
    for entry in manifest.get("inputs", []):
        lines.append(
            "  input        {:<12} {}:{}".format(
                entry.get("role", "?"), entry.get("digest_algorithm"), entry.get("digest", "")[:12]
            )
        )
    for step in manifest.get("steps", []):
        findings = step.get("findings") or {}
        detail = ""
        if findings.get("counts"):
            detail = " [{}]".format(
                ", ".join(
                    "{} {}".format(count, status)
                    for status, count in sorted(findings["counts"].items())
                )
            )
        elif step.get("diagnostic"):
            detail = " [diagnostic: {}]".format(step["diagnostic"].get("path"))
        lines.append(
            "  step {:<12} {:<8} {}{}".format(
                step.get("id"), step.get("status"), step.get("analysis"), detail
            )
        )
    return "\n".join(lines)
