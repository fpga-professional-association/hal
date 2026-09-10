"""Sessions: a loaded netlist that outlives a single tool call.

This is the module that makes the server worth running.  ``hal_load_netlist``
imports ``hal_py``, loads every HAL plugin and parses the design *once*; the
resulting :class:`Session` is kept in a :class:`SessionStore` for the lifetime
of the process and every later tool call looks it up by id.

Nothing here imports ``hal_py`` at module scope.  The import happens on the
first call that actually needs HAL, so the protocol half of the server runs on
a bare checkout with no build.
"""

import os
import subprocess
import sys
import time

__all__ = [
    "ToolError",
    "Session",
    "SessionStore",
    "halenv",
    "hal_py_module",
]


class ToolError(Exception):
    """A tool-level failure: a bad session id, a netlist that will not load.

    These are *not* JSON-RPC errors.  ``tools/call`` reports them as a normal
    result with ``"isError": true`` and this message as the text, which is what
    lets a model read the failure and try something else.  Write the message
    accordingly: say what went wrong *and* what to do instead.
    """


# ---------------------------------------------------------------------------
# reaching HAL, carefully
# ---------------------------------------------------------------------------

_HALENV = None


def halenv():
    """Return ``hal_viz.halenv``, the shared HAL import/netlist-loading module.

    Reused rather than reimplemented: it already knows how to extend
    ``sys.path`` from ``$HAL_PY_PATH``, that HAL's parsers are plugins and must
    be loaded before any netlist can be read, and what to say when they are
    not.
    """
    global _HALENV
    if _HALENV is None:
        try:
            from hal_viz import halenv as module
        except ImportError as exc:
            raise ToolError(
                "hal_mcp needs HAL's tools/ directory on sys.path so that "
                "hal_viz.halenv is importable ({}). Launch the server as "
                "'python3 tools/hal_mcp' from the repository root, or set "
                "PYTHONPATH=<repo>/tools.".format(exc)
            )
        _HALENV = module
    return _HALENV


#: Imported in a throwaway process before this one imports ``hal_py``.
#:
#: HAL's ``utils.cpp`` aborts the *process* -- ``log_critical`` then exit -- when it cannot
#: determine its base path, which happens for any plain interpreter that does not export
#: ``$HAL_BASE_PATH``.  In a one-shot CLI that is merely a bad error message; in a long-lived
#: server it kills the session registry and every netlist in it, mid-conversation, with no
#: chance to answer the caller.  So the risky import is done somewhere expendable first and
#: only repeated here once it is known to survive.
_PROBE_SCRIPT = (
    "import os, sys\n"
    "for entry in os.environ.get('HAL_PY_PATH', '').split(os.pathsep):\n"
    "    if entry.strip():\n"
    "        sys.path.insert(0, entry.strip())\n"
    "import hal_py\n"
)

_PROBE_TIMEOUT = 120

_HAL_PY = None
_PROBE_RESULT = None


def _probe_hal_py():
    """Check in a subprocess that ``import hal_py`` terminates normally."""
    global _PROBE_RESULT
    if _PROBE_RESULT is not None:
        return _PROBE_RESULT
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _PROBE_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Could not even run the probe; fall through and let the real import
        # speak for itself rather than inventing a failure.
        _PROBE_RESULT = "probe could not run: {}".format(exc)
        return _PROBE_RESULT

    if completed.returncode == 0:
        _PROBE_RESULT = ""
        return _PROBE_RESULT

    detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
    hint = ""
    if any("HAL_BASE_PATH" in line for line in detail):
        hint = (
            " Set HAL_BASE_PATH to HAL's build or install directory (the one "
            "holding bin/ and lib/) in the environment the server is launched "
            "with; hal_py aborts the interpreter without it."
        )
    _PROBE_RESULT = (
        "importing hal_py fails in this environment (exit {}).{}\nLast lines of "
        "its output:\n  {}".format(
            completed.returncode, hint, "\n  ".join(detail[-6:]) or "<no output>"
        )
    )
    return _PROBE_RESULT


def hal_py_module():
    """Import ``hal_py``, load every HAL plugin, and return the module.

    Idempotent: the plugins are loaded once per process (``halenv`` tracks
    that), and the module is cached here.
    """
    global _HAL_PY
    if _HAL_PY is not None:
        return _HAL_PY

    problem = _probe_hal_py()
    if problem:
        raise ToolError(problem)

    env = halenv()
    try:
        module = env.import_hal_py()
        # Gate libraries and netlist parsers are plugins: with none loaded,
        # NetlistFactory silently returns None for every format.
        env.load_all_plugins(module)
    except env.HalUnavailable as exc:
        raise ToolError(str(exc))
    _HAL_PY = module
    return module


# ---------------------------------------------------------------------------
# the sessions themselves
# ---------------------------------------------------------------------------


class Session(object):
    """One loaded netlist plus how it got here."""

    def __init__(self, identifier, kind, source, gate_library, netlist, hal_py):
        self.id = identifier
        self.kind = kind  # "netlist" or "project"
        self.source = source
        self.gate_library = gate_library
        self.netlist = netlist
        self.hal_py = hal_py
        self.opened_at = time.time()
        self.calls = 0

    def describe(self):
        """A cheap summary; nothing here walks the netlist more than once."""
        netlist = self.netlist
        top = netlist.get_top_module()
        library = netlist.get_gate_library()
        return {
            "session_id": self.id,
            "kind": self.kind,
            "source": self.source,
            "gate_library": self.gate_library,
            "gate_library_name": library.get_name() if library is not None else None,
            "design_name": netlist.get_design_name(),
            "gates": len(netlist.get_gates()),
            "nets": len(netlist.get_nets()),
            "modules": len(netlist.get_modules()),
            "top_module": top.get_name() if top is not None else None,
            "opened_at": self.opened_at,
            "tool_calls": self.calls,
        }


class SessionStore(object):
    """Every open session, keyed by a short id.

    The server is single threaded -- one request is answered before the next is
    read -- so no locking is needed here.
    """

    def __init__(self):
        self._sessions = {}
        self._counter = 0

    # -- lifecycle ----------------------------------------------------------

    def _next_id(self):
        self._counter += 1
        return "s{}".format(self._counter)

    def _register(self, kind, source, gate_library, netlist, hal_py):
        session = Session(self._next_id(), kind, source, gate_library, netlist, hal_py)
        self._sessions[session.id] = session
        return session

    def open_netlist(self, path, gate_library=None):
        """Load a netlist file (or a project directory) into a new session."""
        env = halenv()
        hal_py = hal_py_module()
        try:
            netlist = env.load_netlist(hal_py, path, gate_library)
        except env.NetlistLoadError as exc:
            raise ToolError(str(exc))
        except env.HalUnavailable as exc:
            raise ToolError(str(exc))
        resolved = os.path.abspath(os.path.expanduser(str(path)))
        library = (
            os.path.abspath(os.path.expanduser(str(gate_library)))
            if gate_library
            else None
        )
        kind = "project" if os.path.isdir(resolved) else "netlist"
        return self._register(kind, resolved, library, netlist, hal_py)

    def open_project(self, path):
        """Load a HAL project directory into a new session."""
        env = halenv()
        hal_py = hal_py_module()
        try:
            netlist = env.load_hal_project(hal_py, path)
        except env.NetlistLoadError as exc:
            raise ToolError(str(exc))
        except env.HalUnavailable as exc:
            raise ToolError(str(exc))
        resolved = os.path.abspath(os.path.expanduser(str(path)))
        return self._register("project", resolved, None, netlist, hal_py)

    def get(self, session_id):
        """Return a session or raise a :class:`ToolError` that says what to do."""
        if not isinstance(session_id, str) or not session_id:
            raise ToolError(
                "session_id must be a non-empty string as returned by "
                "hal_load_netlist or hal_load_project."
            )
        session = self._sessions.get(session_id)
        if session is None:
            known = ", ".join(sorted(self._sessions)) or "none"
            raise ToolError(
                "no open session {!r}. Open sessions: {}. Call hal_load_netlist "
                "(or hal_load_project) first and use the session_id it returns; "
                "hal_list_sessions shows what is currently loaded.".format(
                    session_id, known
                )
            )
        session.calls += 1
        return session

    def close(self, session_id):
        session = self.get(session_id)
        del self._sessions[session.id]
        return session

    def close_all(self):
        closed = sorted(self._sessions)
        self._sessions.clear()
        return closed

    # -- listing ------------------------------------------------------------

    def all(self):
        return [self._sessions[key] for key in sorted(self._sessions, key=_sort_key)]

    def __len__(self):
        return len(self._sessions)


def _sort_key(identifier):
    """Sort ``s2`` before ``s10``."""
    try:
        return (0, int(identifier[1:]))
    except (ValueError, IndexError):
        return (1, identifier)
