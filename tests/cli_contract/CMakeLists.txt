# The CLI contract suite discovers every entry point under tools/ from the checkout and holds each
# one to the same contract: --help exits 0, garbage arguments exit nonzero with an error message and
# no traceback, and a --json run writes a document with zero preamble bytes. Those are the bugs
# issue #50 was opened about, and the KNOWN_JSON_FAILURES dict is empty today only because #66 fixed
# the last entry -- a claim nothing verified until this registration (issue #72).
#
# The suite has two halves and tells them apart itself, by looking at HAL_PY_PATH:
#
#   * the half that needs no build at all -- the inventory, --help and bad-argument contracts over
#     every tool, and the --json invocations that never load a netlist. .github/workflows/ubuntu*.yml
#     run that half *before* install_dependencies.sh with the HAL variables cleared, in the same
#     "no HAL" tier as the walkthrough checks, so a broken CLI fails in seconds instead of an hour
#     into the job;
#   * the half that loads a netlist through a tool, which skips itself when HAL_PY_PATH is unset.
#
# This registration is the "built HAL" side of that same split: ctest runs in a build tree, so
# pointing HAL_PY_PATH at it is what makes the skipped half run. Setting HAL_BASE_PATH as well is
# not optional -- hal aborts with "cannot determine base path" when it is neither installed nor told
# where the build tree is, and every CLI this suite spawns is a fresh interpreter that inherits only
# what is set here.
add_test(NAME runTest-cli_contract
         COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/test_cli_contract.py
         WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
set_tests_properties(runTest-cli_contract PROPERTIES
                     ENVIRONMENT "PYTHONPATH=${CMAKE_BINARY_DIR}/lib;HAL_PY_PATH=${CMAKE_BINARY_DIR}/lib;HAL_BASE_PATH=${CMAKE_BINARY_DIR}")
