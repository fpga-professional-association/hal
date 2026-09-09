# PL_RESYNTHESIS and the derived HAL_BUILD_RESYNTHESIS are defined in plugins/CMakeLists.txt,
# because netlist_preprocessing links against this plugin and has to know whether it exists.
if(HAL_BUILD_RESYNTHESIS)

    file(GLOB_RECURSE RESYNTHESIS_INC ${CMAKE_CURRENT_SOURCE_DIR}/include/*.h)
    file(GLOB_RECURSE RESYNTHESIS_SRC ${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp)
    file(GLOB_RECURSE RESYNTHESIS_PYTHON_SRC ${CMAKE_CURRENT_SOURCE_DIR}/python/*.cpp)

    hal_add_plugin(resynthesis
                   SHARED
                   HEADER ${RESYNTHESIS_INC}
                   SOURCES ${RESYNTHESIS_SRC} ${RESYNTHESIS_PYTHON_SRC}
                   PYDOC SPHINX_DOC_INDEX_FILE ${CMAKE_CURRENT_SOURCE_DIR}/documentation/resynthesis.rst
                   LINK_LIBRARIES genlib_writer
                )
endif()
