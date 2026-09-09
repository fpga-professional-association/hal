#!/usr/bin/env python3
"""Tests for ``install_dependencies.sh``.

The script installs system packages, so the only honest way to test it without a throwaway machine
is to run it against *stub* package managers: a directory of fake ``apt-get``/``uname``/``brew``
executables placed first on ``PATH``. That is enough to prove the two things that matter and that
were broken before (upstream emsec/hal#512):

  1. it fails fast. A package that does not exist, an ``apt-get update`` that fails, a
     ``brew bundle`` that fails -- each aborts the run with a non-zero exit code and an error that
     names what failed, instead of continuing and surfacing as 'Could NOT find RapidJSON' in cmake
     20 minutes later.
  2. the package lists are complete. ``rapidjson-dev`` (and z3, spdlog, pybind11, the Python
     headers, ...) are hard cmake requirements, so every distribution list has to carry its
     spelling of them.

What these tests cannot prove is that the package names exist in a real distribution's archive.
That needs a real container: ``HAL_DEPENDENCIES_DRY_RUN=1 bash install_dependencies.sh`` resolves
the full list with ``apt-get --dry-run`` and installs nothing, which is what to run in a fresh
image. For this change it was run in ``ubuntu:24.04`` (all 31 packages resolve) and in
``debian:12`` -- the second one is why ``apport`` is no longer in the list: one APT list serves
every Debian-like distribution, and Debian has no such package.

    python3 tools/test_install_dependencies.py
"""

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "install_dependencies.sh")
BASH = shutil.which("bash")

# Spelled per distribution family, but the same dependency: these come straight out of
# cmake/detect_dependencies.cmake, where each is a find_package(... REQUIRED).
REQUIRED_BY_FAMILY = {
    "APT_PACKAGES": [
        "build-essential",
        "cmake",
        "libboost-all-dev",
        "libpython3-dev",
        "libreadline-dev",
        "libsodium-dev",
        "libspdlog-dev",
        "libz3-dev",
        "pybind11-dev",
        "rapidjson-dev",
        "verilator",
    ],
    "ARCH_PACKAGES": [
        "base-devel",
        "cmake",
        "boost",
        "libsodium",
        "pybind11",
        "python",
        "rapidjson",
        "readline",
        "spdlog",
        "verilator",
        "z3",
    ],
    "RHEL_EPEL_PACKAGES": [
        "boost-devel",
        "pybind11-devel",
        "python3-devel",
        "rapidjson-devel",
        "spdlog-devel",
        "z3-devel",
    ],
}


def read_script():
    with open(SCRIPT, "r", encoding="utf-8") as handle:
        return handle.read()


def parse_array(text, name):
    """Return the entries of the bash array ``name=( ... )`` defined in ``text``."""
    match = re.search(r"^%s=\((.*?)^\)" % re.escape(name), text, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError("install_dependencies.sh does not define an array named " + name)
    entries = []
    for line in match.group(1).splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.extend(line.split())
    return entries


def write_executable(path, body):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)
    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class PackageListTest(unittest.TestCase):
    """The lists themselves, checked without running anything."""

    @classmethod
    def setUpClass(cls):
        cls.text = read_script()

    def test_script_is_fail_fast(self):
        # -e stops at the first failure, -u catches a mistyped variable, -o pipefail keeps a
        # failure inside a pipeline from being hidden, -E makes the ERR trap fire inside functions.
        self.assertIn("set -Eeuo pipefail", self.text)
        self.assertIn("trap 'on_error", self.text)

    def test_every_distribution_list_has_the_required_dependencies(self):
        for array, required in sorted(REQUIRED_BY_FAMILY.items()):
            packages = parse_array(self.text, array)
            for package in required:
                self.assertIn(
                    package,
                    packages,
                    "{} is missing from {}; it is a hard cmake requirement".format(package, array),
                )

    def test_rapidjson_is_listed_for_every_distribution(self):
        # The bug this file exists for: a missing rapidjson-dev used to surface as a cmake error.
        for array, spelling in (
            ("APT_PACKAGES", "rapidjson-dev"),
            ("ARCH_PACKAGES", "rapidjson"),
            ("RHEL_EPEL_PACKAGES", "rapidjson-devel"),
        ):
            self.assertIn(spelling, parse_array(self.text, array))

    def test_no_duplicate_packages(self):
        for array in REQUIRED_BY_FAMILY:
            packages = parse_array(self.text, array)
            duplicates = sorted({p for p in packages if packages.count(p) > 1})
            self.assertEqual([], duplicates, "{} lists {} twice".format(array, duplicates))

    def test_docker_and_ubuntu_share_one_apt_list(self):
        # Two copies of the same list is how rapidjson-dev went missing from one of them upstream.
        self.assertEqual(1, len(re.findall(r"^APT_PACKAGES=\(", self.text, re.MULTILINE)))
        self.assertEqual(
            1,
            len(re.findall(r'apt_install "\$\{APT_PACKAGES\[@\]\}"', self.text)),
            "the docker and linux paths must go through the same apt_install call",
        )

    def test_bash_syntax_is_valid(self):
        if BASH is None:
            self.skipTest("bash is not available")
        result = subprocess.run(
            [BASH, "-n", SCRIPT], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        self.assertEqual(0, result.returncode, result.stdout.decode("utf-8", "replace"))


@unittest.skipIf(BASH is None, "bash is not available")
class StubbedRunTest(unittest.TestCase):
    """Run the script for real, with fake package managers on ``PATH``."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hal_install_deps_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "apt.log")
        self.os_release = os.path.join(self.tmp, "os-release")
        with open(self.os_release, "w", encoding="utf-8", newline="\n") as handle:
            handle.write('ID=ubuntu\nVERSION_ID="24.04"\nID_LIKE=debian\n')
        write_executable(os.path.join(self.bin, "uname"), "#!/bin/sh\necho Linux\n")
        # The script runs package managers directly when it is root and through sudo otherwise;
        # pretending to be root keeps these tests identical on a developer machine and in CI.
        write_executable(os.path.join(self.bin, "id"), "#!/bin/sh\necho 0\n")

    def install_apt_stub(self, unavailable=(), update_fails=False, install_fails=()):
        """A fake apt-get that behaves like the real one for the cases we care about."""
        body = textwrap.dedent(
            """\
            #!/bin/sh
            echo "apt-get $*" >> "{log}"
            if [ "$1" = "update" ]; then
                [ "{update_fails}" = "1" ] && {{ echo "E: update failed" >&2; exit 100; }}
                exit 0
            fi
            status=0
            for arg in "$@"; do
                case " {unavailable} " in
                    *" $arg "*) echo "E: Unable to locate package $arg" >&2; status=100 ;;
                esac
            done
            [ $status -eq 0 ] || exit $status
            case " $* " in
                *" --dry-run "*) exit 0 ;;
            esac
            for arg in "$@"; do
                case " {install_fails} " in
                    *" $arg "*) echo "E: Sub-process returned an error code" >&2; exit 100 ;;
                esac
            done
            exit 0
            """
        ).format(
            log=self.log,
            update_fails="1" if update_fails else "0",
            unavailable=" ".join(unavailable),
            install_fails=" ".join(install_fails),
        )
        write_executable(os.path.join(self.bin, "apt-get"), body)

    def run_script(self, script=None, env=None, dry_run=True):
        environment = dict(os.environ)
        environment["PATH"] = self.bin + os.pathsep + environment.get("PATH", "")
        environment["HAL_OS_RELEASE"] = self.os_release
        if dry_run:
            environment["HAL_DEPENDENCIES_DRY_RUN"] = "1"
        environment.pop("additional_deps", None)
        environment.update(env or {})
        return subprocess.run(
            [BASH, script or SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            cwd=REPO_ROOT,
        )

    def apt_log(self):
        if not os.path.exists(self.log):
            return ""
        with open(self.log, "r", encoding="utf-8") as handle:
            return handle.read()

    def copy_script_with(self, replacement):
        """A copy of the script with one edit applied -- how a bogus package is injected."""
        text = read_script()
        old, new = replacement
        self.assertIn(old, text)
        path = os.path.join(self.tmp, "install_dependencies.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text.replace(old, new, 1))
        return path

    # -- the happy path -----------------------------------------------------

    def test_dry_run_resolves_the_full_list_and_installs_nothing(self):
        self.install_apt_stub()
        result = self.run_script()
        self.assertEqual(0, result.returncode, result.stdout.decode("utf-8", "replace"))
        log = self.apt_log()
        self.assertIn("--dry-run", log)
        self.assertIn("rapidjson-dev", log)
        self.assertNotIn("apt-get install -y -- build-essential", log)

    def test_full_run_installs_every_package(self):
        self.install_apt_stub()
        result = self.run_script(dry_run=False)
        self.assertEqual(0, result.returncode, result.stdout.decode("utf-8", "replace"))
        packages = parse_array(read_script(), "APT_PACKAGES")
        install_lines = [
            line
            for line in self.apt_log().splitlines()
            if line.startswith("apt-get install") and "--dry-run" not in line
        ]
        self.assertEqual(1, len(install_lines), self.apt_log())
        for package in packages:
            self.assertIn(package, install_lines[0].split())

    def test_additional_deps_are_appended(self):
        self.install_apt_stub()
        result = self.run_script(env={"additional_deps": "libfoo-dev libbar-dev"})
        self.assertEqual(0, result.returncode, result.stdout.decode("utf-8", "replace"))
        self.assertIn("libfoo-dev", self.apt_log())
        self.assertIn("libbar-dev", self.apt_log())

    def test_docker_path_uses_the_same_list(self):
        # HAL_DOCKER=1 is what the top-level Dockerfile sets; the base image has neither sudo nor
        # lsb_release, so the path must work from /etc/os-release as root.
        self.install_apt_stub()
        write_executable(
            os.path.join(self.bin, "sudo"),
            "#!/bin/sh\necho 'sudo must not be used as root' >&2\nexit 111\n",
        )
        write_executable(os.path.join(self.bin, "uname"), "#!/bin/sh\necho NotLinux\n")
        result = self.run_script(env={"HAL_DOCKER": "1"})
        self.assertEqual(0, result.returncode, result.stdout.decode("utf-8", "replace"))
        self.assertIn("rapidjson-dev", self.apt_log())

    # -- fail fast ----------------------------------------------------------

    def test_a_bogus_package_aborts_before_installing_anything(self):
        # Exactly the throwaway-container experiment, run against the stub: inject a package name
        # that the archive does not have and check the run stops with that name in the message.
        script = self.copy_script_with(("    rapidjson-dev\n", "    rapidjson-dev\n    hal-bogus-package\n"))
        self.install_apt_stub(unavailable=["hal-bogus-package"])
        result = self.run_script(script=script, dry_run=False)
        output = result.stdout.decode("utf-8", "replace")
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("hal-bogus-package", output)
        self.assertIn("not available on ubuntu 24.04", output)
        self.assertIn("nothing was installed", output)
        install_lines = [
            line
            for line in self.apt_log().splitlines()
            if line.startswith("apt-get install") and "--dry-run" not in line
        ]
        self.assertEqual([], install_lines, "the script installed packages despite a bad list")

    def test_failed_update_aborts_with_a_message(self):
        self.install_apt_stub(update_fails=True)
        result = self.run_script()
        output = result.stdout.decode("utf-8", "replace")
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("apt-get update", output)
        self.assertNotIn("--dry-run", self.apt_log())

    def test_a_package_that_fails_to_install_is_named(self):
        # Resolvable, but its installation fails (broken maintainer script, full disk, ...): the
        # script retries package by package so the error names the culprit rather than the list.
        self.install_apt_stub(install_fails=["libz3-dev"])
        result = self.run_script(dry_run=False)
        output = result.stdout.decode("utf-8", "replace")
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("libz3-dev", output)
        self.assertIn("failed to install", output)

    def test_unsupported_distribution_is_reported(self):
        self.install_apt_stub()
        with open(self.os_release, "w", encoding="utf-8", newline="\n") as handle:
            handle.write('ID=plan9\nVERSION_ID="4"\n')
        result = self.run_script()
        output = result.stdout.decode("utf-8", "replace")
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("unsupported Linux distribution 'plan9'", output)
        self.assertIn("rapidjson-dev", output)

    def test_undetectable_distribution_is_reported(self):
        # Neither source of truth answers: no readable os-release file, and an lsb_release that
        # fails (it is stubbed rather than removed from PATH, because whether the host has one is
        # not something this test may depend on).
        self.install_apt_stub()
        write_executable(
            os.path.join(self.bin, "lsb_release"),
            "#!/bin/sh\necho 'No LSB modules are available.' >&2\nexit 1\n",
        )
        result = self.run_script(env={"HAL_OS_RELEASE": os.path.join(self.tmp, "missing")})
        output = result.stdout.decode("utf-8", "replace")
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("cannot determine the Linux distribution", output)

    def test_lsb_release_is_used_when_there_is_no_os_release_file(self):
        # The fallback path: an image without /etc/os-release but with lsb-release installed still
        # gets the right package list instead of the 'unsupported distribution' error.
        self.install_apt_stub()
        write_executable(
            os.path.join(self.bin, "lsb_release"),
            "#!/bin/sh\n"
            'case "$1" in -is) echo Ubuntu ;; -rs) echo 24.04 ;; *) exit 1 ;; esac\n',
        )
        result = self.run_script(env={"HAL_OS_RELEASE": os.path.join(self.tmp, "missing")})
        output = result.stdout.decode("utf-8", "replace")
        self.assertEqual(0, result.returncode, output)
        self.assertIn("ubuntu 24.04", output)
        self.assertIn("rapidjson-dev", self.apt_log())

    def test_missing_root_and_sudo_is_reported(self):
        if shutil.which("sudo") is not None:
            self.skipTest("sudo is installed here, so the 'no sudo' path cannot be reached")
        # id(1) is what the script uses to decide whether sudo is needed: as a normal user without
        # sudo the run has to stop with an explanation rather than with a permission error.
        self.install_apt_stub()
        write_executable(os.path.join(self.bin, "id"), "#!/bin/sh\necho 1000\n")
        result = self.run_script()
        output = result.stdout.decode("utf-8", "replace")
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("sudo", output)
        self.assertIn("needs root", output)

    # -- macOS --------------------------------------------------------------

    def test_macos_brew_failure_aborts(self):
        write_executable(os.path.join(self.bin, "uname"), "#!/bin/sh\necho Darwin\n")
        write_executable(
            os.path.join(self.bin, "brew"),
            "#!/bin/sh\n[ \"$1\" = bundle ] && { echo 'Error: formula not found' >&2; exit 1; }\n"
            "echo /opt/homebrew\n",
        )
        result = self.run_script()
        output = result.stdout.decode("utf-8", "replace")
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("brew bundle", output)

    def test_macos_missing_brew_is_reported(self):
        if shutil.which("brew") is not None:
            self.skipTest("Homebrew is installed here, so the 'no brew' path cannot be reached")
        write_executable(os.path.join(self.bin, "uname"), "#!/bin/sh\necho Darwin\n")
        result = self.run_script()
        output = result.stdout.decode("utf-8", "replace")
        self.assertNotEqual(0, result.returncode, output)
        self.assertIn("Homebrew", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
