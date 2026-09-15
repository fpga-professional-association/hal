"""Topological levelling of a directed graph, in pure stdlib.

Like :mod:`hal_viz.dot` this module knows nothing about HAL: it works on
hashable node keys and ``(source, target)`` pairs, so the whole graph-theory
half of the ``dag`` view is unit testable without a build, without Graphviz and
without a netlist.

The caller is responsible for *cutting* the graph before handing it over -- for
a netlist that means dropping the edges that end at a flip-flop or latch, which
is what turns the combinational core into a DAG.  What is left may still
contain a real combinational loop, and that is not an error here: every node
gets a level, and the nodes that sit on a cycle are reported separately so the
drawing can highlight them and the caller can warn about them.
"""

import collections

__all__ = [
    "Levels",
    "compute_levels",
    "strongly_connected_components",
]


class Levels(object):
    """The result of :func:`compute_levels`.

    * ``levels`` maps every node key to its integer level (0 = source rank).
    * ``by_level`` maps a level to the list of its nodes, in input order.
    * ``cycle_nodes`` is the set of nodes that sit on a directed cycle.
    * ``cycle_groups`` lists those cycles as node lists (one per non-trivial
      strongly connected component, plus any self-loop), in input order.
    * ``broken_edges`` are the edges that had to be ignored to level a cyclic
      graph; empty for an acyclic one.
    """

    def __init__(self, levels, by_level, cycle_nodes, cycle_groups, broken_edges):
        self.levels = levels
        self.by_level = by_level
        self.cycle_nodes = cycle_nodes
        self.cycle_groups = cycle_groups
        self.broken_edges = broken_edges

    @property
    def level_count(self):
        return len(self.by_level)

    @property
    def max_level(self):
        return max(self.levels.values()) if self.levels else -1

    @property
    def has_cycles(self):
        return bool(self.cycle_nodes)


def _adjacency(nodes, edges):
    """Return ``(order, successors, predecessors)`` with self-loops kept.

    Duplicate edges are collapsed: parallel nets between the same two gates say
    nothing extra about depth.  Edges touching a node that is not in ``nodes``
    are ignored, so a caller can pass a scope rather than a whole netlist.
    """
    order = []
    seen = set()
    for node in nodes:
        if node not in seen:
            seen.add(node)
            order.append(node)

    successors = dict((node, []) for node in order)
    predecessors = dict((node, []) for node in order)
    known = set()
    for source, target in edges:
        if source not in seen or target not in seen:
            continue
        if (source, target) in known:
            continue
        known.add((source, target))
        successors[source].append(target)
        predecessors[target].append(source)
    return order, successors, predecessors


def strongly_connected_components(nodes, edges):
    """Tarjan's SCC, iterative so a deep netlist cannot blow the stack.

    Returns the components as lists of node keys.  Components are returned in
    the order Tarjan closes them; the nodes inside one keep the input order.
    """
    order, successors, _predecessors = _adjacency(nodes, edges)
    position = dict((node, index) for index, node in enumerate(order))

    index_of = {}
    lowlink = {}
    on_stack = set()
    stack = []
    components = []
    counter = [0]

    for root in order:
        if root in index_of:
            continue
        # (node, iterator state) frames, walked without recursion
        work = [(root, 0)]
        while work:
            node, child_index = work[-1]
            if child_index == 0:
                index_of[node] = lowlink[node] = counter[0]
                counter[0] += 1
                stack.append(node)
                on_stack.add(node)

            recursed = False
            children = successors[node]
            while child_index < len(children):
                child = children[child_index]
                child_index += 1
                if child not in index_of:
                    work[-1] = (node, child_index)
                    work.append((child, 0))
                    recursed = True
                    break
                if child in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[child])
            if recursed:
                continue

            work[-1] = (node, child_index)
            work.pop()
            if lowlink[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                component.sort(key=lambda key: position[key])
                components.append(component)
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
    return components


def _cycles(order, ordered_edges, edges_set):
    """Non-trivial SCCs plus self-loops, i.e. every node on a directed cycle.

    ``ordered_edges`` rather than ``edges_set`` is what goes into the SCC walk:
    iterating a set of tuples follows hash order, which moves between
    interpreters, and that would reorder the reported cycles run to run.
    """
    groups = []
    for component in strongly_connected_components(order, ordered_edges):
        if len(component) > 1:
            groups.append(component)
        elif (component[0], component[0]) in edges_set:
            groups.append(component)
    return groups


def compute_levels(nodes, edges):
    """Assign a topological level to every node of a (possibly cyclic) graph.

    Kahn's algorithm: a node with no remaining unprocessed predecessor is a
    source at level 0, and every other node sits one level behind its deepest
    driver.  When the graph is not acyclic, Kahn stalls with a non-empty
    remainder; rather than dropping those nodes -- which would silently hide
    gates from the drawing -- the node with the fewest unresolved predecessors
    (ties broken by input order) is released at the level its already-resolved
    predecessors imply, the edges that were ignored to do so are recorded in
    ``broken_edges``, and levelling continues.  Every node is therefore always
    levelled exactly once, and the result is deterministic.
    """
    order, successors, predecessors = _adjacency(nodes, edges)
    ordered_edges = [
        (source, target) for source in order for target in successors[source]
    ]
    edges_set = set(ordered_edges)

    position = dict((node, index) for index, node in enumerate(order))
    remaining = dict((node, len(predecessors[node])) for node in order)
    levels = {}
    broken_edges = []

    # A node's level is only read once every predecessor has been processed, so
    # the order the queue is drained in does not change the result; it is a
    # plain FIFO seeded in input order, which keeps the cycle report stable.
    ready = collections.deque()
    for node in order:
        if remaining[node] == 0:
            levels[node] = 0
            del remaining[node]
            ready.append(node)

    while ready or remaining:
        while ready:
            node = ready.popleft()
            for target in successors[node]:
                if target not in remaining:
                    continue
                levels[target] = max(levels.get(target, 0), levels[node] + 1)
                remaining[target] -= 1
                if remaining[target] == 0:
                    del remaining[target]
                    ready.append(target)
        if not remaining:
            break
        # Stalled: everything left is on, or downstream of, a cycle.
        stuck = min(remaining, key=lambda key: (remaining[key], position[key]))
        for source in predecessors[stuck]:
            if source not in levels:
                broken_edges.append((source, stuck))
        levels[stuck] = levels.get(stuck, 0)
        del remaining[stuck]
        ready.append(stuck)

    by_level = {}
    for node in order:
        by_level.setdefault(levels[node], []).append(node)

    cycle_groups = _cycles(order, ordered_edges, edges_set)
    cycle_nodes = set()
    for group in cycle_groups:
        cycle_nodes.update(group)

    return Levels(levels, by_level, cycle_nodes, cycle_groups, broken_edges)
