"""hal_explain - compose existing analyses into an evidence-linked block model.

Three analyses in this repository already recover *something* about a stripped
netlist, and none of them produces an explanation:

* dataflow analysis (DANA) groups flip-flops into candidate word-level
  registers -- a structural guess, and nothing more;
* ``module_identification`` proves that a cone of gates implements a specific
  word-level operation -- an addition, a counter, a comparison -- by formal
  verification against a reference function;
* ``solve_fsm`` (through :mod:`hal_fsm`) recovers a state transition relation
  for a state register somebody nominated.

``hal_explain`` reads their *results* -- the shared ``tools/hal_findings``
documents they already write -- and composes them into one **recovered-block
model**: a versioned JSON document in which every block carries the gates it is
made of and every claim about it carries a reference back to the finding that
produced it.

Three properties are the whole point:

* **A verified function and a heuristic label are never the same thing.**  A
  block's ``confidence`` is derived from the *status* of the findings that
  support it, not from how confident the tool feels.  ``proven_*`` becomes
  ``verified``, ``heuristic`` stays ``heuristic``, and ``unknown``/``timeout``/
  ``error``/``unsupported`` becomes ``unknown``.  The diagram draws the three
  differently and the report says which is which in words.
* **Unclassified gates stay in the model.**  Every gate that no analysis
  claimed ends up in an explicit ``unknown_region`` -- a connected component of
  unclaimed gates -- which is drawn in the block diagram and listed in the
  report.  A block diagram that shows only the parts a tool understood is a
  misleading block diagram.
* **Nothing here needs a language model.**  Composition, the diagram and the
  report are deterministic templates over structured evidence.  ``explain
  prose`` (optional, off by default, and never part of the core path) is the
  only place an LLM could ever be involved, and it consumes the same JSON a
  reader can check by hand.

Layout (the same split the rest of the fork uses: exactly one module touches
``hal_py``)::

    hal_explain.schema        the versioned recovered-block schema
    hal_explain.model         builders for the intermediate representation
    hal_explain.serialize     deterministic JSON + digests
    hal_explain.validate      schema + cross-reference validation
    hal_explain.inventory     the netlist snapshot blocks are resolved against
    hal_explain.adapters.*    findings documents -> block contributions
    hal_explain.compose       contributions + inventory -> the block model
    hal_explain.diagram       deterministic Graphviz block diagram (hal_viz.dot)
    hal_explain.report        the templated Markdown/text report
    hal_explain.collect       netlist + plugins -> inventory + findings   (hal_py)
    hal_explain.run_in_hal    the ``hal --python-script`` entry point
    hal_explain.cli           ``python tools/hal_explain ...``

Run the unit tests with a plain interpreter from the repository root::

    python -m unittest discover -s tools/hal_explain -t tools -p "test_*.py"
"""

__version__ = "1.0.0"

#: Environment variable naming the request file of an in-HAL collect run.
REQUEST_ENV = "HAL_EXPLAIN_REQUEST"

__all__ = ["__version__", "REQUEST_ENV"]
