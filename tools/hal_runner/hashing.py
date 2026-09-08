"""Content digests for run inputs, configurations and artifacts.

Everything the runner claims to be reproducible is pinned here.  Three kinds of
digest exist and they are deliberately *named* differently in the manifest, so
that nobody mistakes one for another:

``sha256``
    a plain file hash, the only digest that a third party can reproduce with
    ``sha256sum``.
``sha256-tree/1``
    a hash over a directory: for every regular file below the root, in sorted
    order, the POSIX-style relative path and the file's ``sha256`` are fed into
    one SHA-256.  A HAL project directory has no canonical serialization, so
    this is the closest thing to pinning one -- but it is *our* construction,
    which is why it never appears in a field called ``sha256``.
``sha256-json/1``
    a hash over the canonical JSON form of a value (sorted keys, no
    insignificant whitespace).  Used for configuration digests and cache keys.

Empty directories are invisible to ``sha256-tree/1`` and so are file modes: the
digest answers "does every file still have the same content under the same
name", which is what a cache key needs.
"""

import hashlib
import json
import os

from hal_findings.serialize import sha256_file

__all__ = [
    "ALGORITHM_FILE",
    "ALGORITHM_TREE",
    "ALGORITHM_JSON",
    "sha256_file",
    "sha256_bytes",
    "canonical_json",
    "json_digest",
    "tree_digest",
    "describe_input",
]

#: Digest algorithm identifiers as they appear in manifests and cache entries.
ALGORITHM_FILE = "sha256"
ALGORITHM_TREE = "sha256-tree/1"
ALGORITHM_JSON = "sha256-json/1"

#: Directory listings longer than this are summarized by the tree digest alone.
MAX_LISTED_FILES = 256


def sha256_bytes(data):
    """SHA-256 of a ``bytes`` object."""
    return hashlib.sha256(data).hexdigest()


def canonical_json(value):
    """Compact, sorted-key JSON -- the exact bytes every JSON digest is taken over."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def json_digest(value):
    """SHA-256 over :func:`canonical_json` of ``value``."""
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _relative_files(root):
    """Every regular file below ``root`` as ``(posix_relative_path, absolute_path)``.

    Sorted by the relative path, so the digest does not depend on the order the
    filesystem happens to enumerate directories in.
    """
    root = os.path.abspath(root)
    entries = []
    for directory, subdirectories, files in os.walk(root):
        for name in files:
            absolute = os.path.join(directory, name)
            if not os.path.isfile(absolute):
                # A broken symlink is not content; recording it as an empty file would
                # make two different trees hash the same.
                continue
            entries.append((os.path.relpath(absolute, root).replace(os.sep, "/"), absolute))
    return sorted(entries)


def tree_digest(root, max_listed_files=MAX_LISTED_FILES):
    """Digest a directory. Returns ``(hexdigest, entries, file_count, total_bytes)``.

    ``entries`` is the per-file listing (path + ``sha256`` + size), truncated to
    ``max_listed_files`` because a manifest should stay readable; the digest
    always covers *every* file regardless of the listing.
    """
    digest = hashlib.sha256()
    entries = []
    file_count = 0
    total_bytes = 0
    for relative, absolute in _relative_files(root):
        file_hash = sha256_file(absolute)
        size = os.path.getsize(absolute)
        # The separators are what stop ("ab", "c") and ("a", "bc") from colliding.
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
        file_count += 1
        total_bytes += size
        if len(entries) < max_listed_files:
            entries.append({"path": relative, "sha256": file_hash, "size_bytes": size})
    return digest.hexdigest(), entries, file_count, total_bytes


def describe_input(path, role="netlist", max_listed_files=MAX_LISTED_FILES):
    """Describe a run input as a manifest entry, pinned by content.

    Files (including the ``.zip`` project archives the examples ship as) get a
    real ``sha256``; directories get a ``sha256-tree/1`` digest plus, when it is
    short enough, the per-file listing that produced it.
    """
    absolute = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.exists(absolute):
        raise FileNotFoundError("input {} does not exist: {}".format(role, absolute))

    entry = {"role": role, "resolved_path": absolute}
    if os.path.isdir(absolute):
        value, entries, file_count, total_bytes = tree_digest(absolute, max_listed_files)
        entry.update(
            {
                "kind": "directory",
                "digest_algorithm": ALGORITHM_TREE,
                "digest": value,
                "file_count": file_count,
                "size_bytes": total_bytes,
            }
        )
        if file_count <= max_listed_files:
            entry["files"] = entries
        else:
            entry["files_note"] = (
                "{} files; the per-file listing is omitted, the digest covers all of "
                "them".format(file_count)
            )
    else:
        file_hash = sha256_file(absolute)
        entry.update(
            {
                "kind": "file",
                "digest_algorithm": ALGORITHM_FILE,
                "digest": file_hash,
                # Duplicated under its conventional name so that consumers looking for
                # a plain content hash find one, and only where there really is one.
                "sha256": file_hash,
                "size_bytes": os.path.getsize(absolute),
            }
        )
    return entry
