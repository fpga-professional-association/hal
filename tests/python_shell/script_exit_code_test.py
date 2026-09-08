#!/usr/bin/env python3
"""Check that ``hal --python-script`` tells the truth with its exit code.

The python shell plugin used to throw away the result of every ``PyRun_SimpleString`` call and to
return a falsy value at the end of a successful run, while ``main.cpp`` turned a falsy plugin result
into a zero exit code. A script that raised and a script path that did not exist therefore both left
HAL reporting success, which is the worst possible answer for automated analysis: a pipeline cannot
tell a finished run from a broken one.

What is under test is how the process ends, so every case runs the real ``hal`` binary in a
subprocess and looks at its exit status. The cases run in a scratch directory because HAL writes a
log file next to wherever it is started.

Usage: script_exit_code_test.py <hal-binary> <fixture-directory>
"""

import os
import subprocess
import sys
import tempfile

TIMEOUT_SECONDS = 300


def run_hal(hal_binary, script_argument, workdir):
    """Runs hal on one --python-script argument and returns (returncode, combined output)."""
    result = subprocess.run(
        [hal_binary, "--python-script", script_argument],
        capture_output=True,
        cwd=workdir,
        env=os.environ.copy(),
        timeout=TIMEOUT_SECONDS,
    )
    output = result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")
    return result.returncode, output


def check(description, returncode, output, expect_success, expected_output):
    """Compares one run against its expectation and returns a failure description or None."""
    problems = []

    if returncode < 0:
        problems.append(f"terminated by signal {-returncode}")
    elif expect_success and returncode != 0:
        problems.append(f"expected exit code 0 but got {returncode}")
    elif not expect_success and returncode == 0:
        problems.append("expected a nonzero exit code but the run reported success")

    for needle in expected_output:
        if needle not in output:
            problems.append(f"expected {needle!r} in the output")

    print(f"  {description}: {'ok' if not problems else 'FAILED'}")
    if not problems:
        return None
    return description, problems, output[-2000:]


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2

    hal_binary, fixture_dir = sys.argv[1], sys.argv[2]

    if not os.path.isfile(hal_binary):
        print(f"the hal binary '{hal_binary}' does not exist -- build it before running this test", file=sys.stderr)
        return 2

    cases = [
        # (description, --python-script argument, expect success, substrings the output must contain)
        (
            "a script that returns normally exits 0",
            os.path.join(fixture_dir, "exits_cleanly.py"),
            True,
            ["regression fixture ran against hal_py"],
        ),
        (
            "an uncaught exception fails the run",
            os.path.join(fixture_dir, "raises_uncaught.py"),
            False,
            ["deliberate failure from the python shell regression fixture", "RuntimeError"],
        ),
        (
            "a nonexistent script path fails the run",
            os.path.join(fixture_dir, "there_is_no_such_script.py"),
            False,
            ["is not a python script file"],
        ),
        (
            "a script path that is a directory fails the run",
            fixture_dir,
            False,
            ["is not a python script file"],
        ),
        (
            "a script path that is not a .py file fails the run",
            os.path.join(fixture_dir, "not_a_python_file.txt"),
            False,
            ["is not a python script file"],
        ),
    ]

    failures = []
    with tempfile.TemporaryDirectory(prefix="hal-python-exit-code-") as workdir:
        for description, script_argument, expect_success, expected_output in cases:
            try:
                returncode, output = run_hal(hal_binary, script_argument, workdir)
            except subprocess.TimeoutExpired:
                print(f"  {description}: FAILED")
                failures.append((description, [f"did not finish within {TIMEOUT_SECONDS}s"], ""))
                continue

            failure = check(description, returncode, output, expect_success, expected_output)
            if failure is not None:
                failures.append(failure)

    if failures:
        print(f"\n{len(failures)} of {len(cases)} case(s) reported the wrong result:\n", file=sys.stderr)
        for description, problems, output in failures:
            print(f"--- {description} ---", file=sys.stderr)
            for problem in problems:
                print(f"    {problem}", file=sys.stderr)
            print(output, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
