"""Proposing state-register candidates from feedback structure.

The input is a :class:`SequentialGraph`: one node per sequential gate, an edge
``u -> v`` when the next-state function of ``u`` depends on an output of ``v``
through combinational logic only, plus the combinational cone, the free input
nets and the fan-out of every node.  :mod:`hal_fsm.extract` builds one from a
netlist; nothing in this module imports ``hal_py``, so the proposal logic is
unit-testable against hand-written graphs.

Three independent generators propose candidates:

``scc``
    Non-trivial strongly connected components of the dependency graph.  A
    controller's state bits are mutually dependent -- state 0 can lead to state
    1 and back -- so an SCC of two or more flip-flops is the strongest purely
    structural signal there is.

``self_loop_cluster``
    Flip-flops that depend on themselves, grouped by weak connectivity.  This
    is what catches a counter: ``c1``'s next state depends on ``c0``, but
    ``c0``'s never depends on ``c1``, so a counter is *not* an SCC.  It is
    still a state machine, and it is still a plausible candidate -- just a less
    FSM-shaped one, which the score reflects.

``dataflow``
    DANA's register groups, intersected with the flip-flops that have feedback.
    One-bit groups whose next-state functions read each other are closed up
    first -- DANA sees a counter as a chain of one-bit groups, and a third of a
    register is not a candidate.  Optional: it needs the ``dataflow`` plugin.

Identical proposals from different generators are merged, and agreement raises
the score slightly.  Nothing here proves anything; every candidate becomes a
``heuristic`` finding.

Using it
--------

:func:`propose` returns **two** values -- the ranked candidates and a list of
plain-language notes about what it deliberately left out.  The notes are not
optional decoration: "these 12 flip-flops have no feedback path and were not
proposed" is usually the most informative line of a run::

    from hal_fsm import candidates, extract

    extraction = extract.extract(netlist)               # builds a SequentialGraph
    proposed, notes = candidates.propose(extraction.graph)
    for note in notes:
        print("note:", note)

Each entry of ``proposed`` is a :class:`Candidate`.  Its attributes are
``gate_ids`` (a sorted tuple of HAL gate IDs), ``sources`` (which generators
proposed it: ``"scc"``, ``"self_loop_cluster"``, ``"dataflow"``), ``score``,
``features``, ``reasons`` and ``size``; ``names(graph)`` turns the IDs into
gate names.  They are *not* called ``members`` or ``origins`` -- ``origin``
does exist but is a different thing, ``"heuristic"`` or ``"user_override"``::

    best = proposed[0]
    print(best.score, best.size, best.sources)         # 0.83 4 ('scc',)
    print(best.gate_ids)                               # (12, 13, 14, 15)
    print(best.names(extraction.graph))                # ['g1', 'g2', 'g4', 'g6']
    for reason in best.reasons:
        print(" -", reason)

:func:`propose` already returns its list ranked, so :func:`rank` is only needed
when candidates are filtered, merged or hand-built.  It takes the **list**, not
the pair -- passing the pair raises a :class:`TypeError` that says so::

    shortlist = [c for c in proposed if c.size <= 8]
    for candidate in candidates.rank(shortlist):
        ...

    candidates.rank(candidates.propose(graph))         # TypeError: unpack it first
    candidates.rank(candidates.propose(graph)[0])      # fine

:func:`ambiguous_group` answers the question a score alone cannot -- whether
the top candidate is actually distinguishable from the runner-up::

    tied = candidates.ambiguous_group(proposed, margin=0.05)
    if tied:
        print("{} candidates within 0.05 of the best score".format(len(tied)))
"""

__all__ = [
    "SequentialGate",
    "SequentialGraph",
    "Candidate",
    "WEIGHTS",
    "strongly_connected_components",
    "propose",
    "rank",
    "ambiguous_group",
]


class SequentialGate(object):
    """One sequential gate, described in plain JSON-able values."""

    def __init__(
        self,
        gate_id,
        name,
        gate_type=None,
        properties=(),
        clock_nets=(),
        data_net=None,
        control=(),
        init_value=None,
        module=None,
    ):
        self.id = int(gate_id)
        self.name = name
        self.type = gate_type
        self.properties = tuple(sorted(properties))
        self.clock_nets = tuple(sorted(clock_nets))
        #: net ID feeding the single ``data`` pin, or ``None`` when there is no
        #: single data pin (which ``solve_fsm`` refuses outright).
        self.data_net = data_net
        #: ``{"pin", "pin_type", "net_id", "net_name", "constant"}`` per
        #: asynchronous control pin (set/reset/enable/...).
        self.control = list(control)
        #: value of the gate's ``INIT``-style attribute, or ``None``.
        self.init_value = init_value
        self.module = module

    def to_json(self):
        out = {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "properties": list(self.properties),
            "clock_nets": list(self.clock_nets),
            "data_net": self.data_net,
            "control": list(self.control),
        }
        if self.init_value is not None:
            out["init_value"] = self.init_value
        if self.module is not None:
            out["module"] = self.module
        return out


class SequentialGraph(object):
    """Sequential gates plus how their next-state functions depend on each other."""

    def __init__(self):
        self.gates = {}
        #: ``{gate_id: set(gate_id)}`` -- the next state of the key depends on
        #: an output of each value.
        self.depends = {}
        #: ``{gate_id: set(gate_id)}`` -- combinational gates in the next-state cone.
        self.cones = {}
        #: ``{gate_id: set(net_id)}`` -- nets entering the cone from outside.
        self.free_inputs = {}
        #: ``{gate_id: set(gate_id)}`` -- combinational gates driven by this gate.
        self.fanout = {}
        #: ``{gate_id: {net_id: source sequential gate id or None}}`` -- every net
        #: entering the next-state cone from outside.  Which of them counts as a
        #: *free input* depends on the candidate, so it is decided per candidate
        #: in :meth:`free_inputs_of` rather than baked in here.
        self.boundaries = {}
        #: gate IDs the extractor could not describe well enough to solve.
        self.unusable = {}

    def add(self, gate, depends=(), cone=(), free_inputs=(), fanout=()):
        self.gates[gate.id] = gate
        self.depends[gate.id] = set(int(x) for x in depends)
        self.cones[gate.id] = set(int(x) for x in cone)
        self.free_inputs[gate.id] = set(int(x) for x in free_inputs)
        self.fanout[gate.id] = set(int(x) for x in fanout)
        return gate

    # -- derived views ---------------------------------------------------

    def ids(self):
        return sorted(self.gates)

    def cone_of(self, gate_ids):
        """Union of the next-state cones of ``gate_ids``."""
        cone = set()
        for gate_id in gate_ids:
            cone |= self.cones.get(gate_id, set())
        return cone

    def free_inputs_of(self, gate_ids):
        """Nets the candidate's transition logic reads from outside the candidate.

        A net driven by a flip-flop *of the candidate* is state, not input; a net
        driven by any other flip-flop is an input as far as this machine is
        concerned, and one with no driver at all is a primary input.
        """
        members = set(int(gate_id) for gate_id in gate_ids)
        inputs = set()
        for gate_id in members:
            boundaries = self.boundaries.get(gate_id)
            if boundaries is None:
                inputs |= self.free_inputs.get(gate_id, set())
                continue
            for net_id, source in boundaries.items():
                if source is None or int(source) not in members:
                    inputs.add(int(net_id))
        return inputs

    def to_json(self):
        return {
            "gates": [self.gates[gate_id].to_json() for gate_id in self.ids()],
            "depends": {
                str(gate_id): sorted(self.depends[gate_id]) for gate_id in self.ids()
            },
            "unusable": dict(self.unusable),
        }


def strongly_connected_components(nodes, edges):
    """Tarjan's SCC, iterative so a deep netlist cannot blow the stack.

    ``edges`` maps a node to the nodes it points at.  Returns a list of sorted
    node lists, itself sorted, so the output is deterministic.
    """
    index_of = {}
    low = {}
    on_stack = set()
    stack = []
    result = []
    counter = [0]

    for root in nodes:
        if root in index_of:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index_of[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            node, successors = work[-1]
            advanced = False
            for successor in successors:
                if successor not in nodes:
                    continue
                if successor not in index_of:
                    index_of[successor] = low[successor] = counter[0]
                    counter[0] += 1
                    stack.append(successor)
                    on_stack.add(successor)
                    work.append((successor, iter(sorted(edges.get(successor, ())))))
                    advanced = True
                    break
                if successor in on_stack:
                    low[node] = min(low[node], index_of[successor])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                result.append(sorted(component))
    return sorted(result)


def _weak_components(nodes, edges):
    """Weakly connected components of the subgraph induced by ``nodes``."""
    adjacency = {node: set() for node in nodes}
    for node in nodes:
        for other in edges.get(node, ()):
            if other in adjacency and other != node:
                adjacency[node].add(other)
                adjacency[other].add(node)
    seen = set()
    components = []
    for node in sorted(nodes):
        if node in seen:
            continue
        stack = [node]
        component = []
        seen.add(node)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbour in sorted(adjacency[current]):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        components.append(sorted(component))
    return sorted(components)


def _merge_connected_singletons(groups, edges):
    """Merge one-flip-flop dataflow groups whose next-state functions read each other.

    DANA groups registers by data-flow *shape*, and a counter is a chain rather
    than a word: ``cnt_r1``'s next state reads ``cnt_r0``, but ``cnt_r0``'s
    never reads ``cnt_r1``.  Real DANA therefore hands back one group per
    counter bit, and proposing those singletons proposes a third of a register
    each -- fragments that then compete with the whole counter and, being
    smaller, sort ahead of it on a tie.  A flip-flop whose next-state function
    is read by another proposed flip-flop is a *bit* of a state register, not a
    state register, so the singletons that reach each other through the
    next-state dependency graph are closed up into the register they came from.

    Groups of two or more flip-flops are left exactly as DANA reported them: a
    multi-bit group is the plugin's own answer, and joining two of those would
    merge, say, a controller with the counter it enables.  Returns
    ``(groups, merged)`` where ``merged`` lists only the components that were
    actually closed up, so the caller can say so in a note.
    """
    singletons = sorted(group[0] for group in groups if len(group) == 1)
    result = [sorted(group) for group in groups if len(group) != 1]
    merged = []
    for component in _weak_components(set(singletons), edges):
        result.append(sorted(component))
        if len(component) > 1:
            merged.append(sorted(component))
    return result, merged


#: Feature weights.  They sum to 1 so the score reads as a confidence in
#: ``[0, 1]``; they are published because a hidden weighting is not a heuristic,
#: it is a magic number.
WEIGHTS = {
    "size_fit": 0.15,
    "mutual_feedback": 0.30,
    "closure": 0.15,
    "input_pressure": 0.15,
    "control_fanout": 0.10,
    "uniformity": 0.10,
    "agreement": 0.05,
}


class Candidate(object):
    """One proposed state register, with the features that produced its score."""

    def __init__(self, gate_ids, sources=(), features=None, score=0.0, reasons=(), origin=None):
        self.gate_ids = tuple(sorted(int(x) for x in gate_ids))
        self.sources = tuple(sorted(set(sources)))
        self.features = dict(features or {})
        self.score = float(score)
        self.reasons = list(reasons)
        #: ``"heuristic"`` or ``"user_override"`` -- a user selection is never scored.
        self.origin = origin or "heuristic"

    @property
    def key(self):
        return self.gate_ids

    @property
    def size(self):
        return len(self.gate_ids)

    def names(self, graph):
        return [graph.gates[gate_id].name for gate_id in self.gate_ids]

    def to_json(self, graph=None):
        out = {
            "gate_ids": list(self.gate_ids),
            "sources": list(self.sources),
            "origin": self.origin,
            "features": {k: round(float(v), 4) for k, v in sorted(self.features.items())},
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
        }
        if graph is not None:
            out["gate_names"] = self.names(graph)
        return out


def _size_fit(size):
    if size <= 0:
        return 0.0
    if size == 1:
        return 0.6
    if size <= 8:
        return 1.0
    if size <= 16:
        return 0.5
    return 0.2


def _mutual_feedback(graph, gate_ids):
    """1.0 for a real SCC, 0.5 for self-dependence only, 0.2 for neither."""
    members = set(gate_ids)
    sub_edges = {
        gate_id: (graph.depends.get(gate_id, set()) & members) for gate_id in members
    }
    components = strongly_connected_components(members, sub_edges)
    biggest = max((len(component) for component in components), default=0)
    if len(members) >= 2 and biggest == len(members):
        return 1.0
    if all(gate_id in graph.depends.get(gate_id, set()) for gate_id in members):
        return 0.5 if len(members) > 1 else 0.7
    return 0.2


def _closure(graph, gate_ids):
    """Fraction of the members' sequential dependencies that stay inside the set."""
    members = set(gate_ids)
    inside = 0
    total = 0
    for gate_id in members:
        for dependency in graph.depends.get(gate_id, set()):
            total += 1
            if dependency in members:
                inside += 1
    if total == 0:
        return 0.0
    return float(inside) / float(total)


def _input_pressure(count):
    """A controller reacts to a handful of signals, not to a data bus."""
    if count == 0:
        return 0.5
    if count <= 4:
        return 1.0
    if count <= 8:
        return 0.7
    if count <= 16:
        return 0.4
    return 0.15


def _control_fanout(graph, gate_ids):
    members = set(gate_ids)
    cone = graph.cone_of(members)
    outside = set()
    for gate_id in members:
        outside |= graph.fanout.get(gate_id, set()) - cone
    return 1.0 if outside else 0.3


def _uniformity(graph, gate_ids):
    types = {graph.gates[gate_id].type for gate_id in gate_ids}
    clocks = set()
    for gate_id in gate_ids:
        clocks |= set(graph.gates[gate_id].clock_nets)
    score = 0.0
    score += 0.5 if len(types) == 1 else 0.0
    score += 0.5 if len(clocks) == 1 else 0.0
    return score


def score_candidate(graph, gate_ids, sources):
    """Score a proposed set and explain the score in words."""
    free_inputs = graph.free_inputs_of(gate_ids)
    features = {
        "size_fit": _size_fit(len(gate_ids)),
        "mutual_feedback": _mutual_feedback(graph, gate_ids),
        "closure": _closure(graph, gate_ids),
        "input_pressure": _input_pressure(len(free_inputs)),
        "control_fanout": _control_fanout(graph, gate_ids),
        "uniformity": _uniformity(graph, gate_ids),
        "agreement": 1.0 if len(set(sources)) > 1 else 0.6,
    }
    score = sum(WEIGHTS[name] * value for name, value in features.items())

    reasons = []
    if features["mutual_feedback"] >= 1.0:
        reasons.append(
            "the {} flip-flops form a strongly connected component of the next-state "
            "dependency graph".format(len(gate_ids))
        )
    elif features["mutual_feedback"] >= 0.5:
        reasons.append(
            "every flip-flop depends on its own output, but they are not mutually "
            "dependent -- a counter-like shape rather than a controller"
        )
    else:
        reasons.append("the set has little feedback structure of its own")
    reasons.append(
        "{} of the set's sequential dependencies stay inside it".format(
            "all" if features["closure"] >= 1.0 else "{:.0%}".format(features["closure"])
        )
    )
    reasons.append(
        "the next-state cone reads {} net(s) from outside the register".format(len(free_inputs))
    )
    if features["control_fanout"] >= 1.0:
        reasons.append("the register drives logic outside its own transition cone")
    else:
        reasons.append("the register drives nothing outside its own transition cone")
    if features["uniformity"] < 1.0:
        reasons.append(
            "the flip-flops do not share a single gate type and clock net, which is "
            "unusual for one state register"
        )
    if len(set(sources)) > 1:
        reasons.append(
            "proposed independently by {}".format(", ".join(sorted(set(sources))))
        )
    return features, score, reasons


def propose(graph, dataflow_groups=None, limits=None):
    """Propose and rank candidates.  Returns ``(candidates, notes)``."""
    notes = []
    nodes = set(graph.ids())
    edges = {gate_id: set(graph.depends.get(gate_id, set())) for gate_id in nodes}

    proposals = {}

    def record(gate_ids, source):
        key = tuple(sorted(gate_ids))
        if not key:
            return
        proposals.setdefault(key, set()).add(source)

    for component in strongly_connected_components(nodes, edges):
        if len(component) >= 2:
            record(component, "scc")

    self_dependent = {
        gate_id for gate_id in nodes if gate_id in graph.depends.get(gate_id, set())
    }
    covered_by_scc = set()
    for key in proposals:
        covered_by_scc |= set(key)
    for component in _weak_components(self_dependent, edges):
        if set(component) <= covered_by_scc and len(component) > 1:
            continue
        record(component, "self_loop_cluster")

    dataflow_proposals = []
    for group_id, group_gate_ids in sorted((dataflow_groups or {}).items()):
        filtered = sorted(set(int(x) for x in group_gate_ids) & (self_dependent | covered_by_scc))
        if not filtered:
            continue
        dataflow_proposals.append(filtered)
    dataflow_proposals, merged_groups = _merge_connected_singletons(dataflow_proposals, edges)
    for component in merged_groups:
        notes.append(
            "{} single-flip-flop dataflow group(s) were closed up into one candidate "
            "because their next-state functions read each other: {}. A counter is a "
            "chain of one-bit groups to DANA, and its bits are not separate state "
            "registers".format(
                len(component),
                ", ".join(graph.gates[gate_id].name for gate_id in component),
            )
        )
    for group in dataflow_proposals:
        record(group, "dataflow")

    no_feedback = sorted(nodes - self_dependent - covered_by_scc)
    if no_feedback:
        notes.append(
            "{} sequential gate(s) have no feedback path to their own next-state "
            "function and were not proposed as state registers: {}".format(
                len(no_feedback),
                ", ".join(graph.gates[gate_id].name for gate_id in no_feedback[:8])
                + (" ..." if len(no_feedback) > 8 else ""),
            )
        )

    candidates = []
    for gate_ids, sources in proposals.items():
        features, score, reasons = score_candidate(graph, gate_ids, sources)
        candidates.append(
            Candidate(gate_ids, sources=sources, features=features, score=score, reasons=reasons)
        )

    candidates = rank(candidates)

    if limits is not None:
        oversized = [c for c in candidates if c.size > limits.max_state_bits]
        if oversized:
            notes.append(
                "{} candidate(s) exceed the max_state_bits limit of {} and will not be "
                "handed to the solver: {}".format(
                    len(oversized),
                    limits.max_state_bits,
                    "; ".join(
                        "{} flip-flops".format(c.size) for c in oversized[:4]
                    ),
                )
            )
        if len(candidates) > limits.max_candidates:
            notes.append(
                "{} candidates were proposed; only the {} highest scoring are "
                "reported (max_candidates)".format(len(candidates), limits.max_candidates)
            )
            candidates = candidates[: limits.max_candidates]

    return candidates, notes


def rank(candidates):
    """Sort by score, then deterministically by size and gate IDs.

    Takes the *list* of :class:`Candidate` objects, not the ``(candidates,
    notes)`` pair :func:`propose` returns.  Handing it that pair used to raise
    ``AttributeError: 'list' object has no attribute 'score'`` from inside the
    sort key, which names neither the mistake nor the fix, so the pair is
    rejected up front with a message that names both.
    """
    if isinstance(candidates, tuple):
        raise TypeError(
            "rank() takes the list of Candidate objects, but it was given the "
            "2-tuple that propose() returns. propose() gives back "
            "(candidates, notes): unpack it first --\n"
            "    proposed, notes = propose(graph)\n"
            "    ranked = rank(proposed)\n"
            "or rank(propose(graph)[0]). Note that propose() already returns "
            "its list ranked, so a second rank() call is usually redundant."
        )
    return sorted(candidates, key=lambda c: (-c.score, c.size, c.gate_ids))


def ambiguous_group(candidates, margin):
    """The candidates that are within ``margin`` of the best score.

    Returned whenever it holds more than one entry: a tie between two plausible
    state registers is a fact about the design that the report has to state,
    not something to break silently by sort order.
    """
    if not candidates:
        return []
    best = candidates[0].score
    tied = [c for c in candidates if best - c.score <= margin + 1e-12]
    return tied if len(tied) > 1 else []
