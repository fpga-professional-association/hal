"""A small, deterministic, dependency-free SAT solver for the APB checker.

The checker must be able to say *proven up to k*, *refuted with this witness*,
*vacuous*, or *I ran out of budget* -- and it must say the same thing on every
machine, in CI, and inside the ``halbuild`` container. Depending on an optional
``z3``/``python-sat`` install would make the third answer environment-dependent
and the first one unavailable wherever the install is missing, so the decision
procedure lives here: a Tseitin encoder plus CDCL -- two-watched-literal unit
propagation, first-UIP clause learning, non-chronological backjumping, activity
based decisions with phase saving, and geometric restarts.

Plain DPLL was tried first and is not enough: finding a counterexample is easy
(any satisfying assignment will do), but *proving* a property over a twelve-cycle
unrolling means refuting every assignment of roughly a hundred free input bits,
and chronological backtracking does not come back from that. Clause learning
turns the same queries into milliseconds. Whenever the search does run out of
road it raises an explicit :class:`Budget`, and the analysis reports ``timeout``
with the limit it hit -- never a proof it did not earn.

``expr.to_smt2`` writes the same query as SMT-LIB 2 so a real solver can check
the answer independently; that file is attached to findings as evidence.
"""

import heapq
import time

from . import expr

__all__ = ["Budget", "CNF", "encode", "solve", "solve_term", "SAT", "UNSAT"]

SAT = "sat"
UNSAT = "unsat"


class Budget(Exception):
    """Raised when the solver exceeds its decision or conflict budget."""

    def __init__(self, kind, limit):
        self.kind = kind
        self.limit = limit
        Exception.__init__(
            self, "SAT search exceeded its {} budget of {}".format(kind, limit)
        )


class CNF(object):
    """A CNF built by Tseitin encoding, remembering the named variables."""

    def __init__(self):
        self.clauses = []
        self.num_vars = 0
        self.name_to_var = {}
        self.var_to_name = {}
        self._cache = {}
        self._true = self.new_var()
        self.clauses.append([self._true])

    def new_var(self):
        self.num_vars += 1
        return self.num_vars

    def var_for(self, name):
        literal = self.name_to_var.get(name)
        if literal is None:
            literal = self.new_var()
            self.name_to_var[name] = literal
            self.var_to_name[literal] = name
        return literal

    def add(self, *literals):
        self.clauses.append(list(literals))

    def literal(self, term):
        """Return a literal equisatisfiable with ``term``, adding clauses."""
        cached = self._cache.get(term)
        if cached is not None:
            return cached
        kind = term[0]
        if kind == "const":
            result = self._true if term[1] else -self._true
        elif kind == "var":
            result = self.var_for(term[1])
        elif kind == "not":
            result = -self.literal(term[1])
        elif kind in ("and", "or"):
            operands = [self.literal(operand) for operand in term[1]]
            out = self.new_var()
            if kind == "and":
                for operand in operands:
                    self.add(-out, operand)
                self.add(out, *[-operand for operand in operands])
            else:
                for operand in operands:
                    self.add(out, -operand)
                self.add(-out, *operands)
            result = out
        elif kind == "xor":
            left = self.literal(term[1][0])
            right = self.literal(term[1][1])
            out = self.new_var()
            self.add(-out, left, right)
            self.add(-out, -left, -right)
            self.add(out, -left, right)
            self.add(out, left, -right)
            result = out
        else:
            raise ValueError("unknown term kind {!r}".format(kind))
        self._cache[term] = result
        return result

    def assert_term(self, term):
        self.add(self.literal(term))


def encode(assertions):
    """Tseitin-encode ``assertions`` (all asserted true) into a :class:`CNF`."""
    cnf = CNF()
    # Encode the named variables first, in sorted order, so the search order --
    # and therefore the witness the solver reports -- is reproducible.
    names = set()
    for assertion in assertions:
        expr.variables(assertion, into=names)
    for name in sorted(names):
        cnf.var_for(name)
    for assertion in assertions:
        cnf.assert_term(assertion)
    return cnf


_VAR_DECAY = 0.95
_RESTART_FIRST = 100
_RESTART_GROWTH = 1.5


def _normalise(clauses):
    """Drop duplicate literals and tautological clauses, preserving order."""
    out = []
    for clause in clauses:
        seen = {}
        tautology = False
        for literal in clause:
            if -literal in seen:
                tautology = True
                break
            seen[literal] = True
        if tautology:
            continue
        out.append(list(seen))
    return out


def solve(cnf, decision_limit=None, conflict_limit=None, timeout_s=None):
    """CDCL. Returns ``(SAT, {name: bool})`` or ``(UNSAT, None)``.

    Raises :class:`Budget` when a limit is hit, so an exhausted search can never
    be mistaken for ``unsat``. ``timeout_s`` is checked between conflicts, which
    is often enough for the query sizes this checker produces and keeps the hot
    propagation loop free of clock reads.
    """
    deadline = None if timeout_s is None else time.time() + float(timeout_s)
    num_vars = cnf.num_vars
    clauses = _normalise(cnf.clauses)

    value = [0] * (num_vars + 1)          # 0 unassigned, 1 true, -1 false
    level = [0] * (num_vars + 1)
    reason = [None] * (num_vars + 1)
    activity = [0.0] * (num_vars + 1)
    phase = [False] * (num_vars + 1)
    trail = []
    trail_lim = []
    watches = {}
    qhead = 0

    def watch(literal, index):
        watches.setdefault(literal, []).append(index)

    def literal_value(literal):
        assigned = value[abs(literal)]
        if assigned == 0:
            return 0
        return assigned if literal > 0 else -assigned

    units = []
    for index, clause in enumerate(clauses):
        if not clause:
            return UNSAT, None
        if len(clause) == 1:
            units.append(clause[0])
        else:
            watch(clause[0], index)
            watch(clause[1], index)

    def enqueue(literal, from_clause):
        variable = abs(literal)
        value[variable] = 1 if literal > 0 else -1
        level[variable] = len(trail_lim)
        reason[variable] = from_clause
        trail.append(literal)

    def propagate():
        nonlocal qhead
        while qhead < len(trail):
            propagated = trail[qhead]
            qhead += 1
            falsified = -propagated
            pending = watches.get(falsified)
            if not pending:
                continue
            watches[falsified] = []
            keep = watches[falsified]
            position = 0
            while position < len(pending):
                index = pending[position]
                position += 1
                clause = clauses[index]
                if clause[0] == falsified:
                    clause[0], clause[1] = clause[1], clause[0]
                if literal_value(clause[0]) == 1:
                    keep.append(index)
                    continue
                moved = False
                for offset in range(2, len(clause)):
                    if literal_value(clause[offset]) != -1:
                        clause[1], clause[offset] = clause[offset], clause[1]
                        watch(clause[1], index)
                        moved = True
                        break
                if moved:
                    continue
                keep.append(index)
                if literal_value(clause[0]) == -1:
                    keep.extend(pending[position:])
                    return index
                enqueue(clause[0], index)
        return None

    var_inc = [1.0]

    def bump(variable):
        activity[variable] += var_inc[0]
        if activity[variable] > 1e100:
            for other in range(1, num_vars + 1):
                activity[other] *= 1e-100
            var_inc[0] *= 1e-100
        if value[variable] == 0:
            heapq.heappush(heap, (-activity[variable], variable))

    heap = [(-0.0, variable) for variable in range(1, num_vars + 1)]
    heapq.heapify(heap)

    def pick_branch():
        while heap:
            _, variable = heapq.heappop(heap)
            if value[variable] == 0:
                return variable
        for variable in range(1, num_vars + 1):
            if value[variable] == 0:
                return variable
        return None

    def backtrack(to_level):
        nonlocal qhead
        while len(trail_lim) > to_level:
            limit = trail_lim.pop()
            while len(trail) > limit:
                literal = trail.pop()
                variable = abs(literal)
                phase[variable] = value[variable] == 1
                value[variable] = 0
                reason[variable] = None
                heapq.heappush(heap, (-activity[variable], variable))
        qhead = len(trail)

    def analyze(conflict_index):
        """First-UIP conflict analysis; returns (learned clause, backjump level)."""
        learned = [0]
        seen = set()
        counter = 0
        pivot = None
        index = len(trail) - 1
        current_level = len(trail_lim)
        clause = clauses[conflict_index]
        while True:
            start = 0 if pivot is None else 1
            for literal in clause[start:]:
                variable = abs(literal)
                if variable in seen or level[variable] == 0:
                    continue
                seen.add(variable)
                bump(variable)
                if level[variable] >= current_level:
                    counter += 1
                else:
                    learned.append(literal)
            while abs(trail[index]) not in seen:
                index -= 1
            pivot = trail[index]
            index -= 1
            seen.discard(abs(pivot))
            counter -= 1
            if counter <= 0:
                break
            clause = clauses[reason[abs(pivot)]]
        learned[0] = -pivot

        if len(learned) == 1:
            return learned, 0
        best = max(range(1, len(learned)), key=lambda i: level[abs(learned[i])])
        learned[1], learned[best] = learned[best], learned[1]
        return learned, level[abs(learned[1])]

    for literal in units:
        if literal_value(literal) == -1:
            return UNSAT, None
        if literal_value(literal) == 0:
            enqueue(literal, None)

    decisions = 0
    conflicts = 0
    restart_at = _RESTART_FIRST
    conflicts_since_restart = 0

    while True:
        conflict_index = propagate()
        if conflict_index is not None:
            conflicts += 1
            conflicts_since_restart += 1
            if conflict_limit is not None and conflicts > conflict_limit:
                raise Budget("conflict", conflict_limit)
            if deadline is not None and time.time() > deadline:
                raise Budget("time", timeout_s)
            if not trail_lim:
                return UNSAT, None
            learned, backjump = analyze(conflict_index)
            backtrack(backjump)
            if len(learned) == 1:
                enqueue(learned[0], None)
            else:
                clauses.append(learned)
                index = len(clauses) - 1
                watch(learned[0], index)
                watch(learned[1], index)
                enqueue(learned[0], index)
            var_inc[0] /= _VAR_DECAY
            continue

        if conflicts_since_restart >= restart_at:
            conflicts_since_restart = 0
            restart_at = int(restart_at * _RESTART_GROWTH)
            backtrack(0)
            continue

        variable = pick_branch()
        if variable is None:
            model = {
                name: value[literal] == 1 for name, literal in cnf.name_to_var.items()
            }
            return SAT, model

        decisions += 1
        if decision_limit is not None and decisions > decision_limit:
            raise Budget("decision", decision_limit)
        trail_lim.append(len(trail))
        enqueue(variable if phase[variable] else -variable, None)


def solve_term(assertions, decision_limit=None, conflict_limit=None, timeout_s=None):
    """Convenience wrapper: encode and solve a list of asserted terms."""
    return solve(
        encode(assertions),
        decision_limit=decision_limit,
        conflict_limit=conflict_limit,
        timeout_s=timeout_s,
    )
