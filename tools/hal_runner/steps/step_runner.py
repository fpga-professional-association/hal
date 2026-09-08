"""Entry point executed by ``hal --python-script``. Not importable machinery.

Everything this file can rely on is unusual, so it assumes nothing:

* there is no ``__file__`` -- HAL reads the script and runs its *source*
  (``PyRun_SimpleString`` in ``plugins/python_shell/src/plugin_python_shell.cpp``),
  so the file's own location is unknowable from inside;
* ``sys.argv`` is not the script's arguments -- HAL fills it from
  ``--python-args``, split on spaces, so a path with a space in it would arrive
  in pieces;
* ``hal_py`` is already imported into the globals by the shell, and the HAL
  library directory is already on ``sys.path``.

Hence: the one thing this script needs, the path of its request file, arrives in
the ``HAL_RUNNER_REQUEST`` environment variable, and everything else is read
from that request.

Exit codes are the runner's primary failure signal (``hal`` propagates them
faithfully since issue #11): 0 on success, 1 when the analysis failed, 2 when
the request itself was unusable.
"""

import json
import os
import sys
import traceback

_REQUEST_ENV = "HAL_RUNNER_REQUEST"


def _emergency_result(request, message, detail):
    """Best-effort result record for a failure that happened before dispatch.

    The runner treats a missing result as a failed step anyway, so this is about
    the *reason* surviving, not about the verdict.
    """
    try:
        output_dir = request.get("output_dir")
        if not output_dir:
            return
        path = os.path.join(output_dir, request.get("result_file", "result.json"))
        os.makedirs(output_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {
                    "result_version": "1.0.0",
                    "status": "error",
                    "analysis": request.get("analysis", "unknown"),
                    "artifacts": [],
                    "error": {"kind": "internal", "message": message, "detail": detail},
                },
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
    except Exception:  # pragma: no cover - the runner still fails the step
        pass


def _run():
    request_path = os.environ.get(_REQUEST_ENV)
    if not request_path:
        sys.stderr.write(
            "[hal_runner] ${} is not set. This script is executed by hal_runner, not "
            "by hand; run 'python tools/hal_runner run <config.json>' instead.\n".format(
                _REQUEST_ENV
            )
        )
        return 2

    try:
        with open(request_path, "r", encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            "[hal_runner] could not read the step request {}: {}\n".format(request_path, exc)
        )
        return 2

    tools_path = request.get("tools_path")
    if tools_path and tools_path not in sys.path:
        sys.path.insert(0, tools_path)

    try:
        from hal_runner.steps.dispatch import execute
    except ImportError as exc:
        message = "could not import hal_runner from {!r}: {}".format(tools_path, exc)
        sys.stderr.write("[hal_runner] {}\n".format(message))
        _emergency_result(request, message, traceback.format_exc())
        return 2

    return execute(request)


sys.exit(_run())
