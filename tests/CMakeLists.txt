# project(tests)

# Download and unpack googletest at configure time
configure_file(googletest-CMakeLists.txt.in "${CMAKE_BINARY_DIR}/googletest-download/CMakeLists.txt")
execute_process(COMMAND "${CMAKE_COMMAND}" -G "${CMAKE_GENERATOR}" .
    WORKING_DIRECTORY "${CMAKE_BINARY_DIR}/googletest-download")
execute_process(COMMAND "${CMAKE_COMMAND}" --build .
    WORKING_DIRECTORY "${CMAKE_BINARY_DIR}/googletest-download")

# Prevent GoogleTest from overriding our compiler/linker options
# when building with Visual Studio
set(gtest_force_shared_crt ON CACHE BOOL "" FORCE)

# Add googletest directly to our build. This adds
# the following targets: gtest, gtest_main, gmock
# and gmock_main
add_subdirectory("${CMAKE_BINARY_DIR}/googletest-src"
    "${CMAKE_BINARY_DIR}/googletest-build")

# The gtest/gmock targets carry header search path
# dependencies automatically when using CMake 2.8.11 or
# later. Otherwise we have to add them here ourselves.
if(CMAKE_VERSION VERSION_LESS 2.8.11)
    include_directories("${gtest_SOURCE_DIR}/include"
        "${gmock_SOURCE_DIR}/include")
endif()

# Now simply link your own targets against gtest, gmock,
# etc. as appropriate
add_subdirectory(test_utils)
add_subdirectory(core)
add_subdirectory(netlist)
add_subdirectory(netlist_parser)
add_subdirectory(netlist_writer)
add_subdirectory(python_bindings)
add_subdirectory(python_shell)
add_subdirectory(headless_smoke)
add_subdirectory(packaging)

# The Python tiers of issues #49, #50 and #52-#57. All three encode currently-broken behaviour as
# expected failures and are therefore designed to go red when a bug is fixed and the marker has to
# come off -- which only works if something runs them, and until issue #72 nothing did. They are
# deterministic and run on every push, here, alongside the C++ suites.
add_subdirectory(acceptance)
add_subdirectory(cli_contract)
add_subdirectory(roundtrip)

# The fuzz tier is a search rather than a fixed set of assertions, so it is registered only with
# -DBUILD_FUZZ_TESTS=ON and labelled "fuzz": the Ubuntu 24.04 workflow turns it on and runs the
# fixed-seed sweep on every push, and issue #51's nightly is what widens it to a seed matrix. See
# tests/fuzz/CMakeLists.txt for the reasoning and the measured runtimes.
add_subdirectory(fuzz)
