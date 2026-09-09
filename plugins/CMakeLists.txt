foreach(i IN ITEMS "" "_DEBUG" "_RELEASE" "_MINSIZEREL" "_RELWITHDEBINFO")
    foreach(j IN ITEMS "RUNTIME" "ARCHIVE" "LIBRARY")
        set(TMP_${j}_OUTPUT_DIR${i} ${CMAKE_${j}_OUTPUT_DIRECTORY${i}})
        set(CMAKE_${j}_OUTPUT_DIRECTORY${i} "${TMP_${j}_OUTPUT_DIR${i}}/hal_plugins")
    endforeach()
endforeach()

# Build netlist_preprocessing and dependend libraries
option(PL_NETLIST_PREPROCESSING "PL_NETLIST_PREPROCESSING" ON)

# netlist_preprocessing links against the resynthesis plugin, so whether that plugin is built has to
# be decided here, before either directory is added. cmake/hal_resynthesis_option.cmake turns
# PL_RESYNTHESIS (AUTO / ON / OFF) into HAL_BUILD_RESYNTHESIS; it is a module of its own so that it
# can be tested without building HAL, see tests/packaging/test_resynthesis_option.py.
include(hal_resynthesis_option)

include_directories(${include_directories}
                    ${CMAKE_SOURCE_DIR}/include
                    ${CMAKE_SOURCE_DIR}/hal_plugins
                   )

file(MAKE_DIRECTORY ${CMAKE_BINARY_DIR}/lib/hal_plugins/)
file(WRITE ${CMAKE_BINARY_DIR}/lib/hal_plugins/__init__.py "")
install(FILES ${CMAKE_BINARY_DIR}/lib/hal_plugins/__init__.py DESTINATION ${PLUGIN_LIBRARY_INSTALL_DIRECTORY})


include_directories(${CMAKE_CURRENT_SOURCE_DIR})

SUBDIRLIST(SUBDIRS ${CMAKE_CURRENT_SOURCE_DIR})
FOREACH(subdir ${SUBDIRS})
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/${subdir}/CMakeLists.txt")
        ADD_SUBDIRECTORY(${subdir})
    endif()
ENDFOREACH()

foreach(i IN ITEMS "" "_DEBUG" "_RELEASE" "_MINSIZEREL" "_RELWITHDEBINFO")
    foreach(j IN ITEMS "RUNTIME" "ARCHIVE" "LIBRARY")
        set(CMAKE_${j}_OUTPUT_DIRECTORY${i} "${TMP_${j}_OUTPUT_DIR${i}}")
    endforeach()
endforeach()

set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${TMP_RUNTIME_OUTPUT_DIR})
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_DEBUG ${TMP_RUNTIME_OUTPUT_DIR_DEBUG})
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY_RELEASE ${TMP_RUNTIME_OUTPUT_DIR})

set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${TMP_ARCHIVE_OUTPUT_DIR})
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY_DEBUG ${TMP_ARCHIVE_OUTPUT_DIR_DEBUG})
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY_RELEASE ${TMP_ARCHIVE_OUTPUT_DIR})
