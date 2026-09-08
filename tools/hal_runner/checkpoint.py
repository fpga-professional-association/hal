"""Checkpoint reuse that has to prove itself.

A cache that answers with a stale result is worse than no cache: it turns a
reproducible pipeline into one that quietly reports yesterday's answer about
today's netlist.  So this store is built around one rule -- **a hit must be
justified, a doubt is a miss**.

A checkpoint is addressed by :func:`cache_key`, a hash over

* the digests of every run input (the netlist, and the gate library if one was
  given),
* the analysis name and its *resolved* configuration, defaults included,
* the HAL version the step would run against, plus how that version was
  determined (a version guessed from the checkout must not collide with the
  same string read from a real binary),
* the versions of hal_runner, its analysis registry, the findings schema and
  the cache format itself.

Change any of those and the key changes, so the old entry is simply never
looked at.  On a key match the entry is still verified before it is used: the
recorded key components must match the ones just computed, every artifact must
still be present, and every artifact's ``sha256`` must still match what was
recorded when it was stored.  A mismatch is reported as a rejection with a
reason -- which lands in the manifest -- and the step re-runs.
"""

import json
import os
import shutil

from . import __version__
from .analyses import REGISTRY_VERSION
from .hashing import json_digest, sha256_file
from .manifest import write as write_json

__all__ = [
    "CACHE_FORMAT_VERSION",
    "ENTRY_FILE",
    "ARTIFACT_DIR",
    "key_components",
    "cache_key",
    "CheckpointEntry",
    "CheckpointStore",
]

#: Bumped whenever the on-disk layout of an entry changes. Part of the key, so
#: an older entry can never be read by a newer runner (or the other way round).
CACHE_FORMAT_VERSION = "1.0.0"

ENTRY_FILE = "entry.json"
ARTIFACT_DIR = "artifacts"


def key_components(step, inputs, tool):
    """The exact facts a cached result depends on.

    :param step: the step's ``as_json()`` form (analysis + resolved config).
    :param inputs: manifest input entries (``role``/``digest_algorithm``/``digest``).
    :param tool: :func:`hal_runner.manifest.tool_info` output.
    """
    hal = tool.get("hal", {})
    return {
        "cache_format": CACHE_FORMAT_VERSION,
        "analysis": step["analysis"],
        "config": step["config"],
        "inputs": sorted(
            (
                {
                    "role": entry.get("role"),
                    "digest_algorithm": entry.get("digest_algorithm"),
                    "digest": entry.get("digest"),
                }
                for entry in inputs
            ),
            key=lambda entry: (entry["role"] or "", entry["digest"] or ""),
        ),
        "tool": {
            "hal_version": hal.get("version"),
            "hal_version_source": hal.get("source"),
            "hal_runner": __version__,
            "analyses": REGISTRY_VERSION,
            "findings_schema": tool.get("findings_schema_version"),
        },
    }


def cache_key(components):
    """The content address of a checkpoint."""
    return json_digest(components)


class CheckpointEntry(object):
    """A stored checkpoint, already verified against its recorded hashes."""

    def __init__(self, key, path, record):
        self.key = key
        self.path = path
        self.record = record

    @property
    def artifacts(self):
        return self.record.get("artifacts", [])

    @property
    def result(self):
        return self.record.get("result", {})


class CheckpointStore(object):
    """A content-addressed store of step results under one directory."""

    def __init__(self, root, enabled=True, refresh=False):
        self.root = os.path.abspath(str(root))
        self.enabled = bool(enabled)
        #: With ``refresh`` set, existing entries are ignored but new ones are
        #: still written -- the way to rebuild a cache you no longer trust.
        self.refresh = bool(refresh)
        self.rejections = []

    def entry_dir(self, key):
        return os.path.join(self.root, key)

    # -- lookup -------------------------------------------------------------

    def lookup(self, key, components):
        """Return a verified :class:`CheckpointEntry`, or ``None`` with a reason.

        The reason is available as the second element of the returned tuple and
        is recorded in the manifest so that a cache that never hits is visible
        rather than merely slow.
        """
        if not self.enabled:
            return None, "caching disabled"
        if self.refresh:
            return None, "cache refresh requested"

        directory = self.entry_dir(key)
        record_path = os.path.join(directory, ENTRY_FILE)
        if not os.path.isfile(record_path):
            return None, "no entry for this key"

        try:
            with open(record_path, "r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError) as exc:
            return None, "entry is unreadable ({})".format(exc)

        if record.get("cache_format") != CACHE_FORMAT_VERSION:
            return None, "entry was written by cache format {!r}, this is {!r}".format(
                record.get("cache_format"), CACHE_FORMAT_VERSION
            )
        if record.get("key") != key:
            return None, "entry records key {!r} but sits under {!r}".format(
                record.get("key"), key
            )
        stored_components = record.get("key_components")
        if stored_components != components:
            # The key is a hash of exactly these components, so this can only
            # happen if an entry was edited, truncated or hand-copied. Refuse it.
            return None, "entry key components do not match the ones just computed"

        for artifact in record.get("artifacts", []):
            cached = os.path.join(directory, ARTIFACT_DIR, artifact["path"])
            if not os.path.isfile(cached):
                return None, "cached artifact {!r} is missing".format(artifact["path"])
            if sha256_file(cached) != artifact.get("sha256"):
                return None, "cached artifact {!r} no longer matches its recorded sha256".format(
                    artifact["path"]
                )

        return CheckpointEntry(key, directory, record), "hit"

    # -- storing ------------------------------------------------------------

    def store(self, key, components, step_dir, artifacts, result):
        """Copy a successful step's artifacts into the store and record them.

        ``artifacts`` are manifest artifact entries with ``path`` relative to
        ``step_dir``.  Storing is best effort in the sense that a failure to
        write the cache must never fail the run -- but it is *not* best effort
        about correctness: a partially written entry is removed rather than
        left for a later run to trip over.
        """
        if not self.enabled:
            return None
        directory = self.entry_dir(key)
        try:
            if os.path.isdir(directory):
                shutil.rmtree(directory)
            os.makedirs(os.path.join(directory, ARTIFACT_DIR), exist_ok=True)
            stored = []
            for artifact in artifacts:
                source = os.path.join(step_dir, artifact["path"])
                if not os.path.isfile(source):
                    continue
                target = os.path.join(directory, ARTIFACT_DIR, artifact["path"])
                parent = os.path.dirname(target)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                shutil.copyfile(source, target)
                stored.append(dict(artifact))
            record = {
                "cache_format": CACHE_FORMAT_VERSION,
                "key": key,
                "key_components": components,
                "artifacts": stored,
                "result": result,
            }
            write_json(record, os.path.join(directory, ENTRY_FILE))
            return CheckpointEntry(key, directory, record)
        except OSError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            self.rejections.append("could not store checkpoint {}: {}".format(key[:12], exc))
            return None

    # -- restoring ----------------------------------------------------------

    def restore(self, entry, step_dir):
        """Copy a verified entry's artifacts into ``step_dir``; returns their paths."""
        os.makedirs(step_dir, exist_ok=True)
        restored = []
        for artifact in entry.artifacts:
            source = os.path.join(entry.path, ARTIFACT_DIR, artifact["path"])
            target = os.path.join(step_dir, artifact["path"])
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copyfile(source, target)
            restored.append(artifact["path"])
        return restored
