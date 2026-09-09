"""Schema and cross-field validation for plugin capability documents.

Two layers, same split as :mod:`hal_findings.validate`:

* :func:`schema_errors` checks the document against the JSON Schema, using the
  dependency-free validator from ``hal_findings.jsonschema_mini`` (or the real
  ``jsonschema`` library when it is installed and asked for);
* :func:`semantic_errors` checks what a schema cannot: that the declared gate
  type properties and pin types are names HAL actually knows, that an allow/deny
  list is not empty, and that a document claiming library independence does not
  also carry a list.

A capability declaration that names a property HAL does not have is worse than
no declaration at all -- it makes the discovery tool report "not supported for
this netlist" forever, for a reason nobody can act on.  Hence the closed
vocabularies below; they are read out of the bindings' enums (see
``src/python_bindings/bindings/gate_type.cpp``).
"""

from hal_findings import jsonschema_mini

from .schema import SUPPORTED_CAPABILITIES_VERSIONS, load_schema

__all__ = [
    "CapabilitiesValidationError",
    "GATE_TYPE_PROPERTIES",
    "PIN_TYPES",
    "schema_errors",
    "semantic_errors",
    "collect_errors",
    "validate_document",
    "is_valid",
]


class CapabilitiesValidationError(ValueError):
    """Raised by :func:`validate_document` with every problem found."""

    def __init__(self, errors):
        self.errors = list(errors)
        super(CapabilitiesValidationError, self).__init__(
            "capability declaration is invalid:\n  - " + "\n  - ".join(self.errors)
        )


#: ``hal_py.GateTypeProperty`` members, from ``src/python_bindings/bindings/gate_type.cpp``.
GATE_TYPE_PROPERTIES = frozenset(
    [
        "combinational",
        "sequential",
        "tristate",
        "power",
        "ground",
        "ff",
        "latch",
        "ram",
        "fifo",
        "shift_register",
        "io",
        "dsp",
        "pll",
        "oscillator",
        "scan",
        "c_buffer",
        "c_inverter",
        "c_and",
        "c_nand",
        "c_or",
        "c_nor",
        "c_xor",
        "c_xnor",
        "c_aoi",
        "c_oai",
        "c_mux",
        "c_carry",
        "c_half_adder",
        "c_full_adder",
        "c_lut",
    ]
)

#: ``hal_py.PinType`` members, from ``src/python_bindings/bindings/gate_type.cpp``.
PIN_TYPES = frozenset(
    [
        "none",
        "power",
        "ground",
        "lut",
        "state",
        "neg_state",
        "clock",
        "enable",
        "set",
        "reset",
        "data",
        "address",
        "io_pad",
        "select",
        "carry",
        "sum",
        "status",
        "error",
        "error_detection",
        "done",
        "control",
    ]
)


def _version_of(document):
    if not isinstance(document, dict):
        return None
    return document.get("capabilities_version")


def schema_errors(document, prefer_jsonschema=False):
    """Validate ``document`` against its schema; returns a list of messages."""
    version = _version_of(document)
    if version is None:
        return ["document has no 'capabilities_version'"]
    if version not in SUPPORTED_CAPABILITIES_VERSIONS:
        return [
            "unsupported capabilities_version {!r}; this build reads {}".format(
                version, ", ".join(SUPPORTED_CAPABILITIES_VERSIONS)
            )
        ]

    schema = load_schema(version)
    if prefer_jsonschema:
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            pass
        else:
            # jsonschema < 4 (Ubuntu 22.04 ships 3.x) has no draft 2020-12 validator,
            # and a validator that does not know the draft must not be used on a
            # draft 2020-12 schema; fall through to the builtin validator instead.
            draft = getattr(jsonschema, "Draft202012Validator", None)
            if draft is not None:
                validator = draft(schema)
                return [
                    "{}: {}".format("/".join(str(part) for part in error.absolute_path) or "<root>", error.message)
                    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
                ]
    return [str(error) for error in jsonschema_mini.iter_errors(document, schema)]


def _unknown(values, vocabulary, where, kind, errors):
    for value in values or []:
        if value not in vocabulary:
            errors.append(
                "{}: {!r} is not a known {} (HAL knows: {})".format(
                    where, value, kind, ", ".join(sorted(vocabulary))
                )
            )


def semantic_errors(document):
    """Checks the schema cannot express; returns a list of messages."""
    errors = []
    if not isinstance(document, dict):
        return ["capability declaration must be a JSON object"]

    requires = document.get("requires") or {}
    properties = requires.get("gate_type_properties") or {}
    _unknown(properties.get("all_of"), GATE_TYPE_PROPERTIES,
             "requires.gate_type_properties.all_of", "GateTypeProperty", errors)
    _unknown(properties.get("any_of"), GATE_TYPE_PROPERTIES,
             "requires.gate_type_properties.any_of", "GateTypeProperty", errors)
    _unknown(requires.get("gate_type_pin_types"), PIN_TYPES,
             "requires.gate_type_pin_types", "PinType", errors)

    if requires.get("gate_type_pin_types") and not (
        properties.get("all_of") or properties.get("any_of")
    ):
        errors.append(
            "requires.gate_type_pin_types is only meaningful together with "
            "requires.gate_type_properties: without a property there is no set of "
            "gate types the pin requirement could apply to"
        )

    libraries = document.get("supported_gate_libraries") or {}
    mode = libraries.get("mode")
    names = libraries.get("names") or []
    if mode in ("allow_list", "deny_list") and not names:
        errors.append(
            "supported_gate_libraries.mode is {!r} but 'names' is empty; an empty "
            "allow list supports nothing and an empty deny list is 'any'".format(mode)
        )
    if mode == "any" and names:
        errors.append(
            "supported_gate_libraries.mode is 'any' but 'names' is not empty; use "
            "'verified' for the libraries actually exercised"
        )

    plugin = document.get("plugin") or {}
    findings = document.get("findings")
    if plugin.get("kind") == "analysis" and findings is None:
        errors.append(
            "plugin.kind is 'analysis' but no 'findings' block declares which "
            "hal_findings statuses it can emit"
        )

    for entry in document.get("entry_points") or []:
        if entry.get("language") == "python" and not entry.get("symbol", "").startswith(
            ("hal_plugins.", "hal_py.", "tools.", "plugins.")
        ):
            errors.append(
                "entry_points[{!r}].symbol {!r} does not look like an importable HAL "
                "symbol (expected hal_plugins.<plugin>..., hal_py..., or a "
                "repository path)".format(entry.get("name"), entry.get("symbol"))
            )

    return errors


def collect_errors(document, prefer_jsonschema=False):
    """All schema errors, plus the semantic ones when the schema passed."""
    errors = schema_errors(document, prefer_jsonschema=prefer_jsonschema)
    if errors:
        return errors
    return semantic_errors(document)


def validate_document(document, prefer_jsonschema=False):
    """Raise :class:`CapabilitiesValidationError` unless ``document`` is valid."""
    errors = collect_errors(document, prefer_jsonschema=prefer_jsonschema)
    if errors:
        raise CapabilitiesValidationError(errors)
    return document


def is_valid(document, prefer_jsonschema=False):
    """``True`` if ``document`` passes both validation layers."""
    return not collect_errors(document, prefer_jsonschema=prefer_jsonschema)
