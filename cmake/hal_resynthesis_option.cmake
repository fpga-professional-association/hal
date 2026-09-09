# Resolves PL_RESYNTHESIS into HAL_BUILD_RESYNTHESIS.
#
# The resynthesis plugin is a build dependency of netlist_preprocessing, which is why it used to be
# switched on implicitly by `if(PL_RESYNTHESIS OR PL_NETLIST_PREPROCESSING OR BUILD_ALL_PLUGINS)` in
# plugins/resynthesis/CMakeLists.txt. That made -DPL_RESYNTHESIS=OFF a no-op instead of an error, and
# a source tree without plugins/resynthesis failed to compile because netlist_preprocessing included
# resynthesis/resynthesis.h unconditionally.
#
# PL_RESYNTHESIS is therefore a tri-state:
#   AUTO (default) - build resynthesis whenever a plugin that can use it is built (previous behaviour)
#   ON             - always build it
#   OFF            - never build it; netlist_preprocessing is then compiled without resynthesis
#                    support and its resynthesis-based entry points return an error at runtime.
#
# Inputs:  PL_RESYNTHESIS, PL_NETLIST_PREPROCESSING, BUILD_ALL_PLUGINS, and the directory this file is
#          included from, which is where plugins/resynthesis is looked for.
# Outputs: HAL_BUILD_RESYNTHESIS and PL_RESYNTHESIS_UPPER, in the including scope.
#
# This lives in its own module so that tests/packaging/test_resynthesis_option.py can configure it in
# isolation instead of building HAL once per combination.

# Caches written before PL_RESYNTHESIS became a tri-state hold a BOOL entry whose value is the old
# default OFF, which back then still resulted in a resynthesis build. Taking that at face value would
# silently drop the plugin from existing build directories, so an untouched old entry is migrated to
# AUTO. Anyone who really wants it off says so again once.
get_property(PL_RESYNTHESIS_CACHE_TYPE CACHE PL_RESYNTHESIS PROPERTY TYPE)
if(PL_RESYNTHESIS_CACHE_TYPE STREQUAL "BOOL" AND NOT PL_RESYNTHESIS)
    message(STATUS "PL_RESYNTHESIS: migrating cache entry from the old boolean default to AUTO")
    unset(PL_RESYNTHESIS CACHE)
endif()

set(PL_RESYNTHESIS "AUTO" CACHE STRING "Build the resynthesis plugin: ON, OFF or AUTO (build it when another enabled plugin needs it)")
set_property(CACHE PL_RESYNTHESIS PROPERTY STRINGS AUTO ON OFF)

string(TOUPPER "${PL_RESYNTHESIS}" PL_RESYNTHESIS_UPPER)
if(PL_RESYNTHESIS_UPPER STREQUAL "AUTO")
    if(PL_NETLIST_PREPROCESSING OR BUILD_ALL_PLUGINS)
        set(HAL_BUILD_RESYNTHESIS ON)
    else()
        set(HAL_BUILD_RESYNTHESIS OFF)
    endif()
elseif(PL_RESYNTHESIS)
    set(HAL_BUILD_RESYNTHESIS ON)
else()
    set(HAL_BUILD_RESYNTHESIS OFF)
    message(STATUS "PL_RESYNTHESIS=OFF: the resynthesis plugin is not built, dependent plugins are built with reduced functionality")
endif()

# The plugin can also be missing from the source tree altogether - release tarballs and sparse
# checkouts leave plugins out. AUTO then resolves to "not available" rather than to a link against a
# target that was never defined, which is the other half of the build failure this replaces. An
# explicit PL_RESYNTHESIS=ON cannot be honoured and says so instead of failing later at link time.
if(HAL_BUILD_RESYNTHESIS AND NOT EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/resynthesis/CMakeLists.txt")
    if(PL_RESYNTHESIS_UPPER STREQUAL "AUTO")
        message(STATUS "PL_RESYNTHESIS=AUTO: plugins/resynthesis is not part of this source tree, dependent plugins are built with reduced functionality")
        set(HAL_BUILD_RESYNTHESIS OFF)
    else()
        message(FATAL_ERROR "PL_RESYNTHESIS=ON, but plugins/resynthesis is not part of this source tree")
    endif()
endif()

message(STATUS "HAL_BUILD_RESYNTHESIS: ${HAL_BUILD_RESYNTHESIS}")
