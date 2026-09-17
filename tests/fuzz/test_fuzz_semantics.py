#!/usr/bin/env python3
"""Self-test for the fuzz tier's *verdict* -- not for the properties it checks.

The two harnesses next door are the only tests in this repository whose exit
code depends on a failure *not* happening: every entry in their ``KNOWN_BUGS``
and ``EXPECTED_FAILURE_SEEDS`` lists is an assertion that a specific defect is
still there, and an entry that stops failing means the marker outlived the bug.
Since the lists are empty-or-nearly-empty by design, nothing in an ordinary run
exercises that path -- it fires exactly once, on the day someone else's fix
silently makes a marker stale, which is the worst possible moment to discover
that it only printed a note and exited 0 (it did, until issue #51).

So this drives both harnesses with planted markers, in a subprocess each, and
asserts on the exit code and on the text:

    reproducing   a planted marker that still fails      -> exit 0
    fixed         a planted KNOWN_BUGS entry that passes -> exit 1, names the entry
    stale-seed    a planted EXPECTED_FAILURE_SEEDS seed
                  that passes                            -> exit 1, names the seed

The planted markers are patched into the imported module, so nothing is written
to the harness files and the real lists are untouched.

Usage
-----
    python3 tests/fuzz/test_fuzz_semantics.py

Needs hal_py on the path like the harnesses themselves (HAL_BASE_PATH /
HAL_PY_PATH / PYTHONPATH); ctest sets those when BUILD_FUZZ_TESTS is ON.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESSES = {
    "boolean": HERE / "fuzz_boolean_function.py",
    "verilog": HERE / "fuzz_verilog_roundtrip.py",
}

MARKER_ID = {"boolean": "SELFTEST-BF", "verilog": "SELFTEST-VL"}

# A gate whose name equals a net name is renamed by the Verilog writer, so this
# netlist fails the round trip's structure check -- the KB-4 shape, inlined so
# this test does not depend on KB-4 still being listed.
REPRODUCING_VERILOG = (
    "module m (a, c) ;\n"
    "  input a ; output c ; wire x ;\n"
    "INV x (\n"
    "    .I(a),\n"
    "    .O(x)\n"
    ") ;\n"
    "BUF b0 (\n"
    "    .I(x),\n"
    "    .O(c)\n"
    ") ;\n"
    "endmodule\n"
)

# ... and the same netlist with the collision removed round-trips cleanly, which
# is what a KNOWN_BUGS entry looks like once the bug behind it is gone.
CLEAN_VERILOG = REPRODUCING_VERILOG.replace("INV x (", "INV i0 (")

# One seed, few samples: this test is about the verdict, not about coverage.
CHILD_ENV = {"FUZZ_SEED": "1", "FUZZ_SAMPLES": "4"}

SELFTEST_SEED = 1


# --------------------------------------------------------------------------- #
# child: run one harness with a planted marker
# --------------------------------------------------------------------------- #


def _load(path):
    spec = importlib.util.spec_from_file_location("fuzz_harness_" + path.stem, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_child(harness, mode):
    module = _load(HARNESSES[harness])
    marker = MARKER_ID[harness]

    if mode == "stale-seed":
        module.KNOWN_BUGS = []
        module.EXPECTED_FAILURE_SEEDS = {SELFTEST_SEED: "planted by test_fuzz_semantics.py"}
    elif harness == "boolean":
        reproduces = mode == "reproducing"
        module.KNOWN_BUGS = [{
            "id": marker,
            "summary": "planted by test_fuzz_semantics.py",
            "probe": lambda: reproduces,
        }]
    else:
        module.KNOWN_BUGS = [{
            "id": marker,
            "summary": "planted by test_fuzz_semantics.py",
            "expect": "structure",
            "source": REPRODUCING_VERILOG if mode == "reproducing" else CLEAN_VERILOG,
        }]
    return module.main()


# --------------------------------------------------------------------------- #
# parent: the expectations
# --------------------------------------------------------------------------- #

# (harness, mode, expected exit code, substrings the output must contain)
CASES = [
    ("boolean", "reproducing", 0, ["SELFTEST-BF"]),
    ("boolean", "fixed", 1, ["SELFTEST-BF", "NO LONGER fail", "remove the named entry",
                             "fuzz_boolean_function.py"]),
    ("boolean", "stale-seed", 1, ["EXPECTED_FAILURE_SEEDS entry 1", "NO LONGER fail"]),
    ("verilog", "reproducing", 0, ["SELFTEST-VL"]),
    ("verilog", "fixed", 1, ["SELFTEST-VL", "NO LONGER fail", "remove the named entry",
                             "fuzz_verilog_roundtrip.py"]),
    ("verilog", "stale-seed", 1, ["EXPECTED_FAILURE_SEEDS entry 1", "NO LONGER fail"]),
]


def main():
    failures = []
    for harness, mode, expected_code, expected_text in CASES:
        label = "%s/%s" % (harness, mode)
        environment = dict(os.environ)
        environment.update(CHILD_ENV)
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child", harness, mode],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=environment, universal_newlines=True)
        output = completed.stdout or ""

        problems = []
        if completed.returncode != expected_code:
            problems.append("exit code %d, expected %d" % (completed.returncode, expected_code))
        for needle in expected_text:
            if needle not in output:
                problems.append("output does not mention %r" % needle)
        if problems:
            failures.append((label, problems, output))
            print("FAIL %s: %s" % (label, "; ".join(problems)))
        else:
            print("ok   %s: exit %d" % (label, completed.returncode))

    if failures:
        print("")
        for label, problems, output in failures:
            print("--- %s ---" % label)
            print(output)
        print("test_fuzz_semantics: %d of %d cases FAILED" % (len(failures), len(CASES)))
        return 1
    print("test_fuzz_semantics: OK (%d cases)" % len(CASES))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        sys.exit(run_child(sys.argv[2], sys.argv[3]))
    sys.exit(main())
