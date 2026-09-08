"""The property catalogue: what a policy obligation means, formally.

Three obligations, all single-clock, all with a one-cycle lookahead. Each is
stated once, as a pair of terms over a :mod:`hal_secprop.context` context, and
both the bounded check and the witness replay evaluate the same pair.

``secprop/locked-write-blocked/<REG>``
    *While the lock is engaged and reset is inactive, the protected bits do not
    change.* Reset is excluded from the antecedent on purpose: a register being
    forced to its reset value is not an access-control failure, and folding the
    two together would report every reset as a break-in.

``secprop/reset-clears/<REG>``
    *While reset is asserted, the protected bits take the reset value the policy
    declares.* A bit whose policy gives no ``reset_value`` is not checked and is
    reported as a coverage gap, never assumed to be zero.

``secprop/lock-integrity``
    *Once engaged, the lock stays engaged until reset.* Without this, a proof of
    the first obligation is worth very little: an interface that can clear the
    lock does not need to write through it.

Every obligation also carries an **exercise condition** -- the antecedent
strengthened by "and an external write to this register is actually being
attempted". A clean result whose exercise condition is unreachable inside the
bound is reported as vacuous, not as a pass. That is the failure mode a wrong
policy produces (a lock signal that is tied off, an access condition naming the
wrong pin), and it is the one a green report hides best.

Deliberately **not** in this catalogue, and reported as declared coverage gaps
on every run: read-side confidentiality and any other information-flow property
(they need a two-copy/self-composition encoding, not a one-copy trace property),
multi-clock and clock-domain-crossing effects, glitch, timing and power side
channels, and anything about a primitive the front end does not model.
"""

from hal_apb_check import expr

from .errors import PolicyError

__all__ = ["Property", "build", "EXCLUSIONS"]


class Property(object):
    """One obligation: an antecedent, a consequent and what it is about."""

    def __init__(
        self,
        property_id,
        title,
        description,
        kind,
        severity,
        antecedent,
        consequent,
        exercise=None,
        horizon=1,
        signals=(),
        register=None,
        bit_terms=(),
    ):
        self.id = property_id
        self.title = title
        self.description = description
        #: ``access_control``, ``reset`` or ``integrity`` -- for tags and sorting.
        self.kind = kind
        self.severity = severity
        self._antecedent = antecedent
        self._consequent = consequent
        self._exercise = exercise
        self.horizon = horizon
        #: Design signals the property reads; a missing one makes it unsupported.
        self.signals = tuple(signals)
        self.register = register
        #: ``[(label, callable(ctx, cycle) -> term)]`` -- the per-bit obligations,
        #: so a counterexample can name the bit that actually moved.
        self.bit_terms = tuple(bit_terms)

    def antecedent(self, context, cycle):
        return self._antecedent(context, cycle)

    def consequent(self, context, cycle):
        return self._consequent(context, cycle)

    def exercise(self, context, cycle):
        if self._exercise is None:
            return self._antecedent(context, cycle)
        return self._exercise(context, cycle)

    def failing_bits(self, trace_context, cycle):
        failing = []
        for label, term in self.bit_terms:
            if not expr.const_value(term(trace_context, cycle)):
                failing.append(label)
        return failing

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<Property {}>".format(self.id)


def _locked_write(policy, register):
    signals = set(register.signals)
    signals.add(policy.reset_signal)
    signals.add(policy.lock_signal)
    signals.update(policy.exercise_signals(register))

    def antecedent(context, cycle):
        return expr.and_(
            expr.not_(context.reset_active(cycle)), context.locked(cycle)
        )

    def consequent(context, cycle):
        return context.unchanged(register.signals, cycle)

    def exercise(context, cycle):
        return expr.and_(
            antecedent(context, cycle),
            context.any_access(register.write_accesses, cycle),
        )

    def bit_term(signal):
        return lambda context, cycle: context.unchanged([signal], cycle)

    return Property(
        "secprop/locked-write-blocked/{}".format(register.name),
        "{} cannot be written while the lock is engaged".format(register.name),
        "While {} is at its locked value and reset is inactive, no run changes any bit "
        "of {}. The check is scoped to the external write controls the policy declares; "
        "a legitimate hardware update of this register while locked would be reported "
        "here as a violation, which is why the policy has to say that there is "
        "none.".format(policy.lock_signal, register.name),
        "access_control",
        "critical",
        antecedent,
        consequent,
        exercise=exercise,
        signals=sorted(signals),
        register=register.name,
        bit_terms=[(bit.name, bit_term(bit.signal)) for bit in register.bits],
    )


def _reset_clears(policy, register):
    checked = register.bits_with_reset_value()
    signals = {bit.signal for bit in checked}
    signals.add(policy.reset_signal)

    def antecedent(context, cycle):
        return context.reset_active(cycle)

    def consequent(context, cycle):
        return expr.and_(
            *[
                context.equals(bit.signal, cycle + 1, bit.reset_value)
                for bit in checked
            ]
        )

    def bit_term(bit):
        return lambda context, cycle: context.equals(bit.signal, cycle + 1, bit.reset_value)

    return Property(
        "secprop/reset-clears/{}".format(register.name),
        "{} takes its declared reset value while reset is asserted".format(register.name),
        "While {} is asserted, every bit of {} whose reset value the policy declares "
        "reaches that value at the next active clock edge. Bits with no declared reset "
        "value are not checked and are reported as a coverage gap.".format(
            policy.reset_signal, register.name
        ),
        "reset",
        "high",
        antecedent,
        consequent,
        signals=sorted(signals),
        register=register.name,
        bit_terms=[(bit.name, bit_term(bit)) for bit in checked],
    )


def _lock_integrity(policy):
    signals = {policy.lock_signal, policy.reset_signal}

    def antecedent(context, cycle):
        return expr.and_(
            expr.not_(context.reset_active(cycle)), context.locked(cycle)
        )

    def consequent(context, cycle):
        return context.locked(cycle + 1)

    all_accesses = [access.id for access in policy.accesses]

    def exercise(context, cycle):
        if not all_accesses:
            return antecedent(context, cycle)
        return expr.and_(
            antecedent(context, cycle), context.any_access(all_accesses, cycle)
        )

    exercise_signals = set()
    for access_id in all_accesses:
        exercise_signals.update(policy.access_by_id[access_id].condition)

    return Property(
        "secprop/lock-integrity",
        "the lock cannot be cleared from the interface once engaged",
        "Once {} reaches its locked value, no run with reset inactive clears it. Without "
        "this obligation a locked-write proof means little: an interface that can unlock "
        "does not have to write through the lock.".format(policy.lock_signal),
        "integrity",
        "high",
        antecedent,
        consequent,
        exercise=exercise,
        signals=sorted(signals | exercise_signals),
        bit_terms=[("lock", lambda context, cycle: context.locked(cycle + 1))],
    )


def build(policy):
    """The obligations this policy asks for, in a stable order."""
    properties = []
    for register in policy.registers:
        if register.wants("locked_write"):
            if not policy.lock_signal:
                raise PolicyError(
                    "sensitive register {!r} asks for the locked_write obligation but the "
                    "policy declares no 'lock'".format(register.name)
                )
            properties.append(_locked_write(policy, register))
        if register.wants("reset_clears") and register.bits_with_reset_value():
            properties.append(_reset_clears(policy, register))
    if policy.lock_signal:
        properties.append(_lock_integrity(policy))
    return properties


#: Declared limits of this analysis, emitted as ``unsupported`` findings on every
#: run so a reader never has to infer them from silence.
EXCLUSIONS = (
    {
        "id": "secprop/coverage/information-flow",
        "title": "read-side confidentiality and information flow are not checked",
        "kind": "construct",
        "reason": (
            "Whether a sensitive value can be *observed* through the interface is a "
            "two-execution (hyper-) property: it needs a self-composed model, not the "
            "single-trace unrolling used here. Observation points are recorded in every "
            "witness so a trace can be inspected, but no claim is made about leakage."
        ),
    },
    {
        "id": "secprop/coverage/multi-clock",
        "title": "one clock domain only",
        "kind": "configuration",
        "reason": (
            "One step of the model is one active edge of the single declared clock. "
            "Gated, divided and derived clocks are refused by the front end, and nothing "
            "here says anything about clock-domain crossings -- use tools/hal_cdc for "
            "those."
        ),
    },
    {
        "id": "secprop/coverage/side-channels",
        "title": "timing, power and glitch side channels are out of scope",
        "kind": "construct",
        "reason": (
            "The model is cycle-based and functional. Sub-cycle behaviour, glitches, "
            "X-propagation, timing and power are not represented, so no result here is "
            "evidence about them in either direction."
        ),
    },
    {
        "id": "secprop/coverage/policy-is-trusted",
        "title": "the policy is an input, not a finding",
        "kind": "configuration",
        "reason": (
            "Which registers are sensitive, which bit is the lock and which pins are "
            "externally driveable are all statements by the analyst. Nothing is inferred "
            "from net names, so a wrong policy produces a confidently wrong answer -- the "
            "policy file is hashed into this document as an artifact for exactly that "
            "reason."
        ),
    },
)
