"""hal_analysis_api -- a bounded, structured analysis API for agents.

An agent driving HAL today has one interface: write Python, run it through
``hal --python-script``, read whatever it printed.  That works for a human who
can look at 400 lines of gate names and decide what matters, and it fails for
an agent, which either receives a dump it cannot afford to read or a summary it
cannot verify.  Both failure modes are the same mistake: *the interface has no
bounds*.

This package is the local API that fixes that, and the MCP server in
:mod:`hal_analysis_api.mcp_adapter` is a thin adapter over it -- never the other
way round.  Everything an agent can ask for is:

* a **named tool** with a JSON Schema for its request *and* its response
  (``hal.schema``, and :mod:`hal_analysis_api.schemas`);
* answered inside an **explicit envelope**: ``{"ok": true, "result": ...}`` or
  ``{"ok": false, "error": {"code": ..., ...}}``, never a bare payload and
  never a stack trace;
* **bounded**: every listing is paginated, every unbounded traversal (a cone,
  an endpoint list, an artifact body) carries a ``truncation`` object saying
  whether it was cut and why.  A truncated answer is never silently short.

The four groups of operations, in the order one investigation uses them:

``hal.capabilities`` / ``hal.schema`` / ``analysis.list``
    what this build can do, and what each tool accepts.  Reuses
    :mod:`hal_runner.analyses` as the registry of analyses, so the API cannot
    advertise an analysis the runner would refuse.
``project.open`` / ``project.list`` / ``project.describe`` / ``project.close``
    explicit, content-pinned project handles.  Every read names a handle; a
    stale or invented one is an ``unknown_project`` error, never a guess.
``netlist.*``
    read-only, scoped queries: a summary, filtered listings, one object,
    or a bounded fan-in/fan-out cone.  These never modify the project -- the
    in-HAL side loads the netlist, answers, and exits without saving.
``analysis.submit`` / ``analysis.status`` / ``findings.get`` / ``artifact.*``
    analyses run through :mod:`hal_runner`, which already pins inputs, enforces
    limits and validates findings.  The API adds only submission, status and
    bounded retrieval on top of it.

Layout (everything except :mod:`hal_analysis_api.inhal` runs on a plain
interpreter with no HAL build, which is what makes the test suite meaningful on
a machine that cannot build HAL)::

    errors.py          typed errors and the response envelope
    limits.py          the numeric bounds, in one place
    schema/            the versioned request/response schemas
    schemas.py         the tool registry and request/response validation
    handles.py         project handles, pinned by content digest
    netlist_query.py   the read-only queries, duck-typed against hal_py
    inhal/             the in-HAL side, run by 'hal --python-script'
    queries.py         host side of a netlist query (subprocess + limits)
    jobs.py            analysis jobs on top of hal_runner
    worker.py          the detached process that executes one job
    api.py             the dispatch table -- the API itself
    cli.py             'python tools/hal_analysis_api call <tool> ...'
    mcp_adapter.py     optional MCP transport (mcp 1.x and 2.x)

Run the unit tests with a plain interpreter from the repository root::

    python -m unittest discover -s tools/hal_analysis_api -t tools -p "test_*.py"
"""

__version__ = "1.0.0"

#: Version of the tool set and the envelope. Bumped when a tool's request or
#: response changes meaning; the schema file carries the same version.
API_VERSION = "1.0.0"

__all__ = ["__version__", "API_VERSION"]
