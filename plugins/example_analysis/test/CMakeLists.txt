if(BUILD_TESTS)
    include_directories(${gtest_SOURCE_DIR}/include ${gtest_SOURCE_DIR} ${CMAKE_SOURCE_DIR}/include ${CMAKE_SOURCE_DIR}/tests ${CMAKE_SOURCE_DIR}/plugins/example_analysis/include)

    add_executable(runTest-example_analysis example_analysis.cpp)

    target_link_libraries(runTest-example_analysis example_analysis pthread gtest hal::core hal::netlist test_utils)

    add_test(runTest-example_analysis ${CMAKE_BINARY_DIR}/bin/hal_plugins/runTest-example_analysis --gtest_output=xml:${CMAKE_BINARY_DIR}/gtestresults-runTest-example_analysis.xml)

    if(${CMAKE_BUILD_TYPE} STREQUAL "Debug")
        add_sanitizers(runTest-example_analysis)
    endif()
endif()
