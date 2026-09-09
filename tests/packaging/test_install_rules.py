#!/usr/bin/env python3
"""Tests for HAL's install rules -- ordering and DESTDIR behaviour.

Two defects motivated these (issue #41):

* the ldconfig post-install step was registered in the top-level CMakeLists.txt,
  and CMake emits a directory's install rules *before* those of its
  subdirectories, so ldconfig ran before src/, app/ and plugins/ had installed a
  single shared object;
* that step copied a file into /etc/ld.so.conf.d/ and ran ldconfig
  unconditionally, which is a write outside the staging tree when DESTDIR is set
  and breaks distro packaging and unprivileged installs.

These tests read the generated install script and run the generated post-install
script in isolation, so they cost a fraction of a second and need no root. What
they cannot cover -- that a real ``cmake --install`` produces a loadable tree --
is what the container verification in the issue is for.

    HAL_BUILD_DIR=<build> python3 -m unittest discover -s tests/packaging
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

BUILD_DIR = os.environ.get("HAL_BUILD_DIR", "")


def _cache_value(build_dir, key):
    cache = os.path.join(build_dir, "CMakeCache.txt")
    with open(cache, "r", encoding="utf-8") as handle:
        for line in handle:
            match = re.match(r"^%s:[A-Z]+=(.*)$" % re.escape(key), line.strip())
            if match:
                return match.group(1)
    return None


@unittest.skipIf(not BUILD_DIR, "HAL_BUILD_DIR is not set")
class InstallOrderTest(unittest.TestCase):
    """The ldconfig step has to be the last thing the install does."""

    def setUp(self):
        self.install_script = os.path.join(BUILD_DIR, "cmake_install.cmake")
        if not os.path.isfile(self.install_script):
            self.skipTest("no generated cmake_install.cmake in %s" % BUILD_DIR)
        with open(self.install_script, "r", encoding="utf-8") as handle:
            self.lines = handle.read().splitlines()

    def _included_scripts(self):
        included = []
        for line in self.lines:
            match = re.search(r'include\("([^"]*cmake_install\.cmake)"\)', line)
            if match:
                included.append(match.group(1).replace("\\", "/"))
        return included

    def test_post_install_is_the_last_subdirectory(self):
        included = self._included_scripts()
        self.assertTrue(included, "no subdirectory install scripts found")
        self.assertTrue(
            included[-1].endswith("packaging/post_install/cmake_install.cmake"),
            "packaging/post_install must be the last add_subdirectory() of the top-level CMakeLists.txt, "
            "otherwise ldconfig runs before the libraries are installed; last include is %s" % included[-1],
        )

    def test_ldconfig_step_is_not_registered_in_the_top_level_list(self):
        # a top-level install(SCRIPT) is emitted before every subdirectory's install rules, which is
        # the ordering bug this whole arrangement exists to avoid
        for line in self.lines:
            self.assertNotRegex(
                line,
                r'include\("[^"]*/post_install\.cmake"\)',
                "the ldconfig step is registered in the top-level CMakeLists.txt again",
            )

    def test_ldconfig_step_is_registered_in_the_post_install_subdirectory(self):
        script = os.path.join(BUILD_DIR, "packaging", "post_install", "cmake_install.cmake")
        if not os.path.isfile(script):
            self.skipTest("no generated packaging/post_install/cmake_install.cmake")
        with open(script, "r", encoding="utf-8") as handle:
            content = handle.read()
        if not os.path.isfile(os.path.join(BUILD_DIR, "post_install.cmake")):
            self.skipTest("configured with ENABLE_INSTALL_LDCONFIG=OFF or not on Linux")
        self.assertRegex(content, r'include\("[^"]*post_install\.cmake"\)')


@unittest.skipIf(not BUILD_DIR, "HAL_BUILD_DIR is not set")
class PostInstallScriptTest(unittest.TestCase):
    """The post-install script must not touch the system when the install is staged."""

    def setUp(self):
        self.script = os.path.join(BUILD_DIR, "post_install.cmake")
        if not os.path.isfile(self.script):
            self.skipTest("configured with ENABLE_INSTALL_LDCONFIG=OFF or not on Linux")
        self.cmake = shutil.which("cmake")
        if not self.cmake:
            self.skipTest("cmake not on PATH")
        self.prefix = _cache_value(BUILD_DIR, "CMAKE_INSTALL_PREFIX")
        self.assertTrue(self.prefix, "CMAKE_INSTALL_PREFIX not found in CMakeCache.txt")

    def _run(self, prefix, destdir=None):
        environment = dict(os.environ)
        if destdir is None:
            environment.pop("DESTDIR", None)
        else:
            environment["DESTDIR"] = destdir
        completed = subprocess.run(
            [self.cmake, "-DCMAKE_INSTALL_PREFIX=" + prefix, "-P", self.script],
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        return completed.stdout + completed.stderr

    def _assert_system_untouched(self, output):
        # The skip message names ldconfig too ("... and not running ldconfig"), so match the action
        # messages as whole lines rather than as substrings.
        for line in output.splitlines():
            stripped = line.strip()
            self.assertNotEqual(stripped, "-- hal: running ldconfig", "ldconfig was run for a staged install")
            self.assertFalse(
                stripped.startswith("-- hal: installing "),
                "the ld.so.conf.d file was written for a staged install: " + stripped,
            )

    def test_destdir_install_skips_system_directories(self):
        with tempfile.TemporaryDirectory() as staging:
            output = self._run(self.prefix, destdir=staging)
            staged_conf = os.path.join(staging, "etc", "ld.so.conf.d", "hal.conf")
            staged = os.path.exists(staged_conf)

        self.assertIn("DESTDIR", output)
        self.assertIn("not touching /etc/ld.so.conf.d", output)
        self._assert_system_untouched(output)
        self.assertFalse(staged, "the ldconfig configuration must be left to the package's postinst, not staged")

    def test_relocated_prefix_skips_system_directories(self):
        # cpack and `cmake --install --prefix ...` install somewhere other than the configured prefix;
        # the hal.conf that was generated at configure time does not describe that tree, so writing it
        # to /etc would point the dynamic linker at the wrong directories.
        with tempfile.TemporaryDirectory() as other_prefix:
            output = self._run(other_prefix)

        self.assertIn("instead of the configured prefix", output)
        self._assert_system_untouched(output)


if __name__ == "__main__":
    unittest.main()
