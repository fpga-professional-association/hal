# The two fuzz harnesses are property tests over randomly generated inputs -- random Verilog
# netlists that must survive parse -> write -> parse unchanged, and random BooleanFunction trees
# that must survive print/parse, simplify() and double negation with their truth tables intact.
# Both are deterministic: 25 fixed seeds each, never the clock, plus a set of minimized KNOWN_BUGS
# repros that are re-run on every invocation. Each exits 0 while every failure it sees is an
# expected one and 1 on anything else, printing a one-line repro command, so ctest's verdict is the
# harness' own and an unexpected finding is a red run rather than a note in a log.
#
# They are not slow -- measured against a Debug build in the ubuntu:24.04 bench container,
# fuzz_boolean_function takes 13.5 s (the 2**n truth-table sweeps) and fuzz_verilog_roundtrip
# 0.4 s, which is the same order as the deterministic tiers next door. What sets them apart is that
# they are a *search*: the default sweep is one sample of a space that issue #51 wants explored with
# a seed matrix plus a fresh random seed every night, and a finding from a random seed is a new bug
# report rather than a regression in the change under test.
#
# So the split is by *where*, not by whether they run at all:
#
#   * BUILD_FUZZ_TESTS defaults to OFF, which keeps the harnesses out of a plain -DBUILD_TESTS=ON
#     build and off the platforms where nothing has ever run them (Ubuntu 22.04/26.04, macOS,
#     arm64) -- a first run of a fuzz harness belongs in a job someone is watching, not in the gate
#     of an unrelated pull request;
#   * .github/workflows/ubuntu24.04.yml configures with -DBUILD_FUZZ_TESTS=ON, so the fixed-seed
#     sweep does run on every push, on the one platform whose result is known (this file and that
#     flag were added together, with both harnesses green there);
#   * issue #51's nightly is what widens that to a seed matrix and a random seed, with
#     -DBUILD_FUZZ_TESTS=ON and the label below. The nightly workflow itself is not implemented
#     here.
#
#   cmake ... -DBUILD_TESTS=ON -DBUILD_FUZZ_TESTS=ON && ctest -L fuzz
#
# Running them by hand needs no CMake at all:
#
#   HAL_BASE_PATH=<build> PYTHONPATH=<build>/lib python3 tests/fuzz/fuzz_verilog_roundtrip.py
#   FUZZ_SEED=12345 ... python3 tests/fuzz/fuzz_boolean_function.py   # one-off repro
#   FUZZ_ITERS=200  ... python3 tests/fuzz/fuzz_boolean_function.py   # deeper sweep
if(BUILD_FUZZ_TESTS)
    # Needs hal_py only -- the trees are built through the BooleanFunction constructors, no netlist
    # and no parser plugin is involved.
    add_test(NAME runTest-fuzz_boolean_function
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/fuzz_boolean_function.py
             WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})

    # Needs the Verilog parser and writer and the HGL parser as well: it generates netlists over
    # tests/test_utils/gate_libraries/test.hgl, the library the Verilog parser's own gtest suite
    # uses. All three plugins are ON by default.
    add_test(NAME runTest-fuzz_verilog_roundtrip
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/fuzz_verilog_roundtrip.py
             WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})

    # The timeout is a bound on a runaway sweep (FUZZ_ITERS, a pathological generated netlist), not
    # an estimate of the runtime.
    set_tests_properties(runTest-fuzz_boolean_function runTest-fuzz_verilog_roundtrip PROPERTIES
                         LABELS "fuzz"
                         TIMEOUT 3600
                         ENVIRONMENT "PYTHONPATH=${CMAKE_BINARY_DIR}/lib;HAL_PY_PATH=${CMAKE_BINARY_DIR}/lib;HAL_BASE_PATH=${CMAKE_BINARY_DIR}")
endif()
