"""The explicit correspondence between two builds, and what follows from it.

``z3_utils::compare_netlists`` matches sequential gates *implicitly*, by name,
and reports a name that has no counterpart as inequivalence.  That is the one
behaviour of the existing checker most likely to be misread: a build where the
synthesizer renamed a register comes back "not equivalent" and looks like a
behavioural regression.

This package therefore refuses to guess.  The correspondence is a file, it is
validated before anything is loaded, and every observation point is derived
from it.  A name that does not appear in the mapping is a *gap in the mapping*,
reported as ``unsupported``, and never folded into an equivalence verdict.

Mapping file (JSON)::

    {
      "version": "1.0",
      "description": "base build vs. demorgan'd rewrite",
      "netlist_a": {"label": "base",    "path": "fixtures/decoder_base.v"},
      "netlist_b": {"label": "rewrite", "path": "fixtures/decoder_rewrite.v"},
      "top_inputs":  "by_name",
      "top_outputs": "by_name",
      "sequential_gates": {"sel_reg_0": "sel_reg_0", ...},
      "ignore_outputs": ["SCAN_OUT"],
      "notes": ["..."]
    }

``"by_name"`` is the identity mapping over whatever both netlists have; it is a
shorthand, not a different model, and the document records which entries it
expanded to.  An explicit dict may rename: this package renames the *variable*
of a renamed boundary point, which the C++ implementation cannot do (it carries
a TODO for exactly that).  Renaming disables the ``z3_utils`` cross-check,
because the plugin would compare two functions over disjoint variable sets and
report a difference that is not one -- :attr:`Correspondence.is_identity` is
what callers gate on.
"""

import json
import os

from hal_findings.adapters.common import call

from .cones import is_sequential

__all__ = [
    "CorrespondenceError",
    "BY_NAME",
    "Correspondence",
    "load",
    "identity",
    "Problem",
    "ObservationPoint",
    "sequential_gates_by_name",
    "build_observation_points",
]

#: Value accepted in place of a dict: the identity mapping over shared names.
BY_NAME = "by_name"

_KNOWN_KEYS = {
    "version",
    "description",
    "netlist_a",
    "netlist_b",
    "top_inputs",
    "top_outputs",
    "sequential_gates",
    "ignore_outputs",
    "notes",
}
_SUPPORTED_VERSIONS = ("1.0",)


class CorrespondenceError(ValueError):
    """The mapping file is malformed, ambiguous or of an unsupported version."""


class Problem(object):
    """A gap in the correspondence, or a precondition the pair does not meet.

    Every problem becomes an ``unsupported`` finding.  None of them may ever be
    silently dropped: an unmapped register is the difference between "these
    builds agree" and "these builds agree about the part I looked at".
    """

    def __init__(self, kind, identifier, message, detail=None, gates_a=None, gates_b=None):
        self.kind = kind
        self.identifier = identifier
        self.message = message
        self.detail = dict(detail or {})
        self.gates_a = list(gates_a or [])
        self.gates_b = list(gates_b or [])

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Problem({!r}, {!r})".format(self.kind, self.identifier)


class ObservationPoint(object):
    """One pair of nets whose functions are compared.

    ``kind`` is ``"top_output"`` (a shared top-module output pin) or
    ``"sequential_input"`` (an input pin of a matched sequential gate).  Those
    are exactly the points ``compare_netlists`` covers; the coverage statement
    in the report is derived from this list, not asserted by hand.
    """

    def __init__(self, key, kind, label, net_a, net_b, detail=None):
        self.key = key
        self.kind = kind
        self.label = label
        self.net_a = net_a
        self.net_b = net_b
        self.detail = dict(detail or {})

    def __repr__(self):  # pragma: no cover - debugging aid
        return "ObservationPoint({!r})".format(self.key)


def _check_mapping(value, field):
    if value == BY_NAME:
        return BY_NAME
    if not isinstance(value, dict):
        raise CorrespondenceError(
            "{!r} must be an object or the string {!r}, got {}".format(
                field, BY_NAME, type(value).__name__
            )
        )
    seen = {}
    for key, mapped in sorted(value.items()):
        if not isinstance(key, str) or not isinstance(mapped, str) or not key or not mapped:
            raise CorrespondenceError(
                "{}: every entry must map a non-empty name to a non-empty name "
                "({!r} -> {!r})".format(field, key, mapped)
            )
        if mapped in seen:
            raise CorrespondenceError(
                "{}: {!r} and {!r} both map to {!r}; a correspondence must be "
                "injective or the comparison is meaningless".format(
                    field, seen[mapped], key, mapped
                )
            )
        seen[mapped] = key
    return dict(value)


class Correspondence(object):
    """A validated mapping between two builds."""

    def __init__(
        self,
        top_inputs=BY_NAME,
        top_outputs=BY_NAME,
        sequential_gates=BY_NAME,
        ignore_outputs=(),
        description=None,
        path=None,
        labels=("netlist_a", "netlist_b"),
        notes=(),
    ):
        self.top_inputs = _check_mapping(top_inputs, "top_inputs")
        self.top_outputs = _check_mapping(top_outputs, "top_outputs")
        self.sequential_gates = _check_mapping(sequential_gates, "sequential_gates")
        self.ignore_outputs = sorted(set(ignore_outputs or ()))
        self.description = description
        self.path = path
        self.labels = tuple(labels)
        self.notes = list(notes or ())

    @property
    def is_identity(self):
        """True when no entry renames anything.

        Only then does the ``z3_utils`` cross-check compare the same thing this
        package does.
        """
        for mapping in (self.top_inputs, self.top_outputs, self.sequential_gates):
            if mapping == BY_NAME:
                continue
            for key, mapped in mapping.items():
                if key != mapped:
                    return False
        return True

    def renamings(self):
        """The entries that actually rename something, as a flat list."""
        renamed = []
        for field, mapping in (
            ("top_inputs", self.top_inputs),
            ("top_outputs", self.top_outputs),
            ("sequential_gates", self.sequential_gates),
        ):
            if mapping == BY_NAME:
                continue
            for key, mapped in sorted(mapping.items()):
                if key != mapped:
                    renamed.append({"field": field, "a": key, "b": mapped})
        return renamed

    # -- boundary variable renaming -------------------------------------------------

    def boundary_substitutions(self):
        """``{name in A -> name in B}`` for the boundary variables it renames.

        A sequential gate ``r`` renamed to ``s`` turns every ``r_<pin>`` into
        ``s_<pin>``; a top input pin ``P`` renamed to ``Q`` turns
        ``GLOBAL_IN_P`` into ``GLOBAL_IN_Q``.  Both follow the naming scheme in
        :mod:`hal_semantic_diff.cones`, which follows ``z3_utils``.
        """
        substitutions = {}
        if isinstance(self.top_inputs, dict):
            for key, mapped in self.top_inputs.items():
                if key != mapped:
                    substitutions["GLOBAL_IN_" + key] = "GLOBAL_IN_" + mapped
        return substitutions

    def rename_boundary(self, name):
        """Translate one boundary variable name of netlist A into netlist B's."""
        direct = self.boundary_substitutions().get(name)
        if direct is not None:
            return direct
        if isinstance(self.sequential_gates, dict):
            for key, mapped in self.sequential_gates.items():
                if key == mapped:
                    continue
                prefix = key + "_"
                if name.startswith(prefix):
                    return mapped + "_" + name[len(prefix):]
        return name

    def as_dict(self):
        return {
            "version": "1.0",
            "description": self.description,
            "labels": list(self.labels),
            "top_inputs": self.top_inputs,
            "top_outputs": self.top_outputs,
            "sequential_gates": self.sequential_gates,
            "ignore_outputs": self.ignore_outputs,
            "identity": self.is_identity,
            "renamings": self.renamings(),
            "path": self.path,
        }


def identity(**kwargs):
    """A correspondence that maps every shared name to itself."""
    return Correspondence(**kwargs)


def load(path):
    """Read and validate a correspondence mapping file."""
    path = os.path.abspath(os.path.expanduser(str(path)))
    try:
        with open(path, "r") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise CorrespondenceError("could not read correspondence {}: {}".format(path, exc))

    if not isinstance(raw, dict):
        raise CorrespondenceError(
            "{}: a correspondence file must contain a JSON object".format(path)
        )
    unknown = sorted(set(raw) - _KNOWN_KEYS)
    if unknown:
        raise CorrespondenceError(
            "{}: unknown key(s) {}; supported keys are {}".format(
                path, ", ".join(repr(key) for key in unknown), ", ".join(sorted(_KNOWN_KEYS))
            )
        )
    version = raw.get("version", "1.0")
    if version not in _SUPPORTED_VERSIONS:
        raise CorrespondenceError(
            "{}: correspondence version {!r} is not supported (this build reads "
            "{})".format(path, version, ", ".join(_SUPPORTED_VERSIONS))
        )

    labels = (
        (raw.get("netlist_a") or {}).get("label") or "netlist_a",
        (raw.get("netlist_b") or {}).get("label") or "netlist_b",
    )
    return Correspondence(
        top_inputs=raw.get("top_inputs", BY_NAME),
        top_outputs=raw.get("top_outputs", BY_NAME),
        sequential_gates=raw.get("sequential_gates", BY_NAME),
        ignore_outputs=raw.get("ignore_outputs", ()),
        description=raw.get("description"),
        path=path,
        labels=labels,
        notes=raw.get("notes", ()),
    )


# ---------------------------------------------------------------------------
# deriving observation points
# ---------------------------------------------------------------------------


def sequential_gates_by_name(netlist):
    """``{gate name -> gate}`` for every gate whose type is ``sequential``.

    A duplicated name is not resolved by picking one: it is returned in the
    second element so the caller can report it, because "the register called
    ``q``" is not well defined in that netlist.
    """
    by_name = {}
    duplicates = set()
    for gate in call(netlist, "get_gates", default=[]) or []:
        if not is_sequential(gate):
            continue
        name = call(gate, "get_name", default="")
        if name in by_name:
            duplicates.add(name)
            continue
        by_name[name] = gate
    return by_name, sorted(duplicates)


def _top_module(netlist):
    return call(netlist, "get_top_module")


def _output_pin_names(netlist):
    top = _top_module(netlist)
    if top is None:
        return []
    return list(call(top, "get_output_pin_names", default=[]) or [])


def _top_input_pin_names(netlist):
    top = _top_module(netlist)
    if top is None:
        return []
    return list(call(top, "get_input_pin_names", default=[]) or [])


def _net_of_pin(netlist, pin_name):
    top = _top_module(netlist)
    if top is None:
        return None
    pin = call(top, "get_pin_by_name", pin_name)
    if pin is None:
        return None
    return call(pin, "get_net")


def _gate_type_name(gate):
    return call(call(gate, "get_type"), "get_name", default="<untyped>")


def _input_pin_names(gate):
    gate_type = call(gate, "get_type")
    names = call(gate_type, "get_input_pin_names", default=None)
    if names:
        return list(names)
    return [
        call(pin, "get_name", default="")
        for pin in call(gate_type, "get_input_pins", default=[]) or []
    ]


def _safe_key(text):
    """Make a name safe for a findings identifier (``[A-Za-z0-9_.:/-]``)."""
    return "".join(character if character.isalnum() or character in "_.-" else "_"
                   for character in str(text)) or "unnamed"


def build_observation_points(netlist_a, netlist_b, mapping):
    """Derive the comparison points from the correspondence.

    Returns ``(points, problems, coverage)``.  ``coverage`` is what the report
    has to state: every top-level output of either build and what happened to
    it, plus the register counts.  Anything that could not be turned into a
    point is in ``problems`` -- never dropped.
    """
    points = []
    problems = []

    # -- the input boundary --------------------------------------------------
    # Top-level inputs are not observation points; they are the *variables* the
    # comparison quantifies over. An input that exists in only one build (or is
    # not in the mapping) silently becomes a variable name that occurs on one
    # side only, and the solver would then report a difference that is really a
    # boundary mismatch. Hence: reported, not compared around.
    inputs_a = _top_input_pin_names(netlist_a)
    inputs_b = _top_input_pin_names(netlist_b)
    if mapping.top_inputs == BY_NAME:
        input_pairs = {name: name for name in inputs_a if name in inputs_b}
    else:
        input_pairs = dict(mapping.top_inputs)

    for pin_a in sorted(set(inputs_a) | set(input_pairs)):
        pin_b = input_pairs.get(pin_a)
        if pin_b is None:
            problems.append(
                Problem(
                    "input_correspondence",
                    "input:" + _safe_key(pin_a),
                    "top-module input pin {!r} of {} has no counterpart in the "
                    "correspondence; the two builds are then compared over different "
                    "input variables".format(pin_a, mapping.labels[0]),
                    detail={"pin_a": pin_a},
                )
            )
        elif pin_a not in inputs_a:
            problems.append(
                Problem(
                    "input_correspondence",
                    "input:" + _safe_key(pin_a),
                    "the mapping names input pin {!r}, which {} does not have".format(
                        pin_a, mapping.labels[0]
                    ),
                    detail={"pin_a": pin_a, "pin_b": pin_b},
                )
            )
        elif pin_b not in inputs_b:
            problems.append(
                Problem(
                    "input_correspondence",
                    "input:" + _safe_key(pin_a),
                    "input pin {!r} is mapped to {!r}, which {} does not have".format(
                        pin_a, pin_b, mapping.labels[1]
                    ),
                    detail={"pin_a": pin_a, "pin_b": pin_b},
                )
            )

    for pin_b in sorted(set(inputs_b) - set(input_pairs.values())):
        problems.append(
            Problem(
                "input_correspondence",
                "input_b:" + _safe_key(pin_b),
                "top-module input pin {!r} exists only in {}".format(pin_b, mapping.labels[1]),
                detail={"pin_b": pin_b},
            )
        )

    outputs_a = _output_pin_names(netlist_a)
    outputs_b = _output_pin_names(netlist_b)

    if mapping.top_outputs == BY_NAME:
        output_pairs = {name: name for name in outputs_a if name in outputs_b}
    else:
        output_pairs = dict(mapping.top_outputs)

    ignored = set(mapping.ignore_outputs)
    covered_outputs = {}

    for pin_a in sorted(set(outputs_a) | set(output_pairs)):
        if pin_a in ignored:
            covered_outputs[pin_a] = "ignored_by_mapping"
            continue
        pin_b = output_pairs.get(pin_a)
        if pin_b is None:
            covered_outputs[pin_a] = "unmapped"
            problems.append(
                Problem(
                    "output_correspondence",
                    "output:" + _safe_key(pin_a),
                    "top-module output pin {!r} of {} has no counterpart in the "
                    "correspondence".format(pin_a, mapping.labels[0]),
                    detail={"pin_a": pin_a},
                )
            )
            continue
        if pin_b not in outputs_b:
            covered_outputs[pin_a] = "missing_in_b"
            problems.append(
                Problem(
                    "output_correspondence",
                    "output:" + _safe_key(pin_a),
                    "output pin {!r} is mapped to {!r}, which {} does not have".format(
                        pin_a, pin_b, mapping.labels[1]
                    ),
                    detail={"pin_a": pin_a, "pin_b": pin_b},
                )
            )
            continue
        net_a = _net_of_pin(netlist_a, pin_a)
        net_b = _net_of_pin(netlist_b, pin_b)
        if net_a is None or net_b is None:
            covered_outputs[pin_a] = "unconnected"
            problems.append(
                Problem(
                    "output_correspondence",
                    "output:" + _safe_key(pin_a),
                    "output pin {!r}/{!r} is not connected to a net in one of the "
                    "builds".format(pin_a, pin_b),
                    detail={"pin_a": pin_a, "pin_b": pin_b},
                )
            )
            continue
        covered_outputs[pin_a] = "compared"
        points.append(
            ObservationPoint(
                "output:" + _safe_key(pin_a),
                "top_output",
                "top-module output {}".format(pin_a),
                net_a,
                net_b,
                detail={"pin_a": pin_a, "pin_b": pin_b},
            )
        )

    for pin_b in sorted(set(outputs_b) - set(output_pairs.values()) - ignored):
        problems.append(
            Problem(
                "output_correspondence",
                "output_b:" + _safe_key(pin_b),
                "top-module output pin {!r} exists only in {}".format(pin_b, mapping.labels[1]),
                detail={"pin_b": pin_b},
            )
        )

    sequential_a, duplicates_a = sequential_gates_by_name(netlist_a)
    sequential_b, duplicates_b = sequential_gates_by_name(netlist_b)
    for label, duplicates in ((mapping.labels[0], duplicates_a), (mapping.labels[1], duplicates_b)):
        if duplicates:
            problems.append(
                Problem(
                    "duplicate_sequential_names",
                    "sequential.duplicates:" + _safe_key(label),
                    "{} has {} sequential gate name(s) used more than once; a "
                    "name-based correspondence is not well defined for them".format(
                        label, len(duplicates)
                    ),
                    detail={"names": duplicates[:50]},
                )
            )

    if mapping.sequential_gates == BY_NAME:
        gate_pairs = {name: name for name in sequential_a if name in sequential_b}
    else:
        gate_pairs = dict(mapping.sequential_gates)

    # Names that came through the whole matching -- same pair, same gate type.
    # Everything else is "unmatched" and its state is not shared between the
    # two builds, however the mapping file spells it.
    matched_a = set()
    matched_b = set()

    for name_a in sorted(set(sequential_a) | set(gate_pairs)):
        name_b = gate_pairs.get(name_a)
        gate_a = sequential_a.get(name_a)
        if name_b is None:
            problems.append(
                Problem(
                    "sequential_correspondence",
                    "sequential:" + _safe_key(name_a),
                    "sequential gate {!r} of {} is not mapped to anything in {}".format(
                        name_a, mapping.labels[0], mapping.labels[1]
                    ),
                    detail={"gate_a": name_a},
                    gates_a=[gate_a] if gate_a is not None else [],
                )
            )
            continue
        gate_b = sequential_b.get(name_b)
        if gate_a is None or gate_b is None:
            problems.append(
                Problem(
                    "sequential_correspondence",
                    "sequential:" + _safe_key(name_a),
                    "the mapping {!r} -> {!r} names a sequential gate that does not "
                    "exist ({} in {})".format(
                        name_a,
                        name_b,
                        "gate_a" if gate_a is None else "gate_b",
                        mapping.labels[0] if gate_a is None else mapping.labels[1],
                    ),
                    detail={"gate_a": name_a, "gate_b": name_b},
                    gates_a=[gate_a] if gate_a is not None else [],
                    gates_b=[gate_b] if gate_b is not None else [],
                )
            )
            continue
        type_a = _gate_type_name(gate_a)
        type_b = _gate_type_name(gate_b)
        if type_a != type_b:
            problems.append(
                Problem(
                    "sequential_type_mismatch",
                    "sequential.type:" + _safe_key(name_a),
                    "matched sequential gates {!r}/{!r} have different types ({} vs "
                    "{}); their state elements cannot be assumed to correspond".format(
                        name_a, name_b, type_a, type_b
                    ),
                    detail={"gate_a": name_a, "gate_b": name_b, "type_a": type_a, "type_b": type_b},
                    gates_a=[gate_a],
                    gates_b=[gate_b],
                )
            )
            continue
        matched_a.add(name_a)
        matched_b.add(name_b)
        for pin in _input_pin_names(gate_a):
            net_a = call(gate_a, "get_fan_in_net", pin)
            net_b = call(gate_b, "get_fan_in_net", pin)
            if net_a is None and net_b is None:
                continue
            if net_a is None or net_b is None:
                problems.append(
                    Problem(
                        "sequential_pin_connectivity",
                        "sequential.pin:{}.{}".format(_safe_key(name_a), _safe_key(pin)),
                        "input pin {!r} of {!r}/{!r} is connected in one build and "
                        "open in the other".format(pin, name_a, name_b),
                        detail={"gate_a": name_a, "gate_b": name_b, "pin": pin},
                        gates_a=[gate_a],
                        gates_b=[gate_b],
                    )
                )
                continue
            points.append(
                ObservationPoint(
                    "sequential:{}.{}".format(_safe_key(name_a), _safe_key(pin)),
                    "sequential_input",
                    "next-state input {}.{}".format(name_a, pin),
                    net_a,
                    net_b,
                    detail={
                        "gate_a": name_a,
                        "gate_b": name_b,
                        "pin": pin,
                        "gate_type": type_a,
                    },
                )
            )

    for name_b in sorted(set(sequential_b) - set(gate_pairs.values())):
        problems.append(
            Problem(
                "sequential_correspondence",
                "sequential_b:" + _safe_key(name_b),
                "sequential gate {!r} exists only in {}".format(name_b, mapping.labels[1]),
                detail={"gate_b": name_b},
                gates_b=[sequential_b[name_b]],
            )
        )

    # Sequential gates without a usable counterpart. A cone that reads one of
    # them cannot be compared at all -- see compare._uncovered_state_boundaries.
    unmatched_a = sorted(set(sequential_a) - matched_a)
    unmatched_b = sorted(set(sequential_b) - matched_b)

    coverage = {
        "unmatched_sequential_a": unmatched_a,
        "unmatched_sequential_b": unmatched_b,
        "top_inputs_a": sorted(inputs_a),
        "top_inputs_b": sorted(inputs_b),
        "top_outputs_a": sorted(outputs_a),
        "top_outputs_b": sorted(outputs_b),
        "top_output_disposition": covered_outputs,
        "sequential_gates_a": len(sequential_a),
        "sequential_gates_b": len(sequential_b),
        "matched_sequential_gates": len(matched_a),
        "observation_points": len(points),
        "observation_points_by_kind": {
            kind: len([point for point in points if point.kind == kind])
            for kind in sorted({point.kind for point in points})
        },
    }
    return points, problems, coverage
