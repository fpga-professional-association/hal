#!/usr/bin/env python3
"""Check that ``hal --python-script`` composes with HAL's own project loading.

A UI-plugin flag used to take over ``main()`` before any project argument was looked at, so
``hal --project-dir <project> --python-script script.py`` ran the script against nothing and every
script that touched a netlist died on ``NameError: name 'netlist' is not defined``. For a headless
HAL that is the front door, so this checks the whole path: the project is opened and the netlist is
parsed first, the netlist reaches the script under the name ``netlist``, what the script changes is
written back to the project, and a run that cannot load what was asked for fails before the script
gets to run at all.

The cases that must keep working unchanged are here too: a script started without any project
argument still gets a bare interpreter with no ``netlist`` bound, ``--python-args`` still becomes
``sys.argv``, and the exit code still tells the truth (see script_exit_code_test.py and issue #11).

What is under test spans the process -- argument handling, plugin dispatch and the interpreter -- so
every case runs the real ``hal`` binary in a subprocess.

Usage: project_context_test.py <hal-binary> <fixture-directory> <netlist> <gate-library>
"""

import os
import subprocess
import sys
import tempfile

TIMEOUT_SECONDS = 600


def run_hal(hal_binary, arguments, workdir):
    """Runs hal with the given arguments and returns (returncode, combined output)."""
    result = subprocess.run(
        [hal_binary] + arguments,
        capture_output=True,
        cwd=workdir,
        env=os.environ.copy(),
        timeout=TIMEOUT_SECONDS,
    )
    output = result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")
    return result.returncode, output


class Checker:
    """Collects the outcome of the cases so that all of them run before anything is reported."""

    def __init__(self, hal_binary, workdir):
        self.hal_binary = hal_binary
        self.workdir = workdir
        self.failures = []
        self.case_count = 0

    def run(self, description, arguments, expect_success, expected=(), forbidden=()):
        """Runs one case and records what it got wrong, if anything. Returns the output."""
        self.case_count += 1

        try:
            returncode, output = run_hal(self.hal_binary, arguments, self.workdir)
        except subprocess.TimeoutExpired:
            print("  {}: FAILED".format(description))
            self.failures.append((description, ["did not finish within {}s".format(TIMEOUT_SECONDS)], ""))
            return ""

        problems = []

        if returncode < 0:
            problems.append("terminated by signal {}".format(-returncode))
        elif expect_success and returncode != 0:
            problems.append("expected exit code 0 but got {}".format(returncode))
        elif not expect_success and returncode == 0:
            problems.append("expected a nonzero exit code but the run reported success")

        for needle in expected:
            if needle not in output:
                problems.append("expected {!r} in the output".format(needle))

        for needle in forbidden:
            if needle in output:
                problems.append("did not expect {!r} in the output".format(needle))

        print("  {}: {}".format(description, "ok" if not problems else "FAILED"))
        if problems:
            self.failures.append((description, problems, output[-3000:]))

        return output


def main():
    if len(sys.argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2

    hal_binary, fixture_dir, netlist_path, gate_library_path = sys.argv[1:5]

    for path, what in (
        (hal_binary, "the hal binary"),
        (fixture_dir, "the fixture directory"),
        (netlist_path, "the netlist"),
        (gate_library_path, "the gate library"),
    ):
        if not os.path.exists(path):
            print("{} '{}' does not exist -- build HAL before running this test".format(what, path), file=sys.stderr)
            return 2

    def script(name):
        return os.path.join(fixture_dir, name)

    with tempfile.TemporaryDirectory(prefix="hal-python-project-") as workdir:
        checker = Checker(hal_binary, workdir)
        project_dir = os.path.join(workdir, "imported_project")

        # importing a netlist and running a script against it in one call is the case the fix is for
        output = checker.run(
            "--import-netlist hands the parsed netlist to the script",
            [
                "--import-netlist",
                netlist_path,
                "--gate-library",
                gate_library_path,
                "--project-dir",
                project_dir,
                "--python-script",
                script("reports_netlist.py"),
            ],
            expect_success=True,
            expected=["NETLIST=present", "IS_HAL_NETLIST=True", "NETLIST_TYPE=Netlist"],
            forbidden=["NETLIST=absent", "NameError"],
        )

        # the netlist has to be the imported one, not an empty stand-in
        gate_count = 0
        for line in output.splitlines():
            if line.startswith("GATE_COUNT="):
                gate_count = int(line.split("=", 1)[1])
        if gate_count <= 0:
            checker.failures.append(
                ("--import-netlist hands the parsed netlist to the script", ["the netlist reported {} gates".format(gate_count)], output[-3000:])
            )

        checker.run(
            "--project-dir reopens the project for the script",
            ["--project-dir", project_dir, "--python-script", script("reports_netlist.py")],
            expect_success=True,
            expected=["NETLIST=present", "GATE_COUNT={}".format(gate_count)],
            forbidden=["NETLIST=absent", "NameError"],
        )

        checker.run(
            "--python-args still becomes sys.argv when a project is loaded",
            [
                "--project-dir",
                project_dir,
                "--python-script",
                script("reports_netlist.py"),
                "--python-args",
                "alpha beta",
            ],
            expect_success=True,
            expected=["NETLIST=present", "ARGV=alpha,beta"],
        )

        checker.run(
            "a script without a project argument gets no netlist",
            ["--python-script", script("requires_no_netlist.py")],
            expect_success=True,
            expected=["NETLIST=absent"],
            forbidden=["NETLIST=present"],
        )

        checker.run(
            "a script that raises still fails the run with a project loaded",
            ["--project-dir", project_dir, "--python-script", script("raises_uncaught.py")],
            expect_success=False,
            expected=["deliberate failure from the python shell regression fixture", "RuntimeError"],
        )

        checker.run(
            "a project that cannot be opened fails before the script runs",
            [
                "--project-dir",
                os.path.join(workdir, "there_is_no_such_project"),
                "--python-script",
                script("reports_netlist.py"),
            ],
            expect_success=False,
            expected=["Cannot open project"],
            forbidden=["NETLIST=present", "NETLIST=absent"],
        )

        checker.run(
            "a netlist that cannot be read fails before the script runs",
            [
                "--import-netlist",
                os.path.join(workdir, "there_is_no_such_netlist.v"),
                "--gate-library",
                gate_library_path,
                "--project-dir",
                os.path.join(workdir, "project_without_a_netlist"),
                "--python-script",
                script("reports_netlist.py"),
            ],
            expect_success=False,
            forbidden=["NETLIST=present", "NETLIST=absent"],
        )

        # a run that was asked not to write anything must not write the script's changes either
        checker.run(
            "--volatile-mode runs the script but keeps its changes out of the project",
            [
                "--project-dir",
                project_dir,
                "--volatile-mode",
                "--python-script",
                script("renames_top_module.py"),
                "--python-args",
                "volatile_only_name",
            ],
            expect_success=True,
            expected=["TOP_MODULE=volatile_only_name"],
        )

        checker.run(
            "the next run does not see the change from the volatile run",
            ["--project-dir", project_dir, "--python-script", script("reports_netlist.py")],
            expect_success=True,
            expected=["NETLIST=present"],
            forbidden=["TOP_MODULE=volatile_only_name"],
        )

        # what the script changed otherwise has to survive the run, the same way a cli plugin's changes do
        checker.run(
            "what the script changes is written back to the project",
            [
                "--project-dir",
                project_dir,
                "--python-script",
                script("renames_top_module.py"),
                "--python-args",
                "renamed_by_the_script",
            ],
            expect_success=True,
            expected=["TOP_MODULE=renamed_by_the_script"],
        )

        checker.run(
            "the next run sees what the previous script changed",
            ["--project-dir", project_dir, "--python-script", script("reports_netlist.py")],
            expect_success=True,
            expected=["TOP_MODULE=renamed_by_the_script"],
        )

    if checker.failures:
        print(
            "\n{} of {} case(s) reported the wrong result:\n".format(len(checker.failures), checker.case_count),
            file=sys.stderr,
        )
        for description, problems, output in checker.failures:
            print("--- {} ---".format(description), file=sys.stderr)
            for problem in problems:
                print("    {}".format(problem), file=sys.stderr)
            print(output, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
