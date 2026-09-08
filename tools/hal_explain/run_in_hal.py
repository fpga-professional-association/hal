"""Entry point executed by ``hal --python-script``. Not importable machinery.

The constraints are the ones ``tools/hal_runner/steps/step_runner.py`` and
``tools/hal_fsm/run_in_hal.py`` document:

* there is no ``__file__`` -- HAL reads the file and runs its *source*, so this
  script cannot locate itself;
* ``sys.argv`` is whatever ``--python-args`` was, split on spaces, so no path
  may travel that way;
* ``hal_py`` is already imported by the shell, but the plugins are not loaded.

So the one thing this script needs -- the path of its request file -- arrives in
``HAL_EXPLAIN_REQUEST``, and everything else is read from that request.
"""

import json
import os
import sys
import traceback

_REQUEST_ENV = "HAL_EXPLAIN_REQUEST"


def _emergency_result(request, message, detail):
    """Best-effort record for a failure that happened before dispatch."""
    try:
        output_dir = request.get("output_dir")
        if not output_dir:
            return
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, request.get("result_file", "result.json"))
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {
                    "result_version": "1.0.0",
                    "analysis": "hal_explain.collect",
                    "status": "error",
                    "artifacts": {},
                    "steps": [],
                    "error": {"kind": "internal", "message": message, "detail": detail},
                },
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
    except Exception:  # pragma: no cover - the caller still fails the run
        pass


def _run():
    request_path = os.environ.get(_REQUEST_ENV)
    if not request_path:
        sys.stderr.write(
            "[hal_explain] ${} is not set. This script is executed by hal_explain, "
            "not by hand; run 'python tools/hal_explain collect <netlist>' "
            "instead.\n".format(_REQUEST_ENV)
        )
        return 2

    try:
        with open(request_path, "r", encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            "[hal_explain] could not read the request {}: {}\n".format(request_path, exc)
        )
        return 2

    tools_path = request.get("tools_path")
    if tools_path and tools_path not in sys.path:
        sys.path.insert(0, tools_path)

    try:
        from hal_explain.collect import execute
    except ImportError as exc:
        message = "could not import hal_explain from {!r}: {}".format(tools_path, exc)
        sys.stderr.write("[hal_explain] {}\n".format(message))
        _emergency_result(request, message, traceback.format_exc())
        return 2

    return execute(request)


sys.exit(_run())
