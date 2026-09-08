"""hal_runner - reproducible, headless HAL analysis runs.

A HAL analysis today is a hand-assembled sequence of plugin calls: someone
unpacks a project, opens a shell, runs DANA or graph_algorithm, and drops files
somewhere.  Nothing about that is reproducible -- there is no record of which
netlist was used, which HAL built it, what the analysis was configured with, or
whether a step finished at all.

``hal_runner`` makes the *run* the artifact.  A run configuration (plain JSON,
validated against a versioned schema) declares the netlist, the gate library,
an ordered list of analysis steps with their configuration and per-step
time/memory limits, and an output directory.  Executing it produces a manifest
that pins every input by content hash, records the HAL version the steps ran
against, every step's configuration digest, outcome and artifact hashes, and a
findings document per step written through :mod:`hal_findings`.

Design decisions worth knowing before reading the code:

*Steps run in their own process.*  Each step is a ``hal --python-script``
invocation.  That is the only way to get a hard time limit: HAL analyses are
C++ code with no cancellation points, so a thread or a signal handler cannot
stop one.  A process can be killed.  It also means a step that segfaults or
exhausts memory costs a step, not the whole run, and that the exit code of
``hal`` (trustworthy since issue #11) is the primary success signal.

*Nothing is trusted, including our own steps.*  A step that exits 0 but writes
no result record, or writes a findings document that fails validation, is a
failed step.  Silence is never success.

*A cache entry is proof, not a promise.*  Checkpoint reuse is keyed by the
input digests, the step configuration digest and the tool versions, and the
stored entry is re-verified against its recorded artifact hashes before it is
reused.  Anything that does not add up is a miss, never a stale answer.

Layout (mirrors ``tools/hal_viz`` and ``tools/hal_findings``: everything except
:mod:`hal_runner.steps` runs on a plain interpreter with no HAL build)::

    hal_runner.config       run configuration parsing + schema validation
    hal_runner.analyses     the registry of analyses a step may name
    hal_runner.hashing      file, directory and JSON digests
    hal_runner.execute      subprocess execution with timeout, kill and logs
    hal_runner.checkpoint   content-addressed checkpoint store
    hal_runner.manifest     run manifest construction and deterministic output
    hal_runner.runner       the orchestrator
    hal_runner.cli          ``python tools/hal_runner run <config.json>``
    hal_runner.steps.*      the in-HAL side, executed by ``hal --python-script``

Run the unit tests with a plain interpreter from the repository root::

    python -m unittest discover -s tools/hal_runner -t tools -p "test_*.py"
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
