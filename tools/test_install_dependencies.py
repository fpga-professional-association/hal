#!/usr/bin/env python3
"""Tests that install_dependencies.sh installs the same things everywhere.

HAL is built both on a developer machine / CI runner (the ``linux`` branch of
install_dependencies.sh) and inside a container (the ``docker`` branch, selected
by ``HAL_DOCKER=1``). The two branches are separate package lists, so they drift
apart silently -- and the failure that drift produces is not a build error but a
wrong analysis result: the docker branch used to install ``libz3-dev`` without
``z3``, and ``netlist_preprocessing::remove_redundant_gates`` treats an
unanswerable SMT query as "these gates are not equivalent", so it removed nothing
and its test failed in containers while passing on GitHub runners (issue #30).

These tests run on a plain interpreter -- no HAL build, no apt.

    python3 tools/test_install_dependencies.py
"""

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL_SCRIPT = os.path.join(REPO_ROOT, "install_dependencies.sh")
BREWFILE = os.path.join(REPO_ROOT, "Brewfile")

# Packages that legitimately differ between the two Ubuntu package lists. Everything else has to be
# in both, so that a container build gets the same HAL as a runner build.
EXPECTED_DIFFERENCES = {
    # crash reporting, pointless in a container
    "apport",
    # expanded by the script itself from the Ubuntu release, not a package name
    "$additional_deps",
}


def _read(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _apt_package_lists(script):
    """Return every ``apt-get install`` package list in the script, in order of appearance."""
    lists = []
    lines = script.splitlines()
    index = 0
    while index < len(lines):
        if "apt-get install" not in lines[index]:
            index += 1
            continue

        # a command continues across lines while they end in a backslash
        command = []
        while index < len(lines):
            line = lines[index]
            index += 1
            command.append(line)
            if not line.rstrip().endswith("\\"):
                break

        text = " ".join(line.rstrip().rstrip("\\") for line in command)
        text = re.sub(r"#.*$", "", text)                  # trailing comments
        text = re.sub(r"^.*apt-get install\b", "", text)   # everything up to the command

        packages = set()
        for token in text.split():
            if token.startswith("-") or token in ("&&", "|", ";"):
                continue
            packages.add(token)
        lists.append(packages)
    return lists


class InstallDependenciesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = _read(INSTALL_SCRIPT)
        # The lists of interest are the two full dependency lists: the Ubuntu/Mint one and the docker
        # one. Both mention build-essential; the small helper installs (e.g. reinstalls) do not.
        cls.full_lists = [pkgs for pkgs in _apt_package_lists(cls.script) if "build-essential" in pkgs]

    def test_two_full_apt_package_lists_are_found(self):
        # if this fails the parser has drifted from the script, not the other way round
        self.assertEqual(len(self.full_lists), 2, "expected an Ubuntu and a docker apt package list")

    def test_solver_binary_is_installed_by_every_branch(self):
        # libz3-dev is the library HAL links against, z3 is the binary the default SMT QueryConfig
        # shells out to. Both are required; installing only the former is the bug behind issue #30.
        for index, packages in enumerate(self.full_lists):
            self.assertIn("libz3-dev", packages, "package list %d is missing libz3-dev" % index)
            self.assertIn("z3", packages, "package list %d is missing the z3 solver binary" % index)

    def test_apt_package_lists_agree(self):
        ubuntu, docker = self.full_lists
        difference = ubuntu.symmetric_difference(docker)
        self.assertEqual(
            difference - EXPECTED_DIFFERENCES,
            set(),
            "the Ubuntu and docker package lists in install_dependencies.sh disagree; add the package "
            "to both branches, or add it to EXPECTED_DIFFERENCES here with a reason",
        )

    def test_brewfile_installs_the_solver(self):
        self.assertIn('brew "z3"', _read(BREWFILE))


if __name__ == "__main__":
    unittest.main()
