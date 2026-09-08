"""Combinational cones: extraction, boundary naming and structural signatures.

A *cone* here is exactly what ``z3_utils::compare_nets`` builds a function
over: the maximal set of gates carrying the ``combinational`` gate type
property that feed one net, cut at every net whose driver is not such a gate.
The cut nets are the cone's *boundary*, and the names given to them are what
makes two cones from two different netlists comparable at all.

Boundary naming mirrors ``substitute_net_ids`` in
``plugins/z3_utils/src/netlist_comparison.cpp`` exactly, because this package
must not invent a second, subtly different correspondence model:

===================================  =======================================
boundary net                         variable name
===================================  =======================================
global input net of the top module   ``GLOBAL_IN_<top module pin name>``
driven by a *sequential* gate        ``<gate name>_<output pin name>``
anything else (undriven, or driven   the raw net *name*
by a gate that is neither
combinational nor sequential)
===================================  =======================================

The third row is the one that deserves suspicion, and this module marks it as
:data:`BOUNDARY_DANGLING` / :data:`BOUNDARY_UNMODELLED` so callers can report
it instead of quietly relying on two net names happening to match.

Nothing here imports ``hal_py``; every netlist object is used through the
duck-typed accessors the bindings expose, so the whole module is exercised
with stub objects in ``test_hal_semantic_diff.py``.
"""

import hashlib

from hal_findings.adapters.common import call, gate_type_properties

__all__ = [
    "ConeError",
    "BOUNDARY_GLOBAL_INPUT",
    "BOUNDARY_SEQUENTIAL",
    "BOUNDARY_UNMODELLED",
    "BOUNDARY_DANGLING",
    "Boundary",
    "Cone",
    "is_combinational",
    "is_sequential",
    "boundary_name",
    "extract_cone",
]

#: The boundary net is a top-module input; its value is a primary input.
BOUNDARY_GLOBAL_INPUT = "global_input"
#: The boundary net is driven by a sequential gate; its value is state.
BOUNDARY_SEQUENTIAL = "sequential"
#: Driven by a gate that is neither combinational nor sequential -- outside the model.
BOUNDARY_UNMODELLED = "unmodelled"
#: No driver at all and not a top-module input -- correspondence rests on the net name.
BOUNDARY_DANGLING = "dangling"


class ConeError(RuntimeError):
    """A cone could not be built. ``kind`` is a findings ``error.kind``."""

    def __init__(self, message, kind="invalid_input"):
        RuntimeError.__init__(self, message)
        self.kind = kind


def is_combinational(gate):
    """True when the gate's *type* carries the ``combinational`` property.

    This is the same predicate ``z3_utils`` uses to decide what to traverse.
    It is deliberately not a guess about what a primitive does.
    """
    gate_type = call(gate, "get_type")
    if gate_type is None:
        return False
    return "combinational" in gate_type_properties(gate_type)


def is_sequential(gate):
    """True when the gate's type carries the ``sequential`` property."""
    gate_type = call(gate, "get_type")
    if gate_type is None:
        return False
    return "sequential" in gate_type_properties(gate_type)


class Boundary(object):
    """One cut net of a cone, with the variable name it becomes.

    ``raw_name`` is the name the ``z3_utils`` scheme gives it in its own
    netlist; ``name`` is that name after the correspondence's renaming, i.e.
    the *shared* namespace both builds are compared in.  They differ only when
    the mapping file renames something.
    """

    def __init__(self, net, name, kind, gate=None, pin=None, raw_name=None):
        self.net = net
        self.name = name
        self.raw_name = raw_name if raw_name is not None else name
        self.kind = kind
        self.gate = gate
        self.pin = pin

    @property
    def net_id(self):
        return call(self.net, "get_id")

    @property
    def net_name(self):
        return call(self.net, "get_name", default="")

    def as_dict(self):
        entry = {"name": self.name, "kind": self.kind, "net": self.net_name}
        if self.raw_name != self.name:
            entry["renamed_from"] = self.raw_name
        if self.net_id is not None:
            entry["net_id"] = self.net_id
        return entry

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Boundary({!r}, {})".format(self.name, self.kind)


def _top_input_pin_name(netlist, net):
    """Name of the top-module pin a global input net is bound to, if any."""
    top = call(netlist, "get_top_module")
    if top is None:
        return None
    pin = call(top, "get_pin_by_net", net)
    if pin is None:
        return None
    return call(pin, "get_name")


def boundary_name(netlist, net):
    """Return ``(name, kind, gate, pin)`` for a cut net, mirroring z3_utils."""
    if call(net, "is_global_input_net", default=False):
        pin_name = _top_input_pin_name(netlist, net)
        if pin_name:
            return "GLOBAL_IN_" + str(pin_name), BOUNDARY_GLOBAL_INPUT, None, pin_name
        # z3_utils errors out here; this package reports it rather than crashing.
        return (
            call(net, "get_name", default=""),
            BOUNDARY_DANGLING,
            None,
            None,
        )

    sources = call(net, "get_sources", default=[]) or []
    sequential = []
    unmodelled = []
    for endpoint in sources:
        gate = call(endpoint, "get_gate")
        if gate is None:
            continue
        if is_sequential(gate):
            sequential.append(endpoint)
        elif not is_combinational(gate):
            unmodelled.append(endpoint)

    if sequential:
        endpoint = sequential[0]
        gate = call(endpoint, "get_gate")
        pin = call(endpoint, "get_pin")
        pin_name = call(pin, "get_name", default="")
        gate_name = call(gate, "get_name", default="")
        return (
            "{}_{}".format(gate_name, pin_name),
            BOUNDARY_SEQUENTIAL,
            gate,
            pin_name,
        )

    if unmodelled:
        endpoint = unmodelled[0]
        gate = call(endpoint, "get_gate")
        pin_name = call(call(endpoint, "get_pin"), "get_name", default="")
        return (
            call(net, "get_name", default=""),
            BOUNDARY_UNMODELLED,
            gate,
            pin_name,
        )

    return call(net, "get_name", default=""), BOUNDARY_DANGLING, None, None


def _fan_in_nets(gate):
    """Ordered ``(pin name, net)`` pairs of a gate's connected input pins."""
    pairs = []
    for endpoint in call(gate, "get_fan_in_endpoints", default=[]) or []:
        pin = call(endpoint, "get_pin")
        pairs.append((call(pin, "get_name", default=""), call(endpoint, "get_net")))
    pairs.sort(key=lambda entry: entry[0])
    return pairs


class Cone(object):
    """The combinational cone feeding one net, with its structure summarized."""

    def __init__(self, netlist, output_net, gates, boundaries, signature, gate_signatures):
        self.netlist = netlist
        self.output_net = output_net
        #: Gates of the cone, ordered by HAL id so two runs agree.
        self.gates = list(gates)
        #: :class:`Boundary` objects keyed in insertion order.
        self.boundaries = list(boundaries)
        #: Canonical signature of the whole cone (a hex digest).
        self.signature = signature
        #: ``gate id -> signature of the sub-cone rooted at that gate``.
        self.gate_signatures = dict(gate_signatures)

    @property
    def boundary_names(self):
        return sorted({boundary.name for boundary in self.boundaries})

    def boundary_kinds(self):
        kinds = {}
        for boundary in self.boundaries:
            kinds.setdefault(boundary.kind, []).append(boundary.name)
        return {kind: sorted(set(names)) for kind, names in kinds.items()}

    def gate_type_histogram(self):
        histogram = {}
        for gate in self.gates:
            name = call(call(gate, "get_type"), "get_name", default="<untyped>")
            histogram[name] = histogram.get(name, 0) + 1
        return histogram

    def subsignatures(self):
        """Every sub-cone signature, used to localize which gates differ."""
        return set(self.gate_signatures.values())

    def gates_with_signature_not_in(self, other):
        """Gates whose sub-cone does not occur anywhere in ``other``."""
        foreign = other.subsignatures()
        return [
            gate
            for gate in self.gates
            if self.gate_signatures.get(call(gate, "get_id")) not in foreign
        ]

    def summary(self):
        return {
            "signature": self.signature,
            "gate_count": len(self.gates),
            "gate_types": self.gate_type_histogram(),
            "boundary": sorted(boundary.as_dict()["name"] for boundary in self.boundaries),
            "boundary_kinds": self.boundary_kinds(),
        }


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def extract_cone(netlist, net, max_gates=4096, rename=None):
    """Build the combinational cone feeding ``net``.

    ``rename`` is applied to every boundary variable name, which is how a
    correspondence that renames a register or a top-level pin is honoured: the
    cone of build A is expressed in build B's namespace, so the two functions
    are comparable.  With the identity mapping this changes nothing.

    Raises :class:`ConeError` for a multi-driven net (which ``z3_utils`` also
    refuses) and for a cone larger than ``max_gates`` -- an unbounded traversal
    that silently succeeds after minutes is not a better answer.
    """
    if net is None:
        raise ConeError("cannot build a cone for a net that does not exist")

    gates = {}
    boundaries = {}
    signatures = {}
    gate_signatures = {}
    in_progress = set()

    def signature_of_net(current):
        net_id = call(current, "get_id")
        if net_id in signatures:
            return signatures[net_id]
        if net_id in in_progress:
            # A combinational loop. z3_utils would recurse forever here.
            raise ConeError(
                "combinational feedback loop through net {!r}".format(
                    call(current, "get_name", default="")
                ),
                kind="invalid_input",
            )
        in_progress.add(net_id)
        try:
            sources = call(current, "get_sources", default=[]) or []
            if len(sources) > 1:
                raise ConeError(
                    "net {!r} is driven by {} sources; z3_utils refuses multi-driven "
                    "nets and so does this comparison".format(
                        call(current, "get_name", default=""), len(sources)
                    ),
                    kind="invalid_input",
                )

            driver = call(sources[0], "get_gate") if sources else None
            if driver is None or not is_combinational(driver):
                raw, kind, gate, pin = boundary_name(netlist, current)
                name = rename(raw) if rename is not None else raw
                boundaries.setdefault(
                    net_id, Boundary(current, name, kind, gate=gate, pin=pin, raw_name=raw)
                )
                signature = "IN({})".format(name)
                signatures[net_id] = signature
                return signature

            gate_id = call(driver, "get_id")
            gates.setdefault(gate_id, driver)
            if len(gates) > max_gates:
                raise ConeError(
                    "the cone of net {!r} exceeds the {} gate budget".format(
                        call(net, "get_name", default=""), max_gates
                    ),
                    kind="resource",
                )

            parts = []
            for pin_name, fan_in in _fan_in_nets(driver):
                if fan_in is None:
                    parts.append("{}=<open>".format(pin_name))
                    continue
                parts.append("{}={}".format(pin_name, signature_of_net(fan_in)))

            type_name = call(call(driver, "get_type"), "get_name", default="<untyped>")
            out_pin = call(sources[0], "get_pin")
            out_pin_name = call(out_pin, "get_name", default="")
            signature = "{}:{}({})".format(type_name, out_pin_name, ",".join(parts))
            signatures[net_id] = signature
            gate_signatures[gate_id] = _digest(signature)
            return signature
        finally:
            in_progress.discard(net_id)

    root = signature_of_net(net)

    ordered_gates = [gates[key] for key in sorted(gates)]
    ordered_boundaries = [boundaries[key] for key in sorted(boundaries)]
    return Cone(
        netlist,
        net,
        ordered_gates,
        ordered_boundaries,
        _digest(root),
        gate_signatures,
    )
