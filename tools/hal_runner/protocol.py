"""The contract between the runner and a step running inside HAL.

The two sides are separate processes -- deliberately, see :mod:`hal_runner.execute`
-- so everything they say to each other is a file:

``request.json``
    written by the runner before the step starts: which analysis, which
    configuration, which netlist, where to put things.  Its path is handed over
    in the ``HAL_RUNNER_REQUEST`` environment variable and **not** on the
    command line, because ``hal --python-script`` passes ``--python-args`` to
    Python after splitting it on spaces (see
    ``plugins/python_shell/src/plugin_python_shell.cpp``): any path containing a
    space would arrive as two arguments.  An environment variable has no such
    problem.

``result.json``
    written by the step before it exits: what it produced, how long it took,
    and -- if it failed -- why.  The runner treats a *missing* result as a
    failed step even when the process exited 0, because a step that exits
    without saying what it did has not done anything provable.

Both files carry their own version so that a runner and a step from different
checkouts refuse to talk to each other instead of half-understanding.
"""

import json
import os

__all__ = [
    "REQUEST_VERSION",
    "RESULT_VERSION",
    "REQUEST_ENV",
    "SUPPORTED_REQUEST_VERSIONS",
    "SUPPORTED_RESULT_VERSIONS",
    "ProtocolError",
    "write_json",
    "read_request",
    "read_result",
    "result",
]

REQUEST_VERSION = "1.0.0"
RESULT_VERSION = "1.0.0"

SUPPORTED_REQUEST_VERSIONS = ("1.0.0",)
SUPPORTED_RESULT_VERSIONS = ("1.0.0",)

#: Environment variable naming the request file of the step being executed.
REQUEST_ENV = "HAL_RUNNER_REQUEST"


class ProtocolError(ValueError):
    """Raised when a request or result does not fit the contract."""


def write_json(document, path):
    """Write ``document`` deterministically (sorted keys, LF, trailing newline)."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2) + "\n")
    return path


def _read(path, version_key, supported, what):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise ProtocolError("could not read the step {} at {}: {}".format(what, path, exc))
    except ValueError as exc:
        raise ProtocolError("the step {} at {} is not valid JSON: {}".format(what, path, exc))
    version = document.get(version_key)
    if version not in supported:
        raise ProtocolError(
            "the step {} at {} declares {} {!r}; this hal_runner understands {}".format(
                what, path, version_key, version, ", ".join(supported)
            )
        )
    return document


def read_request(path):
    """Read and version-check a step request."""
    return _read(path, "request_version", SUPPORTED_REQUEST_VERSIONS, "request")


def read_result(path):
    """Read and version-check a step result."""
    return _read(path, "result_version", SUPPORTED_RESULT_VERSIONS, "result")


def result(
    status,
    analysis,
    artifacts=None,
    metrics=None,
    error=None,
    started_at=None,
    finished_at=None,
    duration_s=None,
    notes=None,
):
    """Build a step result record.

    ``artifacts`` are ``{"path": <relative to the step directory>, "role": ...}``
    entries; the runner hashes them itself rather than trusting a hash the step
    reports about its own output.
    """
    if status not in ("ok", "error"):
        raise ProtocolError("a step result status must be 'ok' or 'error', got {!r}".format(status))
    if status == "error" and not error:
        raise ProtocolError("a failed step result must carry an error")
    document = {
        "result_version": RESULT_VERSION,
        "status": status,
        "analysis": analysis,
        "artifacts": list(artifacts or []),
    }
    if metrics:
        document["metrics"] = metrics
    if error:
        document["error"] = error
    if started_at:
        document["started_at"] = started_at
    if finished_at:
        document["finished_at"] = finished_at
    if duration_s is not None:
        document["duration_s"] = round(float(duration_s), 3)
    if notes:
        document["notes"] = list(notes)
    return document
