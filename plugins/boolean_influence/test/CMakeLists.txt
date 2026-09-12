if(BUILD_TESTS)
    include_directories(${gtest_SOURCE_DIR}/include ${gtest_SOURCE_DIR} ${CMAKE_SOURCE_DIR}/include ${CMAKE_SOURCE_DIR}/tests ${CMAKE_SOURCE_DIR}/plugins/boolean_influence/include)

    add_executable(runTest-boolean_influence boolean_influence.cpp)

    target_link_libraries(runTest-boolean_influence boolean_influence pthread gtest hal::core hal::netlist test_utils)

    add_test(runTest-boolean_influence ${CMAKE_BINARY_DIR}/bin/hal_plugins/runTest-boolean_influence --gtest_output=xml:${CMAKE_BINARY_DIR}/gtestresults-runTest-boolean_influence.xml)

    if(${CMAKE_BUILD_TYPE} STREQUAL "Debug")
        add_sanitizers(runTest-boolean_influence)
    endif()
endif()
