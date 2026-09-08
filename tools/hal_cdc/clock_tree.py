"""Optional cross-check against the ``clock_tree_extractor`` plugin.

``hal_cdc`` resolves clock sources itself (see :mod:`hal_cdc.domains`) because
it has to answer a question the extractor does not: *which declared clock does
this register belong to*.  The extractor answers a different and complementary
one: *what does the recovered clock distribution network look like*.

Running both and comparing them is cheap and catches real problems -- a
register the extractor placed in a clock tree but hal_cdc could not resolve, or
a buffer chain hal_cdc walked through that the extractor did not recover.

Known limitation of the extractor as of the version in this tree: when a
flip-flop's clock pin is driven straight by a global input net, ``from_netlist``
records the net and the flip-flop as *vertices* but adds no edge between them,
so the flat "clock straight from a port" case produces isolated vertices rather
than a tree.  hal_cdc therefore treats the extractor as corroborating evidence
and never as the source of truth for a register's domain.
"""

__all__ = ["ClockTreeInfo", "extract_clock_tree"]


class ClockTreeInfo(object):
    """What the extractor recovered, reduced to ids of *this* netlist."""

    __slots__ = ("available", "reason", "gate_ids", "net_ids", "dot_path", "error")

    def __init__(self, available=False, reason="", gate_ids=(), net_ids=(), dot_path=None,
                 error=None):
        self.available = bool(available)
        self.reason = reason
        self.gate_ids = frozenset(gate_ids)
        self.net_ids = frozenset(net_ids)
        self.dot_path = dot_path
        self.error = error

    def coverage(self, view, clock_resolutions):
        """Compare the recovered tree with hal_cdc's own clock resolution."""
        sequential = [gate for gate in view.sequential_gates()]
        in_tree = [gate for gate in sequential if gate.id in self.gate_ids]
        resolved = [
            gate
            for gate in sequential
            if clock_resolutions.get(gate.id) is not None
            and clock_resolutions[gate.id].is_known
        ]
        resolved_ids = {gate.id for gate in resolved}
        return {
            "sequential_gates": len(sequential),
            "in_recovered_clock_tree": len(in_tree),
            "clock_resolved_by_hal_cdc": len(resolved),
            "in_tree_but_unresolved": sorted(
                gate.name for gate in in_tree if gate.id not in resolved_ids
            )[:32],
            "resolved_but_not_in_tree": sorted(
                gate.name for gate in resolved if gate.id not in self.gate_ids
            )[:32],
            "clock_tree_gates": len(self.gate_ids),
            "clock_tree_nets": len(self.net_ids),
        }

    def to_json(self):
        data = {"available": self.available}
        if self.reason:
            data["reason"] = self.reason
        if self.error:
            data["error"] = self.error
        if self.available:
            data["gate_count"] = len(self.gate_ids)
            data["net_count"] = len(self.net_ids)
        if self.dot_path:
            data["dot"] = self.dot_path
        return data


def extract_clock_tree(netlist, dot_path=None):
    """Run ``clock_tree_extractor.ClockTree.from_netlist`` if it is available.

    Never raises: a missing or failing plugin becomes an unavailable
    :class:`ClockTreeInfo` with a reason, because the audit must still run.
    """
    try:
        module = __import__(
            "hal_plugins.clock_tree_extractor", fromlist=["clock_tree_extractor"]
        )
    except ImportError as exc:
        return ClockTreeInfo(
            reason="the clock_tree_extractor plugin is not importable ({}); hal_cdc ran "
            "without the clock-tree cross-check".format(exc)
        )

    clock_tree_class = getattr(module, "ClockTree", None)
    if clock_tree_class is None or not hasattr(clock_tree_class, "from_netlist"):
        return ClockTreeInfo(
            reason="hal_plugins.clock_tree_extractor has no ClockTree.from_netlist binding"
        )

    try:
        tree = clock_tree_class.from_netlist(netlist)
    except Exception as exc:  # noqa: BLE001 - a plugin failure must not abort the audit
        return ClockTreeInfo(error="ClockTree.from_netlist raised {}".format(exc))
    if tree is None:
        return ClockTreeInfo(error="ClockTree.from_netlist returned None (see the HAL log)")

    gate_ids, net_ids = set(), set()
    try:
        for gate in tree.get_gates() or []:
            gate_ids.add(gate.get_id())
        for net in tree.get_nets() or []:
            net_ids.add(net.get_id())
    except Exception as exc:  # noqa: BLE001
        return ClockTreeInfo(error="reading the clock tree failed: {}".format(exc))

    written = None
    if dot_path:
        try:
            if tree.export(str(dot_path)):
                written = str(dot_path)
        except Exception:  # noqa: BLE001 - the DOT is evidence, not a result
            written = None

    return ClockTreeInfo(available=True, gate_ids=gate_ids, net_ids=net_ids, dot_path=written)
