# The last install step of the whole project: register HAL's library directories with the dynamic
# linker. This directory exists only to control ordering -- it is added by the top-level CMakeLists.txt
# after every other subdirectory, so the install(SCRIPT) below is emitted after all install(TARGETS)
# rules and ldconfig sees the libraries that were just installed rather than the previous ones.
#
# The script itself decides whether it may touch the system at all (DESTDIR / staged prefixes), see
# cmake/post_install.cmake.in.

if(ENABLE_INSTALL_LDCONFIG AND LINUX)
    configure_file(
        "${CMAKE_SOURCE_DIR}/cmake/post_install.cmake.in"
        "${CMAKE_BINARY_DIR}/post_install.cmake"
        @ONLY)
    install(SCRIPT "${CMAKE_BINARY_DIR}/post_install.cmake")
endif()
