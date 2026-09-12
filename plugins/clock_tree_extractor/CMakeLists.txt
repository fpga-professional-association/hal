option(PL_CLOCK_TREE_EXTRACTOR "PL_CLOCK_TREE_EXTRACTOR" ON)

if(PL_CLOCK_TREE_EXTRACTOR OR BUILD_ALL_PLUGINS)

    if(IWYU)
        set(CMAKE_CXX_INCLUDE_WHAT_YOU_USE "include-what-you-use")
        message(STATUS "include-what-you-use turned ON")
    else()
        message(STATUS "include-what-you-use turned OFF")
    endif()

    file(GLOB_RECURSE CLOCK_TREE_EXTRACTOR_INC ${CMAKE_CURRENT_SOURCE_DIR}/include/*.h)
    file(GLOB_RECURSE CLOCK_TREE_EXTRACTOR_SRC ${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp)
    file(GLOB_RECURSE CLOCK_TREE_EXTRACTOR_PYTHON_SRC ${CMAKE_CURRENT_SOURCE_DIR}/python/*.cpp)

    # No COMPILE_OPTIONS: hal_add_plugin appends them to the INTERFACE section of
    # target_compile_options, so the "-march=native" that used to be here was never applied to this
    # plugin's own sources -- it was only handed to whoever links against it. That is nobody in the
    # build tree except the test below, where it would break the macOS build, as Apple's clang does
    # not accept "-march=native" on arm64.
    hal_add_plugin(clock_tree_extractor
                    SHARED
                    HEADER ${CLOCK_TREE_EXTRACTOR_INC}
                    SOURCES ${CLOCK_TREE_EXTRACTOR_SRC} ${CLOCK_TREE_EXTRACTOR_PYTHON_SRC}
                    LINK_LIBRARIES graph_algorithm
                )

    add_subdirectory(test)

endif()
