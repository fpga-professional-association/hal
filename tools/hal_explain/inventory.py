"""The netlist snapshot a block model is resolved against.

A findings document says "gates 12, 13, 14 form a candidate register".  Turning
that into a block diagram needs three things the findings document does not
carry: the *complete* gate list (so unclaimed gates can be found), the pin/net
wiring (so block boundaries and edges can be computed), and gate-type semantics
(so a sequential gate can be told from a buffer).  That is the inventory.

It is built from :class:`hal_cdc.netlist_view.NetlistView`, which already exists
in two flavours -- one from ``hal_py`` and one from a dependency-free reader for
the fixtures -- so this module needs no HAL build and no second Verilog parser.

The inventory is written to disk as its own versioned JSON document, which is
what lets the whole composition step run outside HAL: ``hal_explain collect``
produces it inside HAL once, and every later ``compose``/``diagram``/``report``
run reads it back on a plain interpreter.
"""

import os

from .schema import INVENTORY_VERSION, SUPPORTED_INVENTORY_VERSIONS

__all__ = [
    "InventoryError",
    "Inventory",
    "from_netlist_view",
    "from_json",
]


class InventoryError(ValueError):
    """The inventory document is unusable."""


class Inventory(object):
    """An immutable, plain-data snapshot of one netlist."""

    def __init__(self, artifact_id, design, gates, nets, gate_types):
        self.artifact_id = str(artifact_id)
        #: the ``design`` object of the recovered-block schema
        self.design = dict(design)
        #: gate id -> ``{"id","name","type","fan_in","fan_out","module"?}``
        self.gates = {int(entry["id"]): dict(entry) for entry in gates}
        #: net id -> ``{"id","name","sources","destinations",flags...}``
        self.nets = {int(entry["id"]): dict(entry) for entry in nets}
        #: type name -> ``{"properties":[...], "pins":[{"name","direction","type"}]}``
        self.gate_types = {str(key): dict(value) for key, value in gate_types.items()}

        self._by_name = {}
        for gate in self.gates.values():
            self._by_name.setdefault(gate["name"], []).append(int(gate["id"]))
        self._control_pins = {}
        for name, entry in self.gate_types.items():
            self._control_pins[name] = frozenset(
                pin["name"]
                for pin in entry.get("pins", [])
                if pin.get("direction") == "input"
                and pin.get("type") in ("clock", "enable", "set", "reset", "select", "control")
            )

    # -- lookup ----------------------------------------------------------

    def gate_ids(self):
        return sorted(self.gates)

    def gate(self, gate_id):
        return self.gates.get(int(gate_id))

    def net(self, net_id):
        return self.nets.get(int(net_id))

    def gate_id_by_name(self, name):
        """The unique gate with this name, or ``None`` when it is ambiguous.

        Ambiguity is not resolved by picking the first match: two gates with the
        same name mean the reference cannot be resolved, and the caller records
        that instead of guessing.
        """
        matches = self._by_name.get(name, [])
        return matches[0] if len(matches) == 1 else None

    def gate_type(self, name):
        return self.gate_types.get(name, {})

    def properties(self, gate_id):
        gate = self.gates.get(int(gate_id))
        if gate is None:
            return frozenset()
        return frozenset(self.gate_types.get(gate["type"], {}).get("properties", []))

    def is_sequential(self, gate_id):
        return "sequential" in self.properties(gate_id)

    def is_constant(self, gate_id):
        return bool(self.properties(gate_id) & {"power", "ground"})

    def sequential_gate_ids(self):
        return [gid for gid in self.gate_ids() if self.is_sequential(gid)]

    def is_control_pin(self, gate_id, pin_name):
        gate = self.gates.get(int(gate_id))
        if gate is None:
            return False
        return pin_name in self._control_pins.get(gate["type"], frozenset())

    # -- connectivity ----------------------------------------------------

    def input_nets(self, gate_id):
        """``[(pin, net_id)]`` for every connected input pin."""
        gate = self.gates.get(int(gate_id))
        if gate is None:
            return []
        return sorted(
            (pin, int(net)) for pin, net in gate.get("fan_in", {}).items() if net is not None
        )

    def output_nets(self, gate_id):
        gate = self.gates.get(int(gate_id))
        if gate is None:
            return []
        return sorted({int(net) for net in gate.get("fan_out", {}).values() if net is not None})

    def net_drivers(self, net_id):
        net = self.nets.get(int(net_id))
        if net is None:
            return []
        return [int(entry[0]) for entry in net.get("sources", [])]

    def net_loads(self, net_id):
        """``[(gate_id, pin)]`` for every load of the net."""
        net = self.nets.get(int(net_id))
        if net is None:
            return []
        return [(int(entry[0]), str(entry[1])) for entry in net.get("destinations", [])]

    def is_constant_net(self, net_id):
        net = self.nets.get(int(net_id))
        if net is None:
            return True
        if net.get("is_constant"):
            return True
        drivers = self.net_drivers(net_id)
        if not drivers:
            return False
        return all(self.is_constant(gid) for gid in drivers)

    def boundary_nets(self):
        """``(inputs, outputs)`` net ids of the design boundary."""
        inputs = sorted(nid for nid, net in self.nets.items() if net.get("is_global_input"))
        outputs = sorted(nid for nid, net in self.nets.items() if net.get("is_global_output"))
        return inputs, outputs

    # -- references ------------------------------------------------------

    def gate_ref(self, gate_id):
        from . import model

        gate = self.gates.get(int(gate_id))
        if gate is None:
            raise InventoryError("no gate with id {} in the inventory".format(gate_id))
        return model.gate_ref(self.artifact_id, gate["id"], gate["name"], gate_type=gate["type"])

    def net_ref(self, net_id, role=None):
        from . import model

        net = self.nets.get(int(net_id))
        if net is None:
            raise InventoryError("no net with id {} in the inventory".format(net_id))
        return model.net_ref(self.artifact_id, net["id"], net["name"], role=role)

    def gate_type_histogram(self, gate_ids):
        histogram = {}
        for gate_id in gate_ids:
            gate = self.gates.get(int(gate_id))
            if gate is None:
                continue
            histogram[gate["type"]] = histogram.get(gate["type"], 0) + 1
        return histogram

    # -- serialization ---------------------------------------------------

    def to_json(self):
        return {
            "inventory_version": INVENTORY_VERSION,
            "artifact_id": self.artifact_id,
            "design": dict(self.design),
            "gate_types": {
                name: {
                    "properties": sorted(entry.get("properties", [])),
                    "pins": sorted(
                        entry.get("pins", []),
                        key=lambda pin: (pin.get("direction", ""), pin.get("name", "")),
                    ),
                }
                for name, entry in sorted(self.gate_types.items())
            },
            "gates": [self.gates[gid] for gid in sorted(self.gates)],
            "nets": [self.nets[nid] for nid in sorted(self.nets)],
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Inventory({} gates, {} nets)".format(len(self.gates), len(self.nets))


def from_netlist_view(view, artifact_id="netlist", path=None, sha256=None,
                      unhashed_reason=None):
    """Build an :class:`Inventory` from a :class:`hal_cdc.netlist_view.NetlistView`."""
    from . import model

    gate_types = {}
    for name, gate_type in sorted(view.gate_types().items()):
        gate_types[name] = {
            "properties": sorted(gate_type.properties),
            "pins": [
                {"name": pin.name, "direction": pin.direction, "type": pin.type}
                for pin in gate_type.pins
            ],
        }

    gates = []
    for gate in view.sorted_gates():
        entry = {
            "id": int(gate.id),
            "name": str(gate.name),
            "type": gate.type.name,
            "fan_in": {str(pin): int(net) for pin, net in gate.fan_in.items() if net is not None},
            "fan_out": {
                str(pin): int(net) for pin, net in gate.fan_out.items() if net is not None
            },
        }
        if gate.module_id is not None or gate.module_name is not None:
            entry["module"] = {"id": gate.module_id, "name": gate.module_name}
        gates.append(entry)

    nets = []
    for net in view.sorted_nets():
        nets.append(
            {
                "id": int(net.id),
                "name": str(net.name),
                "sources": [[int(gid), str(pin)] for gid, pin in net.sources],
                "destinations": [[int(gid), str(pin)] for gid, pin in net.destinations],
                "is_global_input": bool(net.is_global_input),
                "is_global_output": bool(net.is_global_output),
                "is_constant": bool(net.is_constant),
            }
        )

    source_path = path if path is not None else view.input_filename
    if source_path:
        source_path = str(source_path)
    if not sha256 and not unhashed_reason:
        if source_path and os.path.isfile(source_path):
            from hal_findings.serialize import sha256_file

            sha256 = sha256_file(source_path)
        else:
            unhashed_reason = (
                "the netlist has no readable source file (in-memory or modified netlist)"
            )

    gate_library = None
    if view.gate_library_name or view.gate_library_path:
        gate_library = {}
        if view.gate_library_name:
            gate_library["name"] = view.gate_library_name
        if view.gate_library_path:
            gate_library["path"] = str(view.gate_library_path)

    design = model.design(
        artifact_id,
        len(gates),
        len(nets),
        design_name=view.design_name or None,
        device_name=view.device_name or None,
        path=source_path or None,
        sha256=sha256,
        unhashed_reason=unhashed_reason,
        gate_library=gate_library,
    )
    return Inventory(artifact_id, design, gates, nets, gate_types)


def from_json(payload):
    """Rebuild an :class:`Inventory` from its JSON form."""
    if not isinstance(payload, dict):
        raise InventoryError("an inventory document must be a JSON object")
    version = payload.get("inventory_version")
    if version not in SUPPORTED_INVENTORY_VERSIONS:
        raise InventoryError(
            "unsupported inventory_version {!r}; this build understands {}".format(
                version, ", ".join(SUPPORTED_INVENTORY_VERSIONS)
            )
        )
    for key in ("artifact_id", "design", "gates", "nets"):
        if key not in payload:
            raise InventoryError("inventory document has no {!r}".format(key))
    return Inventory(
        payload["artifact_id"],
        payload["design"],
        payload["gates"],
        payload["nets"],
        payload.get("gate_types", {}),
    )
