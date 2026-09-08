"""Typed errors and the response envelope.

Two rules, and everything else follows from them:

1. **An agent must never have to parse prose to find out what went wrong.**
   Every failure is one of the codes in :data:`ERROR_CODES`, carried in an
   envelope with ``ok: false``.  The message is for a human reading a log; the
   *code* is what a caller branches on.
2. **A failure is never a silent empty result.**  An unknown gate id is
   ``unknown_object``, not an empty list.  A cone that hit its budget is a
   result *plus* a truncation record, not a short list.  A query that ran out
   of time is ``timeout`` carrying the limit it hit, not an internal error.

The distinction the codes are designed around is *whose fault it is*:
``invalid_request`` / ``unknown_*`` mean the caller asked for something that
does not exist, ``timeout`` / ``limit_exceeded`` / ``not_ready`` mean the
request was fine but the answer is not available under the given bounds, and
``hal_unavailable`` / ``hal_error`` / ``internal`` mean this side broke.
"""

from . import API_VERSION

__all__ = [
    "ERROR_CODES",
    "ApiError",
    "InvalidRequest",
    "UnknownTool",
    "UnknownProject",
    "UnknownObject",
    "UnknownAnalysis",
    "UnknownJob",
    "UnknownArtifact",
    "NotReady",
    "LimitExceeded",
    "Timeout",
    "HalUnavailable",
    "HalError",
    "Internal",
    "ok_envelope",
    "error_envelope",
]

#: Every error code this API can return, with what it means.  The list is part
#: of the contract: ``hal.capabilities`` reports it, and a caller may treat an
#: unlisted code as a protocol violation.
ERROR_CODES = {
    "invalid_request": "the request does not match the tool's schema, or is self-contradictory",
    "unknown_tool": "no such tool in this API version",
    "unknown_project": "no such project handle; open one with project.open",
    "unknown_object": "no gate/net/module with that id in this netlist",
    "unknown_analysis": "no such analysis; see analysis.list",
    "unknown_job": "no such analysis job in this workspace",
    "unknown_artifact": "the job produced no artifact with that path",
    "not_ready": "the answer does not exist yet (the job has not finished)",
    "limit_exceeded": "the request asks for more than the configured bounds allow",
    "timeout": "the operation hit its time limit and was stopped",
    "hal_unavailable": "no usable 'hal' binary or hal_py for this operation",
    "hal_error": "HAL ran and failed; the logs are the evidence",
    "internal": "this API broke; the detail is a bug report, not a design answer",
}


class ApiError(Exception):
    """A failure with a code a caller can branch on.

    ``detail`` is free-form context (a stderr tail, the list of schema
    violations); ``hint`` is the next action a caller could take; ``data`` is
    machine-readable context, e.g. the limit that was exceeded.
    """

    code = "internal"

    def __init__(self, message, detail=None, hint=None, data=None, code=None):
        if code is not None:
            self.code = code
        if self.code not in ERROR_CODES:
            raise ValueError("unknown error code {!r}".format(self.code))
        self.message = str(message)
        self.detail = detail
        self.hint = hint
        self.data = data
        Exception.__init__(self, "{}: {}".format(self.code, self.message))

    def as_json(self):
        payload = {"code": self.code, "message": self.message}
        if self.detail is not None:
            payload["detail"] = str(self.detail)
        if self.hint is not None:
            payload["hint"] = str(self.hint)
        if self.data is not None:
            payload["data"] = self.data
        return payload


def _error_class(name, code):
    return type(name, (ApiError,), {"code": code, "__doc__": ERROR_CODES[code]})


InvalidRequest = _error_class("InvalidRequest", "invalid_request")
UnknownTool = _error_class("UnknownTool", "unknown_tool")
UnknownProject = _error_class("UnknownProject", "unknown_project")
UnknownObject = _error_class("UnknownObject", "unknown_object")
UnknownAnalysis = _error_class("UnknownAnalysis", "unknown_analysis")
UnknownJob = _error_class("UnknownJob", "unknown_job")
UnknownArtifact = _error_class("UnknownArtifact", "unknown_artifact")
NotReady = _error_class("NotReady", "not_ready")
LimitExceeded = _error_class("LimitExceeded", "limit_exceeded")
Timeout = _error_class("Timeout", "timeout")
HalUnavailable = _error_class("HalUnavailable", "hal_unavailable")
HalError = _error_class("HalError", "hal_error")
Internal = _error_class("Internal", "internal")


def ok_envelope(tool, result):
    """The success envelope. ``result`` is always an object, never a bare value."""
    return {"ok": True, "api_version": API_VERSION, "tool": tool, "result": result}


def error_envelope(tool, error):
    """The failure envelope for an :class:`ApiError` (or any exception)."""
    if not isinstance(error, ApiError):
        error = Internal(
            "unhandled {}: {}".format(type(error).__name__, error),
            hint="this is a bug in hal_analysis_api; please report it with the detail",
        )
    return {"ok": False, "api_version": API_VERSION, "tool": tool, "error": error.as_json()}
