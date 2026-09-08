"""Running one netlist query as a ``hal`` subprocess, with a limit that bites.

Why a process per query rather than a long-lived HAL session:

* **The limit has to be real.**  A HAL netlist load or a large cone is C++ with
  no cancellation points; a thread cannot be interrupted, a process can be
  killed.  This reuses :class:`hal_runner.execute.ProcessExecutor`, which
  already escalates SIGTERM to SIGKILL across the child's whole process group
  and reports whether a memory limit could be enforced.
* **A read cannot modify the project.**  The child loads the netlist, answers
  and exits without saving.  There is no session to accidentally mutate and
  nothing to persist.
* **A crash costs one query.**  A segfault in a parser is an error for one
  call, not a dead server.

The price is honest and documented: every ``netlist.*`` call pays a HAL start
and a netlist load (seconds).  That is why the API offers ``netlist.cone`` and
filtered listings rather than encouraging a per-gate loop -- the shape of the
tools is what keeps the cost down, not a cache that would have to be invalidated
correctly.

Everything this module returns is either a query result or a typed
:class:`hal_analysis_api.errors.ApiError`.  A timeout is ``timeout`` and carries
the limit it hit; a HAL crash is ``hal_error`` and carries the tail of stderr;
a missing response after a clean exit is ``internal``, never a silent empty
answer.
"""

import os
import shutil
import uuid

from hal_findings.adapters.common import utc_now
from hal_runner.execute import ProcessExecutor, tail
from hal_runner.runner import RunnerError, resolve_hal_binary

from . import limits as limit_module
from . import query_protocol
from .errors import ERROR_CODES, ApiError, HalError, HalUnavailable, Internal, Timeout

_KNOWN_CODES = frozenset(ERROR_CODES)

__all__ = ["QUERY_SCRIPT", "TOOLS_PATH", "QueryRunner"]

TOOLS_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The script every query is executed as. A plain file on purpose: HAL's
#: ``--python-script`` takes a path to a ``.py`` file, not a module.
QUERY_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "inhal", "query_script.py"
)


class QueryRunner(object):
    """Executes read-only netlist queries against a project handle."""

    def __init__(self, workspace, hal_binary=None, executor=None, keep_scratch=False):
        self.workspace = os.path.abspath(workspace)
        self.hal_binary = hal_binary
        self.executor = executor if executor is not None else ProcessExecutor()
        self.keep_scratch = keep_scratch

    # -- environment ---------------------------------------------------------

    def resolve_binary(self):
        """The ``hal`` executable, or ``hal_unavailable`` with the actionable message."""
        if self.hal_binary and os.path.isfile(self.hal_binary):
            return self.hal_binary
        try:
            self.hal_binary = resolve_hal_binary(self.hal_binary)
        except RunnerError as exc:
            raise HalUnavailable(
                str(exc),
                hint="netlist.* tools need a built HAL; hal.capabilities reports "
                "whether one was found, and every other tool works without it",
            )
        return self.hal_binary

    def availability(self):
        """``hal.capabilities``' view of the HAL binary: found, or why not."""
        try:
            return {"available": True, "binary": self.resolve_binary(), "reason": None}
        except HalUnavailable as exc:
            return {"available": False, "binary": None, "reason": exc.message}

    # -- execution -----------------------------------------------------------

    def build_command(self, hal_binary):
        return [hal_binary, "--python-script", QUERY_SCRIPT]

    def run(self, query, handle, params, timeout_s=None):
        """Answer one query, or raise a typed :class:`ApiError`."""
        hal_binary = self.resolve_binary()
        # float, not int: the API's schema only lets a caller ask for whole
        # seconds, but the smoke test drives this method directly with a
        # millisecond limit to prove the limit is really enforced.
        timeout_s = float(timeout_s or limit_module.DEFAULT_QUERY_TIMEOUT_S)

        scratch = os.path.join(
            self.workspace, "queries", "{}-{}".format(query.replace(".", "_"), uuid.uuid4().hex[:8])
        )
        os.makedirs(scratch, exist_ok=True)

        request = {
            "request_version": query_protocol.REQUEST_VERSION,
            "query": query,
            "project": handle.id,
            "tools_path": TOOLS_PATH,
            "netlist": handle.netlist_path,
            "gate_library": handle.gate_library,
            "params": params,
            "output_dir": scratch,
            "response_file": "response.json",
            "requested_at": utc_now(),
        }
        request_path = os.path.join(scratch, "request.json")
        query_protocol.write_json(request, request_path)

        environment = os.environ.copy()
        environment[query_protocol.REQUEST_ENV] = request_path
        stdout_path = os.path.join(scratch, "stdout.log")
        stderr_path = os.path.join(scratch, "stderr.log")

        execution = self.executor.execute(
            self.build_command(hal_binary),
            cwd=scratch,
            env=environment,
            timeout_s=timeout_s,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

        try:
            return self._collect(query, scratch, execution, timeout_s)
        finally:
            if not self.keep_scratch:
                self._cleanup(scratch, execution)

    def _collect(self, query, scratch, execution, timeout_s):
        response_path = os.path.join(scratch, "response.json")
        stderr_tail = tail(os.path.join(scratch, "stderr.log"))

        if execution.timed_out:
            raise Timeout(
                "the query {!r} exceeded its {}s limit and was stopped".format(query, timeout_s),
                detail=stderr_tail or None,
                hint="raise timeout_s, or narrow the query (a smaller cone, a smaller "
                "page); the netlist load alone dominates on a large design",
                data={"timeout_s": timeout_s, "killed": execution.killed, "query": query},
            )

        if not os.path.isfile(response_path):
            raise HalError(
                "hal exited with {} and wrote no answer".format(execution.exit_code),
                detail=stderr_tail or "no stderr output was captured",
                hint="check that $HAL_BASE_PATH points at a build whose plugins load; "
                "the logs of this query are kept in {}".format(scratch),
                data={"exit_code": execution.exit_code, "scratch": scratch},
            )

        try:
            document = query_protocol.read_response(response_path)
        except query_protocol.QueryProtocolError as exc:
            raise Internal(str(exc), detail=stderr_tail or None)

        if document.get("status") != "ok":
            error = document.get("error") or {}
            code = error.get("code", "hal_error")
            message = error.get("message", "the query failed without a message")
            raise ApiError(
                message,
                detail=error.get("detail") or stderr_tail or None,
                hint=error.get("hint"),
                data=error.get("data"),
                code=code if code in _KNOWN_CODES else "hal_error",
            )

        result = document.get("result")
        if not isinstance(result, dict):
            raise Internal(
                "the in-HAL side reported success without a result object",
                detail="response: {}".format(response_path),
            )
        result["query"] = {
            "duration_s": document.get("duration_s"),
            "hal_version": document.get("hal_version"),
            "exit_code": execution.exit_code,
        }
        return result

    def _cleanup(self, scratch, execution):
        """Keep the scratch directory only when it is evidence.

        A successful query leaves nothing behind; a failed one leaves its logs,
        because a log file is the only explanation of why a plugin gave up.
        """
        if not execution.succeeded:
            return
        shutil.rmtree(scratch, ignore_errors=True)
