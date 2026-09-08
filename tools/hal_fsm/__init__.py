"""hal_fsm - propose, solve and explain finite state machines in a netlist.

``plugins/solve_fsm`` already reconstructs a state transition graph *once
somebody hands it the state register and the transition logic*.  Everything
around that is missing: which flip-flops are the state register, what the
initial state is, whether the reset the design has is even modelled, whether
the recovered graph is complete, and how to reach a particular state.

``hal_fsm`` is that workflow, and it is deliberately conservative about what it
claims:

* **Candidate selection is a heuristic and stays one.**  Register candidates
  come from feedback structure (strongly connected components of the flip-flop
  dependency graph), from self-dependent flip-flop clusters, and -- when the
  plugin is available -- from DANA's register groups.  Each candidate is one
  ``heuristic`` finding with a confidence score and the features that produced
  it.  The user can override the selection entirely from the configuration
  file, in which case the choice is recorded as a ``user_provided`` assumption
  instead of as evidence.
* **Solver-verified transitions are a separate claim.**  The transition
  relation returned by ``solve_fsm`` is recorded as
  ``proven_under_assumptions`` -- with every assumption spelled out, including
  the ones the solver silently makes (asynchronous set/reset inputs are *not*
  part of the modelled next-state function, the initial state is a claim about
  power-up, the transition cone must be complete).
* **Nothing is claimed after a limit is hit.**  ``solve_fsm`` is all-or-nothing:
  it returns ``None`` on a solver timeout, never a partial graph.  A run that
  hits a limit therefore reports ``timeout``/``error`` and keeps only the
  structural findings it had already earned.  See ``README.md``, "Partial
  recovery".

Layout (the split is the same as ``tools/hal_viz``, ``tools/hal_findings`` and
``tools/hal_runner``: only two modules touch ``hal_py``)::

    hal_fsm.config        the run/override configuration and its limits
    hal_fsm.candidates    flip-flop dependency graph, candidate proposal, scoring
    hal_fsm.transitions   transition tables, bit orders, reachability, witnesses
    hal_fsm.diagram       Graphviz state diagrams (via hal_viz.dot)
    hal_fsm.reference     ground-truth comparison
    hal_fsm.findings      findings documents (via hal_findings.model)
    hal_fsm.extract       netlist -> dependency graph                (needs hal_py)
    hal_fsm.solve         the solve_fsm wrapper                      (needs hal_py)
    hal_fsm.run           the in-HAL orchestration
    hal_fsm.run_in_hal    the ``hal --python-script`` entry point
    hal_fsm.cli           ``python tools/hal_fsm analyze <netlist>``

Run the unit tests with a plain interpreter from the repository root::

    python -m unittest discover -s tools/hal_fsm -t tools -p "test_*.py"
"""

__version__ = "1.0.0"

#: Environment variable naming the request file of an in-HAL run.
REQUEST_ENV = "HAL_FSM_REQUEST"

__all__ = ["__version__", "REQUEST_ENV"]
