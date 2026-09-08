"""The contract between the host CLI and the campaign step running inside HAL.

Same shape and same reasoning as :mod:`hal_runner.protocol`, with its own
environment variable so that a campaign step and a runner step can never be
handed each other's request:

``request.json``
    written by the host before HAL starts.  Its path travels in
    ``HAL_FAULT_CAMPAIGN_REQUEST`` and **not** on the command line, because
    ``hal --python-script`` splits ``--python-args`` on spaces before Python
    ever sees it, so any path containing a space would arrive in pieces.

``result.json``
    written by the step before it exits.  A missing result is a failed run even
    when HAL exits 0: a step that does not say what it did has not proved that
    it did anything.

Both files carry their own version so that a host and a step from different
checkouts refuse to talk rather than half-understand each other.
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

#: Environment variable naming the request file of the campaign being executed.
REQUEST_ENV = "HAL_FAULT_CAMPAIGN_REQUEST"


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
        raise ProtocolError("could not read the campaign {} at {}: {}".format(what, path, exc))
    except ValueError as exc:
        raise ProtocolError("the campaign {} at {} is not valid JSON: {}".format(what, path, exc))
    version = document.get(version_key)
    if version not in supported:
        raise ProtocolError(
            "the campaign {} at {} declares {} {!r}; this build understands {}".format(
                what, path, version_key, version, ", ".join(supported)
            )
        )
    return document


def read_request(path):
    return _read(path, "request_version", SUPPORTED_REQUEST_VERSIONS, "request")


def read_result(path):
    return _read(path, "result_version", SUPPORTED_RESULT_VERSIONS, "result")


def result(status, artifacts=None, metrics=None, error=None, started_at=None,
           finished_at=None, duration_s=None, notes=None, summary=None,
           sites=None, faults=None, enumeration=None, instrumentation=None,
           engine=None):
    """Build a campaign result record."""
    if status not in ("ok", "error"):
        raise ProtocolError(
            "a campaign result status must be 'ok' or 'error', got {!r}".format(status)
        )
    if status == "error" and not error:
        raise ProtocolError("a failed campaign result must carry an error")
    document = {
        "result_version": RESULT_VERSION,
        "status": status,
        "artifacts": list(artifacts or []),
    }
    for key, value in (
        ("metrics", metrics),
        ("error", error),
        ("started_at", started_at),
        ("finished_at", finished_at),
        ("summary", summary),
        ("sites", sites),
        ("faults", faults),
        ("enumeration", enumeration),
        ("instrumentation", instrumentation),
        ("engine", engine),
    ):
        if value:
            document[key] = value
    if duration_s is not None:
        document["duration_s"] = round(float(duration_s), 3)
    if notes:
        document["notes"] = list(notes)
    return document
