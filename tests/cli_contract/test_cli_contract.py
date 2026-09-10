#!/usr/bin/env python3
"""CLI contract tests for every entry point under ``tools/`` (issue #50).

The bugs this file exists to catch were all *contract* bugs rather than analysis bugs: HAL's
native log lines leaking onto stdout and corrupting a ``--json`` document, a CLI raising a
``Traceback`` at the user instead of an error message, an entry point that could not even be
asked for ``--help``.  None of them were guarded by a test, and each of them was found by hand,
once per tool.

So the CLI list is not written down here.  It is *discovered* from the checkout every time the
suite runs:

* every direct child of ``tools/`` that is a package with a ``__main__.py`` is a CLI, run the way
  the walkthroughs run it -- ``python3 tools/<name> <args>``;
* every direct child that is a ``*.py`` importing ``argparse`` and guarded by
  ``if __name__ == "__main__"`` is a CLI too, run as ``python3 tools/<name>.py <args>``;
* anything else must be named in :data:`EXEMPT` with a reason.

A new tool is therefore enrolled the moment it lands, and skipping it requires editing a dict in
this file and justifying it in review.  :meth:`ToolInventory.test_every_entry_is_enrolled_or_exempt`
fails on an unclassified entry, and on a stale exemption for something that no longer exists.

The contracts, per enrolled CLI:

1. ``--help`` exits 0 and prints a usage line (:class:`HelpContract`).
2. Garbage arguments -- an unknown option, and an unknown subcommand for the CLIs that take
   subcommands -- exit nonzero with an error message and *no* traceback
   (:class:`BadArgumentContract`).
3. For the CLIs that advertise ``--json``, a real ``--json`` run writes a document to stdout with
   zero preamble bytes, so that ``json.loads`` of the whole stream succeeds
   (:class:`JsonPurityContract`).  ``--json`` support is detected from the help text of the CLI and
   of each of its subcommands, and every CLI found to support it must be listed in
   :data:`JSON_INVOCATIONS` (a cheap invocation that must pass), :data:`KNOWN_JSON_FAILURES` (a
   cheap invocation that fails today, run as an expected failure so the violation stays visible)
   or :data:`SKIPPED_JSON` (a reason why no cheap invocation exists).
4. Issue #53's acceptance: a netlist-loading CLI run *without* ``--json`` also keeps HAL's native
   log lines off stdout (:class:`LogRoutingContract`).  That is not true today -- HAL's spdlog
   sinks write to file descriptor 1 -- so the test is an expected failure and will start reporting
   an unexpected *success* when the logging is rerouted.

Run it against a build tree::

    HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib PYTHONPATH=<build>/lib \\
        python3 tests/cli_contract/test_cli_contract.py

Only the tests that actually load a netlist need the build; ``--help`` and the bad-argument
contracts run against a bare checkout, and the netlist tests skip themselves when the example
netlist or the gate library is missing.
"""

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "tools"

#: A small real design and its gate library, from the walkthrough the CLI docs use.
NETLIST = (
    REPO_ROOT / "examples" / "agilex3_walkthroughs" / "01_blinky_counter" / "netlist.hal.v"
)
GATE_LIBRARY = REPO_ROOT / "plugins" / "gate_libraries" / "definitions" / "AGILEX_TENNM.hgl"

#: Per-command timeout.  Every command this suite runs is either a help text, an argparse error or
#: one small netlist load, so anything near this bound is itself a bug.
TIMEOUT = 120

#: Not repository content: bytecode caches and editor/VCS droppings.  Classified structurally
#: rather than by exemption so that a stale ``__pycache__`` never has to be named in EXEMPT.
IGNORED_NAMES = ("__pycache__",)

#: Tokens that no CLI may accept.
UNKNOWN_OPTION = "--not-a-real-option-zzz"
UNKNOWN_SUBCOMMAND = "not-a-real-subcommand-zzz"

#: ``tools/`` entries that are not CLIs, and why.  An entry here that stops existing, or a CLI
#: that appears without one, fails the inventory test.
EXEMPT = {
    "plugin_template": (
        "Template directory of *.in files expanded by tools/new_plugin.py. It has no Python "
        "entry point at all -- nothing to run, so nothing to contract."
    ),
    "blind.py": (
        "Anonymization helper for Verilog exports: module-level functions only, no "
        "'if __name__ == \"__main__\"' block and no argument parsing. It is imported or "
        "copy-pasted, never invoked."
    ),
    "pydecorator.py": (
        "Instrumentation shim executed inside a HAL Python context (it imports hal_py at module "
        "scope and rebinds hal_py methods). No main, no arguments; loaded by a script, not run."
    ),
    "check_documentation.py": (
        "Upstream Doxygen-comment checker driven by raw sys.argv with a hand-rolled print_usage(); "
        "invoked by the documentation build with fixed paths, never by a user. Enrolling it would "
        "test upstream's ad-hoc argument handling, not this fork's CLI surface."
    ),
    "check_license.py": (
        "Upstream license-header checker, same shape as check_documentation.py: raw sys.argv, "
        "hand-rolled usage, run from CI with fixed paths."
    ),
    "genversion.py": (
        "Build-time version generator: CMake runs it with an optional positional working "
        "directory and captures its stdout. No argparse, no --help, and it must keep printing "
        "exactly the version string."
    ),
    "print_date.py": (
        "Four-line build helper that prints the current timestamp for the packaging scripts. It "
        "takes no arguments whatsoever."
    ),
    "skywater_to_liberty.py": (
        "One-off gate-library conversion script with hand-rolled positional sys.argv parsing, "
        "kept from upstream for the SkyWater PDK. Not part of the tools/* CLI surface."
    ),
    "test_install_dependencies.py": (
        "unittest suite for install_dependencies.sh. It is a test, not a tool CLI; its argparse "
        "surface is unittest's own."
    ),
    "test_new_plugin.py": (
        "unittest suite for tools/new_plugin.py (which *is* enrolled). A test, not a tool CLI."
    ),
    "test_pydecorator.py": (
        "unittest suite for tools/pydecorator.py. A test, not a tool CLI."
    ),
}

#: A cheap, real ``--json`` invocation per CLI that supports ``--json``.  Cheap is the point: the
#: contract under test is the purity of stdout, not the analysis, so each of these is a listing or
#: a small load that finishes in well under a second.
JSON_INVOCATIONS = {
    "hal_capabilities": ["--json", "list", "--declared-only"],
    "hal_analysis_api": ["tools", "--json"],
    "hal_bitstream": ["families", "--json"],
}

#: CLIs whose ``--json`` mode has no cheap invocation, and why.  Listing them keeps the ``--json``
#: audit honest: :meth:`JsonPurityContract.test_every_json_cli_is_covered_or_skipped` fails when a
#: CLI advertises ``--json`` and appears in none of the three dicts.
SKIPPED_JSON = {
    "hal_fault_campaign": (
        "The only --json mode is 'manifest --json', which reads the manifest of an *already "
        "executed* campaign; there is no manifest fixture in the tree, and producing one means "
        "running a fault-injection campaign (many hal --python-script subprocesses, minutes). "
        "That is what tests/headless_smoke/fault_campaign_smoke.py is for. No cheap invocation "
        "exists."
    ),
}

#: ``--json`` invocations that are cheap and real but *violate* the purity contract today, with
#: the reason.  They run as expected failures rather than being hidden in :data:`SKIPPED_JSON`: a
#: known violation that nobody can see is the state issue #50 was opened about.  When the tool is
#: fixed, unittest reports an unexpected success and the entry moves to :data:`JSON_INVOCATIONS`.
KNOWN_JSON_FAILURES = {
    "hal_secprop": (
        [
            "cones",
            str(TOOLS / "hal_secprop" / "fixtures" / "secreg_ok.policy.json"),
            "--source",
            "hal",
            "--netlist",
            str(TOOLS / "hal_secprop" / "fixtures" / "secreg_ok.v"),
            "--gate-library",
            str(
                REPO_ROOT
                / "plugins"
                / "gate_libraries"
                / "definitions"
                / "NangateOpenCellLibrary.hgl"
            ),
            "--json",
        ],
        "Two violations in one run. 'hal_secprop cones --json' is documented as 'also print the "
        "raw cone data', so it writes its human report to stdout and appends the JSON -- stdout "
        "is a mixed stream by design, not a document. On top of that the run loads a netlist "
        "without the fd guard tools/hal_capabilities carries, so HAL's own [core] [info] lines "
        "land on stdout too (issue #53). Needs a decision on the CLI: make --json exclusive (and "
        "route the prose to stderr), which is what every other --json mode in tools/ does.",
    ),
}

#: A HAL native log line: ``[core] [info] ...``, ``[stdout] [warning] ...``.
HAL_LOG_LINE = re.compile(
    r"^\[[A-Za-z0-9_ ]+\] \[(trace|debug|info|warn|warning|err|error|critical)\]"
)


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


class ToolCli(object):
    """One discovered CLI: its name, how to spawn it, and why it counts as a CLI."""

    def __init__(self, name, path, kind):
        self.name = name
        self.path = path
        self.kind = kind  # "package" or "script"

    #: The identifier used in test method names and in the dicts above.
    @property
    def key(self):
        return self.name[:-3] if self.kind == "script" else self.name

    @property
    def display(self):
        return str(self.path.relative_to(REPO_ROOT)).replace(os.sep, "/")

    def __repr__(self):
        return "ToolCli({!r})".format(self.display)


def _script_has_argparse_main(path):
    """True when a top-level ``tools/*.py`` is a command line program.

    Two signals together, both read from the source rather than by importing it (importing would
    run module-level code -- ``tools/pydecorator.py`` imports ``hal_py`` at module scope): it
    parses arguments with ``argparse``, and it has an ``if __name__ == "__main__"`` entry point.
    The upstream helpers that hand-roll ``sys.argv`` parsing deliberately do not match; they are
    named in :data:`EXEMPT`.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "argparse" in source and "__main__" in source


def discover_clis():
    """Return ``([ToolCli], [unclassified names])`` for the current checkout."""
    clis = []
    unclassified = []
    for entry in sorted(TOOLS.iterdir(), key=lambda path: path.name):
        name = entry.name
        if name in IGNORED_NAMES or name.startswith("."):
            continue
        if entry.is_dir():
            if (entry / "__main__.py").is_file():
                # Run the directory, exactly as the walkthroughs do: python3 tools/<name> ...
                clis.append(ToolCli(name, entry, "package"))
            elif name not in EXEMPT:
                unclassified.append(name)
            continue
        if entry.suffix == ".py":
            if _script_has_argparse_main(entry):
                clis.append(ToolCli(name, entry, "script"))
            elif name not in EXEMPT:
                unclassified.append(name)
            continue
        if name not in EXEMPT:
            unclassified.append(name)
    return clis, unclassified


CLIS, UNCLASSIFIED = discover_clis()
CLIS_BY_KEY = dict((cli.key, cli) for cli in CLIS)


# ---------------------------------------------------------------------------
# running a CLI
# ---------------------------------------------------------------------------


def _environment():
    environment = dict(os.environ)
    search = [str(TOOLS)]
    hal_py_path = environment.get("HAL_PY_PATH", "")
    search.extend(part for part in hal_py_path.split(os.pathsep) if part)
    if environment.get("PYTHONPATH"):
        search.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(search)
    environment["PYTHONIOENCODING"] = "utf-8"
    # No terminal, no colour, no pager -- the same shape CI and an agent see.
    environment["TERM"] = "dumb"
    return environment


class Result(object):
    def __init__(self, argv, returncode, stdout, stderr):
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def combined(self):
        return self.stdout + self.stderr

    def report(self, limit=2000):
        return "\n$ {}\n-- exit {} --\n-- stdout --\n{}\n-- stderr --\n{}".format(
            " ".join(self.argv), self.returncode, self.stdout[:limit], self.stderr[:limit]
        )


_RUN_CACHE = {}


def run_cli(cli, args, cache=False):
    """Run ``python3 <cli> <args>`` from the repository root with separated streams."""
    argv = [sys.executable, cli.display] + list(args)
    key = tuple(argv)
    if cache and key in _RUN_CACHE:
        return _RUN_CACHE[key]
    completed = subprocess.run(
        argv,
        cwd=str(REPO_ROOT),
        env=_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=TIMEOUT,
    )
    result = Result(argv, completed.returncode, completed.stdout, completed.stderr)
    if cache:
        _RUN_CACHE[key] = result
    return result


def help_text(cli, subcommand=None):
    """The CLI's (or one subcommand's) help text, cached across tests."""
    args = ([subcommand] if subcommand else []) + ["--help"]
    result = run_cli(cli, args, cache=True)
    return result.stdout + result.stderr


def takes_subcommands(cli):
    """True when the CLI dispatches to subcommands.

    argparse renders a subparser positional as a trailing ``...`` in the usage line, whatever the
    metavar is (``{a,b} ...``, ``COMMAND ...``).  Nothing else in an argparse usage line ends that
    way, and it does not confuse a plain choices argument (``[--to {markdown,debian}]``) for a
    subcommand set.
    """
    usage = _usage_block(help_text(cli))
    return usage.rstrip().endswith("...")


def _usage_block(text):
    """The ``usage: ...`` paragraph of a help text."""
    lines = []
    started = False
    for line in text.splitlines():
        if not started:
            if line.startswith("usage:"):
                started = True
                lines.append(line)
            continue
        if line.startswith((" ", "\t")) and line.strip():
            lines.append(line)
            continue
        break
    return "\n".join(lines)


_CHOICES = re.compile(r"choose from (?P<choices>[^)]+)\)")


def subcommands(cli):
    """The CLI's subcommand names, read from its own invalid-choice error.

    Asking argparse is more robust than parsing the help layout: the error message enumerates the
    choices in every Python version, quoted (<=3.11) or bare (>=3.12).  The command is only run
    for CLIs that :func:`takes_subcommands` identified, so a CLI whose first positional is a real
    argument is never handed a garbage value.
    """
    if not takes_subcommands(cli):
        return []
    result = run_cli(cli, [UNKNOWN_SUBCOMMAND], cache=True)
    match = _CHOICES.search(result.combined)
    if not match:
        return []
    return [
        choice.strip().strip("'\"")
        for choice in match.group("choices").split(",")
        if choice.strip()
    ]


#: ``--json`` as its own option, not as the prefix of another one -- ``hal_findings validate
#: --jsonschema`` and ``hal_migration validate --jsonschema`` are not JSON output modes.
JSON_OPTION = re.compile(r"--json(?![0-9A-Za-z_-])")


def supports_json(cli):
    """True when ``--json`` appears in the CLI's help or in any subcommand's help."""
    if JSON_OPTION.search(help_text(cli)):
        return True
    return any(JSON_OPTION.search(help_text(cli, name)) for name in subcommands(cli))


# ---------------------------------------------------------------------------
# assertions shared by the contracts
# ---------------------------------------------------------------------------


class ContractCase(unittest.TestCase):
    maxDiff = None

    def assert_no_traceback(self, result):
        for stream_name in ("stdout", "stderr"):
            stream = getattr(result, stream_name)
            self.assertNotIn(
                "Traceback (most recent call last)",
                stream,
                "the CLI printed a Python traceback on {}; a user-facing error message is the "
                "contract{}".format(stream_name, result.report()),
            )

    def assert_no_hal_log_lines(self, text, why):
        offenders = [line for line in text.splitlines() if HAL_LOG_LINE.match(line)]
        self.assertEqual(
            [],
            offenders[:10],
            "{}\nHAL native log lines reached stdout ({} of them).".format(why, len(offenders)),
        )


# ---------------------------------------------------------------------------
# the inventory itself
# ---------------------------------------------------------------------------


class ToolInventory(ContractCase):
    """The discovery above must account for every entry in ``tools/``."""

    def test_something_was_discovered(self):
        self.assertTrue(
            CLIS, "no CLI was discovered under {} -- discovery is broken".format(TOOLS)
        )

    def test_every_entry_is_enrolled_or_exempt(self):
        self.assertEqual(
            [],
            sorted(UNCLASSIFIED),
            "these tools/ entries are neither an enrolled CLI nor exempt. If it is a CLI, give it "
            "a __main__.py (packages) or an argparse main (scripts) so it is discovered; if it is "
            "not, add it to EXEMPT in this file with a reason.",
        )

    def test_no_stale_exemptions(self):
        missing = sorted(name for name in EXEMPT if not (TOOLS / name).exists())
        self.assertEqual(
            [], missing, "EXEMPT names tools/ entries that no longer exist; delete them."
        )

    def test_exemptions_are_not_enrolled_clis(self):
        overlap = sorted(set(EXEMPT) & set(cli.name for cli in CLIS))
        self.assertEqual(
            [],
            overlap,
            "these entries are exempt but were also discovered as CLIs; discovery and EXEMPT "
            "disagree.",
        )

    def test_every_exemption_has_a_reason(self):
        thin = sorted(name for name, reason in EXEMPT.items() if len(reason.strip()) < 40)
        self.assertEqual(
            [],
            thin,
            "an exemption is a reviewed decision: write down why the entry is not a CLI.",
        )


# ---------------------------------------------------------------------------
# contract 1 -- --help exits 0 and prints usage
# ---------------------------------------------------------------------------


class HelpContract(ContractCase):
    """``python3 tools/<name> --help`` is the one call every CLI must answer."""


def _make_help_test(cli):
    def test(self):
        result = run_cli(cli, ["--help"], cache=True)
        self.assert_no_traceback(result)
        self.assertEqual(
            0, result.returncode, "--help must exit 0" + result.report()
        )
        self.assertIn(
            "usage",
            result.stdout.lower(),
            "--help must print a usage line on stdout" + result.report(),
        )

    test.__doc__ = "{} --help exits 0 and prints usage".format(cli.display)
    return test


# ---------------------------------------------------------------------------
# contract 2 -- garbage arguments are rejected cleanly
# ---------------------------------------------------------------------------


class BadArgumentContract(ContractCase):
    """Garbage in, nonzero out, with a message and no traceback."""


def _assert_clean_rejection(case, result, what):
    case.assert_no_traceback(result)
    case.assertNotEqual(
        0,
        result.returncode,
        "the CLI accepted {} and exited 0".format(what) + result.report(),
    )
    case.assertTrue(
        result.stderr.strip(),
        "the CLI rejected {} but wrote no error message to stderr".format(what)
        + result.report(),
    )


def _make_unknown_option_test(cli):
    def test(self):
        result = run_cli(cli, [UNKNOWN_OPTION])
        _assert_clean_rejection(self, result, "an unknown option")

    test.__doc__ = "{} rejects an unknown option".format(cli.display)
    return test


def _make_unknown_subcommand_test(cli):
    def test(self):
        if not takes_subcommands(cli):
            self.skipTest("{} takes no subcommands".format(cli.display))
        result = run_cli(cli, [UNKNOWN_SUBCOMMAND], cache=True)
        _assert_clean_rejection(self, result, "an unknown subcommand")
        self.assertIn(
            UNKNOWN_SUBCOMMAND,
            result.combined,
            "the error message must name the rejected subcommand" + result.report(),
        )

    test.__doc__ = "{} rejects an unknown subcommand".format(cli.display)
    return test


# ---------------------------------------------------------------------------
# contract 3 -- --json purity
# ---------------------------------------------------------------------------


class JsonPurityContract(ContractCase):
    """Under ``--json``, stdout is a JSON document and nothing else.

    "Nothing else" is literal: not a leading blank line, not a progress note, and above all not
    HAL's own log output.  A consumer does ``json.loads(subprocess.check_output(...))``, so a
    single preamble byte is a break.
    """

    def test_every_json_cli_is_covered_or_skipped(self):
        advertised = sorted(cli.key for cli in CLIS if supports_json(cli))
        covered = set(JSON_INVOCATIONS) | set(SKIPPED_JSON) | set(KNOWN_JSON_FAILURES)
        uncovered = [key for key in advertised if key not in covered]
        self.assertEqual(
            [],
            uncovered,
            "these CLIs advertise --json but this suite never runs it. Add a cheap invocation to "
            "JSON_INVOCATIONS; if it is cheap but broken, add it to KNOWN_JSON_FAILURES so the "
            "violation is visible; if there is genuinely no cheap invocation, add the tool to "
            "SKIPPED_JSON with a reason.",
        )

    def test_json_dicts_name_real_clis(self):
        keys = set(JSON_INVOCATIONS) | set(SKIPPED_JSON) | set(KNOWN_JSON_FAILURES)
        unknown = sorted(key for key in keys if key not in CLIS_BY_KEY)
        self.assertEqual(
            [],
            unknown,
            "JSON_INVOCATIONS/SKIPPED_JSON/KNOWN_JSON_FAILURES name tools that do not exist.",
        )

    def test_json_dicts_do_not_overlap(self):
        keys = [set(JSON_INVOCATIONS), set(SKIPPED_JSON), set(KNOWN_JSON_FAILURES)]
        overlap = sorted(
            (keys[0] & keys[1]) | (keys[0] & keys[2]) | (keys[1] & keys[2])
        )
        self.assertEqual(
            [], overlap, "a tool is listed in more than one of the three --json dicts."
        )

    def test_skipped_json_still_supports_json(self):
        """A skip or a known failure is only meaningful while the tool has a --json mode."""
        stale = sorted(
            key
            for key in list(SKIPPED_JSON) + list(KNOWN_JSON_FAILURES)
            if key in CLIS_BY_KEY and not supports_json(CLIS_BY_KEY[key])
        )
        self.assertEqual(
            [],
            stale,
            "these tools no longer support --json; drop them from SKIPPED_JSON / "
            "KNOWN_JSON_FAILURES.",
        )

    def test_json_purity_with_a_netlist(self):
        """The interesting case: a --json run that loads a netlist, so HAL logs during it.

        ``hal_capabilities list --netlist`` is the cheapest netlist-loading --json run in the
        tree -- it loads the plugins, parses the design and answers which plugins apply to it --
        which makes it the representative for "HAL was talking while the document was written".
        """
        cli = CLIS_BY_KEY.get("hal_capabilities")
        if cli is None:
            self.skipTest("tools/hal_capabilities is not in this checkout")
        if not NETLIST.is_file() or not GATE_LIBRARY.is_file():
            self.skipTest("the example netlist or the gate library is missing")
        if not os.environ.get("HAL_PY_PATH"):
            self.skipTest("no HAL_PY_PATH: this test needs a built HAL")
        result = run_cli(
            cli,
            [
                "--json",
                "list",
                "--netlist",
                str(NETLIST),
                "--gate-library",
                str(GATE_LIBRARY),
            ],
        )
        self.assert_no_traceback(result)
        self.assertEqual(0, result.returncode, "the run failed" + result.report())
        _assert_pure_json(self, result)
        self.assert_no_hal_log_lines(
            result.stdout, "a --json run that loads a netlist" + result.report()
        )


def _assert_pure_json(case, result):
    case.assertTrue(result.stdout, "--json wrote nothing to stdout" + result.report())
    case.assertIn(
        result.stdout[0],
        "{[",
        "stdout must start with the document itself; byte 0 is {!r}, so there is a "
        "preamble".format(result.stdout[0]) + result.report(),
    )
    try:
        json.loads(result.stdout)
    except ValueError as error:
        case.fail("stdout is not a single JSON document ({})".format(error) + result.report())


def _needs_build_or_fixtures(case, args):
    """Skip when this invocation's inputs, or HAL itself, are not available here."""
    if not os.environ.get("HAL_PY_PATH"):
        case.skipTest("no HAL_PY_PATH: this invocation needs a built HAL")
    missing = [
        entry
        for entry in args
        if entry.startswith(str(REPO_ROOT)) and not os.path.exists(entry)
    ]
    if missing:
        case.skipTest("missing fixture(s): {}".format(", ".join(missing)))


def _make_json_test(cli, args, needs_build=False):
    def test(self):
        if needs_build:
            _needs_build_or_fixtures(self, args)
        result = run_cli(cli, args)
        self.assert_no_traceback(result)
        self.assertEqual(
            0,
            result.returncode,
            "the --json invocation must succeed for its stdout to mean anything"
            + result.report(),
        )
        _assert_pure_json(self, result)
        self.assert_no_hal_log_lines(result.stdout, "a --json run" + result.report())

    test.__doc__ = "{} {} writes pure JSON to stdout".format(cli.display, " ".join(args))
    return test


# ---------------------------------------------------------------------------
# contract 4 -- issue #53's acceptance
# ---------------------------------------------------------------------------


class LogRoutingContract(ContractCase):
    """HAL's native logging belongs on stderr, in *every* mode -- issue #53."""

    @unittest.expectedFailure
    def test_native_logs_stay_off_stdout_without_json(self):
        """Acceptance test for issue #53; expected to fail until the logging is rerouted.

        ``--json`` modes survive today only because ``tools/hal_capabilities`` dups file
        descriptor 1 away for the duration of the run.  Nothing protects the *human* mode: HAL's
        spdlog sinks write ``[core] [info] ...`` to fd 1, so a plain ``hal_capabilities list
        --netlist`` interleaves dozens of plugin-loading log lines with its own report, and every
        walkthrough transcript needs a ``grep -v`` filter.

        Issue #53 asks for the sinks to default to stderr (option 1) or for a
        ``halenv.quiet_hal_logging`` helper used by every CLI (option 2).  When either lands this
        test starts passing and unittest reports an *unexpected success*, which is the signal to
        delete the ``expectedFailure`` decorator above.
        """
        cli = CLIS_BY_KEY.get("hal_capabilities")
        if cli is None:
            self.skipTest("tools/hal_capabilities is not in this checkout")
        if not NETLIST.is_file() or not GATE_LIBRARY.is_file():
            self.skipTest("the example netlist or the gate library is missing")
        if not os.environ.get("HAL_PY_PATH"):
            self.skipTest("no HAL_PY_PATH: this test needs a built HAL")
        result = run_cli(
            cli,
            ["list", "--netlist", str(NETLIST), "--gate-library", str(GATE_LIBRARY)],
        )
        self.assert_no_traceback(result)
        self.assertEqual(0, result.returncode, "the run failed" + result.report())
        self.assert_no_hal_log_lines(
            result.stdout,
            "a netlist-loading run without --json (issue #53)" + result.report(),
        )


# ---------------------------------------------------------------------------
# wiring the per-CLI tests onto the contract classes
# ---------------------------------------------------------------------------


def _attach_per_cli_tests():
    for cli in CLIS:
        setattr(HelpContract, "test_help_{}".format(cli.key), _make_help_test(cli))
        setattr(
            BadArgumentContract,
            "test_unknown_option_{}".format(cli.key),
            _make_unknown_option_test(cli),
        )
        setattr(
            BadArgumentContract,
            "test_unknown_subcommand_{}".format(cli.key),
            _make_unknown_subcommand_test(cli),
        )
        args = JSON_INVOCATIONS.get(cli.key)
        if args:
            setattr(
                JsonPurityContract,
                "test_json_purity_{}".format(cli.key),
                _make_json_test(cli, args),
            )
        known = KNOWN_JSON_FAILURES.get(cli.key)
        if known:
            args, reason = known
            test = _make_json_test(cli, args, needs_build=True)
            test.__doc__ = "{}: known --json violation -- {}".format(cli.display, reason)
            setattr(
                JsonPurityContract,
                "test_json_purity_{}".format(cli.key),
                unittest.expectedFailure(test),
            )


_attach_per_cli_tests()


def print_inventory(stream=sys.stderr):
    stream.write("repository:   {}\n".format(REPO_ROOT))
    stream.write(
        "enrolled CLIs ({}): {}\n".format(
            len(CLIS), ", ".join(cli.display for cli in CLIS)
        )
    )
    stream.write("exempt ({}): {}\n".format(len(EXEMPT), ", ".join(sorted(EXEMPT))))
    stream.write(
        "--json covered: {}\n".format(", ".join(sorted(JSON_INVOCATIONS)) or "none")
    )
    stream.write(
        "--json known-failing: {}\n".format(", ".join(sorted(KNOWN_JSON_FAILURES)) or "none")
    )
    stream.write("--json skipped: {}\n".format(", ".join(sorted(SKIPPED_JSON)) or "none"))
    if UNCLASSIFIED:
        stream.write("UNCLASSIFIED: {}\n".format(", ".join(sorted(UNCLASSIFIED))))
    stream.write("\n")
    stream.flush()


if __name__ == "__main__":
    print_inventory()
    unittest.main(verbosity=2)
