"""The ``hal_py`` side: load an imported export and attach primitive semantics.

``tennm_lcell_comb`` cannot carry its semantics in the gate library.  HAL
derives a LUT function from an init string only for gate types with at most six
inputs and exactly one function per LUT output; the ALM has ten inputs and
three outputs whose meaning depends on ``lut_mask`` *and* on whether the cell is
in normal or arithmetic mode.  So the library declares the structure and this
module attaches the function of each individual gate with
``Gate.add_boolean_function`` -- using the same decoding as
:mod:`hal_agilex.primitives`, and refusing any gate outside the validated
coverage instead of attaching something plausible.

This is the only module in the package that needs a built HAL.
"""

from . import library, primitives

from hal_findings import model
from hal_findings.adapters.common import netlist_artifact, utc_now

__all__ = [
    "PRODUCER",
    "import_hal",
    "load_netlist",
    "elaborate",
    "build_document",
]

PRODUCER = {"name": "hal_agilex.hal_adapter", "version": "1.0.0"}


def import_hal(library_directories=()):
    """Import ``hal_py``, optionally after extending ``sys.path``.

    The HGL gate library parser and the Verilog netlist parser are HAL
    *plugins*: without ``plugin_manager.load_all_plugins()`` nothing is
    registered for ``.hgl``/``.v`` and every load silently returns ``None``.
    Loading them here is what makes :func:`load_netlist` work at all.
    """
    import os
    import sys

    for entry in library_directories:
        path = os.path.abspath(os.path.expanduser(entry))
        if path not in sys.path:
            sys.path.insert(0, path)
    import hal_py

    load_plugins(hal_py)
    return hal_py


def load_plugins(hal_py):
    """Register HAL's parser plugins; idempotent, so callers need not track it."""
    hal_py.plugin_manager.load_all_plugins()
    return hal_py


def load_netlist(hal_py, netlist_path, gate_library_path):
    """Load an imported netlist with the ``AGILEX_TENNM`` library.

    Loads the plugins first: callers that already have a ``hal_py`` (a script running inside
    ``hal --python-script``, a test) would otherwise get ``None`` back from a parser that was
    never registered.
    """
    load_plugins(hal_py)
    netlist = hal_py.NetlistFactory.load_netlist(str(netlist_path), str(gate_library_path))
    if netlist is None:
        raise RuntimeError(
            "hal_py.NetlistFactory.load_netlist({}, {}) returned None; see the HAL "
            "log above".format(netlist_path, gate_library_path)
        )
    return netlist


# ---------------------------------------------------------------------------
# expression construction
# ---------------------------------------------------------------------------


def _minterm(index, pins):
    literals = []
    for position, pin in enumerate(pins):
        if (index >> position) & 1:
            literals.append(pin)
        else:
            literals.append("(! {})".format(pin))
    return "({})".format(" & ".join(literals))


def _sop(mask, pins, offset=0):
    """Sum-of-products string for the mask window addressed by *pins*."""
    width = 1 << len(pins)
    terms = [
        _minterm(index, pins)
        for index in range(width)
        if (mask >> (offset + index)) & 1
    ]
    if not terms:
        return "0b0"
    if len(terms) == width:
        return "0b1"
    return "({})".format(" | ".join(terms))


def combout_expression(mask):
    """Normal-mode ``combout`` as a Boolean expression over ``dataa..dataf``."""
    return _sop(mask, list(primitives.LCELL_DATA_PINS))


def arithmetic_expressions(mask):
    """``(sumout, cout)`` expressions of an arithmetic-mode cell."""
    pins = list(primitives.LCELL_DATA_PINS[:4])
    propagate = _sop(mask, pins, offset=0)
    generate = _sop(mask, pins, offset=16)
    sumout = "({} ^ cin)".format(propagate)
    cout = "(({} & cin) | ((! {}) & {}))".format(propagate, propagate, generate)
    return sumout, cout


# ---------------------------------------------------------------------------
# elaboration
# ---------------------------------------------------------------------------


def _pin_state(gate, pins):
    """Split *pins* into driven ones and ones tied to a constant net."""
    driven = set()
    constants = {}
    for pin in pins:
        net = gate.get_fan_in_net(pin)
        if net is None:
            continue
        if net.is_gnd_net():
            constants[pin] = 0
        elif net.is_vcc_net():
            constants[pin] = 1
        else:
            driven.add(pin)
    return driven, constants


def _lut_mask(gate):
    value = gate.get_data("generic", "lut_mask")
    if not value or not value[1]:
        return None
    try:
        return int(str(value[1]), 16)
    except ValueError:
        return None


def elaborate(hal_py, netlist):
    """Attach Boolean functions to every covered ``tennm_lcell_comb`` gate.

    Returns a report: how many gates were elaborated, and one entry per gate
    that was refused, with the reason.  Refused gates keep no function at all --
    an empty function is honest, a guessed one is not.
    """
    report = {"elaborated": 0, "refused": [], "checked_ff": 0, "constants": 0, "types": {}}

    for gate in netlist.get_gates():
        type_name = gate.get_type().get_name()
        report["types"][type_name] = report["types"].get(type_name, 0) + 1

        if type_name in library.CONSTANT_GATE_TYPES:
            # HAL synthesises these for the 1'b0/1'b1 literals of the export.
            # They are not vendor primitives and they already carry their
            # function from the gate library, so there is nothing to attach and
            # nothing to refuse.
            report["constants"] += 1
            continue

        if type_name not in primitives.COVERED_PRIMITIVES:
            report["refused"].append(
                {
                    "gate": gate.get_name(),
                    "id": gate.get_id(),
                    "type": type_name,
                    "reason": primitives.UNCOVERED_PRIMITIVE_REASONS.get(
                        type_name, "primitive is not modelled by hal_agilex"
                    ),
                }
            )
            continue

        if type_name == primitives.FF:
            driven, constants = _pin_state(gate, primitives.FF_INPUT_PINS)
            try:
                primitives.check_ff_configuration(driven, constants)
            except primitives.UnsupportedConfiguration as exc:
                report["refused"].append(
                    {
                        "gate": gate.get_name(),
                        "id": gate.get_id(),
                        "type": type_name,
                        "reason": str(exc),
                    }
                )
                continue
            report["checked_ff"] += 1
            continue

        driven, constants = _pin_state(gate, primitives.LCELL_INPUT_PINS)
        uses_arithmetic = any(
            gate.get_fan_out_net(pin) is not None for pin in ("sumout", "cout")
        )
        mask = _lut_mask(gate)
        parameters = {
            "lut_mask": mask,
            "extended_lut": (gate.get_data("generic", "extended_lut") or ("", "off"))[1],
            "shared_arith": (gate.get_data("generic", "shared_arith") or ("", "off"))[1],
        }
        try:
            if mask is None:
                raise primitives.UnsupportedConfiguration(
                    "gate has no readable generic 'lut_mask'"
                )
            primitives.check_lcell_configuration(
                parameters, driven, uses_arithmetic, constants
            )
        except primitives.UnsupportedConfiguration as exc:
            report["refused"].append(
                {
                    "gate": gate.get_name(),
                    "id": gate.get_id(),
                    "type": type_name,
                    "reason": str(exc),
                }
            )
            continue

        expressions = {}
        if gate.get_fan_out_net("combout") is not None:
            expressions["combout"] = combout_expression(mask)
        if uses_arithmetic:
            sumout, cout = arithmetic_expressions(mask)
            if gate.get_fan_out_net("sumout") is not None:
                expressions["sumout"] = sumout
            if gate.get_fan_out_net("cout") is not None:
                expressions["cout"] = cout

        for pin, expression in sorted(expressions.items()):
            function = hal_py.BooleanFunction.from_string(expression)
            if function is not None and hasattr(function, "simplify"):
                # A 64-minterm sum of products is correct but unwieldy for every
                # downstream analysis; simplification is semantics-preserving.
                simplified = function.simplify()
                if simplified is not None:
                    function = simplified
            if function is None:
                report["refused"].append(
                    {
                        "gate": gate.get_name(),
                        "id": gate.get_id(),
                        "type": type_name,
                        "reason": "BooleanFunction.from_string failed for pin " + pin,
                    }
                )
                break
            gate.add_boolean_function(pin, function)
        else:
            if expressions:
                report["elaborated"] += 1

    return report


def build_document(netlist, report, artifact_id="netlist", path=None, generated_at=None):
    """Report the elaboration outcome as findings."""
    artifact = netlist_artifact(
        netlist,
        artifact_id,
        path=path,
        description="netlist imported from a Quartus Prime Pro EDA export",
    )
    method = model.method(
        "primitive elaboration",
        "structural",
        False,
        description=(
            "Reads each gate's lut_mask generic and attaches the Boolean functions "
            "of the modelled ALM semantics with Gate.add_boolean_function."
        ),
    )
    findings = []

    if report["refused"]:
        by_type = {}
        for entry in report["refused"]:
            by_type.setdefault(entry["type"], []).append(entry)
        primitives_list = [
            model.unsupported_primitive(
                gate_type,
                "; ".join(sorted({entry["reason"] for entry in entries}))[:900],
                count=len(entries),
                gate_library="AGILEX_TENNM",
                example_gates=[
                    model.gate_ref(artifact_id, entry["id"], entry["gate"], gate_type=gate_type)
                    for entry in entries[:3]
                ],
            )
            for gate_type, entries in sorted(by_type.items())
        ]
        findings.append(
            model.finding(
                "hal_agilex/elaborate/refused",
                "Gates left without semantics",
                model.STATUS_UNSUPPORTED,
                method,
                model.scope([artifact_id], gate_types=sorted(by_type)),
                summary=(
                    "{} gate(s) are outside the validated coverage and were left "
                    "with no Boolean function; any analysis that reads them is "
                    "reading a free variable, not a modelled primitive.".format(
                        len(report["refused"])
                    )
                ),
                severity="high",
                unsupported_dict=model.unsupported(
                    "primitive",
                    "hal_agilex attaches semantics only to validated configurations "
                    "of {}".format(" and ".join(primitives.COVERED_PRIMITIVES)),
                    primitives=primitives_list,
                ),
                tags=["agilex", "coverage"],
            )
        )

    findings.append(
        model.finding(
            "hal_agilex/elaborate/attached",
            "Primitive semantics attached to the imported netlist",
            model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
            method,
            model.scope([artifact_id], gate_types=sorted(report["types"])),
            summary=(
                "{} tennm_lcell_comb gate(s) received Boolean functions derived from "
                "their lut_mask and {} tennm_ff gate(s) were checked against the "
                "modelled configuration.".format(report["elaborated"], report["checked_ff"])
            ),
            bounds_dict=model.unbounded(
                description="a statement about the loaded netlist, not about an execution"
            ),
            assumptions=[
                model.assumption(
                    "primitive-semantics",
                    "The attached functions are the ALM semantics documented in "
                    "tools/hal_agilex/primitives.py and validated against the vendor "
                    "export by simulation.",
                    kind="library",
                )
            ],
            metrics={
                "gates_elaborated": report["elaborated"],
                "gates_refused": len(report["refused"]),
            },
            data={"histogram": report["types"]},
            tags=["agilex"],
        )
    )

    return model.document(
        PRODUCER,
        [artifact],
        {
            "entry_point": "hal_agilex.hal_adapter.elaborate",
            "plugin": {"name": "hal_agilex", "version": "1.0.0"},
        },
        findings,
        generated_at=generated_at or utc_now(),
    )
