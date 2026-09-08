"""The only module that imports ``hal_py``.

Two things happen here that are worth stating plainly.

**The cone functions come from HAL, not from a re-implementation.**
``SubgraphNetlistDecorator.get_subgraph_function`` is what builds them, so gate
semantics are whatever the gate library says.  The only rewriting this module
does is renaming the *boundary* variables (which HAL names ``net_<id>``, unique
per netlist and therefore useless across two of them) to the shared names
:mod:`hal_semantic_diff.cones` derives with the ``z3_utils`` scheme.

**The counterexample comes from HAL's own SMT bindings.**
``z3_utils``'s ``compare_nets`` binding returns ``Optional[bool]`` and its C++
implementation explicitly builds its query ``without_model_generation()``, so no
witness can be recovered through it -- that is a property of the plugin, not of
this tool.  ``hal_py.SMT`` *does* expose ``QueryConfig.with_model_generation()``,
``SolverResult.model`` and ``Model.model``, so this module asks HAL's solver
directly and keeps the assignment.  No ``z3`` Python module is needed, and the
query is the same one ``compare_nets_internal`` builds: "is there an assignment
under which the two subgraph functions differ".

The ``z3_utils`` binding is still used, as a *cross-check* on the verdict, when
it is importable and the correspondence is the identity mapping it requires.
A disagreement is reported; it is never resolved silently.
"""

import os
import time

from .compare import SolveResult

__all__ = [
    "HalUnavailable",
    "NoSolverAvailable",
    "import_hal_py",
    "load_plugins",
    "import_z3_utils",
    "hal_version",
    "select_solver",
    "SmtEngine",
]


class HalUnavailable(RuntimeError):
    """``hal_py`` (or something it needs) is not importable."""


class NoSolverAvailable(RuntimeError):
    """No local SMT solver this build can call is installed."""


#: Preference order for the local solver, most-tested first.  ``Z3`` is what
#: ``z3_utils`` uses, and HAL's *library* call path for Z3 is a stub that
#: returns an error (``Z3::query_library`` in ``src/netlist/boolean_function/
#: solver.cpp``) -- so Z3 means the ``z3`` binary on ``PATH``.
_SOLVER_PREFERENCE = (
    ("Z3", "Binary"),
    ("Bitwuzla", "Library"),
    ("Bitwuzla", "Binary"),
    ("Boolector", "Binary"),
)


def select_solver(hal_py):
    """Pick a local solver this build can actually call.

    Returns ``(SolverType, SolverCall, "Z3/Binary")``.  Raises
    :class:`NoSolverAvailable` rather than letting every query fail one by one
    and turning a missing tool into a pile of ``error`` findings.
    """
    smt = hal_py.SMT
    for type_name, call_name in _SOLVER_PREFERENCE:
        solver_type = getattr(smt.SolverType, type_name, None)
        solver_call = getattr(smt.SolverCall, call_name, None)
        if solver_type is None or solver_call is None:
            continue
        try:
            available = smt.Solver.has_local_solver_for(solver_type, solver_call)
        except Exception:  # pragma: no cover - defensive against binding changes
            available = False
        if available:
            return solver_type, solver_call, "{}/{}".format(type_name, call_name)
    raise NoSolverAvailable(
        "no local SMT solver is available. This check needs one of {} -- in practice "
        "the 'z3' binary on PATH, which is also what z3_utils shells out to. Install it "
        "(apt-get install z3, brew install z3) or build HAL against Bitwuzla.".format(
            ", ".join("{}/{}".format(*entry) for entry in _SOLVER_PREFERENCE)
        )
    )


def import_hal_py(extra_paths=()):
    """Import ``hal_py``, reusing ``hal_viz.halenv`` so there is one loader."""
    try:
        from hal_viz.halenv import HalUnavailable as VizUnavailable
        from hal_viz.halenv import import_hal_py as viz_import
    except ImportError as exc:  # pragma: no cover - broken checkout
        raise HalUnavailable(
            "could not import hal_viz.halenv from the tools directory: {}".format(exc)
        )
    try:
        return viz_import(extra_paths)
    except VizUnavailable as exc:
        raise HalUnavailable(str(exc))


def load_plugins(hal_py):
    """Load HAL's plugins; required before any parser or plugin is available."""
    from hal_viz.halenv import load_all_plugins

    load_all_plugins(hal_py)


def load_netlist(hal_py, path, gate_library=None):
    """Load a project directory, a ``.hal`` file or an HDL netlist."""
    from hal_viz.halenv import NetlistLoadError, load_netlist as viz_load

    try:
        return viz_load(hal_py, path, gate_library)
    except NetlistLoadError as exc:
        raise HalUnavailable(str(exc))


def import_z3_utils():
    """Return the ``z3_utils`` bindings, or ``None`` when the plugin is absent."""
    try:
        module = __import__("hal_plugins.z3_utils", fromlist=["z3_utils"])
    except ImportError:
        return None
    return module if hasattr(module, "compare_nets") else None


def hal_version(hal_py):
    for attribute in ("get_version", "__version__"):
        value = getattr(hal_py, attribute, None)
        if value is None:
            continue
        try:
            return str(value() if callable(value) else value)
        except Exception:  # pragma: no cover - defensive
            continue
    return None


def _value_to_string(value, size):
    """Render an SMT model value as a bit string, LSB last."""
    size = int(size or 1)
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    if size <= 1:
        return "1" if number & 1 else "0"
    return format(number & ((1 << size) - 1), "0{}b".format(size))


class SmtEngine(object):
    """The engine :mod:`hal_semantic_diff.compare` drives, backed by ``hal_py``."""

    #: Human name recorded on the findings ``solver`` object.
    name = "hal_py.SMT"

    def __init__(self, hal_py, generate_model=True, solver=None):
        self.hal_py = hal_py
        self.generate_model = bool(generate_model)
        self.solver_type, self.solver_call, self.solver_label = solver or select_solver(hal_py)
        self.queries = 0
        self.solver_wall_time_s = 0.0
        self._decorators = {}

    # -- functions ---------------------------------------------------------------

    def _decorator(self, netlist):
        key = id(netlist)
        if key not in self._decorators:
            self._decorators[key] = self.hal_py.SubgraphNetlistDecorator(netlist)
        return self._decorators[key]

    def _net_variable(self, net):
        return self.hal_py.BooleanFunctionNetDecorator(net).get_boolean_variable_name()

    def cone_function(self, netlist, cone):
        """Build the Boolean function of ``cone`` over its shared boundary names."""
        boolean_function = self.hal_py.BooleanFunction

        if not cone.gates:
            # The observation point *is* a boundary (a registered output, say):
            # its function is exactly that one variable. HAL's subgraph decorator
            # refuses an empty gate list, and rightly so.
            if len(cone.boundaries) != 1:
                raise RuntimeError(
                    "cone of {!r} has no gates but {} boundary nets".format(
                        cone.output_net, len(cone.boundaries)
                    )
                )
            return boolean_function.Var(cone.boundaries[0].name, 1)

        function = self._decorator(netlist).get_subgraph_function(
            list(cone.gates), cone.output_net
        )
        if function is None:
            raise RuntimeError(
                "SubgraphNetlistDecorator.get_subgraph_function returned None; see the "
                "HAL log for the underlying error"
            )

        substitutions = {}
        for boundary in cone.boundaries:
            variable = self._net_variable(boundary.net)
            if variable != boundary.name:
                substitutions[variable] = boolean_function.Var(boundary.name, 1)
        if substitutions:
            # The dict overload substitutes simultaneously; renaming one at a time
            # could chain a rename into a name that was itself about to be renamed.
            renamed = function.substitute(substitutions)
            if renamed is None:
                raise RuntimeError(
                    "could not rename the boundary variables of the cone of {!r}".format(
                        cone.output_net
                    )
                )
            function = renamed
        return function

    def function_variables(self, function):
        return set(function.get_variable_names())

    def function_text(self, function):
        return str(function)

    # -- solving -----------------------------------------------------------------

    def solve_difference(self, function_a, function_b, timeout_s):
        """Ask whether the two functions can differ; keep the model when they can."""
        smt = self.hal_py.SMT
        boolean_function = self.hal_py.BooleanFunction

        difference = function_a ^ function_b
        constraint = smt.Constraint(difference, boolean_function.Const(1, 1))

        config = smt.QueryConfig().with_local_solver().with_timeout(int(timeout_s))
        config = (
            config.with_model_generation()
            if self.generate_model
            else config.without_model_generation()
        )
        config = config.with_solver(self.solver_type).with_call(self.solver_call)

        started = time.time()
        try:
            result = smt.Solver([constraint]).query(config)
        except Exception as exc:  # noqa: BLE001 - a raising binding is a failed query
            elapsed = round(time.time() - started, 4)
            self.solver_wall_time_s += elapsed
            return SolveResult(
                "error",
                message="{}: {}".format(type(exc).__name__, exc),
                wall_time_s=elapsed,
            )
        elapsed = round(time.time() - started, 4)
        self.queries += 1
        self.solver_wall_time_s += elapsed

        if result is None:
            return SolveResult(
                "error",
                message=(
                    "hal_py.SMT.Solver.query() returned None; the solver could not be "
                    "run (see the HAL log). A missing solver is not equivalence."
                ),
                wall_time_s=elapsed,
            )
        if result.is_unsat():
            return SolveResult("unsat", wall_time_s=elapsed)
        if result.is_unknown():
            # HAL does not report *why* a query is unknown. Attributing it to the
            # timeout only when the query actually ran that long is the honest
            # reading; either way it is never equivalence.
            return SolveResult(
                "unknown",
                timed_out=elapsed >= max(0.0, float(timeout_s) * 0.95),
                wall_time_s=elapsed,
            )

        model = {}
        raw_model = getattr(result, "model", None)
        entries = getattr(raw_model, "model", None) if raw_model is not None else None
        for name, value in (entries or {}).items():
            if isinstance(value, (tuple, list)) and len(value) == 2:
                model[name] = _value_to_string(value[0], value[1])
            else:
                model[name] = str(value)
        return SolveResult("sat", model=model, wall_time_s=elapsed)

    # -- replay ------------------------------------------------------------------

    def evaluate(self, function, assignment):
        """Evaluate ``function`` under a ``{name: "0"/"1"}`` assignment."""
        values = self.hal_py.BooleanFunction.Value
        inputs = {}
        for name, value in assignment.items():
            text = str(value).strip()
            if text in ("1", "0b1", "true", "True"):
                inputs[name] = values.ONE
            elif text in ("0", "0b0", "false", "False"):
                inputs[name] = values.ZERO
            else:
                # A multi-bit or unknown value cannot be replayed through a
                # single-bit evaluation; say so instead of guessing.
                return None
        try:
            result = function.evaluate(inputs)
        except Exception:  # noqa: BLE001 - an evaluation that raises is not a value
            return None
        if result is None:
            return None
        if result == values.ONE:
            return "1"
        if result == values.ZERO:
            return "0"
        return str(result)

    # -- cross-check --------------------------------------------------------------

    def cross_check(self, z3_utils, netlist_a, netlist_b, point, timeout_s):
        """Ask ``z3_utils.compare_nets`` about the same pair, for comparison.

        Returns a dict describing what the plugin said, or ``None`` when the
        plugin is unavailable.  The plugin's boolean cannot distinguish an
        undecided query from a decided one, so both polarities are queried:
        agreement between them is what makes the answer meaningful at all.
        """
        if z3_utils is None:
            return None
        try:
            strict = z3_utils.compare_nets(
                netlist_a, netlist_b, point.net_a, point.net_b, True, int(timeout_s)
            )
            lenient = z3_utils.compare_nets(
                netlist_a, netlist_b, point.net_a, point.net_b, False, int(timeout_s)
            )
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": "{}: {}".format(type(exc).__name__, exc)}
        if strict is None or lenient is None:
            return {"available": False, "error": "compare_nets returned None"}
        if strict and lenient:
            verdict = "equivalent"
        elif not strict and not lenient:
            verdict = "different"
        else:
            verdict = "undecided"
        return {
            "available": True,
            "fail_on_unknown_true": bool(strict),
            "fail_on_unknown_false": bool(lenient),
            "verdict": verdict,
        }


def read_hal_env_paths():
    """``HAL_PY_PATH`` split the way ``hal_viz`` splits it."""
    value = os.environ.get("HAL_PY_PATH", "")
    return [entry for entry in value.split(os.pathsep) if entry]
