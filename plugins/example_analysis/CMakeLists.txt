option(PL_EXAMPLE_ANALYSIS "PL_EXAMPLE_ANALYSIS" OFF)

if(PL_EXAMPLE_ANALYSIS OR BUILD_ALL_PLUGINS)
    file(GLOB_RECURSE EXAMPLE_ANALYSIS_INC ${CMAKE_CURRENT_SOURCE_DIR}/include/*.h)
    file(GLOB_RECURSE EXAMPLE_ANALYSIS_SRC ${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp)
    file(GLOB_RECURSE EXAMPLE_ANALYSIS_PYTHON_SRC ${CMAKE_CURRENT_SOURCE_DIR}/python/*.cpp)

    # capabilities.json is the single source of truth for what this plugin needs (see
    # tools/hal_capabilities). It is used twice:
    #
    #   * compiled into the plugin, so a *loaded* plugin can be asked directly and the discovery
    #     command can tell a stale build from a stale declaration;
    #   * copied into the build tree, so the same command works without loading anything at all.
    #
    # Generating the header from the JSON is what keeps those two from drifting apart. The JSON is
    # embedded in a raw string literal delimited by `caps`, so it must not contain the sequence
    # `)caps"` -- nothing else about it is special.
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/capabilities.json)
    file(READ ${CMAKE_CURRENT_SOURCE_DIR}/capabilities.json EXAMPLE_ANALYSIS_CAPABILITIES_JSON)
    file(WRITE ${CMAKE_CURRENT_BINARY_DIR}/generated/example_analysis/capabilities_generated.h
"#pragma once
// Generated from plugins/example_analysis/capabilities.json at configure time. Do not edit.
namespace hal
{
    namespace example_analysis
    {
        constexpr const char* CAPABILITIES_JSON = R\"caps(${EXAMPLE_ANALYSIS_CAPABILITIES_JSON})caps\";
    }    // namespace example_analysis
}    // namespace hal
")

    file(MAKE_DIRECTORY ${CMAKE_BINARY_DIR}/share/hal/plugin_capabilities/)
    configure_file(${CMAKE_CURRENT_SOURCE_DIR}/capabilities.json
                   ${CMAKE_BINARY_DIR}/share/hal/plugin_capabilities/example_analysis.json COPYONLY)
    install(FILES ${CMAKE_CURRENT_SOURCE_DIR}/capabilities.json
            DESTINATION ${SHARE_INSTALL_DIRECTORY}/plugin_capabilities RENAME example_analysis.json)

    hal_add_plugin(example_analysis
                   SHARED
                   HEADER ${EXAMPLE_ANALYSIS_INC}
                   SOURCES ${EXAMPLE_ANALYSIS_SRC} ${EXAMPLE_ANALYSIS_PYTHON_SRC}
                   INCLUDES PUBLIC $<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}/generated>
                   )

    add_subdirectory(test)
endif()
