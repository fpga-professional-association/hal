"""Entry point executed by ``hal --python-script``. Not importable machinery.

The same three constraints that shape ``hal_runner/steps/step_runner.py`` apply
here, so this file assumes nothing:

* there is no ``__file__`` -- HAL reads the script and runs its *source*
  (``PyRun_SimpleString`` in ``plugins/python_shell/src/plugin_python_shell.cpp``),
  so the file cannot find itself;
* ``sys.argv`` is not this script's arguments -- HAL fills it from
  ``--python-args``, split on spaces;
* ``hal_py`` is already importable, and HAL's library directory is already on
  ``sys.path``.

So the one thing it needs, the path of its request file, arrives in the
``HAL_ANALYSIS_QUERY`` environment variable, and everything else comes out of
that request.

Exit codes: 0 when the query answered, 1 when it failed (a response file with
an error is written), 2 when the request itself was unusable.
"""

import json
import os
import sys
import traceback

_REQUEST_ENV = "HAL_ANALYSIS_QUERY"


def _emergency_response(request, code, message, detail):
    """Best-effort response for a failure that happened before dispatch.

    The host treats a missing response as a failed query anyway; this is about
    the *reason* surviving rather than about the verdict.
    """
    try:
        output_dir = request.get("output_dir")
        if not output_dir:
            return
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, request.get("response_file", "response.json"))
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {
                    "response_version": "1.0.0",
                    "status": "error",
                    "query": request.get("query", "unknown"),
                    "error": {"code": code, "message": message, "detail": detail},
                },
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
    except Exception:  # pragma: no cover - the host still fails the query
        pass


def _run():
    request_path = os.environ.get(_REQUEST_ENV)
    if not request_path:
        sys.stderr.write(
            "[hal_analysis_api] ${} is not set. This script is executed by "
            "hal_analysis_api, not by hand; use 'python tools/hal_analysis_api call "
            "<tool> ...' instead.\n".format(_REQUEST_ENV)
        )
        return 2

    try:
        with open(request_path, "r", encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            "[hal_analysis_api] could not read the query request {}: {}\n".format(
                request_path, exc
            )
        )
        return 2

    tools_path = request.get("tools_path")
    if tools_path and tools_path not in sys.path:
        sys.path.insert(0, tools_path)

    try:
        from hal_analysis_api.inhal.dispatch import execute
    except ImportError as exc:
        message = "could not import hal_analysis_api from {!r}: {}".format(tools_path, exc)
        sys.stderr.write("[hal_analysis_api] {}\n".format(message))
        _emergency_response(request, "internal", message, traceback.format_exc())
        return 2

    return execute(request)


sys.exit(_run())
