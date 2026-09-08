"""Every bound this API enforces, in one place.

They exist because the caller is usually a language model with a context
window, and a 400-gate netlist is small: ``examples/crypto_trojan.zip`` is not.
The numbers are deliberately conservative -- a caller that needs more asks for
more, one page at a time, and can see from ``page.has_more`` that there *is*
more.  The schema enforces the maxima structurally so an over-large request is
rejected before anything runs; :func:`paginate` and :func:`truncation` make the
resulting cut visible in the response.
"""

__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "DEFAULT_FINDINGS_LIMIT",
    "MAX_FINDINGS_LIMIT",
    "DEFAULT_GATE_TYPES",
    "MAX_GATE_TYPES",
    "DEFAULT_ENDPOINTS",
    "MAX_ENDPOINTS",
    "DEFAULT_CONE_DEPTH",
    "MAX_CONE_DEPTH",
    "DEFAULT_CONE_GATES",
    "MAX_CONE_GATES",
    "MAX_CONE_SEEDS",
    "MAX_CONE_EDGES",
    "DEFAULT_QUERY_TIMEOUT_S",
    "MAX_QUERY_TIMEOUT_S",
    "DEFAULT_ANALYSIS_TIMEOUT_S",
    "MAX_ANALYSIS_TIMEOUT_S",
    "MAX_WAIT_S",
    "DEFAULT_ARTIFACT_BYTES",
    "MAX_ARTIFACT_BYTES",
    "as_json",
    "page",
    "paginate",
    "truncation",
]

#: Listings (gates, nets, modules, jobs).
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 500

#: Findings are much larger per item than a gate reference.
DEFAULT_FINDINGS_LIMIT = 20
MAX_FINDINGS_LIMIT = 200

#: Gate-type histogram entries in ``netlist.summary``.
DEFAULT_GATE_TYPES = 32
MAX_GATE_TYPES = 256

#: Endpoints listed per pin direction in ``netlist.gate`` / ``netlist.net``.
DEFAULT_ENDPOINTS = 64
MAX_ENDPOINTS = 1000

#: ``netlist.cone``.
DEFAULT_CONE_DEPTH = 3
MAX_CONE_DEPTH = 32
DEFAULT_CONE_GATES = 200
MAX_CONE_GATES = 2000
MAX_CONE_SEEDS = 64
MAX_CONE_EDGES = 8000

#: A netlist query is one ``hal`` process; the limit is a wall clock.
DEFAULT_QUERY_TIMEOUT_S = 300
MAX_QUERY_TIMEOUT_S = 3600

#: Analyses run detached, so their limit is the runner's per-step limit.
DEFAULT_ANALYSIS_TIMEOUT_S = 900
MAX_ANALYSIS_TIMEOUT_S = 86400

#: ``analysis.status`` may block this long before answering "still running".
MAX_WAIT_S = 300

#: ``artifact.get`` never returns a whole DOT file by accident.
DEFAULT_ARTIFACT_BYTES = 65536
MAX_ARTIFACT_BYTES = 1048576


def as_json():
    """The limits as ``hal.capabilities`` reports them."""
    return {
        "page": {"default": DEFAULT_PAGE_LIMIT, "max": MAX_PAGE_LIMIT},
        "findings_page": {"default": DEFAULT_FINDINGS_LIMIT, "max": MAX_FINDINGS_LIMIT},
        "gate_types": {"default": DEFAULT_GATE_TYPES, "max": MAX_GATE_TYPES},
        "endpoints": {"default": DEFAULT_ENDPOINTS, "max": MAX_ENDPOINTS},
        "cone": {
            "depth": {"default": DEFAULT_CONE_DEPTH, "max": MAX_CONE_DEPTH},
            "gates": {"default": DEFAULT_CONE_GATES, "max": MAX_CONE_GATES},
            "seeds": {"max": MAX_CONE_SEEDS},
            "edges": {"max": MAX_CONE_EDGES},
        },
        "query_timeout_s": {"default": DEFAULT_QUERY_TIMEOUT_S, "max": MAX_QUERY_TIMEOUT_S},
        "analysis_timeout_s": {
            "default": DEFAULT_ANALYSIS_TIMEOUT_S,
            "max": MAX_ANALYSIS_TIMEOUT_S,
        },
        "wait_s": {"max": MAX_WAIT_S},
        "artifact_bytes": {"default": DEFAULT_ARTIFACT_BYTES, "max": MAX_ARTIFACT_BYTES},
    }


def page(offset, limit, returned, total):
    """The pagination record that accompanies every listing.

    ``has_more`` and ``next_offset`` exist so a caller never has to compute
    whether it has seen everything -- getting that arithmetic wrong is how an
    agent silently analyses half a netlist.
    """
    offset = int(offset)
    limit = int(limit)
    returned = int(returned)
    total = int(total)
    has_more = offset + returned < total
    return {
        "offset": offset,
        "limit": limit,
        "returned": returned,
        "total": total,
        "has_more": has_more,
        "next_offset": offset + returned if has_more else None,
    }


def paginate(items, offset, limit):
    """Slice ``items`` and return ``(page_items, page_record)``.

    An ``offset`` past the end is *not* an error: it is an empty page with
    ``total`` still reported, which is what a caller walking a listing that
    shrank underneath it needs to see.
    """
    items = list(items)
    offset = max(0, int(offset))
    limit = max(1, int(limit))
    window = items[offset : offset + limit]
    return window, page(offset, limit, len(window), len(items))


def truncation(truncated, reason=None, limit=None, kind=None):
    """The record that says whether a bounded traversal was cut, and why."""
    return {
        "truncated": bool(truncated),
        "reason": reason if truncated else None,
        "limit": limit if truncated else None,
        "kind": kind if truncated else None,
    }
