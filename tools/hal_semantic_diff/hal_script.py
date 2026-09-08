"""Entry point for ``hal --python-script tools/hal_semantic_diff/hal_script.py``.

Inside HAL's Python shell there is no ``__file__`` and no ``sys.argv``, and
``--python-args`` is split on spaces -- so a path with a space in it, or a JSON
blob, cannot be passed that way.  ``hal_runner`` solved this by handing the step
its request in an environment variable; this script does the same, for the same
reason and with the same shape::

    HAL_SEMANTIC_DIFF_REQUEST=/tmp/request.json \\
        hal --python-script tools/hal_semantic_diff/hal_script.py

The request is either a path to a JSON file or the JSON document itself::

    {
      "tools_dir": "/hal/tools",
      "argv": ["compare", "/work/a", "/work/b",
               "--correspondence", "/work/map.json",
               "-o", "/work/out"]
    }

``argv`` is exactly what the command line takes.  The script exits with the
CLI's exit code, and HAL propagates a failing python script as a failing run,
so a CI job can gate on it directly.
"""

import json
import os
import sys


def _load_request():
    raw = os.environ.get("HAL_SEMANTIC_DIFF_REQUEST")
    if not raw:
        raise SystemExit(
            "HAL_SEMANTIC_DIFF_REQUEST is not set. Point it at a JSON request file "
            "(or pass the JSON itself); --python-args cannot carry one because HAL "
            "splits it on spaces and the script sees no sys.argv."
        )
    if os.path.isfile(raw):
        with open(raw, "r") as handle:
            return json.load(handle)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise SystemExit(
            "HAL_SEMANTIC_DIFF_REQUEST is neither an existing file nor valid JSON: "
            "{}".format(exc)
        )


def run():
    request = _load_request()
    tools_dir = request.get("tools_dir")
    if not tools_dir:
        raise SystemExit(
            "the request needs a 'tools_dir' pointing at the repository's tools/ "
            "directory; HAL's python shell sets no __file__ to derive it from."
        )
    tools_dir = os.path.abspath(os.path.expanduser(str(tools_dir)))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    argv = request.get("argv")
    if not isinstance(argv, list) or not argv:
        raise SystemExit("the request needs a non-empty 'argv' list")

    from hal_semantic_diff.cli import main

    return main([str(entry) for entry in argv])


sys.exit(run())
