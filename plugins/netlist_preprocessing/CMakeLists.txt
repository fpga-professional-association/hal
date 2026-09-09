#Option moved to plugins/CMakeLists.txt since dependencies need to be build as well
#option(PL_NETLIST_PREPROCESSING "PL_NETLIST_PREPROCESSING" ON)

if(PL_NETLIST_PREPROCESSING OR BUILD_ALL_PLUGINS)
    file(GLOB_RECURSE NETLIST_PREPROCESSING_INC ${CMAKE_CURRENT_SOURCE_DIR}/include/*.h)
    file(GLOB_RECURSE NETLIST_PREPROCESSING_SRC ${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp)
    file(GLOB_RECURSE NETLIST_PREPROCESSING_PYTHON_SRC ${CMAKE_CURRENT_SOURCE_DIR}/python/*.cpp)

    # The resynthesis plugin is optional: it is only needed by the resynthesis-based mux
    # optimizations. When it is not built (-DPL_RESYNTHESIS=OFF), this plugin is compiled without
    # HAL_WITH_RESYNTHESIS and those entry points return an error instead of failing to compile.
    # The definition is PUBLIC so that the test suite sees the same feature set as the plugin.
    set(NETLIST_PREPROCESSING_LIBS ${Z3_LIBRARIES} z3_utils OpenMP::OpenMP_CXX nlohmann_json::nlohmann_json)
    set(NETLIST_PREPROCESSING_DEFS "")
    if(HAL_BUILD_RESYNTHESIS)
        list(APPEND NETLIST_PREPROCESSING_LIBS resynthesis)
        set(NETLIST_PREPROCESSING_DEFS PUBLIC HAL_WITH_RESYNTHESIS)
    else()
        message(STATUS "netlist_preprocessing: building without resynthesis support, netlist_preprocessing::manual_mux_optimizations will report an error when called")
    endif()

    hal_add_plugin(netlist_preprocessing
        SHARED
        HEADER ${NETLIST_PREPROCESSING_INC}
        SOURCES ${NETLIST_PREPROCESSING_SRC} ${NETLIST_PREPROCESSING_PYTHON_SRC}
        PYDOC SPHINX_DOC_INDEX_FILE ${CMAKE_CURRENT_SOURCE_DIR}/documentation/netlist_preprocessing.rst
        DEFINITIONS ${NETLIST_PREPROCESSING_DEFS}
        LINK_LIBRARIES PUBLIC ${NETLIST_PREPROCESSING_LIBS}
    )

    add_subdirectory(test)
endif()
