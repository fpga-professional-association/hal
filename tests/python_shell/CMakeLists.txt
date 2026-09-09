# --python-script only exists when the python shell plugin was built, so the test only exists then too.
# The plugin declares PL_PYTHON_SHELL as a cache option and plugins/ is configured before tests/.
if(PL_PYTHON_SHELL OR BUILD_ALL_PLUGINS)
    # What is under test is the exit code of the hal process, which cannot be observed from inside it,
    # so this driver spawns the built binary once per case rather than linking against anything. It
    # needs the built modules and the plugins, which hal finds through HAL_BASE_PATH.
    add_test(NAME runTest-python_shell_script_exit_codes
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/script_exit_code_test.py
                     $<TARGET_FILE:hal> ${CMAKE_CURRENT_SOURCE_DIR}/scripts
             WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
    set_tests_properties(runTest-python_shell_script_exit_codes PROPERTIES
                         ENVIRONMENT "PYTHONPATH=${CMAKE_BINARY_DIR}/lib;HAL_BASE_PATH=${CMAKE_BINARY_DIR}")

    # Same story for the other half of the contract: that --python-script composes with the project
    # arguments. That one needs a netlist to import, so it gets the fsm_controller fixture and the
    # gate library it was written against.
    add_test(NAME runTest-python_shell_project_context
             COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/project_context_test.py
                     $<TARGET_FILE:hal> ${CMAKE_CURRENT_SOURCE_DIR}/scripts
                     ${CMAKE_SOURCE_DIR}/tests/fixtures/fsm_controller/controller.v
                     ${CMAKE_SOURCE_DIR}/plugins/gate_libraries/definitions/example_library.hgl
             WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
    set_tests_properties(runTest-python_shell_project_context PROPERTIES
                         ENVIRONMENT "PYTHONPATH=${CMAKE_BINARY_DIR}/lib;HAL_BASE_PATH=${CMAKE_BINARY_DIR}")
endif()
