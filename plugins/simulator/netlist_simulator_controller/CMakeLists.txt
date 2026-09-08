option(PL_NETLIST_SIMULATOR_CONTROLLER "PL_NETLIST_SIMULATOR_CONTROLLER" OFF)

if(PL_NETLIST_SIMULATOR_CONTROLLER OR PL_SIMULATOR OR BUILD_ALL_PLUGINS)

    enable_cxx_compile_option_if_supported("-O1" "Perf" "PUBLIC")
    enable_cxx_compile_option_if_supported("-g" "Perf" "PUBLIC")
    enable_cxx_compile_option_if_supported("-fno-inline-functions" "Perf" "PUBLIC")
    enable_cxx_compile_option_if_supported("-fno-inline-functions-called-once" "Perf" "PUBLIC")
    enable_cxx_compile_option_if_supported("-fno-optimize-sibling-calls" "Perf" "PUBLIC")


    file(GLOB_RECURSE NETLIST_SIMULATOR_CONTROLLER_INC ${CMAKE_CURRENT_SOURCE_DIR}/include/*.h)
    file(GLOB_RECURSE NETLIST_SIMULATOR_CONTROLLER_SRC ${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp)
    file(GLOB_RECURSE NETLIST_SIMULATOR_CONTROLLER_PYTHON_SRC ${CMAKE_CURRENT_SOURCE_DIR}/python/*.cpp)

    hal_add_plugin(netlist_simulator_controller
                   SHARED
                   #PYDOC SPHINX_DOC_INDEX_FILE ${CMAKE_CURRENT_SOURCE_DIR}/documentation/graph_algorithm.rst
                   HEADER ${NETLIST_SIMULATOR_CONTROLLER_INC}
                   SOURCES ${NETLIST_SIMULATOR_CONTROLLER_SRC} ${NETLIST_SIMULATOR_CONTROLLER_PYTHON_SRC}
                   PYDOC SPHINX_DOC_INDEX_FILE ${CMAKE_CURRENT_SOURCE_DIR}/documentation/netlist_simulator_controller.rst)

    # The standalone SALEAE parser sources get copied into the working directory of external
    # simulation engines (see SimulationEngine::install_saleae_parser) so that the engine can
    # compile them into its testbench. They used to be embedded as a Qt resource, now they are
    # installed as data files, analogous to how the gate library definitions are handled.
    set(SALEAE_PARSER_SOURCE_FILES
        ${CMAKE_CURRENT_SOURCE_DIR}/include/netlist_simulator_controller/saleae_parser.h
        ${CMAKE_CURRENT_SOURCE_DIR}/include/netlist_simulator_controller/saleae_file.h
        ${CMAKE_CURRENT_SOURCE_DIR}/include/netlist_simulator_controller/saleae_directory.h
        ${CMAKE_CURRENT_SOURCE_DIR}/src/saleae_parser.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/src/saleae_file.cpp
        ${CMAKE_CURRENT_SOURCE_DIR}/src/saleae_directory.cpp)

    set(SALEAE_PARSER_SHARE_DIRECTORY ${CMAKE_BINARY_DIR}/share/hal/saleae_parser)
    file(MAKE_DIRECTORY ${SALEAE_PARSER_SHARE_DIRECTORY})

    set(SALEAE_PARSER_BYPRODUCTS "")
    foreach(f ${SALEAE_PARSER_SOURCE_FILES})
        get_filename_component(fname ${f} NAME)
        list(APPEND SALEAE_PARSER_BYPRODUCTS ${SALEAE_PARSER_SHARE_DIRECTORY}/${fname})
    endforeach()

    add_custom_target(update_saleae_parser_sources ALL
                      BYPRODUCTS ${SALEAE_PARSER_BYPRODUCTS}
                      COMMAND ${CMAKE_COMMAND} -E copy_if_different ${SALEAE_PARSER_SOURCE_FILES} ${SALEAE_PARSER_SHARE_DIRECTORY}/
                      DEPENDS ${SALEAE_PARSER_SOURCE_FILES}
                      COMMENT "Update standalone SALEAE parser sources")

    add_dependencies(netlist_simulator_controller update_saleae_parser_sources)

    install(DIRECTORY ${SALEAE_PARSER_SHARE_DIRECTORY} DESTINATION ${SHARE_INSTALL_DIRECTORY})

    add_subdirectory(test)
    add_subdirectory(saleae_cli)

    if(${CMAKE_BUILD_TYPE} STREQUAL "Debug")
        add_sanitizers(netlist_simulator)
    endif()


endif()
