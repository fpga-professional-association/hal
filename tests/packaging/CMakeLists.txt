# The install rules are generated code, and the two things that went wrong with them -- the ldconfig
# step running before the libraries were installed, and it writing outside DESTDIR -- are both visible
# in what CMake generates. Checking that here means ctest catches a regression instead of a packager
# discovering it, and it costs a fraction of a second: no install is performed.
add_test(NAME runTest-install_rules
         COMMAND ${Python3_EXECUTABLE} -m unittest discover
                 -s ${CMAKE_CURRENT_SOURCE_DIR} -t ${CMAKE_CURRENT_SOURCE_DIR} -p "test_install_rules.py"
         WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
set_tests_properties(runTest-install_rules PROPERTIES ENVIRONMENT "HAL_BUILD_DIR=${CMAKE_BINARY_DIR}")

# Whether the resynthesis plugin is built is decided by cmake/hal_resynthesis_option.cmake, which is a
# module of its own so that every combination of the option can be configured in isolation rather than
# by building HAL once per combination (see issue #38).
add_test(NAME runTest-resynthesis_option
         COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/test_resynthesis_option.py
         WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})

# install_dependencies.sh decides what a build environment contains, and its Ubuntu and docker
# branches are two package lists that drift apart silently (see issue #30).
add_test(NAME runTest-install_dependencies
         COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_install_dependencies.py
         WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
