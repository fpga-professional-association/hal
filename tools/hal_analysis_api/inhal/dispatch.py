"""Answering one read-only query inside HAL.

Five steps, none of them guessed at:

1. import ``hal_py`` and load the plugins -- ``--python-script`` takes over
   before HAL loads them, so a script that does not load them itself finds no
   netlist parsers;
2. load the netlist through :mod:`hal_viz.halenv`, the repository's existing
   loader for project directories, ``.hal`` files and HDL + gate library;
3. call the requested function in :mod:`hal_analysis_api.netlist_query`;
4. write the response file;
5. exit 0 on success and non-zero otherwise, because ``hal`` propagates exit
   codes faithfully and the host side treats them as the primary signal.

**Nothing here writes to the project.**  The netlist is loaded, read and
dropped when the process exits; there is no ``ProjectManager.serialize``, no
save, no ``create_*``.  That is the whole enforcement mechanism for "reads do
not modify the project", and it is enforced by the process boundary rather than
by a promise: even a bug in a query function cannot persist anything, because
nobody ever calls a writer.
"""

import os
import time
import traceback

from hal_analysis_api import netlist_query, query_protocol
from hal_analysis_api.errors import ApiError

__all__ = ["QUERIES", "execute"]


def _summary(netlist, params):
    return netlist_query.summary(netlist, max_gate_types=params["max_gate_types"])


def _gates(netlist, params):
    return netlist_query.gates(
        netlist,
        name_contains=params.get("name_contains"),
        gate_type=params.get("gate_type"),
        module_id=params.get("module_id"),
        offset=params["offset"],
        limit=params["limit"],
    )


def _nets(netlist, params):
    return netlist_query.nets(
        netlist,
        name_contains=params.get("name_contains"),
        global_only=params.get("global_only", False),
        offset=params["offset"],
        limit=params["limit"],
    )


def _modules(netlist, params):
    return netlist_query.modules(
        netlist,
        name_contains=params.get("name_contains"),
        offset=params["offset"],
        limit=params["limit"],
    )


def _gate(netlist, params):
    return netlist_query.gate(netlist, params["gate_id"], max_endpoints=params["max_endpoints"])


def _net(netlist, params):
    return netlist_query.net(netlist, params["net_id"], max_endpoints=params["max_endpoints"])


def _cone(netlist, params):
    return netlist_query.cone(
        netlist,
        params["seed_gate_ids"],
        direction=params["direction"],
        depth=params["depth"],
        max_gates=params["max_gates"],
        include_edges=params["include_edges"],
    )


#: Query name -> implementation. The names are the API tool names, so a request
#: cannot ask for something the API does not advertise.
QUERIES = {
    "netlist.summary": _summary,
    "netlist.gates": _gates,
    "netlist.nets": _nets,
    "netlist.modules": _modules,
    "netlist.gate": _gate,
    "netlist.net": _net,
    "netlist.cone": _cone,
}


def _hal_version(hal_py):
    """The HAL version, or ``None``. An unknown version is never invented."""
    for name in ("get_version", "get_version_string"):
        getter = getattr(hal_py, name, None)
        if getter is None:
            continue
        try:
            value = getter()
        except Exception:  # pragma: no cover - defensive against binding changes
            continue
        if value:
            return str(value)
    return None


def execute(request):
    """Run one query request. Returns the process exit code."""
    started = time.time()
    query = request.get("query", "unknown")
    output_dir = request.get("output_dir") or os.getcwd()
    response_path = os.path.join(output_dir, request.get("response_file", "response.json"))
    hal_version = None

    def fail(code, message, detail=None, hint=None, data=None):
        query_protocol.write_json(
            query_protocol.response(
                "error",
                query,
                error={
                    "code": code,
                    "message": message,
                    "detail": detail,
                    "hint": hint,
                    "data": data,
                },
                duration_s=time.time() - started,
                hal_version=hal_version,
            ),
            response_path,
        )
        return 1

    handler = QUERIES.get(query)
    if handler is None:
        return fail(
            "unknown_tool",
            "the in-HAL side has no query {!r}".format(query),
            detail="known queries: {}".format(", ".join(sorted(QUERIES))),
        )

    try:
        from hal_viz.halenv import (
            HalUnavailable,
            NetlistLoadError,
            import_hal_py,
            load_all_plugins,
            load_netlist,
        )
    except ImportError as exc:  # pragma: no cover - a broken checkout
        return fail(
            "internal",
            "could not import hal_viz.halenv from the tools directory: {}".format(exc),
            detail=traceback.format_exc(),
        )

    try:
        hal_py = import_hal_py()
        hal_version = _hal_version(hal_py)
        # The netlist parsers are plugins; without this a .v netlist cannot be read
        # and a project directory may fail to resolve its gate library.
        load_all_plugins(hal_py)
        netlist = load_netlist(hal_py, request["netlist"], request.get("gate_library"))
    except HalUnavailable as exc:
        return fail("hal_unavailable", str(exc), detail=traceback.format_exc())
    except NetlistLoadError as exc:
        return fail(
            "invalid_request",
            str(exc),
            detail=traceback.format_exc(),
            hint="re-open the project with project.open; the handle may point at a "
            "netlist HAL cannot parse without a matching gate library",
        )
    except Exception as exc:  # noqa: BLE001 - any failure here fails the query
        return fail(
            "hal_error",
            "could not load the netlist: {}: {}".format(type(exc).__name__, exc),
            detail=traceback.format_exc(),
        )

    try:
        result = handler(netlist, request.get("params") or {})
    except ApiError as exc:
        # A typed API error survives the process boundary as itself: an unknown
        # gate id must not arrive on the other side as "hal exited with 1".
        return fail(exc.code, exc.message, detail=exc.detail, hint=exc.hint, data=exc.data)
    except Exception as exc:  # noqa: BLE001 - the query raised
        return fail(
            "internal",
            "{} raised {}: {}".format(query, type(exc).__name__, exc),
            detail=traceback.format_exc(),
        )

    query_protocol.write_json(
        query_protocol.response(
            "ok",
            query,
            result=result,
            duration_s=time.time() - started,
            hal_version=hal_version,
        ),
        response_path,
    )
    return 0
