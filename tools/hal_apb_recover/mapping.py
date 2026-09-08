"""The user-supplied APB signal mapping and the assumptions that go with it.

Automatic interface detection is explicitly *out* of the MVP, so the bus is
described by the analyst, not inferred.  That makes the mapping the single
place where an assumption can be wrong, which is why every field is validated
against the netlist up front and every assumption ends up verbatim in the
findings document: a recovered register map is only as good as this file, and
the report has to say so.

Signals are referenced by net name (HAL's expanded names -- ``pwdata(3)``) or
by ``id:<net id>``.  Vectors are given LSB first.
"""

import json

from . import MAPPING_SCHEMA, MAPPING_SCHEMA_VERSION

__all__ = ["MappingError", "ApbMapping", "load_mapping"]

_REQUIRED_SCALARS = ("psel", "penable", "pwrite")
_OPTIONAL_SCALARS = ("pready", "pslverr")
_VECTORS = ("paddr", "pwdata", "prdata", "pstrb")
_QUIESCENT = {"zero": 0, "one": 1, "unconstrained": None}


class MappingError(RuntimeError):
    """The mapping document is malformed or does not fit the netlist."""


class ApbMapping(object):
    """A validated APB mapping, resolved against one circuit."""

    def __init__(self, document, circuit):
        self.document = document
        self.circuit = circuit
        self.design = document.get("design")
        self.errors = []

        bus = document.get("apb")
        if not isinstance(bus, dict):
            raise MappingError("mapping has no 'apb' object")

        self.scalars = {}
        for name in _REQUIRED_SCALARS:
            self.scalars[name] = self._resolve_required(bus.get(name), "apb.{}".format(name))
        for name in _OPTIONAL_SCALARS:
            if bus.get(name) is not None:
                self.scalars[name] = self._resolve_required(bus[name], "apb.{}".format(name))

        self.vectors = {}
        for name in _VECTORS:
            entries = bus.get(name)
            if entries is None:
                if name == "pstrb":
                    self.vectors[name] = []
                    continue
                raise MappingError("mapping has no 'apb.{}' vector".format(name))
            if not isinstance(entries, list) or not entries:
                raise MappingError("'apb.{}' must be a non-empty list, LSB first".format(name))
            self.vectors[name] = [
                self._resolve_required(entry, "apb.{}[{}]".format(name, index))
                for index, entry in enumerate(entries)
            ]

        clock = document.get("clock") or {}
        reset = document.get("reset") or {}
        self.clock_net = self._resolve_optional(clock.get("net"), "clock.net")
        self.clock_edge = clock.get("edge", "rising")
        self.reset_net = self._resolve_optional(reset.get("net"), "reset.net")
        self.reset_active = reset.get("active", "low")
        self.reset_kind = reset.get("kind", "asynchronous")
        if self.reset_active not in ("low", "high"):
            self.errors.append("reset.active must be 'low' or 'high', got {!r}".format(self.reset_active))
        if self.clock_edge not in ("rising", "falling"):
            self.errors.append("clock.edge must be 'rising' or 'falling', got {!r}".format(self.clock_edge))

        address = document.get("address") or {}
        self.address_base = int(address.get("base", 0))
        self.address_stride = int(address.get("stride", 4))
        self.address_count = int(address.get("count", 1 << len(self.vectors["paddr"])))
        if self.address_stride < 1:
            self.errors.append("address.stride must be >= 1")
        if self.address_count < 1:
            self.errors.append("address.count must be >= 1")

        assumptions = document.get("assumptions") or {}
        self.non_apb_inputs = assumptions.get("non_apb_inputs", "zero")
        if self.non_apb_inputs not in _QUIESCENT:
            self.errors.append(
                "assumptions.non_apb_inputs must be one of {}, got {!r}".format(
                    sorted(_QUIESCENT), self.non_apb_inputs
                )
            )
        self.byte_lane_bits = int(assumptions.get("byte_lane_bits", 8))
        self.notes = list(assumptions.get("notes") or [])

        self._validate()

    # -- resolution ---------------------------------------------------------

    def _resolve_required(self, reference, where):
        if reference is None:
            raise MappingError("mapping is missing {}".format(where))
        net = self.circuit.resolve_net(reference)
        if net is None:
            self.errors.append(
                "{} references net {!r}, which does not exist in the netlist".format(
                    where, reference
                )
            )
        return net

    def _resolve_optional(self, reference, where):
        if reference is None:
            return None
        return self._resolve_required(reference, where)

    def _validate(self):
        seen = {}
        for name, net in self.scalars.items():
            self._claim(seen, net, name)
        for name, nets in self.vectors.items():
            for index, net in enumerate(nets):
                self._claim(seen, net, "{}[{}]".format(name, index))
        if self.clock_net is not None:
            self._claim(seen, self.clock_net, "clock")
        if self.reset_net is not None:
            self._claim(seen, self.reset_net, "reset")

        inputs = set(self.circuit.input_nets)
        outputs = set(self.circuit.output_nets)
        for role in ("psel", "penable", "pwrite"):
            net = self.scalars.get(role)
            if net is not None and net not in inputs:
                self.errors.append(
                    "apb.{} ({}) is not a primary input of the netlist".format(
                        role, self.circuit.net_name(net)
                    )
                )
        for role in ("paddr", "pwdata", "pstrb"):
            for index, net in enumerate(self.vectors.get(role, [])):
                if net is not None and net not in inputs:
                    self.errors.append(
                        "apb.{}[{}] ({}) is not a primary input of the netlist".format(
                            role, index, self.circuit.net_name(net)
                        )
                    )
        for index, net in enumerate(self.vectors.get("prdata", [])):
            if net is not None and net not in outputs:
                self.errors.append(
                    "apb.prdata[{}] ({}) is not a primary output of the netlist".format(
                        index, self.circuit.net_name(net)
                    )
                )

        lanes = len(self.vectors.get("pstrb") or [])
        if lanes:
            covered = lanes * self.byte_lane_bits
            if covered != self.data_width:
                self.errors.append(
                    "{} strobe lane(s) of {} bit(s) cover {} bits, but pwdata is {} bits "
                    "wide; fix apb.pstrb or assumptions.byte_lane_bits".format(
                        lanes, self.byte_lane_bits, covered, self.data_width
                    )
                )

        if self.errors:
            raise MappingError(
                "the APB mapping does not fit the netlist:\n  - "
                + "\n  - ".join(self.errors)
            )

    def _claim(self, seen, net, role):
        if net is None:
            return
        if net in seen:
            self.errors.append(
                "net {} is mapped to both {} and {}".format(
                    self.circuit.net_name(net), seen[net], role
                )
            )
        else:
            seen[net] = role

    # -- accessors ----------------------------------------------------------

    @property
    def address_width(self):
        return len(self.vectors["paddr"])

    @property
    def data_width(self):
        return len(self.vectors["pwdata"])

    @property
    def read_width(self):
        return len(self.vectors["prdata"])

    @property
    def strobe_lanes(self):
        return len(self.vectors["pstrb"])

    @property
    def quiescent_value(self):
        return _QUIESCENT[self.non_apb_inputs]

    def addresses(self):
        """The enumerated address window, as integers."""
        return [
            self.address_base + index * self.address_stride
            for index in range(self.address_count)
        ]

    def bus_nets(self):
        """Every net the mapping claims, as a set."""
        nets = set(net for net in self.scalars.values() if net is not None)
        for entries in self.vectors.values():
            nets.update(net for net in entries if net is not None)
        for net in (self.clock_net, self.reset_net):
            if net is not None:
                nets.add(net)
        return nets

    def strobe_lane_of_bit(self, bit):
        if not self.strobe_lanes:
            return None
        lane = bit // self.byte_lane_bits
        return lane if lane < self.strobe_lanes else None

    def assumption_records(self):
        """The assumptions as ``(id, kind, description)`` triples."""
        records = [
            (
                "apb/mapping",
                "user_provided",
                "the APB signal mapping is user-supplied and taken as correct: "
                + ", ".join(
                    "{}={}".format(role, self.circuit.net_name(net))
                    for role, net in sorted(self.scalars.items())
                    if net is not None
                ),
            ),
            (
                "apb/protocol",
                "environment",
                "an access is modelled as the APB access phase: PSEL=1, PENABLE=1 and "
                "PWRITE selecting the direction; no wait states are modelled and PREADY "
                "is not used to qualify the transfer",
            ),
            (
                "apb/address-window",
                "user_provided",
                "only addresses {}..{} with stride {} were enumerated ({} address(es)); "
                "behaviour outside this window was not analysed".format(
                    hex(self.address_base),
                    hex(self.address_base + (self.address_count - 1) * self.address_stride),
                    self.address_stride,
                    self.address_count,
                ),
            ),
        ]
        if self.clock_net is not None:
            records.append(
                (
                    "apb/clock",
                    "user_provided",
                    "every recovered storage bit is assumed to be clocked by {} on the "
                    "{} edge; flip-flops clocked elsewhere are reported separately".format(
                        self.circuit.net_name(self.clock_net), self.clock_edge
                    ),
                )
            )
        if self.reset_net is not None:
            records.append(
                (
                    "apb/reset",
                    "user_provided",
                    "reset values were read with {} asserted ({}-active, {})".format(
                        self.circuit.net_name(self.reset_net),
                        self.reset_active,
                        self.reset_kind,
                    ),
                )
            )
        if self.strobe_lanes:
            records.append(
                (
                    "apb/strobes",
                    "environment",
                    "write probes assert all {} byte strobe lane(s) unless a lane is "
                    "being characterised; each lane covers {} data bits".format(
                        self.strobe_lanes, self.byte_lane_bits
                    ),
                )
            )
        else:
            records.append(
                (
                    "apb/strobes",
                    "environment",
                    "the mapping declares no PSTRB, so writes are modelled as covering "
                    "the full data width",
                )
            )
        if self.quiescent_value is None:
            records.append(
                (
                    "apb/environment",
                    "environment",
                    "non-APB primary inputs were left unconstrained in every probe",
                )
            )
        else:
            records.append(
                (
                    "apb/environment",
                    "environment",
                    "non-APB primary inputs were held at {} for the bounded probes; the "
                    "unbounded probes leave them unconstrained".format(self.quiescent_value),
                )
            )
        for index, note in enumerate(self.notes):
            records.append(("apb/note/{}".format(index), "user_provided", note))
        return records

    def to_json(self):
        return {
            "schema": MAPPING_SCHEMA,
            "schema_version": MAPPING_SCHEMA_VERSION,
            "design": self.design,
            "address_width": self.address_width,
            "data_width": self.data_width,
            "strobe_lanes": self.strobe_lanes,
            "byte_lane_bits": self.byte_lane_bits,
            "address_window": {
                "base": self.address_base,
                "stride": self.address_stride,
                "count": self.address_count,
            },
            "non_apb_inputs": self.non_apb_inputs,
            "signals": {
                role: self.circuit.net_name(net)
                for role, net in sorted(self.scalars.items())
                if net is not None
            },
            "vectors": {
                role: [self.circuit.net_name(net) for net in nets]
                for role, nets in sorted(self.vectors.items())
                if nets
            },
            "clock": None
            if self.clock_net is None
            else {"net": self.circuit.net_name(self.clock_net), "edge": self.clock_edge},
            "reset": None
            if self.reset_net is None
            else {
                "net": self.circuit.net_name(self.reset_net),
                "active": self.reset_active,
                "kind": self.reset_kind,
            },
        }


def load_mapping(path, circuit):
    """Read and validate a mapping document against ``circuit``."""
    with open(path, "r", encoding="utf-8") as handle:
        try:
            document = json.load(handle)
        except ValueError as exc:
            raise MappingError("{} is not valid JSON: {}".format(path, exc))
    version = document.get("schema_version")
    if version is not None and version != MAPPING_SCHEMA_VERSION:
        raise MappingError(
            "mapping {} declares schema_version {!r}; this build reads {!r}".format(
                path, version, MAPPING_SCHEMA_VERSION
            )
        )
    return ApbMapping(document, circuit)
