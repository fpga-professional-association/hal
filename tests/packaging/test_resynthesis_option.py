#!/usr/bin/env python3
"""Tests for the PL_RESYNTHESIS tri-state (issue #38).

``netlist_preprocessing`` links against the ``resynthesis`` plugin and used to
include ``resynthesis/resynthesis.h`` unconditionally, while
``plugins/resynthesis/CMakeLists.txt`` built itself whenever
``PL_NETLIST_PREPROCESSING`` was on. ``-DPL_RESYNTHESIS=OFF`` was therefore
ignored, and a source tree without the plugin did not compile at all.

``cmake/hal_resynthesis_option.cmake`` resolves the option into
``HAL_BUILD_RESYNTHESIS``, and these tests configure that module on its own -- a
few hundred milliseconds per case instead of a HAL build per combination.

    python3 -m unittest discover -s tests/packaging
"""

import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = os.path.join(REPO_ROOT, "cmake", "hal_resynthesis_option.cmake")

PROJECT = """
cmake_minimum_required(VERSION 3.10)
project(resynthesis_option_test NONE)
include("{module}")
message(STATUS "RESULT=${{HAL_BUILD_RESYNTHESIS}}")
"""


@unittest.skipIf(shutil.which("cmake") is None, "cmake not on PATH")
class ResynthesisOptionTest(unittest.TestCase):
    """Every case is a fresh source and build directory, i.e. a fresh cache."""

    def _configure(self, plugin_present=True, cache=None, defines=None):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "src")
            build = os.path.join(tmp, "build")
            os.makedirs(source)
            os.makedirs(build)
            with open(os.path.join(source, "CMakeLists.txt"), "w", encoding="utf-8") as handle:
                handle.write(PROJECT.format(module=MODULE.replace("\\", "/")))
            if plugin_present:
                # only its existence is looked at, the content is never read
                os.makedirs(os.path.join(source, "resynthesis"))
                open(os.path.join(source, "resynthesis", "CMakeLists.txt"), "w").close()
            if cache is not None:
                # a pre-existing cache entry, e.g. one written by an older HAL
                with open(os.path.join(build, "CMakeCache.txt"), "w", encoding="utf-8") as handle:
                    handle.write("CMAKE_PLATFORM_INFO_DIR:INTERNAL=%s\n" % build)
                    handle.write(cache + "\n")

            command = [shutil.which("cmake")]
            command += ["-D%s" % define for define in (defines or [])]
            command += ["-S", source, "-B", build]
            completed = subprocess.run(command, capture_output=True, text=True)
            return completed

    def _result(self, completed):
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        for line in completed.stdout.splitlines():
            if "RESULT=" in line:
                return line.split("RESULT=", 1)[1].strip()
        self.fail("the module did not report HAL_BUILD_RESYNTHESIS:\n" + completed.stdout)

    def test_default_builds_resynthesis_for_netlist_preprocessing(self):
        # the default of PL_NETLIST_PREPROCESSING is ON, and that plugin needs resynthesis
        result = self._configure(defines=["PL_NETLIST_PREPROCESSING=ON"])
        self.assertTrue(self._result(result).upper() in ("ON", "TRUE", "1"))

    def test_default_without_any_dependent_plugin_does_not_build_it(self):
        result = self._configure(defines=["PL_NETLIST_PREPROCESSING=OFF"])
        self.assertTrue(self._result(result).upper() in ("OFF", "FALSE", "0", ""))

    def test_build_all_plugins_builds_it(self):
        result = self._configure(defines=["PL_NETLIST_PREPROCESSING=OFF", "BUILD_ALL_PLUGINS=ON"])
        self.assertTrue(self._result(result).upper() in ("ON", "TRUE", "1"))

    def test_off_is_honoured_even_though_a_plugin_wants_it(self):
        # this is the bug of issue #38: OFF used to be overridden by PL_NETLIST_PREPROCESSING
        result = self._configure(defines=["PL_NETLIST_PREPROCESSING=ON", "PL_RESYNTHESIS=OFF"])
        self.assertTrue(self._result(result).upper() in ("OFF", "FALSE", "0", ""))
        self.assertIn("reduced functionality", result.stdout)

    def test_on_builds_it_without_any_dependent_plugin(self):
        result = self._configure(defines=["PL_NETLIST_PREPROCESSING=OFF", "PL_RESYNTHESIS=ON"])
        self.assertTrue(self._result(result).upper() in ("ON", "TRUE", "1"))

    def test_auto_falls_back_when_the_plugin_is_not_in_the_source_tree(self):
        # release tarballs and sparse checkouts: AUTO must not ask for a target that is never defined
        result = self._configure(plugin_present=False, defines=["PL_NETLIST_PREPROCESSING=ON"])
        self.assertTrue(self._result(result).upper() in ("OFF", "FALSE", "0", ""))
        self.assertIn("not part of this source tree", result.stdout)

    def test_on_fails_loudly_when_the_plugin_is_not_in_the_source_tree(self):
        result = self._configure(plugin_present=False, defines=["PL_RESYNTHESIS=ON"])
        self.assertNotEqual(result.returncode, 0, "an impossible request has to fail at configure time")
        self.assertIn("not part of this source tree", result.stdout + result.stderr)

    def test_old_boolean_cache_entry_is_migrated_to_auto(self):
        # PL_RESYNTHESIS used to be option(... OFF), and that OFF still meant "build it when
        # netlist_preprocessing is built". Reading such an entry as the new OFF would silently drop the
        # plugin from an existing build directory.
        result = self._configure(cache="PL_RESYNTHESIS:BOOL=OFF", defines=["PL_NETLIST_PREPROCESSING=ON"])
        self.assertTrue(self._result(result).upper() in ("ON", "TRUE", "1"))
        self.assertIn("migrating cache entry", result.stdout)

    def test_new_off_cache_entry_is_kept(self):
        # ... while a deliberate OFF, which is a STRING entry, survives a reconfigure
        result = self._configure(cache="PL_RESYNTHESIS:STRING=OFF", defines=["PL_NETLIST_PREPROCESSING=ON"])
        self.assertTrue(self._result(result).upper() in ("OFF", "FALSE", "0", ""))
        self.assertNotIn("migrating cache entry", result.stdout)


if __name__ == "__main__":
    unittest.main()
