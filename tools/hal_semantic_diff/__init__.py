"""hal_semantic_diff -- what *behaviour* changed between two builds of a design.

A textual or graph diff of two synthesized netlists says that something moved.
It does not say whether the circuit still computes the same thing.  This tool
answers the narrower, useful question: for a pair of builds with the same
boundary and the same register names, which observation points still implement
the same function, which ones changed, and -- when one changed -- what input
assignment makes them differ.

The package is split so that everything except :mod:`hal_semantic_diff.halbridge`
imports on a plain interpreter and is unit tested without a built HAL:

``hal_semantic_diff.correspondence``
    The explicit mapping between the two builds: which sequential gate
    corresponds to which, which top-level pins are shared, and which
    observation points follow from that.  Duck-typed; never imports ``hal_py``.
``hal_semantic_diff.cones``
    Combinational cone extraction and canonical structural signatures, used
    both to localize a change and as a sound fast path.  Duck-typed.
``hal_semantic_diff.compare``
    The comparison itself.  It talks to the solver through an injected engine
    object, so the whole classification pipeline -- including counterexample
    replay -- is exercised in the unit tests against a pure-Python engine.
``hal_semantic_diff.findings``
    Assembly of a ``hal_findings`` document from comparison outcomes.
``hal_semantic_diff.diagrams``
    Changed-cone diagrams, built on ``hal_viz.dot`` and rendered with
    ``hal_viz.render``.
``hal_semantic_diff.report``
    A dependency-free static HTML report over findings documents.
``hal_semantic_diff.halbridge``
    The only module that imports ``hal_py``: subgraph functions, the SMT
    solver and the ``z3_utils`` cross-check.
``hal_semantic_diff.cli``
    Argument parsing and the command implementations.

Run ``python tools/hal_semantic_diff --help``.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
