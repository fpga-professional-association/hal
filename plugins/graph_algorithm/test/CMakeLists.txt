if(BUILD_TESTS)
    include_directories(${gtest_SOURCE_DIR}/include ${gtest_SOURCE_DIR} ${CMAKE_SOURCE_DIR}/include ${CMAKE_SOURCE_DIR}/tests ${CMAKE_SOURCE_DIR}/plugins/graph_algorithm/include)

    add_executable(runTest-graph_algorithm graph_algorithm.cpp)

    target_link_libraries(runTest-graph_algorithm graph_algorithm pthread gtest hal::core hal::netlist test_utils)

    add_test(runTest-graph_algorithm ${CMAKE_BINARY_DIR}/bin/hal_plugins/runTest-graph_algorithm --gtest_output=xml:${CMAKE_BINARY_DIR}/gtestresults-runTest-graph_algorithm.xml)

    if(${CMAKE_BUILD_TYPE} STREQUAL "Debug")
        add_sanitizers(runTest-graph_algorithm)
    endif()
endif()
