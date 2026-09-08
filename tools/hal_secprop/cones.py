"""Structural cone selection -- candidate reachability, and nothing more.

Given the flattened next-state function of every register, the transitive fan-in
cone of a sensitive register answers one cheap question: *which primary inputs
can possibly influence this bit, and through how many register stages?* That is
worth having. It bounds what the symbolic check has to look at, it turns "an
attacker can reach the key" into a concrete path a reviewer can follow, and it
costs no solver time.

It is also **not a verdict**, and this module is written so that it cannot be
mistaken for one:

* every result it produces is emitted with status ``heuristic`` and the word
  *candidate* in its title;
* the reachable/unreachable answer never changes what gets checked
  symbolically -- every obligation the policy asks for is checked either way;
* the shipped fixtures are the argument. ``secreg_ok`` and ``secreg_faulty``
  differ by exactly one gate: the faulty variant's debug write path does not
  consult the lock. Their cones are **identical** -- same inputs, same depths,
  same paths -- because the lock still reaches the register through the other
  write path. Structure cannot tell them apart. Only the bounded symbolic check
  can, and ``fixtures/ground_truth.json`` asserts precisely that.

The cone is computed over *signals*, not gates: once definitions are flattened
the support of a next-state term is exactly the set of inputs and registers that
can change it, so there is no over-approximation from re-convergence, and none
of the "a wire passes near it" noise a gate-level graph walk produces.
"""

from hal_apb_check import expr

__all__ = ["Cone", "ConeReport", "analyse"]


class Cone(object):
    """The transitive fan-in of one target, in signal terms."""

    def __init__(self, target, targets):
        self.target = target
        #: State signals the cone starts from (the register's bits).
        self.targets = list(targets)
        self.inputs = {}
        self.states = {}
        self.paths = {}

    def depth_of(self, signal):
        if signal in self.inputs:
            return self.inputs[signal]
        return self.states.get(signal)

    def contains(self, signal):
        return signal in self.inputs or signal in self.states

    def to_dict(self, external_controls=(), lock_signal=None):
        controls = sorted(name for name in external_controls if self.contains(name))
        return {
            "target": self.target,
            "target_signals": sorted(self.targets),
            "input_count": len(self.inputs),
            "state_count": len(self.states),
            "inputs": {name: self.inputs[name] for name in sorted(self.inputs)},
            "states": {name: self.states[name] for name in sorted(self.states)},
            "external_controls_in_cone": controls,
            "external_control_depths": {name: self.depth_of(name) for name in controls},
            "lock_in_cone": bool(lock_signal and self.contains(lock_signal)),
            "example_paths": {name: self.paths[name] for name in controls if name in self.paths},
        }


class ConeReport(object):
    """Cones for every policy target, plus how much of the design they cover."""

    def __init__(self, system, policy):
        self.system = system
        self.policy = policy
        self.cones = {}
        self.design_state_count = len(system.states)
        self.design_input_count = len(system.inputs)

    def add(self, cone):
        self.cones[cone.target] = cone

    def selected_states(self):
        selected = set()
        for cone in self.cones.values():
            selected.update(cone.targets)
            selected.update(cone.states)
        return selected

    def summary(self):
        selected = self.selected_states()
        return {
            "design_states": self.design_state_count,
            "design_inputs": self.design_input_count,
            "states_in_selected_cones": len(selected),
            "state_reduction_fraction": (
                round(1.0 - len(selected) / float(self.design_state_count), 6)
                if self.design_state_count
                else 0.0
            ),
            "targets": sorted(self.cones),
        }


def _dependencies(system, cache):
    """``state -> (input_names, state_names)`` from the flattened next-state term."""

    def deps(state):
        cached = cache.get(state)
        if cached is not None:
            return cached
        term = system.next_terms.get(state)
        if term is None:
            result = (set(), set())
        else:
            names = set(expr.variables(system.flatten(term)))
            result = (
                {name for name in names if name in system.inputs},
                {name for name in names if name in system.initial},
            )
        cache[state] = result
        return result

    return deps


def _cone_for(system, target, targets, deps):
    """Breadth-first fan-in walk, recording the shortest path to each signal."""
    cone = Cone(target, targets)
    parent = {}
    frontier = [state for state in targets if state in system.initial]
    for state in frontier:
        cone.states.setdefault(state, 0)
    depth = 0
    seen = set(frontier)
    while frontier:
        depth += 1
        following = []
        for state in frontier:
            input_names, state_names = deps(state)
            for name in sorted(input_names):
                if name not in cone.inputs:
                    cone.inputs[name] = depth
                    parent[name] = state
            for name in sorted(state_names):
                if name in seen:
                    continue
                seen.add(name)
                cone.states[name] = depth
                parent[name] = state
                following.append(name)
        frontier = following

    for name in cone.inputs:
        path = [name]
        current = parent.get(name)
        guard = 0
        while current is not None and guard <= len(system.states) + 1:
            path.append(current)
            if current in targets:
                break
            current = parent.get(current)
            guard += 1
        cone.paths[name] = path
    return cone


def analyse(system, policy):
    """Compute the fan-in cone of every sensitive register (and of the lock)."""
    cache = {}
    deps = _dependencies(system, cache)
    report = ConeReport(system, policy)
    for register in policy.registers:
        targets = [name for name in register.signals if name in system.initial]
        report.add(_cone_for(system, register.name, targets, deps))
    if policy.lock_signal and policy.lock_signal in system.initial:
        report.add(_cone_for(system, "lock", [policy.lock_signal], deps))
    return report
