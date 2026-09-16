# The acceptance suite says what "done" means for the feature issues it covers, in the form the
# reviewer of each issue checks, and it does that with unittest.expectedFailure: an issue that is
# still open is an expected failure, and the day the feature lands unittest reports an *unexpected
# success*, which is a failing run. That mechanism is worth exactly as much as the number of times
# the suite is executed, which until this registration was zero (issue #72).
#
# It needs a built HAL and cannot be run any other way: tests/acceptance/test_feature_acceptance.py
# raises rather than skips when hal_py is missing, deliberately, because an acceptance suite that
# quietly turns itself off would let the features it guards rot unnoticed. ctest only ever runs in a
# build tree, so the environment below is always the right one -- the same PYTHONPATH/HAL_BASE_PATH
# pair tests/python_bindings uses, plus HAL_PY_PATH, which the suite reads on its own.
#
# The #55 test class imports hal_plugins.module_identification and, for the same reason, fails
# rather than skips when it is absent -- so the suite is only registered when that plugin is part of
# the build. PL_MODULE_IDENTIFICATION is OFF by default; every CI workflow configures with
# BUILD_ALL_PLUGINS=ON, so this runs everywhere CI runs ctest.
if(PL_MODULE_IDENTIFICATION OR BUILD_ALL_PLUGINS)
    add_test(NAME runTest-feature_acceptance
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/test_feature_acceptance.py
             WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
    set_tests_properties(runTest-feature_acceptance PROPERTIES
                         ENVIRONMENT "PYTHONPATH=${CMAKE_BINARY_DIR}/lib;HAL_PY_PATH=${CMAKE_BINARY_DIR}/lib;HAL_BASE_PATH=${CMAKE_BINARY_DIR}")
endif()
