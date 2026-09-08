"""The user-facing configuration: overrides, limits and the initial state.

Candidate proposal is a heuristic, so the one thing this tool must never do is
make its guess unfalsifiable.  Every part of the guess is overridable from a
plain JSON file, and every override is carried into the findings as an
assumption of kind ``user_provided`` rather than as evidence.

Unknown keys are rejected.  A configuration file whose ``max_state_bits`` was
typed as ``max_state_bit`` would otherwise run with the default limit and look
like it honoured the one that was asked for.
"""

import json
import os

__all__ = [
    "ConfigError",
    "Limits",
    "Override",
    "Configuration",
    "DEFAULT_LIMITS",
    "load",
    "from_dict",
]

#: Hard ceiling from ``solve_fsm``: it encodes a state in a ``u64``.
SOLVE_FSM_MAX_STATE_BITS = 64


class ConfigError(ValueError):
    """Raised when a configuration file cannot be used as written."""


DEFAULT_LIMITS = {
    # Refusing a candidate before the solver is called is the only limit that
    # actually protects anything: solve_fsm has no cancellation point.
    "max_state_bits": 12,
    "max_candidates": 8,
    "max_solved_candidates": 1,
    # Applied after the fact, to what the solver returned.
    "max_states": 1024,
    "max_transitions": 8192,
    # Passed to solve_fsm as its per-SMT-query timeout (milliseconds there).
    "smt_timeout_s": 60.0,
    # Our own wall clock around the solver call.  Not a hard limit inside one
    # process -- see README, "Limits are enforced at the process boundary".
    "wall_time_s": 600.0,
    # Brute-force fallback budget: 2**state_bits * 2**inputs evaluations.
    "brute_force_max_states": 256,
    # Witness search and the determinism/totality check.
    "max_cycles": 32,
    "max_condition_vars": 16,
    # Diagram scoping.
    "max_diagram_states": 64,
}

_LIMIT_TYPES = {
    "max_state_bits": int,
    "max_candidates": int,
    "max_solved_candidates": int,
    "max_states": int,
    "max_transitions": int,
    "smt_timeout_s": float,
    "wall_time_s": float,
    "brute_force_max_states": int,
    "max_cycles": int,
    "max_condition_vars": int,
    "max_diagram_states": int,
}

_SOLVER_MODES = ("auto", "smt", "brute_force")
_INITIAL_STATE_SOURCES = ("from_init_attribute", "zero", "explicit")
#: How a non-zero initial state is turned into ``solve_fsm``'s ``initial_state``
#: argument.  See :func:`hal_fsm.transitions.initial_state_argument` for why
#: this is a choice at all.
_INITIAL_STATE_ENCODINGS = ("auto", "transition_index", "argument_order")

_TOP_LEVEL_KEYS = (
    "config_version",
    "name",
    "description",
    "state_registers",
    "transition_logic",
    "exclude_gates",
    "initial_state",
    "initial_state_encoding",
    "solver",
    "solve",
    "targets",
    "reference",
    "limits",
    "ambiguity_margin",
    "min_confidence",
    "emit_diagram",
    "diagram_base",
)

CONFIG_VERSION = "1.0.0"


def _require_type(value, types, what):
    if not isinstance(value, types) or isinstance(value, bool) and bool not in (
        types if isinstance(types, tuple) else (types,)
    ):
        raise ConfigError(
            "{} must be {}, got {!r}".format(
                what,
                " or ".join(t.__name__ for t in (types if isinstance(types, tuple) else (types,))),
                value,
            )
        )
    return value


def _string_list(value, what):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError("{} must be a list of gate names or numeric IDs".format(what))
    out = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, (str, int)):
            raise ConfigError(
                "{} entries must be gate names (string) or gate IDs (integer), got "
                "{!r}".format(what, entry)
            )
        out.append(str(entry))
    return out


class Limits(object):
    """Resolved resource limits, defaults included.

    They are resolved -- rather than looked up lazily -- so that the findings
    document can state the limit the run was *actually* subject to, and so that
    two runs with the same file are comparable.
    """

    def __init__(self, values=None):
        resolved = dict(DEFAULT_LIMITS)
        for key, value in (values or {}).items():
            if key not in DEFAULT_LIMITS:
                raise ConfigError(
                    "unknown limit {!r}; known limits are {}".format(
                        key, ", ".join(sorted(DEFAULT_LIMITS))
                    )
                )
            expected = _LIMIT_TYPES[key]
            if expected is float:
                _require_type(value, (int, float), "limit {!r}".format(key))
                value = float(value)
            else:
                _require_type(value, int, "limit {!r}".format(key))
            if value <= 0:
                raise ConfigError("limit {!r} must be positive, got {!r}".format(key, value))
            resolved[key] = value
        if resolved["max_state_bits"] > SOLVE_FSM_MAX_STATE_BITS:
            raise ConfigError(
                "max_state_bits is {}, but solve_fsm encodes a state in a u64 and "
                "refuses more than {} state flip-flops".format(
                    resolved["max_state_bits"], SOLVE_FSM_MAX_STATE_BITS
                )
            )
        self._values = resolved

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name)

    def __getitem__(self, name):
        return self._values[name]

    def to_json(self):
        return dict(self._values)


class Override(object):
    """An explicit, user-supplied selection.  Never scored, always recorded."""

    def __init__(self, state_registers=(), transition_logic=(), exclude_gates=()):
        self.state_registers = list(state_registers)
        self.transition_logic = list(transition_logic)
        self.exclude_gates = list(exclude_gates)

    @property
    def active(self):
        return bool(self.state_registers)

    def to_json(self):
        out = {}
        if self.state_registers:
            out["state_registers"] = list(self.state_registers)
        if self.transition_logic:
            out["transition_logic"] = list(self.transition_logic)
        if self.exclude_gates:
            out["exclude_gates"] = list(self.exclude_gates)
        return out


class Configuration(object):
    """Everything a run needs that is not the netlist itself."""

    def __init__(
        self,
        name=None,
        description=None,
        override=None,
        initial_state=None,
        initial_state_encoding="auto",
        solver="auto",
        solve="best",
        targets=(),
        reference=None,
        limits=None,
        ambiguity_margin=0.05,
        min_confidence=0.0,
        emit_diagram=True,
        diagram_base=2,
        source_path=None,
    ):
        self.name = name
        self.description = description
        self.override = override or Override()
        #: ``None`` (derive), ``"zero"``, an ``int``, or ``{gate: bit}``.
        self.initial_state = initial_state
        self.initial_state_encoding = initial_state_encoding
        self.solver = solver
        self.solve = solve
        self.targets = list(targets)
        self.reference = reference
        self.limits = limits or Limits()
        self.ambiguity_margin = ambiguity_margin
        self.min_confidence = min_confidence
        self.emit_diagram = emit_diagram
        self.diagram_base = diagram_base
        self.source_path = source_path

    # -- serialization ---------------------------------------------------

    def to_json(self):
        """The resolved configuration, defaults included.

        It round-trips: :func:`from_dict` accepts exactly what this produces, so
        the CLI can resolve a configuration outside HAL, hand it to the in-HAL
        side as JSON, and have that side re-validate the same document rather
        than a summary of it.  It is also what lands in the findings document's
        ``analysis.configuration``.
        """
        out = {
            "config_version": CONFIG_VERSION,
            "solver": self.solver,
            "solve": self.solve,
            "initial_state_encoding": self.initial_state_encoding,
            "ambiguity_margin": self.ambiguity_margin,
            "min_confidence": self.min_confidence,
            "emit_diagram": self.emit_diagram,
            "diagram_base": self.diagram_base,
            "limits": self.limits.to_json(),
        }
        if self.name:
            out["name"] = self.name
        if self.description:
            out["description"] = self.description
        out.update(self.override.to_json())
        if self.initial_state is not None:
            out["initial_state"] = (
                self.initial_state
                if not isinstance(self.initial_state, dict)
                else dict(self.initial_state)
            )
        if self.targets:
            out["targets"] = list(self.targets)
        if self.reference:
            out["reference"] = self.reference
        return out

    @property
    def initial_state_source(self):
        """Where the initial state comes from -- reported as an assumption."""
        if self.initial_state is None:
            return "from_init_attribute"
        if self.initial_state == "zero":
            return "zero"
        return "explicit"


def from_dict(document, source_path=None):
    """Build a :class:`Configuration` from a parsed JSON document."""
    if not isinstance(document, dict):
        raise ConfigError("a hal_fsm configuration must be a JSON object")

    unknown = sorted(set(document) - set(_TOP_LEVEL_KEYS))
    if unknown:
        raise ConfigError(
            "unknown configuration key(s): {}. Known keys are {}".format(
                ", ".join(unknown), ", ".join(sorted(_TOP_LEVEL_KEYS))
            )
        )

    version = document.get("config_version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        raise ConfigError(
            "configuration declares config_version {!r}; this hal_fsm understands "
            "{!r}".format(version, CONFIG_VERSION)
        )

    override = Override(
        state_registers=_string_list(document.get("state_registers"), "state_registers"),
        transition_logic=_string_list(document.get("transition_logic"), "transition_logic"),
        exclude_gates=_string_list(document.get("exclude_gates"), "exclude_gates"),
    )
    if override.transition_logic and not override.state_registers:
        raise ConfigError(
            "transition_logic was given without state_registers; the transition cone "
            "is only meaningful relative to a state register"
        )

    initial_state = document.get("initial_state")
    if initial_state is not None:
        if isinstance(initial_state, bool):
            raise ConfigError("initial_state must be \"zero\", an integer or an object")
        if isinstance(initial_state, str):
            if initial_state != "zero":
                raise ConfigError(
                    "initial_state as a string must be \"zero\"; use an object "
                    "{\"<gate>\": 0|1} or an integer for anything else"
                )
        elif isinstance(initial_state, int):
            if initial_state < 0:
                raise ConfigError("initial_state as an integer must not be negative")
        elif isinstance(initial_state, dict):
            for gate, bit in initial_state.items():
                if bit not in (0, 1, True, False):
                    raise ConfigError(
                        "initial_state[{!r}] must be 0 or 1, got {!r}".format(gate, bit)
                    )
        else:
            raise ConfigError("initial_state must be \"zero\", an integer or an object")

    encoding = document.get("initial_state_encoding", "auto")
    if encoding not in _INITIAL_STATE_ENCODINGS:
        raise ConfigError(
            "initial_state_encoding must be one of {}".format(
                ", ".join(_INITIAL_STATE_ENCODINGS)
            )
        )

    solver = document.get("solver", "auto")
    if solver not in _SOLVER_MODES:
        raise ConfigError("solver must be one of {}".format(", ".join(_SOLVER_MODES)))

    solve = document.get("solve", "best")
    if solve not in ("best", "all", "none"):
        raise ConfigError("solve must be one of best, all, none")

    targets = document.get("targets") or []
    if not isinstance(targets, list) or any(
        isinstance(t, bool) or not isinstance(t, int) for t in targets
    ):
        raise ConfigError(
            "targets must be a list of state values (integers) to find a witness for"
        )

    reference = document.get("reference")
    if reference is not None and not isinstance(reference, str):
        raise ConfigError("reference must be a path to a ground-truth JSON file")
    if reference and source_path:
        reference = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(source_path)), reference)
        )

    margin = document.get("ambiguity_margin", 0.05)
    _require_type(margin, (int, float), "ambiguity_margin")
    if not 0.0 <= float(margin) <= 1.0:
        raise ConfigError("ambiguity_margin must be between 0 and 1")

    min_confidence = document.get("min_confidence", 0.0)
    _require_type(min_confidence, (int, float), "min_confidence")
    if not 0.0 <= float(min_confidence) <= 1.0:
        raise ConfigError("min_confidence must be between 0 and 1")

    emit_diagram = document.get("emit_diagram", True)
    _require_type(emit_diagram, bool, "emit_diagram")

    diagram_base = document.get("diagram_base", 2)
    if diagram_base not in (2, 10, 16):
        raise ConfigError("diagram_base must be 2, 10 or 16")

    return Configuration(
        name=document.get("name"),
        description=document.get("description"),
        override=override,
        initial_state=initial_state,
        initial_state_encoding=encoding,
        solver=solver,
        solve=solve,
        targets=targets,
        reference=reference,
        limits=Limits(document.get("limits")),
        ambiguity_margin=float(margin),
        min_confidence=float(min_confidence),
        emit_diagram=emit_diagram,
        diagram_base=diagram_base,
        source_path=source_path,
    )


def load(path):
    """Read and validate a configuration file."""
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise ConfigError("could not read the configuration {}: {}".format(path, exc))
    except ValueError as exc:
        raise ConfigError("{} is not valid JSON: {}".format(path, exc))
    return from_dict(document, source_path=str(path))
