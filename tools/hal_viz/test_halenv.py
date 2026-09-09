"""Standalone unit tests for ``hal_viz.halenv``'s plugin handling.

HAL's gate-library and netlist parsers are *plugins*. A tool that calls
``hal_py.NetlistFactory.load_netlist`` without ``plugin_manager.load_all_plugins()`` gets ``None``
back and a 'no gate library parser registered for file extension .hgl' in the log -- the bug behind
the fixes in hal_migration and hal_agilex. The fix that scales is not another call site: it is that
*the loader itself* loads the plugins, so every tool in the family (hal_viz, hal_explain, hal_cdc,
hal_capabilities, hal_migration, hal_semantic_diff, ...) inherits it.

These tests pin that down with a stub ``hal_py``: no build, no netlist, no plugins.

    python -m unittest discover -s tools/hal_viz -t tools -p "test_*.py"
"""

import os
import sys
import tempfile
import unittest

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hal_viz import halenv  # noqa: E402


class StubPluginManager(object):
    def __init__(self, fail=False):
        self.load_calls = 0
        self.unload_calls = 0
        self.fail = fail

    def load_all_plugins(self):
        self.load_calls += 1
        if self.fail:
            raise RuntimeError("plugin directory not found")

    def unload_all_plugins(self):
        self.unload_calls += 1


class StubNetlistFactory(object):
    """Mimics the binding, including its failure mode: no plugins -> ``None``."""

    def __init__(self, plugin_manager, netlist="netlist"):
        self._plugin_manager = plugin_manager
        self._netlist = netlist
        self.calls = []

    def load_netlist(self, *args):
        self.calls.append(("load_netlist",) + tuple(args))
        if self._plugin_manager.load_calls == 0:
            return None
        return self._netlist

    def load_hal_project(self, *args):
        self.calls.append(("load_hal_project",) + tuple(args))
        if self._plugin_manager.load_calls == 0:
            return None
        return self._netlist


class StubHalPy(object):
    def __init__(self, fail_plugins=False):
        self.plugin_manager = StubPluginManager(fail=fail_plugins)
        self.NetlistFactory = StubNetlistFactory(self.plugin_manager)


class PluginLoadingTest(unittest.TestCase):
    def setUp(self):
        halenv._PLUGINS_LOADED.clear()
        self.addCleanup(halenv._PLUGINS_LOADED.clear)
        self.tmp = tempfile.mkdtemp(prefix="hal_halenv_")
        self.netlist_path = os.path.join(self.tmp, "design.v")
        with open(self.netlist_path, "w") as handle:
            handle.write("// netlist\n")
        self.library_path = os.path.join(self.tmp, "library.hgl")
        with open(self.library_path, "w") as handle:
            handle.write("{}\n")
        self.project_dir = os.path.join(self.tmp, "project")
        os.makedirs(self.project_dir)

    def test_load_netlist_loads_plugins_first(self):
        hal_py = StubHalPy()
        netlist = halenv.load_netlist(hal_py, self.netlist_path, self.library_path)
        self.assertEqual("netlist", netlist)
        self.assertEqual(1, hal_py.plugin_manager.load_calls)

    def test_load_hal_file_loads_plugins_first(self):
        hal_py = StubHalPy()
        hal_file = os.path.join(self.tmp, "design.hal")
        with open(hal_file, "w") as handle:
            handle.write("{}\n")
        self.assertEqual("netlist", halenv.load_netlist(hal_py, hal_file))
        self.assertEqual(1, hal_py.plugin_manager.load_calls)

    def test_load_hal_project_loads_plugins_first(self):
        hal_py = StubHalPy()
        self.assertEqual("netlist", halenv.load_hal_project(hal_py, self.project_dir))
        self.assertEqual(1, hal_py.plugin_manager.load_calls)

    def test_load_netlist_on_a_directory_goes_through_load_hal_project(self):
        hal_py = StubHalPy()
        self.assertEqual("netlist", halenv.load_netlist(hal_py, self.project_dir))
        self.assertEqual(1, hal_py.plugin_manager.load_calls)
        self.assertEqual("load_hal_project", hal_py.NetlistFactory.calls[0][0])

    def test_plugins_are_loaded_once_per_process(self):
        hal_py = StubHalPy()
        halenv.load_netlist(hal_py, self.netlist_path, self.library_path)
        halenv.load_netlist(hal_py, self.netlist_path, self.library_path)
        halenv.load_hal_project(hal_py, self.project_dir)
        self.assertEqual(1, hal_py.plugin_manager.load_calls)

    def test_explicit_load_all_plugins_satisfies_the_guard(self):
        hal_py = StubHalPy()
        halenv.load_all_plugins(hal_py)
        halenv.load_netlist(hal_py, self.netlist_path, self.library_path)
        self.assertEqual(1, hal_py.plugin_manager.load_calls)

    def test_unloading_makes_the_next_load_reload_the_plugins(self):
        hal_py = StubHalPy()
        halenv.load_netlist(hal_py, self.netlist_path, self.library_path)
        halenv.unload_all_plugins(hal_py)
        halenv.load_netlist(hal_py, self.netlist_path, self.library_path)
        self.assertEqual(2, hal_py.plugin_manager.load_calls)
        self.assertEqual(1, hal_py.plugin_manager.unload_calls)

    def test_plugin_failure_is_reported_as_hal_unavailable(self):
        hal_py = StubHalPy(fail_plugins=True)
        with self.assertRaises(halenv.HalUnavailable) as caught:
            halenv.load_netlist(hal_py, self.netlist_path, self.library_path)
        self.assertIn("could not load HAL plugins", str(caught.exception))
        self.assertEqual([], hal_py.NetlistFactory.calls)

    def test_missing_paths_still_raise_before_anything_is_loaded(self):
        hal_py = StubHalPy()
        with self.assertRaises(halenv.NetlistLoadError):
            halenv.load_netlist(hal_py, os.path.join(self.tmp, "nope.v"), self.library_path)
        with self.assertRaises(halenv.NetlistLoadError):
            halenv.load_hal_project(hal_py, os.path.join(self.tmp, "nope"))
        self.assertEqual([], hal_py.NetlistFactory.calls)

    def test_missing_gate_library_is_reported(self):
        hal_py = StubHalPy()
        with self.assertRaises(halenv.NetlistLoadError) as caught:
            halenv.load_netlist(hal_py, self.netlist_path, os.path.join(self.tmp, "nope.hgl"))
        self.assertIn("gate library file does not exist", str(caught.exception))


class ToolLoadPathAuditTest(unittest.TestCase):
    """Every in-process netlist load in tools/ has to load the plugins.

    The audit behind this test (issue #45) went through every ``tools/*/cli.py``; the two ways to
    be correct are to go through ``hal_viz.halenv`` (which now loads them itself) or to load them
    in the module that calls ``hal_py.NetlistFactory``. This check is what keeps a new tool from
    quietly reintroducing the bug.
    """

    TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    #: Modules that call ``hal_py.NetlistFactory`` directly and therefore have to load the plugins
    #: themselves. Everything else has to go through halenv.
    DIRECT_FACTORY_MODULES = {
        os.path.join("hal_viz", "halenv.py"),
        os.path.join("hal_agilex", "hal_adapter.py"),
        os.path.join("hal_apb_check", "netlist.py"),
        os.path.join("hal_apb_recover", "hal_source.py"),
    }

    def _sources(self):
        for root, dirs, files in os.walk(self.TOOLS):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "fixtures")]
            for name in sorted(files):
                if not name.endswith(".py") or name.startswith("test_"):
                    continue
                path = os.path.join(root, name)
                with open(path, "r", encoding="utf-8") as handle:
                    yield os.path.relpath(path, self.TOOLS), handle.read()

    def test_every_direct_factory_user_loads_plugins(self):
        offenders = []
        for relative, text in self._sources():
            if "NetlistFactory" not in text:
                continue
            if "load_all_plugins" in text:
                self.assertIn(
                    relative,
                    self.DIRECT_FACTORY_MODULES,
                    "{} calls hal_py.NetlistFactory directly; either route it through "
                    "hal_viz.halenv or add it to DIRECT_FACTORY_MODULES".format(relative),
                )
                continue
            offenders.append(relative)
        self.assertEqual(
            [],
            offenders,
            "these modules call hal_py.NetlistFactory without loading HAL's plugins, so every "
            "load returns None: {}".format(offenders),
        )

    def test_the_audited_modules_still_exist(self):
        for relative in sorted(self.DIRECT_FACTORY_MODULES):
            self.assertTrue(
                os.path.isfile(os.path.join(self.TOOLS, relative)),
                "{} was moved or deleted; re-run the audit".format(relative),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
