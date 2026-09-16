#!/usr/bin/env python3
"""Unit tests for ``tools/changelog_converter.py`` -- issue #67.

The converter is a packaging helper: ``cmake/UploadPPA.cmake`` shells out to it
to turn ``CHANGELOG.md`` into a ``debian/changelog``.  A helper invoked from a
build script has exactly one hard obligation, and it is not the conversion: it
must not report success when it produced nothing.  It used to.
``changelog_converter.py garbage-input`` read the word ``garbage-input`` as the
*contents* of a changelog, found no release entry in it, printed "Skipping write
out. No output produced!" and exited 0, so a typo'd path looked to CMake exactly
like a successful run.

Every case below therefore asserts on the exit status and on the message naming
the input, not on the converted text.  The one conversion test is there so that
the error cases cannot pass by breaking the tool outright.

Run it from the repository root, or as part of ``ctest``::

    python3 tools/test_changelog_converter.py
"""

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
CONVERTER = os.path.join(HERE, "changelog_converter.py")
CHANGELOG = os.path.join(REPO, "CHANGELOG.md")

#: A minimal but real ``CHANGELOG.md`` release entry, in the shape the tool's own
#: regex expects (version, ISO timestamp with an offset, urgency).
ONE_ENTRY = """# Changelog

## [4.2.0] - 2026-01-02 03:04:05+00:00 (urgency: medium)

* did a thing

[//]: # (Hyperlink section)
"""


def run(*args):
    """Run the converter with ``args`` and return the completed process."""
    return subprocess.run(
        [sys.executable, CONVERTER] + list(args),
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=60,
    )


class MissingInputTest(unittest.TestCase):
    """#67: a missing or unusable input is an error, and it names the path."""

    def assert_failed_naming(self, result, needle):
        self.assertNotEqual(
            0,
            result.returncode,
            "the converter exited 0 with nothing to convert; stdout was "
            + repr(result.stdout),
        )
        self.assertNotIn(
            "Traceback (most recent call last)",
            result.stdout + result.stderr,
            "a missing input is a user error, not a crash",
        )
        self.assertIn(
            needle,
            result.stderr,
            "the error message must name the input it could not use; stderr was "
            + repr(result.stderr),
        )

    def test_positional_that_is_not_a_file(self):
        """The literal case from the issue: ``changelog_converter.py garbage-input``."""
        result = run("garbage-input")
        self.assert_failed_naming(result, "garbage-input")

    def test_input_file_that_does_not_exist(self):
        missing = os.path.join(REPO, "no-such-changelog-zzz.md")
        result = run("-i", missing)
        self.assert_failed_naming(result, "no-such-changelog-zzz.md")

    def test_no_input_at_all(self):
        result = run()
        self.assertNotEqual(0, result.returncode, "no input at all must be an error")
        self.assertIn("no input", result.stderr)

    def test_an_empty_file_is_not_a_successful_conversion(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False
        ) as handle:
            handle.write("")
            empty = handle.name
        try:
            result = run("-i", empty, "--to", "debian", "-p")
            self.assert_failed_naming(result, os.path.basename(empty))
        finally:
            os.unlink(empty)


class ConversionStillWorksTest(unittest.TestCase):
    """The error cases above must not be passing because the tool is broken."""

    @classmethod
    def setUpClass(cls):
        try:
            import dateutil  # noqa: F401
        except ImportError:
            raise unittest.SkipTest(
                "python-dateutil is not installed; the converter cannot parse "
                "timestamps without it and only the error paths are testable"
            )

    def test_a_real_entry_converts_to_debian(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False
        ) as handle:
            handle.write(ONE_ENTRY)
            source = handle.name
        try:
            result = run("-i", source, "--to", "debian", "-p")
            self.assertEqual(
                0,
                result.returncode,
                "converting a real entry failed: " + result.stderr,
            )
            self.assertIn("hal-reverse (4.2.0) bionic; urgency=medium", result.stdout)
            self.assertIn("did a thing", result.stdout)
        finally:
            os.unlink(source)

    def test_the_repository_changelog_converts(self):
        """The invocation cmake/UploadPPA.cmake actually makes."""
        if not os.path.isfile(CHANGELOG):
            self.skipTest("CHANGELOG.md is missing from this checkout")
        result = run("-i", CHANGELOG, "--to", "debian", "-p")
        self.assertEqual(
            0, result.returncode, "CHANGELOG.md failed to convert: " + result.stderr
        )
        self.assertIn("hal-reverse (", result.stdout)

    def test_a_one_line_positional_that_is_a_real_file_is_read(self):
        """A path handed to the positional argument is read rather than parsed."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False
        ) as handle:
            handle.write(ONE_ENTRY)
            source = handle.name
        try:
            result = run(source, "--to", "debian", "-p")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("hal-reverse (4.2.0)", result.stdout)
        finally:
            os.unlink(source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
