"""A dependency-free validator for the JSON Schema subset used by hal_findings.

The findings schema is deliberately written against a small, well understood
slice of JSON Schema 2020-12 so that validation works in any environment HAL
runs in -- including a bare HAL build container that has no ``jsonschema``
package installed.  When the real ``jsonschema`` library *is* available,
:mod:`hal_findings.validate` cross-checks against it, and the unit tests assert
that both implementations agree.

Supported keywords::

    $ref (local "#/$defs/NAME" and "#" only), type, enum, const,
    properties, required, additionalProperties, minProperties,
    items, minItems, maxItems, uniqueItems,
    minLength, maxLength, pattern,
    minimum, maximum, exclusiveMinimum, exclusiveMaximum,
    allOf, anyOf, oneOf, not, if/then/else

Anything else in a schema is ignored, which is why :func:`check_schema_support`
exists: it walks a schema and reports keywords this module would silently skip,
so the schema can never drift past the validator without the tests noticing.
"""

import re

__all__ = [
    "ValidationError",
    "iter_errors",
    "validate",
    "check_schema_support",
    "SUPPORTED_KEYWORDS",
    "IGNORED_KEYWORDS",
]

#: Keywords this module enforces.
SUPPORTED_KEYWORDS = frozenset(
    [
        "$ref",
        "type",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "minProperties",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "allOf",
        "anyOf",
        "oneOf",
        "not",
        "if",
        "then",
        "else",
    ]
)

#: Annotation-only keywords that carry no constraint.
IGNORED_KEYWORDS = frozenset(
    ["$schema", "$id", "$defs", "title", "description", "default", "examples", "deprecated"]
)


class ValidationError(ValueError):
    """A single schema violation, carrying the JSON pointer it occurred at."""

    def __init__(self, message, pointer="", validator=None):
        self.message = message
        self.pointer = pointer or "#"
        self.validator = validator
        ValueError.__init__(self, "{}: {}".format(self.pointer, message))


def _pointer(parent, token):
    token = str(token).replace("~", "~0").replace("/", "~1")
    return "{}/{}".format(parent, token)


def _is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _type_matches(value, expected):
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return _is_integer(value)
    if expected == "number":
        return _is_number(value)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ValueError("unknown JSON Schema type {!r}".format(expected))


def _resolve(schema, root):
    """Follow local ``$ref`` chains until a ref-free schema is reached."""
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if ref == "#":
            schema = root
        elif ref.startswith("#/"):
            target = root
            for token in ref[2:].split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                if not isinstance(target, dict) or token not in target:
                    raise ValueError("unresolvable $ref {!r}".format(ref))
                target = target[token]
            schema = target
        else:
            raise ValueError("only local $ref is supported, got {!r}".format(ref))
        seen += 1
        if seen > 64:
            raise ValueError("cyclic $ref chain at {!r}".format(ref))
    return schema


def _valid(instance, schema, root):
    for _ in _iter_errors(instance, schema, root, "#"):
        return False
    return True


def _iter_errors(instance, schema, root, pointer):
    if schema is True or schema == {}:
        return
    if schema is False:
        yield ValidationError("value is not allowed here", pointer, "false")
        return

    schema = _resolve(schema, root)
    if not isinstance(schema, dict):
        raise ValueError("schema must be an object or boolean at {}".format(pointer))

    if "type" in schema:
        expected = schema["type"]
        options = expected if isinstance(expected, list) else [expected]
        if not any(_type_matches(instance, option) for option in options):
            yield ValidationError(
                "expected type {}, got {}".format("/".join(options), type(instance).__name__),
                pointer,
                "type",
            )
            return

    if "const" in schema and instance != schema["const"]:
        yield ValidationError(
            "expected the constant {!r}, got {!r}".format(schema["const"], instance),
            pointer,
            "const",
        )
    if "enum" in schema and instance not in schema["enum"]:
        yield ValidationError(
            "{!r} is not one of {}".format(instance, schema["enum"]), pointer, "enum"
        )

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            yield ValidationError(
                "string shorter than {} characters".format(schema["minLength"]),
                pointer,
                "minLength",
            )
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            yield ValidationError(
                "string longer than {} characters".format(schema["maxLength"]),
                pointer,
                "maxLength",
            )
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            yield ValidationError(
                "{!r} does not match {!r}".format(instance, schema["pattern"]),
                pointer,
                "pattern",
            )

    if _is_number(instance):
        if "minimum" in schema and instance < schema["minimum"]:
            yield ValidationError(
                "{!r} is below the minimum {!r}".format(instance, schema["minimum"]),
                pointer,
                "minimum",
            )
        if "maximum" in schema and instance > schema["maximum"]:
            yield ValidationError(
                "{!r} is above the maximum {!r}".format(instance, schema["maximum"]),
                pointer,
                "maximum",
            )
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            yield ValidationError(
                "{!r} is not greater than {!r}".format(instance, schema["exclusiveMinimum"]),
                pointer,
                "exclusiveMinimum",
            )
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            yield ValidationError(
                "{!r} is not less than {!r}".format(instance, schema["exclusiveMaximum"]),
                pointer,
                "exclusiveMaximum",
            )

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            yield ValidationError(
                "expected at least {} items, got {}".format(schema["minItems"], len(instance)),
                pointer,
                "minItems",
            )
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            yield ValidationError(
                "expected at most {} items, got {}".format(schema["maxItems"], len(instance)),
                pointer,
                "maxItems",
            )
        if schema.get("uniqueItems"):
            seen = []
            for item in instance:
                if item in seen:
                    yield ValidationError("items must be unique", pointer, "uniqueItems")
                    break
                seen.append(item)
        if "items" in schema:
            for index, item in enumerate(instance):
                for error in _iter_errors(
                    item, schema["items"], root, _pointer(pointer, index)
                ):
                    yield error

    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                yield ValidationError(
                    "missing required property {!r}".format(name), pointer, "required"
                )
        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            yield ValidationError(
                "expected at least {} properties".format(schema["minProperties"]),
                pointer,
                "minProperties",
            )
        properties = schema.get("properties", {})
        for name, value in instance.items():
            if name in properties:
                for error in _iter_errors(
                    value, properties[name], root, _pointer(pointer, name)
                ):
                    yield error
        if "additionalProperties" in schema:
            extra = schema["additionalProperties"]
            for name, value in instance.items():
                if name in properties:
                    continue
                if extra is False:
                    yield ValidationError(
                        "unknown property {!r}".format(name), pointer, "additionalProperties"
                    )
                else:
                    for error in _iter_errors(
                        value, extra, root, _pointer(pointer, name)
                    ):
                        yield error

    for keyword in ("allOf",):
        for index, subschema in enumerate(schema.get(keyword, [])):
            for error in _iter_errors(instance, subschema, root, pointer):
                yield error

    if "anyOf" in schema:
        if not any(_valid(instance, sub, root) for sub in schema["anyOf"]):
            yield ValidationError(
                "value does not match any of the {} allowed alternatives".format(
                    len(schema["anyOf"])
                ),
                pointer,
                "anyOf",
            )
    if "oneOf" in schema:
        matches = sum(1 for sub in schema["oneOf"] if _valid(instance, sub, root))
        if matches != 1:
            yield ValidationError(
                "value matches {} of the alternatives, expected exactly 1".format(matches),
                pointer,
                "oneOf",
            )
    if "not" in schema and _valid(instance, schema["not"], root):
        yield ValidationError("value matches a forbidden schema", pointer, "not")

    if "if" in schema:
        if _valid(instance, schema["if"], root):
            if "then" in schema:
                for error in _iter_errors(instance, schema["then"], root, pointer):
                    yield error
        elif "else" in schema:
            for error in _iter_errors(instance, schema["else"], root, pointer):
                yield error


def iter_errors(instance, schema):
    """Yield every :class:`ValidationError` for ``instance`` against ``schema``."""
    for error in _iter_errors(instance, schema, schema, "#"):
        yield error


def validate(instance, schema):
    """Raise the first :class:`ValidationError` found, or return ``None``."""
    for error in iter_errors(instance, schema):
        raise error
    return None


def check_schema_support(schema, pointer="#"):
    """Return a sorted list of ``"pointer: keyword"`` entries this module ignores.

    Used by the test suite as a tripwire: if the schema starts using a keyword
    the validator does not implement, the list is non-empty and the test fails
    rather than the validator quietly accepting invalid documents.
    """
    unsupported = set()

    def walk(node, ptr):
        if isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, _pointer(ptr, index))
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            child = _pointer(ptr, key)
            if key in ("properties", "$defs"):
                for name, sub in value.items():
                    walk(sub, _pointer(child, name))
                continue
            if key in SUPPORTED_KEYWORDS or key in IGNORED_KEYWORDS:
                if key in ("allOf", "anyOf", "oneOf", "items", "not", "if", "then", "else",
                           "additionalProperties", "$defs"):
                    walk(value, child)
                continue
            unsupported.add("{}: {}".format(ptr, key))

    walk(schema, pointer)
    return sorted(unsupported)
