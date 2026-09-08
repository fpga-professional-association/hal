# The hal_viz graph builders are exercised with stub objects, so this suite needs neither a built
# HAL nor Graphviz nor a netlist -- only a Python interpreter. Registering it here is what puts it in
# front of anyone who runs ctest, instead of it only ever being run by hand.
add_test(NAME runTest-hal_viz_standalone
         COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/hal_viz/test_hal_viz.py
         WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})

# Same story for hal_runner: the orchestrator talks to HAL through exactly one method
# (ProcessExecutor.execute), so a stub executor exercises the manifest, the checkpoint store,
# failure propagation and the diagnostics without a build. What it cannot cover -- that the steps
# actually run an analysis -- is what tests/headless_smoke/runner_smoke.py is for.
add_test(NAME runTest-hal_runner_standalone
         COMMAND ${Python3_EXECUTABLE} -m unittest discover
                 -s ${CMAKE_SOURCE_DIR}/tools/hal_runner -t ${CMAKE_SOURCE_DIR}/tools -p "test_*.py"
         WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})

# real_netlist_smoke.py and runner_smoke.py in this directory are deliberately NOT registered here.
# They unpack an example, run plugins, round-trip a project and drive hal_viz / hal_runner, and when
# one fails what you want is the whole work directory, not a ctest log line -- so CI runs them as
# their own steps (see .github/workflows/ubuntu24.04.yml) with --work-dir and uploads that directory
# on failure.
#
# To run them by hand against a build tree:
#
#   HAL_BASE_PATH=<build> PYTHONPATH=<build>/lib \
#       python3 tests/headless_smoke/real_netlist_smoke.py
#
#   HAL_BASE_PATH=<build> \
#       python3 tests/headless_smoke/runner_smoke.py --hal-binary <build>/bin/hal
