"""Run the security property check inside ``hal --python-script``.

``hal --python-script`` executes this source with ``PyRun_SimpleString``: there
is no ``__file__``, and ``--python-args`` is split on spaces, so a path with a
space in it cannot survive it. Configuration therefore comes from the
environment, which is unambiguous and easy to set in a container:

    HAL_SECPROP_TOOLS    the repository's tools/ directory (default: ./tools)
    HAL_SECPROP_POLICY   the security policy document (JSON)  (required)
    HAL_SECPROP_NETLIST  netlist file, if it is not the one the policy names
    HAL_SECPROP_LIBRARY  gate library file, for netlist formats that need one
    HAL_SECPROP_OUTPUT   where to write the findings document
                         (default: ./secprop_findings.json)
    HAL_SECPROP_BOUND    override options.bound
    HAL_SECPROP_STRICT   set to 1 to exit non-zero on inconclusive results
    HAL_SECPROP_REPLAY   set to 1 to replay every witness the run produced

``sys.exit`` reaches CPython's ``SystemExit`` handling, so the status below
becomes HAL's exit code, and it is the *verdict*: 0 clean, 1 a policy violation
with a replayable witness, 2 a run that could not produce trustworthy results.

    hal --python-script tools/hal_secprop/scripts/check_in_hal.py
"""

import os
import sys


def _required(name):
    value = os.environ.get(name)
    if not value:
        print(
            "{} is not set; see the header of check_in_hal.py".format(name),
            file=sys.stderr,
        )
        raise SystemExit(2)
    return value


def _run():
    tools = os.path.abspath(os.environ.get("HAL_SECPROP_TOOLS", "tools"))
    if not os.path.isdir(os.path.join(tools, "hal_secprop")):
        print(
            "HAL_SECPROP_TOOLS={!r} does not contain hal_secprop/".format(tools),
            file=sys.stderr,
        )
        return 2
    if tools not in sys.path:
        sys.path.insert(0, tools)

    from hal_secprop.cli import main

    output = os.environ.get("HAL_SECPROP_OUTPUT", "secprop_findings.json")
    argv = [
        "check",
        _required("HAL_SECPROP_POLICY"),
        "--source",
        "hal",
        "-o",
        output,
    ]
    for variable, flag in (
        ("HAL_SECPROP_NETLIST", "--netlist"),
        ("HAL_SECPROP_LIBRARY", "--gate-library"),
        ("HAL_SECPROP_BOUND", "--bound"),
    ):
        value = os.environ.get(variable)
        if value:
            argv += [flag, value]
    if os.environ.get("HAL_SECPROP_STRICT") == "1":
        argv.append("--strict")

    status = main(argv)
    if os.environ.get("HAL_SECPROP_REPLAY") != "1":
        return status

    # Replaying every witness the run wrote is the point of running inside HAL:
    # the witness has to reproduce against the netlist as *HAL* read it, not
    # only against the offline model.
    evidence = os.path.join(os.path.dirname(os.path.abspath(output)) or ".", "evidence")
    if not os.path.isdir(evidence):
        return status
    for name in sorted(os.listdir(evidence)):
        if not name.endswith(".replay.json"):
            continue
        replayed = main(["replay", os.path.join(evidence, name), "--source", "hal"])
        if replayed != 0:
            print("replay of {} failed".format(name), file=sys.stderr)
            return 2
    return status


sys.exit(_run())
