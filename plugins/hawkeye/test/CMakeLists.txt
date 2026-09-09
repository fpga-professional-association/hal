if(BUILD_TESTS)
    include_directories(${gtest_SOURCE_DIR}/include ${gtest_SOURCE_DIR} ${CMAKE_SOURCE_DIR}/include ${CMAKE_SOURCE_DIR}/tests ${CMAKE_SOURCE_DIR}/plugins/hawkeye/include ${CMAKE_SOURCE_DIR}/plugins/hawkeye/src)

    add_executable(runTest-hawkeye hawkeye.cpp)

    # the set of small integers has one implementation per instruction set, selected from the macros the
    # compiler defines for the target: without the same '-march' the plugin is built with, the test would
    # compare the portable implementation against itself instead of against the backend that ships
    target_compile_options(runTest-hawkeye PRIVATE "-march=native")

    target_link_libraries(runTest-hawkeye hawkeye pthread gtest hal::core hal::netlist test_utils)

    add_test(runTest-hawkeye ${CMAKE_BINARY_DIR}/bin/hal_plugins/runTest-hawkeye --gtest_output=xml:${CMAKE_BINARY_DIR}/gtestresults-runTest-hawkeye.xml)

    if(${CMAKE_BUILD_TYPE} STREQUAL "Debug")
        add_sanitizers(runTest-hawkeye)
    endif()
endif()
