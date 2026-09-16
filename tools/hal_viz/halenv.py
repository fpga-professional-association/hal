"""The only hal_viz module that touches ``hal_py``.

Everything here fails loudly and with an actionable message: hal_viz is meant
to run against a *built* HAL, and the most common failure by far is simply not
having HAL's library directory on ``sys.path``.
"""

import contextlib
import os
import sys

__all__ = [
    "HalUnavailable",
    "NetlistLoadError",
    "LIBRARY_PATH_ENV",
    "stdout_reserved_for_document",
    "import_hal_py",
    "import_plugin",
    "load_all_plugins",
    "ensure_plugins_loaded",
    "unload_all_plugins",
    "load_netlist",
    "load_hal_project",
    "find_module",
    "find_gate",
]

#: Colon/semicolon separated extra directories to add to ``sys.path``.
LIBRARY_PATH_ENV = "HAL_PY_PATH"

#: Netlist file extensions that carry their own gate library.
SELF_CONTAINED_SUFFIXES = (".hal",)

_HELP = (
    "hal_viz needs HAL's Python bindings. Point it at a built HAL with\n"
    "  --hal-lib <hal-install-or-build>/lib\n"
    "or export {env}=<hal-install-or-build>/lib, or run the tool from an\n"
    "interpreter that can already 'import hal_py'. HAL must be built from\n"
    "source for this fork; see the Build Instructions in the top-level README."
).format(env=LIBRARY_PATH_ENV)


class HalUnavailable(RuntimeError):
    """Raised when ``hal_py`` (or a HAL plugin) cannot be imported."""


class NetlistLoadError(RuntimeError):
    """Raised when a netlist or gate library cannot be loaded."""


@contextlib.contextmanager
def stdout_reserved_for_document():
    """Keep everything except the caller's document off the real stdout.

    HAL's logger is spdlog writing straight to file descriptor 1, so
    ``contextlib.redirect_stdout`` does not see it and a ``--json`` document
    ends up interleaved with ``[core] [info] ...`` lines.  The only thing that
    works is moving the descriptor: fd 1 is pointed at fd 2 for the duration of
    the block, so HAL's output -- and any stray ``print()`` -- lands on stderr,
    while the stream this yields still writes to the process' original stdout.

    ``tools/hal_capabilities`` has carried this guard inline in its ``main()``
    since it was the only ``--json`` tool that loaded a netlist; this is the
    same three lines, restoring the descriptor on the way out so it is safe
    inside a library and inside a test::

        with halenv.stdout_reserved_for_document() as document:
            netlist = halenv.load_netlist(hal_py, path)     # logs -> stderr
            json.dump(summarize(netlist), document)         # -> real stdout
    """
    sys.stdout.flush()
    sys.stderr.flush()
    saved = os.dup(1)
    document = os.fdopen(os.dup(saved), "w")
    try:
        os.dup2(2, 1)
        yield document
    finally:
        try:
            document.flush()
        finally:
            document.close()
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def _candidate_paths(extra_paths):
    seen = set()
    for entry in list(extra_paths or []):
        if entry and entry not in seen:
            seen.add(entry)
            yield entry
    raw = os.environ.get(LIBRARY_PATH_ENV, "")
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if entry and entry not in seen:
            seen.add(entry)
            yield entry


def import_hal_py(extra_paths=()):
    """Import and return the ``hal_py`` module.

    ``extra_paths`` and ``$HAL_PY_PATH`` are prepended to ``sys.path`` first.
    Raises :class:`HalUnavailable` with build/setup guidance on failure.
    """
    for entry in _candidate_paths(extra_paths):
        expanded = os.path.abspath(os.path.expanduser(entry))
        if not os.path.isdir(expanded):
            raise HalUnavailable(
                "HAL library directory {!r} does not exist.\n\n{}".format(entry, _HELP)
            )
        if expanded not in sys.path:
            sys.path.insert(0, expanded)

    try:
        import hal_py  # noqa: F401  (imported for its side effect + return)
    except ImportError as exc:
        raise HalUnavailable("could not import hal_py: {}\n\n{}".format(exc, _HELP))
    return hal_py


#: Ids of the ``hal_py`` modules whose plugins this process has already loaded. HAL's own
#: ``plugin_manager.load_all_plugins()`` is idempotent, but it is not free (it walks the plugin
#: directory and dlopens everything), so repeated calls from the loaders below are elided.
_PLUGINS_LOADED = set()


def load_all_plugins(hal_py):
    """Load all HAL plugins so ``hal_plugins.*`` modules become importable."""
    try:
        hal_py.plugin_manager.load_all_plugins()
    except Exception as exc:
        raise HalUnavailable("could not load HAL plugins: {}".format(exc))
    _PLUGINS_LOADED.add(id(hal_py))


def ensure_plugins_loaded(hal_py):
    """Load HAL's plugins unless this process already did.

    HAL's gate-library and netlist parsers are *plugins*: with no plugin loaded, nothing is
    registered for ``.hgl``/``.v``/``.vhd``/``.hal`` and every load silently returns ``None`` --
    the failure mode behind 'no gate library parser registered for file extension .hgl'. Calling
    this from :func:`load_netlist` and :func:`load_hal_project` is what makes that impossible for
    every tool that loads a netlist through this module: forgetting the call is no longer an
    option, because there is no path that skips it.

    Callers that want the plugins for their own sake (to ``import hal_plugins.*``) keep calling
    :func:`load_all_plugins`; this is the cheap 'make sure' used by the loaders.
    """
    if id(hal_py) in _PLUGINS_LOADED:
        return
    load_all_plugins(hal_py)


def unload_all_plugins(hal_py):
    """Best-effort plugin teardown; never raises."""
    try:
        hal_py.plugin_manager.unload_all_plugins()
    except Exception:
        pass
    _PLUGINS_LOADED.discard(id(hal_py))


def import_plugin(name):
    """Import ``hal_plugins.<name>``; requires :func:`load_all_plugins` first."""
    try:
        module = __import__("hal_plugins." + name, fromlist=[name])
    except ImportError as exc:
        raise HalUnavailable(
            "could not import the '{}' HAL plugin ({}). It may not have been built "
            "or enabled in this HAL installation.".format(name, exc)
        )
    return module


def load_hal_project(hal_py, path):
    """Load a HAL project directory; loads HAL's plugins first if that has not happened yet."""
    path = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isdir(path):
        raise NetlistLoadError("HAL project directory does not exist: {}".format(path))
    ensure_plugins_loaded(hal_py)
    netlist = hal_py.NetlistFactory.load_hal_project(path)
    if netlist is None:
        raise NetlistLoadError(
            "could not load HAL project from {}. Expected a directory produced by "
            "unzipping one of the examples/ archives or by "
            "'hal --import-netlist ... --project-dir ...'.".format(path)
        )
    return netlist


def load_netlist(hal_py, path, gate_library=None):
    """Load a netlist from a HAL project directory or a netlist file.

    * a directory        -> ``NetlistFactory.load_hal_project``
    * a ``.hal`` file    -> ``NetlistFactory.load_netlist`` (library embedded)
    * any other file     -> ``NetlistFactory.load_netlist`` with ``gate_library``

    HAL's parsers are plugins, so this calls :func:`ensure_plugins_loaded` first: a tool that
    loads a netlist through this function cannot forget to load the plugins.
    """
    path = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.exists(path):
        raise NetlistLoadError("netlist path does not exist: {}".format(path))

    if os.path.isdir(path):
        return load_hal_project(hal_py, path)

    ensure_plugins_loaded(hal_py)
    suffix = os.path.splitext(path)[1].lower()
    if gate_library:
        library = os.path.abspath(os.path.expanduser(str(gate_library)))
        if not os.path.isfile(library):
            raise NetlistLoadError("gate library file does not exist: {}".format(library))
        netlist = hal_py.NetlistFactory.load_netlist(path, library)
    elif suffix in SELF_CONTAINED_SUFFIXES:
        netlist = hal_py.NetlistFactory.load_netlist(path)
    else:
        raise NetlistLoadError(
            "loading {!r} requires a gate library; pass --gate-library <file.hgl|.lib>. "
            "Bundled libraries live in plugins/gate_libraries/definitions, and each "
            "examples/*.zip archive ships the one it needs.".format(os.path.basename(path))
        )

    if netlist is None:
        raise NetlistLoadError(
            "HAL failed to parse {}. Check that the gate library matches the netlist "
            "and that the file format is supported.".format(path)
        )
    return netlist


def _lookup(spec, by_id, all_items, kind):
    """Resolve ``spec`` (numeric id or name) against a netlist collection."""
    spec = str(spec)
    if spec.isdigit():
        item = by_id(int(spec))
        if item is None:
            raise NetlistLoadError("no {} with id {} in this netlist".format(kind, spec))
        return item

    items = list(all_items())
    exact = [item for item in items if item.get_name() == spec]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise NetlistLoadError(
            "{} name {!r} is ambiguous ({} matches: ids {}). Use a numeric id "
            "instead.".format(
                kind, spec, len(exact), ", ".join(str(i.get_id()) for i in exact[:10])
            )
        )

    partial = [item for item in items if spec in item.get_name()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise NetlistLoadError(
            "no {} named exactly {!r}; {} names contain it (e.g. {}). Use an exact "
            "name or a numeric id.".format(
                kind,
                spec,
                len(partial),
                ", ".join(repr(i.get_name()) for i in partial[:5]),
            )
        )
    raise NetlistLoadError("no {} named {!r} in this netlist".format(kind, spec))


def find_module(netlist, spec):
    """Find a module by numeric id, exact name, or unique substring."""
    return _lookup(spec, netlist.get_module_by_id, netlist.get_modules, "module")


def find_gate(netlist, spec):
    """Find a gate by numeric id, exact name, or unique substring."""
    return _lookup(spec, netlist.get_gate_by_id, netlist.get_gates, "gate")
