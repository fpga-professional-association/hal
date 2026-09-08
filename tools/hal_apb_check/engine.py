"""Bounded model checking of APB properties, with the caveats made first-class.

The engine answers four questions per property and refuses to collapse them:

1. *Is there a run of at most ``bound`` cycles that satisfies every environment
   assumption and violates the property?* -- a **bounded counterexample**.
2. *If not, could the property ever have fired at all?* If no run activates its
   antecedent, the "pass" is **vacuous** and reported as ``unknown``, never as a
   proof. This is the failure mode a mapping typo produces, and it is the one a
   green report hides best.
3. *Did the environment assumptions leave any behaviour at all?* If the
   assumptions are contradictory, or admit no transfer inside the bound, the run
   is **overconstrained** and every result derived from it is downgraded.
4. *Did the search finish?* A budget hit is a ``timeout``, not a pass.

What a clean run may claim is ``proven_bounded`` up to ``bound`` cycles -- never
``proven_under_assumptions``, because nothing here does induction or reaches a
fixpoint.
"""

import time

from . import expr, mapping as mapping_module, sat, spec, witness

__all__ = [
    "EngineError",
    "CheckOutcome",
    "PropertyResult",
    "EngineReport",
    "check",
]


class EngineError(RuntimeError):
    """The run cannot start: the mapping and the design do not fit together."""

#: Properties are instantiated from this cycle on. Cycle 0 registers may be
#: unconstrained (no initial-state assumption); by cycle 1 the reset -- which the
#: mapping requires to be held for at least two cycles -- has settled them.
CHECK_FROM = 1


class CheckOutcome(object):
    """Result kinds the engine can produce for one property."""

    VIOLATED = "violated"
    HOLDS_BOUNDED = "holds_bounded"
    VACUOUS = "vacuous"
    NOT_INSTANTIABLE = "not_instantiable"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    ERROR = "error"


class PropertyResult(object):
    """Everything a report needs about one checked property."""

    def __init__(self, prop, outcome, **detail):
        self.property = prop
        self.outcome = outcome
        self.detail = detail

    def __repr__(self):
        return "<PropertyResult {} {}>".format(self.property.id, self.outcome)

    @property
    def is_violation(self):
        return self.outcome == CheckOutcome.VIOLATED


class EngineReport(object):
    """The whole run: obligations, assumptions, diagnostics and timing."""

    def __init__(self, bus_mapping, system, bound):
        self.mapping = bus_mapping
        self.system = system
        self.bound = bound
        self.results = []
        self.assumptions = []
        #: The :class:`~.spec.Property` objects actually assumed, for replay.
        self.assumption_properties = []
        self.diagnostics = {}
        self.queries = 0
        self.duration_s = 0.0
        self.solver = None

    def by_status(self, outcome):
        return [result for result in self.results if result.outcome == outcome]

    @property
    def violations(self):
        return self.by_status(CheckOutcome.VIOLATED)

    @property
    def overconstrained(self):
        return bool(self.diagnostics.get("overconstrained"))


def _instantiation_cycles(bound, horizon):
    """Cycles at which a property with ``horizon`` lookahead can be instantiated."""
    last = bound - horizon
    if last < CHECK_FROM:
        return []
    return list(range(CHECK_FROM, last + 1))


def _reset_schedule(context, bound, reset_cycles):
    """Assert the declared reset sequence: held for ``reset_cycles``, then released."""
    terms = []
    for cycle in range(bound + 1):
        active = context.reset_active(cycle)
        terms.append(active if cycle < reset_cycles else expr.not_(active))
    return terms


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
        """Return ``(status, model)`` where status is ``sat``/``unsat``/``budget``."""
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


def check(bus_mapping, system, bound=None, evidence=None):
    """Run every applicable APB check on ``system`` under ``bus_mapping``.

    ``evidence`` may be an object with ``smt2(name, text)`` used to persist the
    exact query behind each verdict; see :mod:`hal_apb_check.cli`.
    """
    started = time.time()
    bound = int(bound if bound is not None else bus_mapping.option("bound"))
    # Every result is scoped to a known reset sequence, so a reset signal the
    # design does not have is a hard stop rather than a per-property gap.
    if not system.has_signal(bus_mapping.reset_signal):
        raise EngineError(
            "the mapping's reset signal {!r} does not exist in design {!r}. Every result is "
            "scoped to a known reset sequence, so there is nothing to check. Run "
            "'validate-mapping --check-design' to see the full list of unresolved "
            "signals.".format(bus_mapping.reset_signal, system.name)
        )
    problems = system.check()
    unrolling = system.unroll(bound)
    context = mapping_module.SignalContext(bus_mapping, unrolling)
    report = EngineReport(bus_mapping, system, bound)
    report.diagnostics["system_problems"] = problems
    report.diagnostics["unresolved_signals"] = context.unresolved
    report.diagnostics["check_from_cycle"] = CHECK_FROM

    solver = _Solver(
        int(bus_mapping.option("decision_limit")),
        int(bus_mapping.option("conflict_limit")),
        int(bus_mapping.option("timeout_s")),
    )
    report.solver = solver

    include_optional = bool(bus_mapping.option("check_ready_low_when_deselected"))
    obligations = spec.obligations_for(bus_mapping.role, bus_mapping.revision, include_optional)
    assumption_props = spec.assumptions_for(bus_mapping.role, bus_mapping.revision)

    # ---- the base constraint set -----------------------------------------
    base = list(unrolling.constraints)
    base.extend(_reset_schedule(context, bound, bus_mapping.reset_cycles))

    assumed = []
    skipped_assumptions = []
    for prop in assumption_props:
        missing = context.unavailable(prop)
        if missing:
            skipped_assumptions.append({"assumption": prop.id, "missing_signals": missing})
            continue
        horizon = prop.horizon_for(context)
        cycles = _instantiation_cycles(bound, horizon)
        if not cycles:
            skipped_assumptions.append(
                {"assumption": prop.id, "reason": "needs a bound of at least {}".format(
                    CHECK_FROM + horizon
                )}
            )
            continue
        for cycle in cycles:
            base.append(
                expr.implies(prop.antecedent(context, cycle), prop.consequent(context, cycle))
            )
        assumed.append({"assumption": prop.id, "cycles": [cycles[0], cycles[-1]]})
    report.assumptions = assumed
    report.diagnostics["assumptions_not_applied"] = skipped_assumptions
    applied_ids = {entry["assumption"] for entry in assumed}
    report.assumption_properties = [
        prop for prop in assumption_props if prop.id in applied_ids
    ]

    if evidence is not None:
        evidence.smt2(
            "environment",
            expr.to_smt2(base, comment="APB environment: transition relation, reset schedule "
                                      "and environment assumptions"),
        )

    # ---- overconstraint diagnostics --------------------------------------
    status, model = solver.query(base)
    feasible = status == sat.SAT
    report.diagnostics["environment_satisfiable"] = feasible
    if status == "budget":
        report.diagnostics["environment_satisfiable"] = None
        report.diagnostics["environment_budget"] = model
    report.diagnostics["overconstrained"] = feasible is False

    transfer_reachable = None
    if feasible and context.has("PSEL") and context.has("PENABLE"):
        access = expr.or_(
            *[
                expr.and_(context.bit("PSEL", cycle), context.bit("PENABLE", cycle))
                for cycle in range(CHECK_FROM, bound + 1)
            ]
        )
        status, _ = solver.query(base + [access])
        transfer_reachable = None if status == "budget" else (status == sat.SAT)
        if transfer_reachable is False:
            report.diagnostics["overconstrained"] = True
    report.diagnostics["transfer_reachable"] = transfer_reachable

    # ---- per-property checks ---------------------------------------------
    for prop in obligations:
        missing = context.unavailable(prop)
        if missing:
            report.results.append(
                PropertyResult(
                    prop,
                    CheckOutcome.UNSUPPORTED,
                    missing_signals=missing,
                    reason=(
                        "the mapping does not provide {}; the property cannot be stated, let "
                        "alone checked".format(", ".join(missing))
                    ),
                )
            )
            continue

        horizon = prop.horizon_for(context)
        cycles = _instantiation_cycles(bound, horizon)
        if not cycles:
            report.results.append(
                PropertyResult(
                    prop,
                    CheckOutcome.NOT_INSTANTIABLE,
                    horizon=horizon,
                    reason=(
                        "the property looks {} cycle(s) ahead, which does not fit in a bound of "
                        "{}; raise options.bound to at least {}".format(
                            horizon, bound, CHECK_FROM + horizon
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
                    horizon=horizon,
                    cycles=[cycles[0], cycles[-1]],
                    overconstrained=True,
                    reason=(
                        "the environment assumptions admit no APB transfer within the bound, so "
                        "nothing about this property was actually exercised"
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
                    horizon=horizon,
                    cycles=[cycles[0], cycles[-1]],
                    budget=model,
                )
            )
            continue

        if status == sat.SAT:
            try:
                trace, initial_state, unconstrained = witness.trace_from_model(
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
            trace_context = witness.TraceContext(bus_mapping, trace)
            failing = [
                cycle for cycle in cycles if not trace_context.holds(prop, cycle)
            ]
            if not failing:
                report.results.append(
                    PropertyResult(
                        prop,
                        CheckOutcome.ERROR,
                        message=(
                            "the solver reported a violation but re-simulating its model shows "
                            "the property holding; the encoding and the simulator disagree"
                        ),
                    )
                )
                continue
            report.results.append(
                PropertyResult(
                    prop,
                    CheckOutcome.VIOLATED,
                    horizon=horizon,
                    cycles=[cycles[0], cycles[-1]],
                    violation_cycle=failing[0],
                    failing_cycles=failing,
                    trace=trace,
                    initial_state=initial_state,
                    unconstrained=unconstrained,
                )
            )
            continue

        # UNSAT: nothing violates it -- but did it ever fire?
        activation = expr.or_(*[prop.antecedent(context, cycle) for cycle in cycles])
        status, info = solver.query(base + [activation])
        if status == "budget":
            report.results.append(
                PropertyResult(
                    prop,
                    CheckOutcome.TIMEOUT,
                    horizon=horizon,
                    cycles=[cycles[0], cycles[-1]],
                    budget=info,
                    phase="vacuity",
                )
            )
            continue
        if status == sat.UNSAT:
            report.results.append(
                PropertyResult(
                    prop,
                    CheckOutcome.VACUOUS,
                    horizon=horizon,
                    cycles=[cycles[0], cycles[-1]],
                    reason=(
                        "no run within the bound activates the antecedent, so the property "
                        "passed without being exercised"
                    ),
                )
            )
            continue

        report.results.append(
            PropertyResult(
                prop,
                CheckOutcome.HOLDS_BOUNDED,
                horizon=horizon,
                cycles=[cycles[0], cycles[-1]],
            )
        )

    report.queries = solver.queries
    report.diagnostics["solver_wall_time_s"] = round(solver.wall_time_s, 6)
    report.diagnostics["decision_limit"] = solver.decision_limit
    report.diagnostics["conflict_limit"] = solver.conflict_limit
    report.diagnostics["timeout_s"] = solver.timeout_s
    report.duration_s = round(time.time() - started, 6)
    return report
