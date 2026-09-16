# The round-trip corpus suite re-imports every walkthrough netlist, writes it back out with the
# Verilog writer and serializes it to a .hal project, and requires the result to be the same circuit
# each time -- against the published, check.py-verified facts in corpus.json rather than against
# numbers it measured itself. The two netlists HAL can import but not yet save and reload (#54) are
# expectedFailure, so this suite too goes loudly red on the day that bug is fixed and the guards
# need promoting to real assertions. Until it was registered here (issue #72) nothing ran it: the
# Verilog round-trip fixes of #59/#60/#61 landed without CI ever executing the suite written to
# check them.
#
# It needs a built HAL -- test_roundtrip.py exits nonzero when hal_py is missing instead of
# skipping -- and it imports hal_plugins.graph_algorithm for the feedback-structure invariants, so
# it is registered only when that plugin is in the build. PL_GRAPH_ALGORITHM is ON by default.
if(PL_GRAPH_ALGORITHM OR BUILD_ALL_PLUGINS)
    add_test(NAME runTest-roundtrip_corpus
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/test_roundtrip.py
             WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
    set_tests_properties(runTest-roundtrip_corpus PROPERTIES
                         ENVIRONMENT "PYTHONPATH=${CMAKE_BINARY_DIR}/lib;HAL_PY_PATH=${CMAKE_BINARY_DIR}/lib;HAL_BASE_PATH=${CMAKE_BINARY_DIR}")
endif()
