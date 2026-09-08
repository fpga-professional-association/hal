option(PL_GEXF_WRITER "PL_GEXF_WRITER" ON)
if(PL_GEXF_WRITER OR BUILD_ALL_PLUGINS)
    file(GLOB_RECURSE GEXF_WRITER_INC ${CMAKE_CURRENT_SOURCE_DIR}/include/*.h)
    file(GLOB_RECURSE GEXF_WRITER_SRC ${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp)

    hal_add_plugin(gexf_writer
                   SHARED
                   HEADER ${GEXF_WRITER_INC}
                   SOURCES ${GEXF_WRITER_SRC}
                   )
endif()
