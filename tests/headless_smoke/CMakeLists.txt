# The hal_viz graph builders are exercised with stub objects and the report generator with synthetic
# findings documents, so this suite needs neither a built HAL nor Graphviz nor a netlist -- only a
# Python interpreter. Registering it here is what puts it in front of anyone who runs ctest, instead
# of it only ever being run by hand. Discovery (rather than naming test_hal_viz.py) is what makes a
# newly added test file run without touching this file again.
add_test(NAME runTest-hal_viz_standalone
         COMMAND ${Python3_EXECUTABLE} -m unittest discover
                 -s ${CMAKE_SOURCE_DIR}/tools/hal_viz -t ${CMAKE_SOURCE_DIR}/tools -p "test_*.py"
         WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})

# Same story for hal_runner: the orchestrator talks to HAL through exactly one method
# (ProcessExecutor.execute), so a stub executor exercises the manifest, the checkpoint store,
# failure propagation and the diagnostics without a build. What it cannot cover -- that the steps
# actually run an analysis -- is what tests/headless_smoke/runner_smoke.py is for.
add_test(NAME runTest-hal_runner_standalone
         COMMAND ${Python3_EXECUTABLE} -m unittest discover
                 -s ${CMAKE_SOURCE_DIR}/tools/hal_runner -t ${CMAKE_SOURCE_DIR}/tools -p "test_*.py"
         WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})

# The plugin scaffold generator produces files, and hal_capabilities reasons about declarations and
# stub netlists, so both are checkable without a build. Between them they cover the generated tree,
# the capability schema and the supported/partial/unsupported verdict; what they cannot cover -- that
# the generated C++ compiles and loads -- is what plugins/example_analysis (built by CI, with its own
# runTest-example_analysis) and tests/headless_smoke/plugin_scaffold_smoke.py are for.
add_test(NAME runTest-new_plugin_standalone
         COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_new_plugin.py
         WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})

# hal_bitstream talks to external converters (IceStorm, Trellis, ...), so its unit tests drive the
# whole tool with stub converters written into a temp directory: detection, chain planning, stdout
# capture, the provenance manifest and every error message, on a machine with no FPGA toolchain and
# no HAL. What they cannot cover -- that IceStorm's real output loads into hal_py -- is what
# tests/headless_smoke/bitstream_smoke.py is for.
add_test(NAME runTest-hal_bitstream_standalone
         COMMAND ${Python3_EXECUTABLE} -m unittest discover
                 -s ${CMAKE_SOURCE_DIR}/tools/hal_bitstream -t ${CMAKE_SOURCE_DIR}/tools
                 -p "test_*.py"
         WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})

# install_dependencies.sh is checked against stub package managers (a fake apt-get on PATH), so this
# needs neither root nor a network: it covers the fail-fast behaviour and the completeness of the
# per-distribution package lists. What it cannot cover -- that the names exist in a real archive --
# is what 'HAL_DEPENDENCIES_DRY_RUN=1 bash install_dependencies.sh' in a fresh container is for.
add_test(NAME runTest-install_dependencies_standalone
         COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/test_install_dependencies.py
         WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})

# The MCP server's protocol tier needs no build at all (it drives the server over a pipe and never
# imports hal_py); the session and tool tiers skip themselves when hal_py is unavailable, so this is
# safe to run at any point in the build.
add_test(NAME runTest-hal_mcp_standalone
         COMMAND ${Python3_EXECUTABLE} ${CMAKE_SOURCE_DIR}/tools/hal_mcp/test_hal_mcp.py
         WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})

add_test(NAME runTest-hal_capabilities_standalone
         COMMAND ${Python3_EXECUTABLE} -m unittest discover
                 -s ${CMAKE_SOURCE_DIR}/tools/hal_capabilities -t ${CMAKE_SOURCE_DIR}/tools
                 -p "test_*.py"
         WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})

# real_netlist_smoke.py, runner_smoke.py and plugin_scaffold_smoke.py in this directory are
# deliberately NOT registered here.
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
#
#   HAL_BASE_PATH=<build> HAL_PY_PATH=<build>/lib \
#       python3 tests/headless_smoke/plugin_scaffold_smoke.py --build-dir <build>
