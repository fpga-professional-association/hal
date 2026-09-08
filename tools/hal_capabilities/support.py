"""Decide whether a declared capability set is satisfied by a concrete netlist.

Nothing here imports ``hal_py``.  Every accessor is called through
:func:`hal_findings.adapters.common.call`, so this module works against real
bindings and against the stub objects the unit tests use -- the same trick the
findings adapters play, for the same reason.

The three states this module produces are the point of the whole exercise:

``supported``
    every declared requirement is met by this netlist;
``partial``
    the analysis can run, but some gate types it matched are missing something
    it needs (a clock pin, say), so its answer will have holes -- and those
    holes are named;
``unsupported``
    a requirement is not met at all; running the analysis would produce an
    empty result that reads like "nothing found" rather than "cannot look".

Reporting ``partial`` and ``unsupported`` as *reasons* rather than as an empty
result is what the acceptance criterion "unsupported gate types produce
actionable errors" actually asks for.
"""

from hal_findings.adapters import common

__all__ = [
    "SUPPORTED",
    "PARTIAL",
    "UNSUPPORTED",
    "SupportReport",
    "netlist_profile",
    "evaluate",
    "worst",
]

SUPPORTED = "supported"
PARTIAL = "partial"
UNSUPPORTED = "unsupported"

_RANK = {SUPPORTED: 0, PARTIAL: 1, UNSUPPORTED: 2}


class SupportReport(object):
    """The verdict for one plugin against one netlist, plus why."""

    def __init__(self, plugin, state, reasons=None, unsupported_gate_types=None, matched_gate_types=None):
        self.plugin = plugin
        self.state = state
        self.reasons = list(reasons or [])
        self.unsupported_gate_types = list(unsupported_gate_types or [])
        self.matched_gate_types = list(matched_gate_types or [])

    @property
    def ok(self):
        """``True`` unless the analysis cannot run at all."""
        return self.state != UNSUPPORTED

    def to_json(self):
        return {
            "plugin": self.plugin,
            "state": self.state,
            "reasons": list(self.reasons),
            "unsupported_gate_types": [dict(entry) for entry in self.unsupported_gate_types],
            "matched_gate_types": list(self.matched_gate_types),
        }

    def __repr__(self):
        return "SupportReport({!r}, {!r}, {} reason(s))".format(
            self.plugin, self.state, len(self.reasons)
        )


def _pin_type_names(gate_type):
    """Sorted PinType names exposed by ``gate_type`` (empty if unavailable)."""
    pins = common.call(gate_type, "get_pins", default=None)
    if pins is None:
        return []
    names = set()
    for pin in pins:
        pin_type = common.call(pin, "get_type")
        if pin_type is None:
            continue
        name = getattr(pin_type, "name", None)
        if not isinstance(name, str):
            name = str(pin_type).rsplit(".", 1)[-1]
        names.add(name)
    return sorted(names)


def netlist_profile(netlist):
    """Summarize a netlist as ``{gate_count, gate_library, gate_types}``.

    ``gate_types`` maps a type name to ``{"count", "properties", "pin_types"}``.
    Built once and reused for every plugin, because walking 400k gates per
    plugin would make the discovery command useless on a real design.
    """
    gates = common.call(netlist, "get_gates", default=[]) or []
    gate_types = {}
    for gate in gates:
        gate_type = common.call(gate, "get_type")
        if gate_type is None:
            continue
        name = common.call(gate_type, "get_name", default="<unnamed>")
        entry = gate_types.get(name)
        if entry is None:
            entry = {
                "count": 0,
                "properties": common.gate_type_properties(gate_type),
                "pin_types": _pin_type_names(gate_type),
            }
            gate_types[name] = entry
        entry["count"] += 1

    library = common.call(netlist, "get_gate_library")
    library_name = common.call(library, "get_name") if library is not None else None

    return {
        "gate_count": len(gates),
        "gate_library": library_name,
        "gate_types": gate_types,
    }


def _types_with_property(profile, property_name):
    return sorted(
        name
        for name, entry in profile["gate_types"].items()
        if property_name in entry["properties"]
    )


def _check_gate_type_properties(requires, profile, reasons):
    """Returns the gate type names the analysis matched, or ``None`` if none can be."""
    declared = requires.get("gate_type_properties") or {}
    all_of = list(declared.get("all_of") or [])
    any_of = list(declared.get("any_of") or [])

    matched = set()
    blocked = False

    for property_name in all_of:
        types = _types_with_property(profile, property_name)
        if not types:
            blocked = True
            reasons.append(
                "no gate in this netlist has the '{}' property, which the analysis "
                "requires. Gate library {!r} contributes the types {}; either this "
                "design has none of them or the library does not declare the "
                "property.".format(
                    property_name,
                    profile["gate_library"] or "<unknown>",
                    ", ".join(sorted(profile["gate_types"])) or "<none>",
                )
            )
        else:
            matched.update(types)

    if any_of:
        found = set()
        for property_name in any_of:
            found.update(_types_with_property(profile, property_name))
        if not found:
            blocked = True
            reasons.append(
                "no gate in this netlist has any of the properties {}, at least one "
                "of which the analysis requires.".format(", ".join(sorted(any_of)))
            )
        else:
            matched.update(found)

    if blocked:
        return None
    return sorted(matched)


def _check_pin_types(requires, profile, matched, reasons, unsupported_gate_types):
    required_pin_types = list(requires.get("gate_type_pin_types") or [])
    if not required_pin_types or not matched:
        return matched

    usable = []
    for name in matched:
        entry = profile["gate_types"][name]
        missing = [pin for pin in required_pin_types if pin not in entry["pin_types"]]
        if missing:
            unsupported_gate_types.append(
                {
                    "gate_type": name,
                    "count": entry["count"],
                    "missing_pin_types": sorted(missing),
                    "properties": list(entry["properties"]),
                    "reason": (
                        "gate type {!r} ({} gate(s)) matches the analysis but exposes no "
                        "pin of type {}; the analysis cannot resolve those gates and will "
                        "report them as unsupported rather than omit them".format(
                            name, entry["count"], "/".join(sorted(missing))
                        )
                    ),
                }
            )
        else:
            usable.append(name)

    if not usable:
        reasons.append(
            "every matching gate type ({}) is missing a pin of type {}; there is "
            "nothing this analysis can resolve in this netlist".format(
                ", ".join(matched), "/".join(sorted(required_pin_types))
            )
        )
    return usable


def _check_gate_library(document, profile, reasons):
    libraries = document.get("supported_gate_libraries") or {}
    mode = libraries.get("mode", "any")
    names = set(libraries.get("names") or [])
    library = profile["gate_library"]

    if mode == "any" or library is None:
        return True
    if mode == "allow_list" and library not in names:
        reasons.append(
            "gate library {!r} is not in this plugin's allow list ({}). {}".format(
                library,
                ", ".join(sorted(names)),
                libraries.get("note")
                or "The plugin declares it has only been exercised against those libraries.",
            )
        )
        return False
    if mode == "deny_list" and library in names:
        reasons.append(
            "gate library {!r} is on this plugin's deny list. {}".format(
                library,
                libraries.get("note") or "The plugin declares it does not handle it.",
            )
        )
        return False
    return True


def _check_netlist_shape(requires, profile, reasons):
    declared = requires.get("netlist") or {}
    ok = True
    min_gates = declared.get("min_gates")
    if min_gates is not None and profile["gate_count"] < min_gates:
        ok = False
        reasons.append(
            "netlist has {} gate(s), the analysis needs at least {}".format(
                profile["gate_count"], min_gates
            )
        )
    if declared.get("needs_gate_library") and not profile["gate_library"]:
        ok = False
        reasons.append(
            "the analysis needs a gate library, but the netlist reports none; load "
            "the design with an explicit library (see tools/hal_viz --gate-library)"
        )
    return ok


def evaluate(document, profile):
    """Verdict for one capability declaration against one :func:`netlist_profile`."""
    plugin = (document.get("plugin") or {}).get("name", "<unnamed>")
    requires = document.get("requires") or {}

    reasons = []
    unsupported_gate_types = []
    state = SUPPORTED

    if not _check_gate_library(document, profile, reasons):
        state = UNSUPPORTED
    if not _check_netlist_shape(requires, profile, reasons):
        state = UNSUPPORTED

    matched = _check_gate_type_properties(requires, profile, reasons)
    if matched is None:
        state = UNSUPPORTED
        matched = []
    else:
        before = len(unsupported_gate_types)
        usable = _check_pin_types(requires, profile, matched, reasons, unsupported_gate_types)
        if not usable and matched:
            state = UNSUPPORTED
        elif len(unsupported_gate_types) > before and state != UNSUPPORTED:
            state = PARTIAL
        matched = usable

    return SupportReport(
        plugin,
        state,
        reasons=reasons,
        unsupported_gate_types=unsupported_gate_types,
        matched_gate_types=matched,
    )


def worst(states):
    """The most severe of ``states`` (``supported`` < ``partial`` < ``unsupported``)."""
    return max(states, key=lambda state: _RANK.get(state, 0)) if states else SUPPORTED
