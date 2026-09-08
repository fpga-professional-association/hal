"""Bounded symbolic checking of the policy obligations.

The engine answers four questions per obligation and refuses to collapse them
into one, for the same reasons ``hal_apb_check`` does:

1. *Is there a run of at most ``bound`` cycles, satisfying the declared reset
   schedule and environment, that violates the obligation?* -- a **bounded
   counterexample**, and the witness is re-simulated before it is believed.
2. *If not, was the obligation ever exercised?* For an access-control property
   the interesting question is not "did the antecedent fire" but "was a write
   to this register attempted while it was locked". If no run inside the bound
   does that, the pass is **vacuous** and reported as ``unknown``. A policy that
   names the wrong pin lands here instead of looking like a proof.
3. *Did the environment leave any behaviour at all?* Contradictory assumptions
   make every clean result meaningless, so they are detected once, up front, and
   every obligation is downgraded.
4. *Did the search finish?* A budget hit is a ``timeout``, never a pass.

What a clean run may claim is ``proven_bounded`` up to ``bound`` cycles. This is
bounded model checking: there is no induction and no fixpoint here, so
``proven_under_assumptions`` is never emitted -- the schema would allow it and
the code simply never does.

The solver, the term algebra and the unrolling are ``hal_apb_check``'s
(vendored CDCL, no optional third-party solver), so a verdict is identical on a
laptop, in CI and in the container, and every query is also exported as SMT-LIB
so an independent solver can re-decide it.
"""

import time

from hal_apb_check import expr, sat
from hal_apb_check.engine import CheckOutcome
from hal_apb_check.witness import trace_from_model

from . import cones as cones_module
from . import properties as properties_module
from .context import SymbolicContext, TraceContext
from .errors import EngineError

__all__ = [
    "CHECK_FROM",
    "CheckOutcome",
    "PropertyResult",
    "EngineReport",
    "check",
]

#: Obligations are instantiated from this cycle on. Cycle-0 registers are
#: unconstrained (there is no initial-state assumption); by cycle 1 the reset --
#: which the policy requires to be held for at least two cycles -- has settled
#: them.
CHECK_FROM = 1


class PropertyResult(object):
    """Everything the report needs about one obligation."""

    def __init__(self, prop, outcome, **detail):
        self.property = prop
        self.outcome = outcome
        self.detail = detail

    @property
    def is_violation(self):
        return self.outcome == CheckOutcome.VIOLATED

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<PropertyResult {} {}>".format(self.property.id, self.outcome)


class EngineReport(object):
    """The whole run: obligations, cones, diagnostics and timing."""

    def __init__(self, policy, system, bound):
        self.policy = policy
        self.system = system
        self.bound = bound
        self.results = []
        self.cones = None
        self.diagnostics = {}
        self.queries = 0
        self.duration_s = 0.0
        self.solver = None

    def by_outcome(self, outcome):
        return [result for result in self.results if result.outcome == outcome]

    @property
    def violations(self):
        return self.by_outcome(CheckOutcome.VIOLATED)

    @property
    def overconstrained(self):
        return bool(self.diagnostics.get("overconstrained"))


class _Solver(object):
    """Counts queries and turns a budget hit into a typed outcome."""

    def __init__(self, decision_limit, conflict_limit, timeout_s):
        self.decision_limit = decision_limit
        self.conflict_limit = conflict_limit
        self.timeout_s = timeout_s
        self.queries = 0
        self.sat_count = 0
        self.unsat_count = 0
        self.budget_count = 0
        self.wall_time_s = 0.0

    def query(self, assertions):
        """``(status, model)`` where status is ``sat``/``unsat``/``budget``."""
        self.queries += 1
        started = time.time()
        try:
            status, model = sat.solve_term(
                assertions,
                decision_limit=self.decision_limit,
                conflict_limit=self.conflict_limit,
                timeout_s=self.timeout_s,
            )
        except sat.Budget as budget:
            self.wall_time_s += time.time() - started
            self.budget_count += 1
            return "budget", {"kind": budget.kind, "limit": budget.limit}
        self.wall_time_s += time.time() - started
        if status == sat.SAT:
            self.sat_count += 1
        else:
            self.unsat_count += 1
        return status, model


def _instantiation_cycles(bound, horizon):
    last = bound - horizon
    if last < CHECK_FROM:
        return []
    return list(range(CHECK_FROM, last + 1))


def _reset_schedule(context, bound, reset_cycles):
    """Assert the declared reset sequence: held, then released."""
    terms = []
    for cycle in range(bound + 1):
        active = context.reset_active(cycle)
        terms.append(active if cycle < reset_cycles else expr.not_(active))
    return terms


def _quiescent(context, policy, bound):
    terms = []
    applied = {}
    skipped = []
    for signal in sorted(policy.quiescent_inputs):
        if not context.has(signal):
            skipped.append(signal)
            continue
        value = policy.quiescent_inputs[signal]
        for cycle in range(bound + 1):
            terms.append(context.equals(signal, cycle, value))
        applied[signal] = value
    return terms, applied, skipped


def check(policy, system, bound=None, evidence=None):
    """Run every obligation the policy asks for over ``system``."""
    started = time.time()
    bound = int(bound if bound is not None else policy.bound)
    if bound < 1:
        raise EngineError("the unrolling bound must be at least 1 cycle, got {}".format(bound))
    if not system.has_signal(policy.reset_signal):
        raise EngineError(
            "the policy's reset signal {!r} does not exist in design {!r}. Every result is "
            "scoped to a known reset sequence, so there is nothing to check. Run "
            "'validate-policy --check-design' to see the full list of unresolved "
            "signals.".format(policy.reset_signal, system.name)
        )
    if policy.lock_signal and not system.has_signal(policy.lock_signal):
        raise EngineError(
            "the policy's lock signal {!r} does not exist in design {!r}; every "
            "access-control obligation is stated in terms of it".format(
                policy.lock_signal, system.name
            )
        )

    problems = system.check()
    unrolling = system.unroll(bound)
    context = SymbolicContext(policy, unrolling)
    report = EngineReport(policy, system, bound)
    report.diagnostics["system_problems"] = problems
    report.diagnostics["unresolved_signals"] = context.unresolved
    report.diagnostics["check_from_cycle"] = CHECK_FROM

    solver = _Solver(
        int(policy.option("decision_limit")),
        int(policy.option("conflict_limit")),
        int(policy.option("timeout_s")),
    )
    report.solver = solver

    # ---- structural cone selection: candidates, never verdicts ------------
    report.cones = cones_module.analyse(system, policy)

    # ---- the base constraint set -----------------------------------------
    base = list(unrolling.constraints)
    base.extend(_reset_schedule(context, bound, policy.reset_cycles))
    quiescent_terms, quiescent_applied, quiescent_skipped = _quiescent(
        context, policy, bound
    )
    base.extend(quiescent_terms)
    report.diagnostics["quiescent_inputs_applied"] = quiescent_applied
    report.diagnostics["quiescent_inputs_unresolved"] = quiescent_skipped

    if evidence is not None:
        evidence.smt2(
            "environment",
            expr.to_smt2(
                base,
                comment="hal_secprop environment: the transition relation unrolled {} "
                "cycles, the declared reset schedule and the quiescent input "
                "assumptions".format(bound),
            ),
        )

    status, _ = solver.query(base)
    feasible = status == sat.SAT
    report.diagnostics["environment_satisfiable"] = None if status == "budget" else feasible
    report.diagnostics["overconstrained"] = feasible is False

    obligations = properties_module.build(policy)

    # ---- per-obligation checks -------------------------------------------
    for prop in obligations:
        missing = context.missing(prop.signals)
        if missing:
            report.results.append(
                PropertyResult(
                    prop,
                    CheckOutcome.UNSUPPORTED,
                    missing_signals=missing,
                    reason=(
                        "the design does not contain signal(s) {}; the obligation cannot "
                        "be stated, let alone checked".format(", ".join(missing))
                    ),
                )
            )
            continue

        cycles = _instantiation_cycles(bound, prop.horizon)
        if not cycles:
            report.results.append(
                PropertyResult(
                    prop,
                    CheckOutcome.NOT_INSTANTIABLE,
                    horizon=prop.horizon,
                    reason=(
                        "the obligation looks {} cycle(s) ahead, which does not fit in a "
                        "bound of {}; raise options.bound to at least {}".format(
                            prop.horizon, bound, CHECK_FROM + prop.horizon
                        )
                    ),
                )
            )
            continue

        if report.diagnostics["overconstrained"]:
            report.results.append(
                PropertyResult(
                    prop,
                    CheckOutcome.VACUOUS,
                    horizon=prop.horizon,
                    cycles=[cycles[0], cycles[-1]],
                    overconstrained=True,
                    reason=(
                        "the declared environment admits no run at all within the bound, "
                        "so nothing about this obligation was exercised"
                    ),
                )
            )
            continue

        violation = expr.or_(
            *[
                expr.and_(
                    prop.antecedent(context, cycle),
                    expr.not_(prop.consequent(context, cycle)),
                )
                for cycle in cycles
            ]
        )
        query = base + [violation]
        if evidence is not None:
            evidence.smt2(
                prop.id,
                expr.to_smt2(
                    query,
                    comment="violation query for {} over cycles {}..{} of a {}-cycle "
                    "unrolling".format(prop.id, cycles[0], cycles[-1], bound),
                ),
            )
        status, model = solver.query(query)

        if status == "budget":
            report.results.append(
                PropertyResult(
                    prop,
                    CheckOutcome.TIMEOUT,
                    horizon=prop.horizon,
                    cycles=[cycles[0], cycles[-1]],
                    budget=model,
                    phase="violation",
                )
            )
            continue

        if status == sat.SAT:
            try:
                trace, initial_state, unconstrained = trace_from_model(
                    system, unrolling, model, bound
                )
            except Exception as error:  # noqa: BLE001 - a broken witness is an engine error
                report.results.append(
                    PropertyResult(
                        prop,
                        CheckOutcome.ERROR,
                        message="could not turn the SAT model into an executable trace",
                        error_detail=str(error),
                    )
                )
                continue
            trace_context = TraceContext(policy, trace)
            failing = [cycle for cycle in cycles if not trace_context.holds(prop, cycle)]
            if not failing:
                report.results.append(
                    PropertyResult(
                        prop,
                        CheckOutcome.ERROR,
                        message=(
                            "the solver reported a violation but re-simulating its model "
                            "shows the obligation holding; the encoding and the simulator "
                            "disagree"
                        ),
                    )
                )
                continue
            report.results.append(
                PropertyResult(
                    prop,
                    CheckOutcome.VIOLATED,
                    horizon=prop.horizon,
                    cycles=[cycles[0], cycles[-1]],
                    violation_cycle=failing[0],
                    failing_cycles=failing,
                    failing_bits=trace_context.failing_bits(prop, failing[0]),
                    exercised=trace_context.exercised(prop, failing[0]),
                    trace=trace,
                    initial_state=initial_state,
                    unconstrained=unconstrained,
                )
            )
            continue

        # UNSAT: nothing violates it inside the bound -- but was it exercised?
        exercise = expr.or_(*[prop.exercise(context, cycle) for cycle in cycles])
        if evidence is not None:
            evidence.smt2(
                prop.id + "/exercise",
                expr.to_smt2(
                    base + [exercise],
                    comment="coverage query for {}: can the obligation be exercised at "
                    "all inside {} cycles?".format(prop.id, bound),
                ),
            )
        status, info = solver.query(base + [exercise])
        if status == "budget":
            report.results.append(
                PropertyResult(
                    prop,
                    CheckOutcome.TIMEOUT,
                    horizon=prop.horizon,
                    cycles=[cycles[0], cycles[-1]],
                    budget=info,
                    phase="coverage",
                )
            )
            continue
        if status == sat.UNSAT:
            report.results.append(
                PropertyResult(
                    prop,
                    CheckOutcome.VACUOUS,
                    horizon=prop.horizon,
                    cycles=[cycles[0], cycles[-1]],
                    reason=(
                        "no run within {} cycles exercises this obligation (for an "
                        "access-control property that means no declared external write "
                        "is ever attempted while the lock is engaged), so the absence of "
                        "a violation is not evidence".format(bound)
                    ),
                )
            )
            continue

        report.results.append(
            PropertyResult(
                prop,
                CheckOutcome.HOLDS_BOUNDED,
                horizon=prop.horizon,
                cycles=[cycles[0], cycles[-1]],
                exercised=True,
            )
        )

    report.queries = solver.queries
    report.diagnostics["solver_wall_time_s"] = round(solver.wall_time_s, 6)
    report.diagnostics["decision_limit"] = solver.decision_limit
    report.diagnostics["conflict_limit"] = solver.conflict_limit
    report.diagnostics["timeout_s"] = solver.timeout_s
    report.duration_s = round(time.time() - started, 6)
    return report
