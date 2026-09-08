"""One way to read a signal, symbolically or from a concrete trace.

Every property in :mod:`hal_secprop.properties` is written once, against this
interface. The bounded check evaluates it over a :class:`SymbolicContext` (terms
in the unrolled transition relation) and the witness replay evaluates the *same*
lambda over a :class:`TraceContext` (constant terms from a simulated trace).
That is what keeps a counterexample and its replay from drifting apart: there is
no second definition of any property to drift from.
"""

from hal_apb_check import expr

__all__ = ["SymbolicContext", "TraceContext"]


class _Base(object):
    """Shared policy-aware helpers; subclasses provide :meth:`value`."""

    def __init__(self, policy, bound):
        self.policy = policy
        self.bound = bound
        self.unresolved = []

    # -- to be provided -----------------------------------------------------

    def value(self, signal, cycle):  # pragma: no cover - abstract
        raise NotImplementedError

    def has(self, signal):  # pragma: no cover - abstract
        raise NotImplementedError

    # -- policy-level readings ---------------------------------------------

    def missing(self, signals):
        """Which of ``signals`` the model does not contain."""
        return [name for name in signals if not self.has(name)]

    def reset_active(self, cycle):
        term = self.value(self.policy.reset_signal, cycle)
        return expr.not_(term) if self.policy.reset_active_low else term

    def locked(self, cycle):
        if not self.policy.lock_signal:
            raise ValueError("the policy declares no lock state")
        term = self.value(self.policy.lock_signal, cycle)
        return term if self.policy.lock_locked_value == 1 else expr.not_(term)

    def access(self, access, cycle):
        """The declared condition of one external access, at ``cycle``."""
        terms = []
        for signal in sorted(access.condition):
            wanted = access.condition[signal]
            term = self.value(signal, cycle)
            terms.append(term if wanted else expr.not_(term))
        return expr.and_(*terms)

    def any_access(self, access_ids, cycle):
        return expr.or_(
            *[
                self.access(self.policy.access_by_id[access_id], cycle)
                for access_id in access_ids
            ]
        )

    def unchanged(self, signals, cycle):
        return expr.and_(
            *[
                expr.iff(self.value(name, cycle), self.value(name, cycle + 1))
                for name in signals
            ]
        )

    def equals(self, signal, cycle, value):
        term = self.value(signal, cycle)
        return term if value else expr.not_(term)


class SymbolicContext(_Base):
    """Reads signals out of a :class:`hal_apb_check.system.Unrolling`."""

    def __init__(self, policy, unrolling):
        _Base.__init__(self, policy, unrolling.bound)
        self.unrolling = unrolling
        self.system = unrolling.system
        self.unresolved = sorted(
            name for name in policy.design_signals() if not self.system.has_signal(name)
        )

    def has(self, signal):
        return self.system.has_signal(signal)

    def value(self, signal, cycle):
        return self.unrolling.signal(signal, cycle)


class TraceContext(_Base):
    """Reads signals out of a concrete trace; every term folds to a constant."""

    def __init__(self, policy, trace):
        _Base.__init__(self, policy, len(trace) - 1)
        self.trace = trace

    def has(self, signal):
        return bool(self.trace) and signal in self.trace[0]

    def value(self, signal, cycle):
        if cycle < 0 or cycle >= len(self.trace):
            raise IndexError("cycle {} is outside the replayed trace".format(cycle))
        values = self.trace[cycle]
        if signal not in values:
            raise KeyError("the trace has no signal {!r}".format(signal))
        return expr.TRUE if values[signal] else expr.FALSE

    # -- concrete evaluation ------------------------------------------------

    def holds(self, prop, cycle):
        """``True``/``False`` for ``prop`` at ``cycle`` on this trace."""
        if not expr.const_value(prop.antecedent(self, cycle)):
            return True
        return expr.const_value(prop.consequent(self, cycle))

    def exercised(self, prop, cycle):
        return expr.const_value(prop.exercise(self, cycle))

    def failing_bits(self, prop, cycle):
        """Which of the property's bit obligations fail at ``cycle``."""
        return prop.failing_bits(self, cycle)
