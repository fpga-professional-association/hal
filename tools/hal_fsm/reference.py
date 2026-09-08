"""Ground truth: what the machine is supposed to be.

A recovered transition table is only worth something if it can be checked
against something written down independently.  A reference file records, per
machine, the state register *by flip-flop name with an explicit bit order*, the
initial state, and the full transition relation as ``(source, target)`` pairs
with the condition in human notation.

The bit order is the point.  Renaming the state bits of a design changes the
order in which any tool discovers them, and therefore changes every state
value.  Recording the reference order and permuting the recovered table into it
(:func:`hal_fsm.transitions.compare_tables`) is what makes "recover a
controller with renamed state bits and compare it to a reference" a check
rather than a coincidence.
"""

import json

from .transitions import compare_tables

__all__ = ["ReferenceError", "Machine", "Reference", "load", "from_dict"]

REFERENCE_VERSION = "1.0.0"

_MACHINE_KEYS = (
    "id",
    "description",
    "state_registers",
    "state_names",
    "initial_state",
    "reset",
    "inputs",
    "transitions",
    "reachable_states",
    "unreachable_states",
    "notes",
)


class ReferenceError(ValueError):
    """Raised when a reference file cannot be used as written."""


class Machine(object):
    """One reference machine."""

    def __init__(self, document):
        unknown = sorted(set(document) - set(_MACHINE_KEYS))
        if unknown:
            raise ReferenceError(
                "unknown key(s) in reference machine: {}".format(", ".join(unknown))
            )
        self.id = document.get("id") or "machine"
        self.description = document.get("description")
        self.state_registers = list(document.get("state_registers") or [])
        if not self.state_registers:
            raise ReferenceError(
                "reference machine {!r} has no state_registers; a transition table "
                "without its bit order cannot be compared".format(self.id)
            )
        if len(set(self.state_registers)) != len(self.state_registers):
            raise ReferenceError(
                "reference machine {!r} lists a state register twice".format(self.id)
            )
        self.state_names = {
            int(key): value for key, value in (document.get("state_names") or {}).items()
        }
        self.initial_state = int(document.get("initial_state", 0))
        self.reset = document.get("reset") or {}
        self.inputs = list(document.get("inputs") or [])
        self.transitions = []
        for entry in document.get("transitions") or []:
            if "source" not in entry or "target" not in entry:
                raise ReferenceError(
                    "a transition of reference machine {!r} lacks source/target".format(self.id)
                )
            self.transitions.append(
                {
                    "source": int(entry["source"]),
                    "target": int(entry["target"]),
                    "condition": entry.get("condition", ""),
                }
            )
        if not self.transitions:
            raise ReferenceError(
                "reference machine {!r} has no transitions".format(self.id)
            )
        declared_reachable = document.get("reachable_states")
        self.reachable_states = (
            sorted(int(state) for state in declared_reachable)
            if declared_reachable is not None
            else None
        )
        self.unreachable_states = sorted(
            int(state) for state in (document.get("unreachable_states") or [])
        )
        self.notes = list(document.get("notes") or [])

    @property
    def width(self):
        return len(self.state_registers)

    @property
    def edges(self):
        return [(entry["source"], entry["target"]) for entry in self.transitions]

    def matches_register(self, gate_names):
        """True when ``gate_names`` is the same *set* of flip-flops, any order."""
        return sorted(gate_names) == sorted(self.state_registers)

    def compare(self, table, restrict_to_reachable=None):
        """Compare a recovered :class:`~hal_fsm.transitions.TransitionTable`.

        ``restrict_to_reachable`` limits the comparison to the reference's
        reachable states, which is what an SMT run has to be compared against:
        it only explores forward from the initial state, so it cannot be
        expected to know about unreachable states.
        """
        restrict = None
        if restrict_to_reachable:
            restrict = (
                self.reachable_states
                if self.reachable_states is not None
                else sorted({edge[0] for edge in self.edges})
            )
        report = compare_tables(
            table, self.state_registers, self.edges, restrict_to=restrict
        )
        report["machine"] = self.id
        report["restricted_to_reachable"] = bool(restrict_to_reachable)
        report["initial_state_expected"] = self.initial_state
        return report

    def to_json(self):
        return {
            "id": self.id,
            "state_registers": list(self.state_registers),
            "initial_state": self.initial_state,
            "transitions": [dict(entry) for entry in self.transitions],
            "reachable_states": self.reachable_states,
            "unreachable_states": list(self.unreachable_states),
        }


class Reference(object):
    """A reference file: one or more machines of one design."""

    def __init__(self, document, path=None):
        version = document.get("reference_version")
        if version != REFERENCE_VERSION:
            raise ReferenceError(
                "reference declares reference_version {!r}; this hal_fsm understands "
                "{!r}".format(version, REFERENCE_VERSION)
            )
        self.path = path
        self.name = document.get("name")
        self.description = document.get("description")
        self.netlist = document.get("netlist")
        self.gate_library = document.get("gate_library")
        self.machines = [Machine(entry) for entry in document.get("machines") or []]
        if not self.machines:
            raise ReferenceError("a reference file must describe at least one machine")
        seen = set()
        for machine in self.machines:
            if machine.id in seen:
                raise ReferenceError("duplicate machine id {!r}".format(machine.id))
            seen.add(machine.id)

    def get(self, machine_id):
        for machine in self.machines:
            if machine.id == machine_id:
                return machine
        return None

    def for_register(self, gate_names):
        """The machine whose state register is exactly ``gate_names``, if any."""
        for machine in self.machines:
            if machine.matches_register(gate_names):
                return machine
        return None


def from_dict(document, path=None):
    return Reference(document, path=path)


def load(path):
    try:
        with open(str(path), "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError as exc:
        raise ReferenceError("could not read the reference {}: {}".format(path, exc))
    except ValueError as exc:
        raise ReferenceError("{} is not valid JSON: {}".format(path, exc))
    return Reference(document, path=str(path))
