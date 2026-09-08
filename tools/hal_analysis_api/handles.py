"""Project handles: explicit, content-pinned, and read-only.

Why a handle at all, rather than passing a path to every call?

* A path is ambiguous over time.  ``examples/uart.zip`` today and after an
  edit are different designs, and an agent that quoted gate id 42 from the
  first has said nothing about the second.  A handle is derived from the path
  *and its content digest*, so a changed file gets a different handle and
  ``project.describe`` can say "this no longer hashes to what it did".
* Gate, net and module ids are only meaningful inside one netlist -- the same
  reason ``hal_findings`` makes ``artifact_id`` mandatory on every reference.
  A handle is the API's version of that binding.
* An invented or stale handle must be an error, not an accidental read of some
  other project.  Anything not in the store is ``unknown_project``.

The store is a directory of small JSON files under the workspace, not process
memory, because an analysis job runs in a *different* process and has to be
able to resolve the same handle -- and because a CLI call is its own process.

Nothing here writes to the project.  The one thing it does write is the
workspace: a ``.zip`` project archive is unpacked into
``<workspace>/projects/<handle>/unpacked`` so the in-HAL side has a directory
to load, exactly as ``hal_runner`` unpacks into a run's output directory.
"""

import hashlib
import json
import os
import shutil
import zipfile
from pathlib import PurePosixPath

from hal_findings.adapters.common import utc_now
from hal_runner.hashing import describe_input

from .errors import InvalidRequest, Internal, UnknownProject

__all__ = [
    "PROJECT_KINDS",
    "default_workspace",
    "ProjectHandle",
    "ProjectStore",
]

#: How the netlist behind a handle is loaded.
PROJECT_KINDS = ("hal_project_dir", "hal_project_archive", "hal_netlist", "hdl_netlist")

#: Extensions HAL can parse without a separate gate library.
_SELF_CONTAINED = (".hal",)

_ENV_WORKSPACE = "HAL_ANALYSIS_API_WORKSPACE"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_workspace():
    """Where handles, jobs and query scratch files live.

    ``$HAL_ANALYSIS_API_WORKSPACE`` wins; otherwise ``build/hal_analysis_api``
    inside the checkout, which is already ignored and is where ``hal_runner``
    puts its output too.
    """
    override = os.environ.get(_ENV_WORKSPACE)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(_REPO_ROOT, "build", "hal_analysis_api")


def _classify(path):
    """Return ``(kind, needs_gate_library)`` for an existing path."""
    if os.path.isdir(path):
        return "hal_project_dir", False
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".zip":
        return "hal_project_archive", False
    if suffix in _SELF_CONTAINED:
        return "hal_netlist", False
    return "hdl_netlist", True


def _handle_id(resolved_path, digest):
    """Content-derived handle: same path, same content -> same handle.

    Deterministic on purpose.  An agent that re-opens the project it opened ten
    minutes ago gets the same id back and its earlier notes still refer to
    something, instead of accumulating handles that all mean the same thing.
    """
    raw = "{}\x00{}".format(os.path.normcase(resolved_path), digest).encode("utf-8")
    return "prj-" + hashlib.sha256(raw).hexdigest()[:12]


def _safe_extract(archive_path, destination):
    """Unpack a project archive, refusing any member that escapes ``destination``.

    The same check ``hal_runner.runner.Runner._materialize_netlist`` makes; it
    lives there as a private method tied to a run's output directory, so the
    rule is restated here rather than reached into.
    """
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        for name in names:
            parts = PurePosixPath(name).parts
            if os.path.isabs(name) or ".." in parts or (parts and parts[0].endswith(":")):
                raise InvalidRequest(
                    "refusing to unpack {!r} from {}: the archive escapes its "
                    "destination".format(name, archive_path)
                )
        if os.path.isdir(destination):
            shutil.rmtree(destination)
        os.makedirs(destination, exist_ok=True)
        archive.extractall(destination)

    roots = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
    root_files = {
        name for name in names if len(PurePosixPath(name).parts) == 1 and not name.endswith("/")
    }
    if len(roots) == 1 and not root_files:
        return os.path.join(destination, sorted(roots)[0])
    return destination


class ProjectHandle(object):
    """One open project. Immutable except for the label."""

    def __init__(self, document):
        self.document = dict(document)

    @property
    def id(self):
        return self.document["project"]

    @property
    def netlist_path(self):
        """What the in-HAL side loads (an unpacked directory for an archive)."""
        return self.document["netlist_path"]

    @property
    def gate_library(self):
        return self.document.get("gate_library")

    @property
    def source_path(self):
        """The path the caller named, resolved -- what an analysis run pins."""
        return self.document["resolved_path"]

    def as_json(self):
        return dict(self.document)


class ProjectStore(object):
    """The handle registry: open, look up, list, close."""

    def __init__(self, workspace=None):
        self.workspace = os.path.abspath(workspace or default_workspace())
        self.directory = os.path.join(self.workspace, "projects")

    # -- paths ---------------------------------------------------------------

    def _handle_file(self, project_id):
        return os.path.join(self.directory, "{}.json".format(project_id))

    def _data_dir(self, project_id):
        return os.path.join(self.directory, project_id)

    # -- opening -------------------------------------------------------------

    def open(self, path, gate_library=None, label=None, opened_at=None):
        """Open ``path`` read-only and return ``(handle, reused)``."""
        if not str(path).strip():
            raise InvalidRequest("path must not be empty")
        resolved = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.exists(resolved):
            raise InvalidRequest(
                "no such netlist or project: {}".format(resolved),
                hint="paths are resolved from the process working directory; pass an "
                "absolute path, or one of the shipped examples such as examples/uart.zip",
            )

        kind, needs_library = _classify(resolved)
        library = None
        if gate_library:
            library = os.path.abspath(os.path.expanduser(str(gate_library)))
            if not os.path.isfile(library):
                raise InvalidRequest("gate library file does not exist: {}".format(library))
        elif needs_library:
            raise InvalidRequest(
                "loading {!r} requires a gate library".format(os.path.basename(resolved)),
                hint="pass gate_library=<file.hgl|.lib>; the bundled libraries are in "
                "plugins/gate_libraries/definitions, and each examples/*.zip ships the "
                "one it needs",
            )

        try:
            described = describe_input(resolved, role="netlist")
        except OSError as exc:
            raise Internal("could not hash {}: {}".format(resolved, exc))

        project_id = _handle_id(resolved, described["digest"])
        existing = self._read(project_id)

        netlist_path = resolved
        if kind == "hal_project_archive":
            unpacked = os.path.join(self._data_dir(project_id), "unpacked")
            marker = os.path.join(self._data_dir(project_id), "unpacked.json")
            if existing and os.path.isfile(marker):
                with open(marker, "r", encoding="utf-8") as handle:
                    netlist_path = json.load(handle)["netlist_path"]
            else:
                netlist_path = _safe_extract(resolved, unpacked)
                os.makedirs(self._data_dir(project_id), exist_ok=True)
                with open(marker, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump({"netlist_path": netlist_path}, handle, indent=2, sort_keys=True)

        document = {
            "project": project_id,
            "path": str(path),
            "resolved_path": resolved,
            "netlist_path": netlist_path,
            "kind": kind,
            "gate_library": library,
            "label": label,
            "digest": {
                "algorithm": described["digest_algorithm"],
                "value": described["digest"],
                "file_count": described.get("file_count"),
                "size_bytes": described.get("size_bytes"),
            },
            "opened_at": opened_at or utc_now(),
            "read_only": True,
        }
        reused = False
        if existing is not None:
            # Same path, same content: keep the original opened_at so the handle
            # keeps meaning "this content, first seen then".
            reused = True
            document["opened_at"] = existing.document["opened_at"]
            if label is None:
                document["label"] = existing.document.get("label")
        self._write(document)
        return ProjectHandle(document), reused

    # -- reading -------------------------------------------------------------

    def _read(self, project_id):
        path = self._handle_file(project_id)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return ProjectHandle(json.load(handle))
        except (OSError, ValueError) as exc:
            raise Internal("the handle record {} is unreadable: {}".format(path, exc))

    def get(self, project_id):
        """Resolve a handle or raise ``unknown_project``."""
        handle = self._read(str(project_id))
        if handle is None:
            known = [entry.id for entry in self.list()]
            raise UnknownProject(
                "no open project {!r}".format(project_id),
                detail="open handles: {}".format(", ".join(known) if known else "(none)"),
                hint="call project.open with the path of a HAL project, .hal netlist or "
                "project archive first",
            )
        if not os.path.exists(handle.netlist_path):
            raise UnknownProject(
                "the project behind handle {!r} is gone: {}".format(
                    project_id, handle.netlist_path
                ),
                hint="re-open it with project.open; the handle is pinned to content that "
                "no longer exists",
            )
        return handle

    def list(self):
        """Every open handle, oldest first."""
        if not os.path.isdir(self.directory):
            return []
        handles = []
        for name in sorted(os.listdir(self.directory)):
            if not name.endswith(".json") or name.endswith("unpacked.json"):
                continue
            handle = self._read(name[: -len(".json")])
            if handle is not None:
                handles.append(handle)
        return sorted(handles, key=lambda entry: (entry.document.get("opened_at") or "", entry.id))

    def still_matches(self, handle):
        """Re-hash the source and report whether it is still the pinned content."""
        try:
            described = describe_input(handle.source_path, role="netlist")
        except (OSError, FileNotFoundError):
            return False
        return described["digest"] == handle.document["digest"]["value"]

    # -- closing -------------------------------------------------------------

    def close(self, project_id):
        """Forget a handle (and anything unpacked for it). Never touches the project."""
        handle = self.get(project_id)
        os.remove(self._handle_file(handle.id))
        data_dir = self._data_dir(handle.id)
        if os.path.isdir(data_dir):
            shutil.rmtree(data_dir, ignore_errors=True)
        return handle

    # -- writing -------------------------------------------------------------

    def _write(self, document):
        os.makedirs(self.directory, exist_ok=True)
        path = self._handle_file(document["project"])
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
        return path
