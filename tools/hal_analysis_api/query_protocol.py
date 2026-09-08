"""The contract between the API process and a query running inside HAL.

Deliberately the same shape as :mod:`hal_runner.protocol`, for the same
reasons:

* the two sides are separate processes, so everything they say to each other is
  a file with a version on it;
* the request path travels in an **environment variable**
  (``HAL_ANALYSIS_QUERY``) and not on the command line, because
  ``hal --python-script`` splits ``--python-args`` on spaces before handing
  them to Python -- any workspace path containing a space would arrive in
  pieces;
* the in-HAL script can use neither ``__file__`` nor ``sys.argv``: HAL runs the
  file's *source*, not the file.

A query response is not a findings document.  It is the answer to a read, and
it carries the API's own error codes so that "no gate 9999" survives the
process boundary as ``unknown_object`` rather than as a traceback in a log.
"""

import json
import os

__all__ = [
    "REQUEST_VERSION",
    "RESPONSE_VERSION",
    "SUPPORTED_REQUEST_VERSIONS",
    "SUPPORTED_RESPONSE_VERSIONS",
    "REQUEST_ENV",
    "QueryProtocolError",
    "write_json",
    "read_request",
    "read_response",
    "response",
]

REQUEST_VERSION = "1.0.0"
RESPONSE_VERSION = "1.0.0"

SUPPORTED_REQUEST_VERSIONS = ("1.0.0",)
SUPPORTED_RESPONSE_VERSIONS = ("1.0.0",)

#: Environment variable naming the request file of the query being executed.
REQUEST_ENV = "HAL_ANALYSIS_QUERY"


class QueryProtocolError(ValueError):
    """Raised when a query request or response does not fit the contract."""


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
        raise QueryProtocolError("could not read the query {} at {}: {}".format(what, path, exc))
    except ValueError as exc:
        raise QueryProtocolError(
            "the query {} at {} is not valid JSON: {}".format(what, path, exc)
        )
    version = document.get(version_key)
    if version not in supported:
        raise QueryProtocolError(
            "the query {} at {} declares {} {!r}; this build understands {}".format(
                what, path, version_key, version, ", ".join(supported)
            )
        )
    return document


def read_request(path):
    """Read and version-check a query request."""
    return _read(path, "request_version", SUPPORTED_REQUEST_VERSIONS, "request")


def read_response(path):
    """Read and version-check a query response."""
    return _read(path, "response_version", SUPPORTED_RESPONSE_VERSIONS, "response")


def response(status, query, result=None, error=None, duration_s=None, hal_version=None):
    """Build a query response record."""
    if status not in ("ok", "error"):
        raise QueryProtocolError(
            "a query response status must be 'ok' or 'error', got {!r}".format(status)
        )
    if status == "error" and not error:
        raise QueryProtocolError("a failed query response must carry an error")
    if status == "ok" and result is None:
        raise QueryProtocolError("a successful query response must carry a result")
    document = {"response_version": RESPONSE_VERSION, "status": status, "query": query}
    if result is not None:
        document["result"] = result
    if error is not None:
        document["error"] = error
    if duration_s is not None:
        document["duration_s"] = round(float(duration_s), 3)
    if hal_version:
        document["hal_version"] = str(hal_version)
    return document
