"""Find plugins and tell the three states apart: declared, built, loadable.

"Is the plugin there?" has three different answers and conflating them is the
usual reason a contributor loses an afternoon:

``declared``
    ``plugins/<name>/capabilities.json`` exists in the source tree.  Says
    nothing about any build.
``built``
    the shared object exists in ``<build>/lib/hal_plugins/``.  The plugin's
    CMake option was on for *this* build.
``loadable``
    ``plugin_manager`` produced an instance and ``hal_plugins.<name>`` imported.
    Needs a built HAL; a plugin can be built and still fail here when one of its
    dependencies is missing.

Only the last one needs ``hal_py``, and it is the only function in this module
that imports it -- through ``hal_viz.halenv``, which already knows how to find a
build tree and what to say when it cannot.
"""

import json
import os

from .schema import CAPABILITIES_VERSION
from .validate import collect_errors

__all__ = [
    "PLUGIN_LIBRARY_SUFFIXES",
    "CAPABILITY_FILE_NAME",
    "BUILD_CAPABILITY_SUBDIR",
    "PluginRecord",
    "scan_source_tree",
    "read_capabilities",
    "installed_capabilities",
    "build_library_path",
    "probe_loadable",
    "dependency_problems",
]

#: Extensions ``plugin_manager`` accepts, per platform (see plugin_manager.cpp).
PLUGIN_LIBRARY_SUFFIXES = (".so", ".dylib", ".dll")

CAPABILITY_FILE_NAME = "capabilities.json"

#: Where a plugin's CMakeLists copies its declaration inside the build tree.
BUILD_CAPABILITY_SUBDIR = os.path.join("share", "hal", "plugin_capabilities")


class PluginRecord(object):
    """Everything discovery knows about one plugin."""

    def __init__(self, name, source_dir=None):
        self.name = name
        self.source_dir = source_dir
        #: Parsed ``capabilities.json``, or ``None`` when the plugin declares none.
        self.capabilities = None
        #: Validation messages for :attr:`capabilities` (empty when valid).
        self.capability_errors = []
        #: Path of the shared object in the build tree, or ``None``.
        self.library_path = None
        #: ``True``/``False`` once probed, ``None`` when not probed.
        self.loadable = None
        #: Why loading failed, when it did.
        self.load_error = None
        #: Mismatch between the sidecar and the JSON compiled into the plugin.
        self.drift = None
        #: :class:`hal_capabilities.support.SupportReport` once a netlist was given.
        self.support = None

    @property
    def declared(self):
        return self.capabilities is not None

    @property
    def built(self):
        return self.library_path is not None

    @property
    def kind(self):
        if not self.declared:
            return None
        return (self.capabilities.get("plugin") or {}).get("kind")

    @property
    def declared_dependencies(self):
        if not self.declared:
            return []
        return list((self.capabilities.get("dependencies") or {}).get("plugins") or [])

    def state_row(self):
        """The three states as short strings, for the ``list`` command."""
        return {
            "declared": "yes" if self.declared else "no",
            "built": "yes" if self.built else "no",
            "loadable": {None: "not probed", True: "yes", False: "no"}[self.loadable],
        }

    def to_json(self):
        payload = {
            "name": self.name,
            "source_dir": self.source_dir,
            "declared": self.declared,
            "built": self.built,
            "library_path": self.library_path,
            "loadable": self.loadable,
        }
        if self.capability_errors:
            payload["capability_errors"] = list(self.capability_errors)
        if self.load_error:
            payload["load_error"] = self.load_error
        if self.drift:
            payload["capability_drift"] = self.drift
        if self.declared:
            payload["capabilities"] = self.capabilities
        if self.support is not None:
            payload["support"] = self.support.to_json()
        return payload


def read_capabilities(path):
    """Read and validate one declaration; returns ``(document, errors)``.

    A file that is not JSON at all yields ``(None, [message])`` rather than an
    exception, because one broken plugin must not take the listing down.
    """
    try:
        with open(path, "r") as handle:
            document = json.load(handle)
    except ValueError as exc:
        return None, ["{}: not valid JSON ({})".format(path, exc)]
    except OSError as exc:
        return None, ["{}: cannot be read ({})".format(path, exc)]
    return document, ["{}: {}".format(os.path.basename(path), error)
                      for error in collect_errors(document)]


def build_library_path(build_dir, name):
    """Path of ``name``'s shared object under ``build_dir``, or ``None``."""
    if not build_dir:
        return None
    directory = os.path.join(build_dir, "lib", "hal_plugins")
    for suffix in PLUGIN_LIBRARY_SUFFIXES:
        candidate = os.path.join(directory, name + suffix)
        if os.path.isfile(candidate):
            return candidate
    return None


def scan_source_tree(repo_root, build_dir=None, names=None):
    """Every ``plugins/<name>`` directory, as :class:`PluginRecord` objects.

    Plugins without a ``capabilities.json`` are still listed -- most of the
    plugins in this tree predate the declaration format, and hiding them would
    make the listing lie about what is built.
    """
    plugins_dir = os.path.join(repo_root, "plugins")
    records = []
    if not os.path.isdir(plugins_dir):
        return records

    for entry in sorted(os.listdir(plugins_dir)):
        directory = os.path.join(plugins_dir, entry)
        if not os.path.isdir(directory):
            continue
        if not os.path.isfile(os.path.join(directory, "CMakeLists.txt")):
            continue
        if names and entry not in names:
            continue

        record = PluginRecord(entry, source_dir=directory)
        declaration = os.path.join(directory, CAPABILITY_FILE_NAME)
        if os.path.isfile(declaration):
            document, errors = read_capabilities(declaration)
            record.capabilities = document
            record.capability_errors = errors
        record.library_path = build_library_path(build_dir, entry)
        records.append(record)

    return records


def installed_capabilities(build_dir, name):
    """The declaration a build tree carries for ``name``, or ``None``.

    Lets the listing work against an *installed* HAL whose source tree is not
    around, since every plugin's CMakeLists copies its declaration into
    ``<build>/share/hal/plugin_capabilities/``.
    """
    if not build_dir:
        return None
    path = os.path.join(build_dir, BUILD_CAPABILITY_SUBDIR, name + ".json")
    if not os.path.isfile(path):
        return None
    document, _errors = read_capabilities(path)
    return document


def probe_loadable(records, hal_lib_paths=()):
    """Load HAL and ask ``plugin_manager`` which of ``records`` really load.

    Sets :attr:`PluginRecord.loadable`, :attr:`PluginRecord.load_error` and
    :attr:`PluginRecord.drift` in place.  Raises ``hal_viz.halenv.HalUnavailable``
    when ``hal_py`` cannot be imported at all -- a missing build is a setup
    problem, not a per-plugin result.
    """
    from hal_viz import halenv

    hal_py = halenv.import_hal_py(hal_lib_paths)
    halenv.load_all_plugins(hal_py)
    try:
        loaded = set(hal_py.plugin_manager.get_plugin_names())
        for record in records:
            if record.name not in loaded:
                record.loadable = False
                record.load_error = (
                    "plugin_manager did not load {!r}. It was {} in this build; if it "
                    "was built, one of its dependencies ({}) failed to load "
                    "first.".format(
                        record.name,
                        "built" if record.built else "not built",
                        ", ".join(record.declared_dependencies) or "none declared",
                    )
                )
                continue

            instance = None
            try:
                instance = hal_py.plugin_manager.get_plugin_instance(record.name, True, True)
            except Exception as exc:  # pragma: no cover - depends on the plugin
                record.load_error = "get_plugin_instance({!r}) raised: {}".format(record.name, exc)
            record.loadable = instance is not None
            if instance is None and not record.load_error:
                record.load_error = (
                    "plugin_manager.get_plugin_instance({!r}) returned None".format(record.name)
                )
            if instance is not None:
                _check_drift(record, instance, hal_py)
    finally:
        halenv.unload_all_plugins(hal_py)
    return records


def _check_drift(record, instance, hal_py):
    """Compare the sidecar with what the plugin reports about itself."""
    problems = []

    declared_deps = set(record.declared_dependencies)
    try:
        actual_deps = set(instance.get_dependencies())
    except Exception:
        actual_deps = None
    if actual_deps is not None and record.declared and declared_deps != actual_deps:
        problems.append(
            "capabilities.json declares dependencies {} but get_dependencies() "
            "returns {}".format(sorted(declared_deps) or "[]", sorted(actual_deps) or "[]")
        )

    compiled = getattr(instance, "get_capabilities", None)
    if callable(compiled) and record.declared:
        try:
            embedded = json.loads(compiled())
        except Exception as exc:
            problems.append("get_capabilities() did not return valid JSON: {}".format(exc))
        else:
            if embedded != record.capabilities:
                problems.append(
                    "the capabilities.json compiled into the plugin differs from the "
                    "one in the source tree; rebuild, or the two have diverged"
                )

    if record.declared:
        declared_version = (record.capabilities.get("plugin") or {}).get("version")
        try:
            actual_version = instance.get_version()
        except Exception:
            actual_version = None
        if actual_version is not None and declared_version != actual_version:
            problems.append(
                "capabilities.json says version {!r}, the plugin reports {!r}".format(
                    declared_version, actual_version
                )
            )

    record.drift = problems or None


def dependency_problems(records):
    """Declared plugin dependencies that are absent, not built, or not loadable.

    Returns ``{plugin_name: [message, ...]}``.  This is the "missing dependency"
    case from the acceptance criteria: it is answered from the declaration, so
    it is answerable *before* a build, and refined by ``built``/``loadable``
    once one exists.
    """
    by_name = {record.name: record for record in records}
    problems = {}
    for record in records:
        messages = []
        for dependency in record.declared_dependencies:
            other = by_name.get(dependency)
            if other is None:
                messages.append(
                    "depends on plugin {!r}, which does not exist in plugins/. Fix the "
                    "name in {}/capabilities.json or add the plugin.".format(
                        dependency, record.name
                    )
                )
                continue
            if record.built and not other.built:
                messages.append(
                    "depends on plugin {!r}, which is not built. Enable it (its CMake "
                    "option, or -DBUILD_ALL_PLUGINS=ON) and rebuild.".format(dependency)
                )
            elif other.loadable is False:
                messages.append(
                    "depends on plugin {!r}, which failed to load: {}".format(
                        dependency, other.load_error or "unknown reason"
                    )
                )
        if messages:
            problems[record.name] = messages
    return problems


def capabilities_version():
    """The declaration format version this build writes."""
    return CAPABILITIES_VERSION
