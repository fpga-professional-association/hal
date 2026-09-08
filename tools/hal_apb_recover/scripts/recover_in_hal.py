"""Run the APB register-map recovery inside ``hal --python-script``.

``hal --python-script`` executes this source with ``PyRun_SimpleString``: there
is no ``__file__``, and ``--python-args`` is split on spaces, so paths with a
space in them cannot survive it. Configuration therefore comes from the
environment, which is unambiguous and easy to set in a container:

    HAL_APB_RECOVER_TOOLS    the repository's tools/ directory (default: ./tools)
    HAL_APB_RECOVER_NETLIST  netlist file or HAL project directory  (required)
    HAL_APB_RECOVER_MAPPING  the user-supplied APB mapping (JSON)   (required)
    HAL_APB_RECOVER_LIBRARY  gate library file, for netlist formats that need one
    HAL_APB_RECOVER_OUTPUT   output path prefix (default: ./apb_register_map)
    HAL_APB_RECOVER_REPLAY   set to 1 to replay the generated transactions too

``sys.exit`` reaches CPython's ``SystemExit`` handling, so the status below
becomes HAL's exit code: 0 success, 1 failure, 2 a usage error.

    hal --gate-library <lib.hgl> --python-script \\
        tools/hal_apb_recover/scripts/recover_in_hal.py
"""

import os
import sys


def _required(name):
    value = os.environ.get(name)
    if not value:
        print(
            "{} is not set; see the header of recover_in_hal.py".format(name),
            file=sys.stderr,
        )
        raise SystemExit(2)
    return value


def _run():
    tools = os.path.abspath(os.environ.get("HAL_APB_RECOVER_TOOLS", "tools"))
    if not os.path.isdir(os.path.join(tools, "hal_apb_recover")):
        print(
            "HAL_APB_RECOVER_TOOLS={!r} does not contain hal_apb_recover/".format(tools),
            file=sys.stderr,
        )
        return 2
    if tools not in sys.path:
        sys.path.insert(0, tools)

    from hal_apb_recover.cli import main

    argv = [
        "recover",
        _required("HAL_APB_RECOVER_NETLIST"),
        _required("HAL_APB_RECOVER_MAPPING"),
        "--source",
        "hal",
        "-o",
        os.environ.get("HAL_APB_RECOVER_OUTPUT", "apb_register_map"),
    ]
    library = os.environ.get("HAL_APB_RECOVER_LIBRARY")
    if library:
        argv += ["--gate-library", library]

    status = main(argv)
    if status != 0 or os.environ.get("HAL_APB_RECOVER_REPLAY") != "1":
        return status

    replay_argv = [
        "replay",
        _required("HAL_APB_RECOVER_NETLIST"),
        _required("HAL_APB_RECOVER_MAPPING"),
        os.environ.get("HAL_APB_RECOVER_OUTPUT", "apb_register_map") + ".replay.json",
        "--source",
        "hal",
    ]
    if library:
        replay_argv += ["--gate-library", library]
    return main(replay_argv)


sys.exit(_run())
