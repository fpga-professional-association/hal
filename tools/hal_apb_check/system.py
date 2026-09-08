"""A cycle-accurate transition system, and the unrolling the checker reasons on.

This is the one interface between "where did the design come from" and "what is
being proven". Two producers exist:

* :mod:`hal_apb_check.netlist` -- builds it from a real gate-level netlist via
  ``hal_py`` (Boolean functions of the combinational cone feeding every flip-flop
  data pin, plus the flip-flop's own next-state/clear/preset functions);
* :mod:`hal_apb_check.reference_models` -- hand-written stand-ins for the
  shipped fixtures, so the property engine can be tested without a HAL build.

The abstraction is explicitly *cycle-based*: one step of the system is one
active clock edge. PCLK is therefore not a variable, and everything that depends
on sub-cycle timing (clock trees, gated clocks, setup/hold, glitch behaviour,
multi-clock designs) is outside what any result computed here may claim. The
checker records that as an assumption on every finding rather than leaving it
implied.
"""

from . import expr

__all__ = ["TransitionSystem", "Unrolling", "ff_next"]


def ff_next(data, clear=None, preset=None):
    """Next-state term of a flip-flop with (dominant) asynchronous clear/preset.

    HAL models a flip-flop with a ``next_state`` function plus optional
    ``async_reset``/``async_set`` functions. A cycle-based system has no notion
    of "asynchronous", so this samples clear/preset in the *current* cycle and
    applies them at the edge; the result is a synchronous over-approximation of
    the real behaviour and is recorded as assumption ``async-reset-sampled``.

    Clear wins over preset, preset wins over data -- the common library
    convention. A gate type whose ``async_set_reset_behavior`` says otherwise is
    reported as unsupported by the netlist front end rather than silently
    modelled this way.
    """
    term = data
    if preset is not None:
        term = expr.or_(preset, expr.and_(expr.not_(preset), term))
    if clear is not None:
        term = expr.and_(expr.not_(clear), term)
    return term


class TransitionSystem(object):
    """Free inputs, registers with next-state terms, and named combinational nets."""

    def __init__(self, name):
        self.name = name
        self.inputs = []
        self.states = []
        self.initial = {}
        self.next_terms = {}
        self.defines = {}
        self._order = []
        self.notes = []

    # -- construction -------------------------------------------------------

    def add_input(self, name):
        self._reject_duplicate(name)
        self.inputs.append(name)
        return expr.var(name)

    def add_state(self, name, initial=None, next_term=None):
        """Register ``name``. ``initial=None`` means "unconstrained at cycle 0"."""
        self._reject_duplicate(name)
        self.states.append(name)
        self.initial[name] = initial
        if next_term is not None:
            self.next_terms[name] = next_term
        return expr.var(name)

    def set_next(self, name, term):
        if name not in self.initial:
            raise KeyError("{!r} is not a register of {!r}".format(name, self.name))
        self.next_terms[name] = term

    def define(self, name, term):
        """Name a combinational term. Definitions may refer to earlier ones."""
        self._reject_duplicate(name)
        self.defines[name] = term
        self._order.append(name)
        return expr.var(name)

    def _reject_duplicate(self, name):
        if name in self.inputs or name in self.initial or name in self.defines:
            raise ValueError("signal {!r} is already declared in {!r}".format(name, self.name))

    # -- queries ------------------------------------------------------------

    @property
    def signals(self):
        """Every signal name the system can report a value for."""
        return list(self.inputs) + list(self.states) + list(self.defines)

    def has_signal(self, name):
        return name in self.inputs or name in self.initial or name in self.defines

    def check(self):
        """Validate the system, returning a list of problems (empty when sound)."""
        problems = []
        known = set(self.inputs) | set(self.states)
        for name in self._order:
            for used in expr.variables(self.defines[name]):
                if used not in known and used not in self.defines:
                    problems.append(
                        "definition {!r} of {!r} uses undeclared signal {!r}".format(
                            name, self.name, used
                        )
                    )
            known.add(name)
        for state in self.states:
            if state not in self.next_terms:
                problems.append("register {!r} of {!r} has no next-state term".format(state, self.name))
                continue
            for used in expr.variables(self.next_terms[state]):
                if used not in known:
                    problems.append(
                        "next-state term of {!r} uses undeclared signal {!r}".format(state, used)
                    )
        # A definition that (transitively) refers to itself is a combinational
        # loop; the netlist front end rejects those, and a hand-written model
        # must not introduce one either.
        resolving = set()
        resolved = set()

        def visit(name):
            if name in resolved:
                return
            if name in resolving:
                problems.append("combinational loop through {!r} in {!r}".format(name, self.name))
                return
            resolving.add(name)
            for used in expr.variables(self.defines.get(name, expr.TRUE)):
                if used in self.defines:
                    visit(used)
            resolving.discard(name)
            resolved.add(name)

        for name in self._order:
            visit(name)
        return problems

    # -- unrolling ----------------------------------------------------------

    def flatten(self, term):
        """Resolve every definition in ``term`` down to inputs and registers."""
        current = term
        for _ in range(len(self.defines) + 1):
            used = [name for name in expr.variables(current) if name in self.defines]
            if not used:
                return current
            current = expr.substitute(current, {name: self.defines[name] for name in used})
        raise ValueError("could not flatten a term of {!r}: combinational loop".format(self.name))

    def unroll(self, bound):
        """Unroll ``bound`` transitions, giving cycles ``0 .. bound``."""
        return Unrolling(self, bound)

    def simulate(self, input_values, initial_values=None):
        """Run the system concretely.

        ``input_values`` maps ``(signal, cycle)`` or ``signal`` to a bool; the
        number of cycles is taken from the longest per-signal sequence. Returns
        a list of ``{signal: bool}`` dicts, one per cycle.
        """
        cycles = 0
        for key in input_values:
            if isinstance(key, tuple):
                cycles = max(cycles, key[1] + 1)
        if cycles == 0:
            raise ValueError("simulate() needs at least one (signal, cycle) input value")

        state = {}
        for name in self.states:
            value = self.initial.get(name)
            if initial_values and name in initial_values:
                value = initial_values[name]
            if value is None:
                raise ValueError(
                    "register {!r} has no initial value; pass one via initial_values so the "
                    "run is reproducible".format(name)
                )
            state[name] = bool(value)

        trace = []
        for cycle in range(cycles):
            values = dict(state)
            for name in self.inputs:
                if (name, cycle) in input_values:
                    values[name] = bool(input_values[(name, cycle)])
                elif name in input_values:
                    values[name] = bool(input_values[name])
                else:
                    raise KeyError(
                        "no value for input {!r} at cycle {}".format(name, cycle)
                    )
            for name in self._order:
                values[name] = expr.evaluate(self.defines[name], values)
            trace.append(values)
            state = {
                name: expr.evaluate(self.next_terms[name], values) for name in self.states
            }
        return trace


class Unrolling(object):
    """``bound`` transitions of a :class:`TransitionSystem`, as Boolean terms."""

    def __init__(self, system, bound):
        if bound < 1:
            raise ValueError("the unrolling bound must be at least 1 cycle")
        self.system = system
        self.bound = bound
        self.cycles = bound + 1
        self._cache = {}
        self.constraints = []
        for name in system.states:
            initial = system.initial.get(name)
            if initial is not None:
                term = self.signal(name, 0)
                self.constraints.append(term if initial else expr.not_(term))
        for cycle in range(bound):
            for name in system.states:
                nxt = system.flatten(system.next_terms[name])
                self.constraints.append(
                    expr.iff(self.signal(name, cycle + 1), self._at(nxt, cycle))
                )

    @staticmethod
    def variable(name, cycle):
        return "{}@{}".format(name, cycle)

    def _at(self, term, cycle):
        names = expr.variables(term)
        return expr.substitute(
            term, {name: expr.var(self.variable(name, cycle)) for name in names}
        )

    def signal(self, name, cycle):
        """Term for ``name`` in ``cycle`` (definitions are flattened once)."""
        if cycle < 0 or cycle > self.bound:
            raise IndexError(
                "cycle {} is outside the unrolling 0..{}".format(cycle, self.bound)
            )
        system = self.system
        if name in system.defines:
            key = (name, cycle)
            cached = self._cache.get(key)
            if cached is None:
                cached = self._at(system.flatten(expr.var(name)), cycle)
                self._cache[key] = cached
            return cached
        if not system.has_signal(name):
            raise KeyError("{!r} has no signal {!r}".format(system.name, name))
        return expr.var(self.variable(name, cycle))

    def free_variables(self):
        """Names of the variables a model must assign: inputs and cycle-0 state."""
        names = []
        for cycle in range(self.cycles):
            for name in self.system.inputs:
                names.append(self.variable(name, cycle))
        for name in self.system.states:
            if self.system.initial.get(name) is None:
                names.append(self.variable(name, 0))
        return names
