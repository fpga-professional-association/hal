"""The user-supplied bus mapping: which design signal is which APB signal.

Nothing in this checker guesses. There is no name heuristic, no port-order
inference and no automatic bus recognition -- an APB interface is whatever the
mapping file says it is, and a wrong mapping yields a wrong (but internally
consistent) answer. That is a deliberate trade: focused interface checks become
possible on any design without first solving protocol recognition, at the cost
of making the mapping part of the evidence. The mapping file is therefore hashed
into the findings document as an artifact of its own.

File format (JSON)::

    {
      "mapping_version": "1.0",
      "name": "uart_regs",
      "protocol": {"family": "APB", "revision": "APB4", "dut_role": "completer"},
      "design":   {"reference_model": "completer_ok"},
      "clock":    {"signal": "PCLK", "edge": "rising"},
      "reset":    {"signal": "PRESETn", "active_low": true, "cycles": 2},
      "signals":  {"PSEL": "PSEL", "PADDR": ["PADDR_0", "PADDR_1"], ...},
      "options":  {"max_wait_states": 4, "bound": 12}
    }

A bus is a list of design signal names, **least significant bit first**. The
``design`` block either names a reference model (the shipped fixtures) or points
at a netlist plus its gate library, relative to the mapping file.
"""

import json
import os

from . import expr, spec

__all__ = [
    "MappingError",
    "SUPPORTED_MAPPING_VERSIONS",
    "DEFAULT_OPTIONS",
    "BusMapping",
    "load",
    "loads",
    "SignalContext",
]

SUPPORTED_MAPPING_VERSIONS = ("1.0",)

#: Option -> default. Anything else in ``options`` is rejected.
DEFAULT_OPTIONS = {
    "bound": 12,
    "max_wait_states": 8,
    "check_ready_low_when_deselected": False,
    "decision_limit": 2000000,
    "conflict_limit": 200000,
    "timeout_s": 120,
}

#: Without these there is no APB transfer to talk about at all.
REQUIRED_SIGNALS = ("PRESETn", "PSEL", "PENABLE")

_TOP_LEVEL_KEYS = {
    "mapping_version",
    "name",
    "description",
    "protocol",
    "design",
    "clock",
    "reset",
    "signals",
    "options",
}


class MappingError(ValueError):
    """The mapping file is missing, malformed or internally inconsistent."""

    def __init__(self, problems):
        self.problems = list(problems)
        ValueError.__init__(
            self,
            "bus mapping is invalid ({} problem{}):\n  - {}".format(
                len(self.problems),
                "" if len(self.problems) == 1 else "s",
                "\n  - ".join(self.problems),
            ),
        )


class BusMapping(object):
    """A validated mapping from APB signal names to design signal names."""

    def __init__(self, document, path=None):
        self.document = document
        self.path = path
        self.name = document.get("name") or (
            os.path.splitext(os.path.basename(path))[0] if path else "unnamed"
        )
        self.description = document.get("description")
        protocol = document["protocol"]
        self.family = protocol["family"]
        self.revision = protocol["revision"]
        self.role = protocol["dut_role"]
        self.design = dict(document.get("design") or {})
        clock = document.get("clock") or {}
        self.clock_signal = clock.get("signal")
        self.clock_edge = clock.get("edge", "rising")
        reset = document.get("reset") or {}
        self.reset_signal = reset.get("signal")
        self.reset_active_low = bool(reset.get("active_low", True))
        self.reset_cycles = int(reset.get("cycles", 2))
        self._signals = {}
        for name, value in (document.get("signals") or {}).items():
            self._signals[name] = [value] if isinstance(value, str) else list(value)
        self.options = dict(DEFAULT_OPTIONS)
        self.options.update(document.get("options") or {})

    # -- accessors ----------------------------------------------------------

    def has(self, apb_signal):
        return apb_signal in self._signals

    def bits(self, apb_signal):
        """Design signal names for ``apb_signal``, least significant bit first."""
        if apb_signal not in self._signals:
            raise KeyError("APB signal {!r} is not mapped".format(apb_signal))
        return list(self._signals[apb_signal])

    def width(self, apb_signal):
        return len(self._signals[apb_signal])

    def mapped_signals(self):
        return sorted(self._signals)

    def design_signals(self):
        """Every design signal the mapping refers to, including the reset."""
        names = set()
        for bits in self._signals.values():
            names.update(bits)
        if self.reset_signal:
            names.add(self.reset_signal)
        return sorted(names)

    def expected_direction(self, apb_signal):
        """``'input'``/``'output'`` as seen by the DUT, or ``None`` if not a data signal."""
        if apb_signal in ("PCLK", "PRESETn"):
            return "input"
        if apb_signal in spec.REQUESTER_DRIVEN:
            return "output" if self.role == "requester" else "input"
        if apb_signal in spec.COMPLETER_DRIVEN:
            return "output" if self.role == "completer" else "input"
        return None

    def option(self, key, default=None):
        return self.options.get(key, default)

    def to_dict(self):
        return json.loads(json.dumps(self.document, sort_keys=True))

    def resolve_design_path(self, key):
        """Resolve ``design[key]`` relative to the mapping file."""
        value = self.design.get(key)
        if value is None:
            return None
        if self.path and not os.path.isabs(value):
            return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(self.path)), value))
        return value


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _validate(document, path):
    problems = []

    if not isinstance(document, dict):
        raise MappingError(["a bus mapping must be a JSON object"])

    unknown = sorted(set(document) - _TOP_LEVEL_KEYS)
    if unknown:
        problems.append(
            "unknown top-level key(s) {}; a typo must not be silently ignored".format(unknown)
        )

    version = document.get("mapping_version")
    if version is None:
        problems.append("'mapping_version' is required")
    elif version not in SUPPORTED_MAPPING_VERSIONS:
        problems.append(
            "unsupported mapping_version {!r}; this build understands {}".format(
                version, ", ".join(SUPPORTED_MAPPING_VERSIONS)
            )
        )

    protocol = document.get("protocol")
    if not isinstance(protocol, dict):
        problems.append("'protocol' must be an object with 'family', 'revision' and 'dut_role'")
        protocol = {}
    family = protocol.get("family")
    if family != "APB":
        problems.append(
            "protocol.family is {!r}; this checker only covers 'APB' (AXI4-Lite is a "
            "follow-up, never an approximation)".format(family)
        )
    revision = protocol.get("revision")
    if revision not in spec.REVISIONS:
        problems.append(
            "protocol.revision is {!r}; supported revisions are {} (APB5 signalling such as "
            "PWAKEUP is explicitly out of scope)".format(revision, ", ".join(spec.REVISIONS))
        )
    role = protocol.get("dut_role")
    if role not in spec.ROLES:
        problems.append(
            "protocol.dut_role is {!r}; must be one of {}".format(role, ", ".join(spec.ROLES))
        )

    design = document.get("design")
    if not isinstance(design, dict) or not design:
        problems.append(
            "'design' must name either a 'reference_model' or a 'netlist' (plus 'gate_library')"
        )
    else:
        unknown_design = sorted(set(design) - {"reference_model", "netlist", "gate_library", "project"})
        if unknown_design:
            problems.append("unknown design key(s) {}".format(unknown_design))
        sources = [key for key in ("reference_model", "netlist", "project") if design.get(key)]
        if len(sources) != 1:
            problems.append(
                "design must name exactly one of 'reference_model', 'netlist' or 'project', "
                "found {}".format(sources or "none")
            )
        if design.get("netlist") and not design.get("gate_library"):
            problems.append("design.netlist needs a 'gate_library' to be parsed")

    reset = document.get("reset") or {}
    if not isinstance(reset, dict):
        problems.append("'reset' must be an object")
        reset = {}
    if not reset.get("signal"):
        problems.append(
            "'reset.signal' is required: every result is scoped to a known reset sequence"
        )
    cycles = reset.get("cycles", 2)
    if not isinstance(cycles, int) or cycles < 2:
        problems.append(
            "reset.cycles must be an integer >= 2 so that the state is settled *and* at least "
            "one checked cycle still has reset asserted (got {!r})".format(cycles)
        )

    clock = document.get("clock") or {}
    if isinstance(clock, dict) and clock.get("edge", "rising") not in ("rising", "falling"):
        problems.append("clock.edge must be 'rising' or 'falling'")

    signals = document.get("signals")
    if not isinstance(signals, dict) or not signals:
        problems.append("'signals' must be a non-empty object mapping APB signals to design signals")
        signals = {}

    seen_design_signals = {}
    for name, value in sorted(signals.items()):
        if name not in spec.SIGNALS:
            problems.append(
                "{!r} is not an APB signal; known signals are {}".format(
                    name, ", ".join(sorted(spec.SIGNALS))
                )
            )
            continue
        if revision in spec.REVISIONS and spec.revision_index(
            spec.SIGNALS[name]
        ) > spec.revision_index(revision):
            problems.append(
                "{} does not exist in {} (introduced in {}); remove it or select a newer "
                "revision".format(name, revision, spec.SIGNALS[name])
            )
        bits = [value] if isinstance(value, str) else value
        if not isinstance(bits, list) or not bits or not all(
            isinstance(bit, str) and bit for bit in bits
        ):
            problems.append(
                "signals.{} must be a design signal name or a non-empty list of names "
                "(LSB first)".format(name)
            )
            continue
        if name not in spec.BUS_SIGNALS and len(bits) != 1:
            problems.append("{} is a single-bit signal but {} names were given".format(name, len(bits)))
        for bit in bits:
            previous = seen_design_signals.get(bit)
            if previous is not None:
                problems.append(
                    "design signal {!r} is mapped to both {} and {}".format(bit, previous, name)
                )
            seen_design_signals[bit] = name

    for name in REQUIRED_SIGNALS:
        if name == "PRESETn":
            continue
        if name not in signals:
            problems.append("signals.{} is required".format(name))
    if reset.get("signal") and "PRESETn" in signals:
        mapped = signals["PRESETn"]
        mapped_bits = [mapped] if isinstance(mapped, str) else mapped
        if mapped_bits != [reset["signal"]]:
            problems.append(
                "signals.PRESETn ({}) and reset.signal ({!r}) disagree".format(
                    mapped_bits, reset["signal"]
                )
            )

    options = document.get("options") or {}
    if not isinstance(options, dict):
        problems.append("'options' must be an object")
        options = {}
    for key, value in sorted(options.items()):
        if key not in DEFAULT_OPTIONS:
            problems.append(
                "unknown option {!r}; supported options are {}".format(
                    key, ", ".join(sorted(DEFAULT_OPTIONS))
                )
            )
            continue
        expected = DEFAULT_OPTIONS[key]
        if isinstance(expected, bool):
            if not isinstance(value, bool):
                problems.append("option {!r} must be a boolean".format(key))
        elif not isinstance(value, int) or isinstance(value, bool) or value < 1:
            problems.append("option {!r} must be a positive integer".format(key))

    bound = options.get("bound", DEFAULT_OPTIONS["bound"])
    if isinstance(bound, int) and not isinstance(bound, bool) and isinstance(cycles, int):
        if bound <= cycles + 2:
            problems.append(
                "options.bound ({}) leaves no room after a {}-cycle reset; use a bound of at "
                "least {}".format(bound, cycles, cycles + 3)
            )

    if problems:
        raise MappingError(problems)
    return BusMapping(document, path=path)


def loads(text, path=None):
    """Parse and validate a mapping from JSON text."""
    try:
        document = json.loads(text)
    except ValueError as error:
        raise MappingError(["not valid JSON: {}".format(error)])
    return _validate(document, path)


def load(path):
    """Read and validate a mapping file."""
    with open(path, "r", encoding="utf-8") as handle:
        return loads(handle.read(), path=path)


# ---------------------------------------------------------------------------
# the bridge between a mapping and an unrolled transition system
# ---------------------------------------------------------------------------


class SignalContext(object):
    """Resolves APB signal names to Boolean terms of an :class:`~.system.Unrolling`."""

    def __init__(self, bus_mapping, unrolling):
        self.mapping = bus_mapping
        self.unrolling = unrolling
        self.bound = unrolling.bound
        self.missing = []
        system = unrolling.system
        self.unresolved = sorted(
            name for name in bus_mapping.design_signals() if not system.has_signal(name)
        )

    # -- signal access ------------------------------------------------------

    def has(self, apb_signal):
        """True when the signal is mapped *and* every bit exists in the design."""
        if not self.mapping.has(apb_signal):
            return False
        system = self.unrolling.system
        return all(system.has_signal(bit) for bit in self.mapping.bits(apb_signal))

    def bits(self, apb_signal, cycle):
        return [
            self.unrolling.signal(bit, cycle) for bit in self.mapping.bits(apb_signal)
        ]

    def bit(self, apb_signal, cycle):
        """Term for a single-bit APB signal."""
        names = self.mapping.bits(apb_signal)
        if len(names) != 1:
            raise ValueError(
                "{} is {} bits wide; use bits()/stable() instead of bit()".format(
                    apb_signal, len(names)
                )
            )
        return self.unrolling.signal(names[0], cycle)

    def stable(self, apb_signal, cycle):
        """Every bit of ``apb_signal`` is unchanged between ``cycle`` and ``cycle+1``."""
        return expr.and_(
            *[
                expr.iff(term, later)
                for term, later in zip(
                    self.bits(apb_signal, cycle), self.bits(apb_signal, cycle + 1)
                )
            ]
        )

    def reset_active(self, cycle):
        """Term that is true while the design is held in reset."""
        signal = self.mapping.reset_signal
        term = self.unrolling.signal(signal, cycle)
        return expr.not_(term) if self.mapping.reset_active_low else term

    def option(self, key, default=None):
        return self.mapping.option(key, default)

    # -- property applicability --------------------------------------------

    def unavailable(self, prop):
        """Signals ``prop`` needs that the mapping/design does not provide."""
        missing = []
        for name in prop.requires:
            if name == "PRESETn":
                if not self.mapping.reset_signal:
                    missing.append(name)
                continue
            if not self.has(name):
                missing.append(name)
        return missing
