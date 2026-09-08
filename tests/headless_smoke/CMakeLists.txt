# The hal_viz graph builders are exercised with stub objects, so this suite needs neither a built
# HAL nor Graphviz nor a netlist -- only a Python interpreter. Registering it here is what puts it in
# front of anyone who runs ctest, instead of it only ever being run by hand.
add_test(NAME runTest-hal_viz_standalone
         COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/hal_viz/test_hal_viz.py
         WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})

# real_netlist_smoke.py in this directory is deliberately NOT registered here. It unpacks an example,
# runs a plugin, round-trips a project and shells out to hal_viz, and when it fails what you want is
# the whole work directory, not a ctest log line -- so CI runs it as its own step (see
# .github/workflows/ubuntu24.04.yml) with --work-dir and uploads that directory on failure.
#
# To run it by hand against a build tree:
#
#   HAL_BASE_PATH=<build> PYTHONPATH=<build>/lib \
#       python3 tests/headless_smoke/real_netlist_smoke.py
