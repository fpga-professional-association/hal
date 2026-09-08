"""The policy: what is sensitive, what locks it, and who may write it.

Nothing about a security property is inferable from a synthesized netlist. Which
flip-flops hold a key, which bit is the lockdown bit, and which pins an attacker
can actually drive are all *statements by the analyst*, and the whole analysis is
scoped to them. They therefore live in one explicit, validated document rather
than being guessed from net names -- and the document is hashed into every
findings report as an artifact of its own, exactly as ``hal_apb_check`` does with
its bus mapping.

The MVP is deliberately **single-clock**: one declared clock, one active edge,
one step of the model per edge. A policy that names a second clock domain is
refused rather than analysed under an abstraction that does not hold for it.

Document layout (``fpgapa.security-policy`` 1.0.0)::

    {
      "schema": "fpgapa.security-policy",
      "schema_version": "1.0.0",
      "name": "secreg_ok",
      "design": {"netlist": "secreg_ok.v", "gate_library": "...hgl"},
      "clock":  {"signal": "io_00", "edge": "rising"},
      "reset":  {"signal": "io_01", "active_low": true, "cycles": 2},
      "lock":   {"signal": "n048", "locked_value": 1},
      "external_write_controls": {
        "signals":  {"psel": "io_02", "paddr": ["io_05", "io_06", "io_07"]},
        "accesses": [{"id": "apb_write", "condition": {"psel": 1, ...}}]
      },
      "observation_points": [{"name": "PRDATA[0]", "signal": "io_12"}],
      "sensitive_registers": [
        {"name": "SECRET",
         "bits": [{"name": "SECRET[0]", "signal": "n055", "reset_value": 0}],
         "properties": ["locked_write", "reset_clears"],
         "write_accesses": ["apb_write", "debug_write"]}
      ],
      "environment": {"quiescent_inputs": {"io_20": 0}},
      "options": {"bound": 10}
    }

Buses are **least significant bit first**, like every other mapping file in this
repository. Signals are transition-system signal names, which are net names: the
same strings ``hal_apb_recover`` reports when it recovers the register map, so
the two tools compose without a translation step.
"""

import json
import os

from . import POLICY_SCHEMA, POLICY_SCHEMA_VERSION
from .errors import PolicyError

__all__ = [
    "Policy",
    "SensitiveRegister",
    "RegisterBit",
    "Access",
    "PROPERTY_KINDS",
    "OPTION_DEFAULTS",
    "load",
    "loads",
]

#: Per-register obligations a policy may request.
PROPERTY_KINDS = ("locked_write", "reset_clears")

#: Every option, with its default. An option that is not listed here is a typo,
#: and a typo must never silently disable a check.
OPTION_DEFAULTS = {
    "bound": 10,
    "decision_limit": 400000,
    "conflict_limit": 200000,
    "timeout_s": 120,
}

_TOP_LEVEL_KEYS = {
    "schema",
    "schema_version",
    "name",
    "description",
    "design",
    "clock",
    "reset",
    "lock",
    "external_write_controls",
    "observation_points",
    "sensitive_registers",
    "environment",
    "options",
    "notes",
}


def _require(document, key, where):
    if key not in document:
        raise PolicyError("{} is missing the required key {!r}".format(where, key))
    return document[key]


def _reject_unknown(document, allowed, where):
    unknown = sorted(set(document) - set(allowed))
    if unknown:
        raise PolicyError(
            "{} has unknown key(s) {}; allowed: {}".format(
                where, ", ".join(unknown), ", ".join(sorted(allowed))
            )
        )


def _as_bit(value, where):
    if value in (0, 1, True, False):
        return 1 if value in (1, True) else 0
    raise PolicyError("{} must be 0 or 1, got {!r}".format(where, value))


def _as_signal_list(value, where):
    """Accept a scalar signal name or a list of them; always return a list."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)) and value and all(
        isinstance(entry, str) for entry in value
    ):
        return list(value)
    raise PolicyError(
        "{} must be a signal name or a non-empty list of signal names, got {!r}".format(
            where, value
        )
    )


class RegisterBit(object):
    """One storage bit of a sensitive register."""

    __slots__ = ("name", "signal", "reset_value", "index")

    def __init__(self, name, signal, reset_value, index):
        self.name = name
        self.signal = signal
        #: ``0``, ``1`` or ``None`` when the policy does not claim one. ``None``
        #: makes the reset-clearing obligation *unsupported* for this bit rather
        #: than silently assuming zero.
        self.reset_value = reset_value
        self.index = index


class SensitiveRegister(object):
    """A register the policy protects, and the obligations it carries."""

    def __init__(self, name, bits, properties, write_accesses, description=None):
        self.name = name
        self.bits = list(bits)
        self.properties = tuple(properties)
        self.write_accesses = tuple(write_accesses)
        self.description = description

    @property
    def signals(self):
        return [bit.signal for bit in self.bits]

    def wants(self, kind):
        return kind in self.properties

    def bits_with_reset_value(self):
        return [bit for bit in self.bits if bit.reset_value is not None]

    def bits_without_reset_value(self):
        return [bit for bit in self.bits if bit.reset_value is None]


class Access(object):
    """One externally driveable access, as a condition over interface signals.

    An access is *not* a protocol model. It is the analyst saying "this signal
    combination is what an attacker asserts when they try to write". It is used
    for two things and nothing else: deciding whether the check was actually
    exercised (see :meth:`Policy.exercise_signals`), and decoding a witness back
    into a readable transaction sequence.
    """

    def __init__(self, access_id, condition, description=None):
        self.id = access_id
        #: ``{signal_name: 0|1}`` -- already expanded from vectors.
        self.condition = dict(condition)
        self.description = description


class Policy(object):
    """A validated policy document."""

    def __init__(self, document, path=None):
        self.document = document
        self.path = path
        self._parse()

    # -- parsing ------------------------------------------------------------

    def _parse(self):
        document = self.document
        if not isinstance(document, dict):
            raise PolicyError("a policy document must be a JSON object")
        _reject_unknown(document, _TOP_LEVEL_KEYS, "the policy document")

        schema = _require(document, "schema", "the policy document")
        if schema != POLICY_SCHEMA:
            raise PolicyError(
                "schema is {!r}; this build reads {!r}".format(schema, POLICY_SCHEMA)
            )
        version = _require(document, "schema_version", "the policy document")
        if version != POLICY_SCHEMA_VERSION:
            raise PolicyError(
                "schema_version is {!r}; this build reads {!r}".format(
                    version, POLICY_SCHEMA_VERSION
                )
            )

        self.name = _require(document, "name", "the policy document")
        self.description = document.get("description")
        self.notes = list(document.get("notes") or [])

        self._parse_design(document)
        self._parse_clock(document)
        self._parse_reset(document)
        self._parse_lock(document)
        self._parse_interface(document)
        self._parse_observation_points(document)
        self._parse_registers(document)
        self._parse_environment(document)
        self._parse_options(document)
        self._check_disjoint_roles()

    def _parse_design(self, document):
        design = _require(document, "design", "the policy document")
        _reject_unknown(design, {"netlist", "gate_library", "project"}, "'design'")
        named = [key for key in ("netlist", "project") if design.get(key)]
        if len(named) != 1:
            raise PolicyError(
                "'design' must name exactly one of 'netlist' or 'project', got {}".format(
                    ", ".join(named) or "neither"
                )
            )
        if design.get("netlist") and not design.get("gate_library"):
            raise PolicyError(
                "'design.netlist' is a structural netlist and cannot be read without "
                "'design.gate_library'"
            )
        self.design = dict(design)

    def _parse_clock(self, document):
        clock = _require(document, "clock", "the policy document")
        _reject_unknown(clock, {"signal", "edge", "description"}, "'clock'")
        signal = _require(clock, "signal", "'clock'")
        if not isinstance(signal, str):
            raise PolicyError(
                "'clock.signal' must be one signal name. This MVP models a single clock "
                "domain: one step of the model is one active edge of that clock, and a "
                "second domain would need a different abstraction, not a longer list."
            )
        edge = clock.get("edge", "rising")
        if edge not in ("rising", "falling"):
            raise PolicyError("'clock.edge' must be 'rising' or 'falling', got {!r}".format(edge))
        self.clock_signal = signal
        self.clock_edge = edge
        self.clock_description = clock.get("description")

    def _parse_reset(self, document):
        reset = _require(document, "reset", "the policy document")
        _reject_unknown(reset, {"signal", "active_low", "cycles", "description"}, "'reset'")
        self.reset_signal = _require(reset, "signal", "'reset'")
        if not isinstance(self.reset_signal, str):
            raise PolicyError("'reset.signal' must be a single signal name")
        self.reset_active_low = bool(reset.get("active_low", True))
        self.reset_cycles = int(reset.get("cycles", 2))
        if self.reset_cycles < 2:
            raise PolicyError(
                "'reset.cycles' must be at least 2 so that the state is settled *and* at "
                "least one checked cycle still has reset asserted; got {}".format(
                    self.reset_cycles
                )
            )
        self.reset_description = reset.get("description")

    def _parse_lock(self, document):
        lock = document.get("lock")
        if lock is None:
            self.lock_signal = None
            self.lock_locked_value = None
            self.lock_description = None
            return
        _reject_unknown(lock, {"signal", "locked_value", "description"}, "'lock'")
        self.lock_signal = _require(lock, "signal", "'lock'")
        if not isinstance(self.lock_signal, str):
            raise PolicyError("'lock.signal' must be a single signal name")
        self.lock_locked_value = _as_bit(lock.get("locked_value", 1), "'lock.locked_value'")
        self.lock_description = lock.get("description")

    def _parse_interface(self, document):
        interface = document.get("external_write_controls") or {}
        _reject_unknown(
            interface, {"signals", "accesses", "description"}, "'external_write_controls'"
        )
        self.interface_description = interface.get("description")
        raw_signals = interface.get("signals") or {}
        if not isinstance(raw_signals, dict):
            raise PolicyError("'external_write_controls.signals' must be an object")
        self.interface_signals = {}
        for role, value in raw_signals.items():
            self.interface_signals[role] = _as_signal_list(
                value, "'external_write_controls.signals.{}'".format(role)
            )

        self.accesses = []
        seen = set()
        for index, entry in enumerate(interface.get("accesses") or []):
            where = "'external_write_controls.accesses[{}]'".format(index)
            _reject_unknown(entry, {"id", "condition", "description"}, where)
            access_id = _require(entry, "id", where)
            if access_id in seen:
                raise PolicyError("two accesses share the id {!r}".format(access_id))
            seen.add(access_id)
            condition = {}
            for role, value in (_require(entry, "condition", where) or {}).items():
                signals = self.interface_signals.get(role)
                if signals is None:
                    raise PolicyError(
                        "{} constrains {!r}, which is not declared in "
                        "external_write_controls.signals".format(where, role)
                    )
                if isinstance(value, (list, tuple)):
                    if len(value) != len(signals):
                        raise PolicyError(
                            "{} gives {} bit(s) for {!r}, which is {} bit(s) wide".format(
                                where, len(value), role, len(signals)
                            )
                        )
                    for signal, bit in zip(signals, value):
                        condition[signal] = _as_bit(bit, "{}.condition.{}".format(where, role))
                else:
                    if len(signals) != 1:
                        raise PolicyError(
                            "{} gives one bit for {!r}, which is {} bit(s) wide; pass a "
                            "list, least significant bit first".format(
                                where, role, len(signals)
                            )
                        )
                    condition[signals[0]] = _as_bit(
                        value, "{}.condition.{}".format(where, role)
                    )
            if not condition:
                raise PolicyError(
                    "{} has an empty condition; an access that constrains nothing is "
                    "satisfied by every cycle and would make the coverage check "
                    "meaningless".format(where)
                )
            self.accesses.append(Access(access_id, condition, entry.get("description")))
        self.access_by_id = {access.id: access for access in self.accesses}

    def _parse_observation_points(self, document):
        self.observation_points = []
        for index, entry in enumerate(document.get("observation_points") or []):
            where = "'observation_points[{}]'".format(index)
            if isinstance(entry, str):
                self.observation_points.append({"name": entry, "signal": entry})
                continue
            _reject_unknown(entry, {"name", "signal", "description"}, where)
            signal = _require(entry, "signal", where)
            self.observation_points.append(
                {
                    "name": entry.get("name", signal),
                    "signal": signal,
                    "description": entry.get("description"),
                }
            )

    def _parse_registers(self, document):
        entries = _require(document, "sensitive_registers", "the policy document")
        if not entries:
            raise PolicyError(
                "'sensitive_registers' is empty; there is nothing for this analysis to "
                "protect, and an empty run must not be reported as a clean one"
            )
        self.registers = []
        seen_names = set()
        seen_signals = {}
        for index, entry in enumerate(entries):
            where = "'sensitive_registers[{}]'".format(index)
            _reject_unknown(
                entry,
                {"name", "description", "bits", "properties", "write_accesses"},
                where,
            )
            name = _require(entry, "name", where)
            if name in seen_names:
                raise PolicyError("two sensitive registers are called {!r}".format(name))
            seen_names.add(name)

            bits = []
            raw_bits = _require(entry, "bits", where)
            if not raw_bits:
                raise PolicyError("{} declares no bits".format(where))
            for bit_index, raw in enumerate(raw_bits):
                bit_where = "{}.bits[{}]".format(where, bit_index)
                if isinstance(raw, str):
                    raw = {"signal": raw}
                _reject_unknown(raw, {"name", "signal", "reset_value"}, bit_where)
                signal = _require(raw, "signal", bit_where)
                if signal in seen_signals:
                    raise PolicyError(
                        "signal {!r} is listed as a bit of both {!r} and {!r}".format(
                            signal, seen_signals[signal], name
                        )
                    )
                seen_signals[signal] = name
                reset_value = raw.get("reset_value")
                if reset_value is not None:
                    reset_value = _as_bit(reset_value, "{}.reset_value".format(bit_where))
                bits.append(
                    RegisterBit(
                        raw.get("name", "{}[{}]".format(name, bit_index)),
                        signal,
                        reset_value,
                        bit_index,
                    )
                )

            properties = list(entry.get("properties") or PROPERTY_KINDS)
            unknown = sorted(set(properties) - set(PROPERTY_KINDS))
            if unknown:
                raise PolicyError(
                    "{} asks for unknown propert(ies) {}; this build implements {}".format(
                        where, ", ".join(unknown), ", ".join(PROPERTY_KINDS)
                    )
                )
            write_accesses = list(entry.get("write_accesses") or [])
            missing = [key for key in write_accesses if key not in self.access_by_id]
            if missing:
                raise PolicyError(
                    "{} names write access(es) {} that are not declared in "
                    "external_write_controls.accesses".format(where, ", ".join(missing))
                )
            if "locked_write" in properties and not write_accesses:
                raise PolicyError(
                    "{} asks for the locked_write obligation but declares no "
                    "write_accesses. Without them the tool cannot tell whether a clean "
                    "result means 'no write got through' or 'no write was ever "
                    "attempted', and a proof that was never exercised is worthless.".format(
                        where
                    )
                )
            self.registers.append(
                SensitiveRegister(
                    name, bits, properties, write_accesses, entry.get("description")
                )
            )

    def _parse_environment(self, document):
        environment = document.get("environment") or {}
        _reject_unknown(environment, {"quiescent_inputs", "notes"}, "'environment'")
        self.quiescent_inputs = {}
        for signal, value in (environment.get("quiescent_inputs") or {}).items():
            self.quiescent_inputs[signal] = _as_bit(
                value, "'environment.quiescent_inputs.{}'".format(signal)
            )
        self.environment_notes = list(environment.get("notes") or [])

    def _parse_options(self, document):
        options = document.get("options") or {}
        _reject_unknown(options, OPTION_DEFAULTS, "'options'")
        self.options = dict(OPTION_DEFAULTS)
        for key, value in options.items():
            self.options[key] = value
        if int(self.options["bound"]) < 1:
            raise PolicyError(
                "'options.bound' must be at least 1 cycle, got {}".format(
                    self.options["bound"]
                )
            )

    def _check_disjoint_roles(self):
        """A signal must not be two things at once."""
        roles = {}

        def claim(signal, role):
            previous = roles.get(signal)
            if previous is not None and previous != role:
                raise PolicyError(
                    "signal {!r} is declared both as {} and as {}; a policy whose roles "
                    "overlap cannot be checked".format(signal, previous, role)
                )
            roles[signal] = role

        claim(self.clock_signal, "the clock")
        claim(self.reset_signal, "the reset")
        if self.lock_signal:
            claim(self.lock_signal, "the lock state")
        for role, signals in self.interface_signals.items():
            for signal in signals:
                claim(signal, "external write control {!r}".format(role))
        for register in self.registers:
            for bit in register.bits:
                claim(bit.signal, "a bit of sensitive register {!r}".format(register.name))
        self.signal_roles = roles

    # -- queries ------------------------------------------------------------

    def option(self, key, default=None):
        return self.options.get(key, default)

    @property
    def bound(self):
        return int(self.options["bound"])

    def design_signals(self):
        """Every design signal the policy names, sorted."""
        names = {self.reset_signal}
        if self.lock_signal:
            names.add(self.lock_signal)
        for signals in self.interface_signals.values():
            names.update(signals)
        for point in self.observation_points:
            names.add(point["signal"])
        for register in self.registers:
            names.update(register.signals)
        names.update(self.quiescent_inputs)
        return sorted(names)

    def exercise_signals(self, register):
        """Signals whose values decide whether ``register``'s check was exercised."""
        names = set()
        for access_id in register.write_accesses:
            names.update(self.access_by_id[access_id].condition)
        return sorted(names)

    def register_by_name(self, name):
        for register in self.registers:
            if register.name == name:
                return register
        return None

    def resolve_path(self, key):
        """Resolve ``design[key]`` relative to the policy file."""
        value = self.design.get(key)
        if not value:
            return None
        if os.path.isabs(value) or not self.path:
            return value
        return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(self.path)), value))

    def to_dict(self):
        """The document as read, for embedding in a witness bundle."""
        return json.loads(json.dumps(self.document))

    def summary(self):
        return {
            "name": self.name,
            "clock": self.clock_signal,
            "clock_edge": self.clock_edge,
            "reset": self.reset_signal,
            "reset_active_low": self.reset_active_low,
            "reset_cycles": self.reset_cycles,
            "lock": self.lock_signal,
            "locked_value": self.lock_locked_value,
            "sensitive_registers": [
                {
                    "name": register.name,
                    "bits": len(register.bits),
                    "properties": list(register.properties),
                    "write_accesses": list(register.write_accesses),
                }
                for register in self.registers
            ],
            "accesses": [access.id for access in self.accesses],
            "observation_points": [point["signal"] for point in self.observation_points],
            "options": dict(self.options),
        }


def loads(text, path=None):
    try:
        document = json.loads(text)
    except ValueError as error:
        raise PolicyError("{} is not valid JSON: {}".format(path or "the policy", error))
    return Policy(document, path=path)


def load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return loads(handle.read(), path=path)
