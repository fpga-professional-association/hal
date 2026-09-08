"""Entry point executed by ``hal --python-script``. Not importable machinery.

The constraints are the same ones :mod:`hal_runner.steps.step_runner`
documents, and they are worth repeating because they are not obvious:

* there is no ``__file__`` -- HAL reads the file and runs its *source*
  (``PyRun_SimpleString`` in ``plugins/python_shell/src/plugin_python_shell.cpp``),
  so this script cannot find itself on disk;
* ``sys.argv`` is not this script's arguments -- HAL fills it from
  ``--python-args``, split on spaces, so any path with a space in it would
  arrive in pieces;
* ``hal_py`` is already imported into the globals and HAL's library directory is
  already on ``sys.path``, but HAL's *plugins* are not loaded yet.

So the one thing this script needs -- the path of its request file -- arrives in
``HAL_FAULT_CAMPAIGN_REQUEST``, and the request carries everything else,
including where the ``tools/`` directory lives.

Exit codes are the host's primary failure signal (``hal`` propagates them
faithfully since issue #11): 0 on success, 1 when the campaign failed, 2 when
the request itself was unusable.
"""

import json
import os
import sys
import traceback

_REQUEST_ENV = "HAL_FAULT_CAMPAIGN_REQUEST"


def _emergency_result(request, message, detail):
    """Best-effort result record for a failure that happened before dispatch."""
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
                    "artifacts": [],
                    "error": {"kind": "internal", "message": message, "detail": detail},
                },
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
    except Exception:  # pragma: no cover - the host still fails the campaign
        pass


def _run():
    request_path = os.environ.get(_REQUEST_ENV)
    if not request_path:
        sys.stderr.write(
            "[hal_fault_campaign] ${} is not set. This script is executed by "
            "hal_fault_campaign, not by hand; run 'python tools/hal_fault_campaign run "
            "<campaign.json>' instead.\n".format(_REQUEST_ENV)
        )
        return 2

    try:
        with open(request_path, "r", encoding="utf-8") as handle:
            request = json.load(handle)
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            "[hal_fault_campaign] could not read the request {}: {}\n".format(
                request_path, exc
            )
        )
        return 2

    tools_path = request.get("tools_path")
    if tools_path and tools_path not in sys.path:
        sys.path.insert(0, tools_path)

    try:
        from hal_fault_campaign.steps.campaign_step import execute
    except ImportError as exc:
        message = "could not import hal_fault_campaign from {!r}: {}".format(
            tools_path, exc
        )
        sys.stderr.write("[hal_fault_campaign] {}\n".format(message))
        _emergency_result(request, message, traceback.format_exc())
        return 2

    return execute(request)


sys.exit(_run())
