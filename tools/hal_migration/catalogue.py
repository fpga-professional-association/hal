"""Loading a target capability catalogue and resolving mappings against it.

A catalogue is a *manually reviewed* table: for each source primitive it states
the category a human reviewer was willing to sign off (``supported``,
``candidate`` or ``unresolved``), the target primitive, the assumptions the
mapping rests on, the source metadata it needs, and the verification
obligations it leaves open.

Resolution here is deliberately pessimistic, and only ever downwards:

* a gate type that is **not in the catalogue** is ``unresolved`` -- silence is
  not approval;
* a mapping whose ``requires_metadata`` the inventory does not provide is
  ``unresolved``, however confident the catalogue was: an assessment made on
  metadata that was never read is not an assessment;
* a primitive the *source* gate library describes as a black box is
  ``unresolved`` even if the catalogue proposes a target, because there is no
  source behaviour to compare a target against.

Nothing here can raise a category. ``supported`` in the output means exactly
"a reviewer proposed this mapping and the inventory carried the metadata the
mapping needs" -- never "converted", and never "verified".
"""

import os

from . import formats
from . import obligations as obligations_module
from .inventory import metadata_value

__all__ = [
    "BUNDLED_DIR",
    "CatalogueError",
    "CATEGORIES",
    "load",
    "bundled_catalogues",
    "resolve_path",
    "mappings_by_type",
    "template_index",
    "check_obligation_ids",
    "library_mismatch",
    "resolve_mapping",
]

#: Catalogues shipped with this tool.
BUNDLED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalogues")

#: Mapping categories, weakest last.
CATEGORIES = ("supported", "candidate", "unresolved")


class CatalogueError(ValueError):
    """A catalogue is unusable (unknown obligation id, missing file, ...)."""


def bundled_catalogues():
    """Return ``{catalogue_id: path}`` for the catalogues shipped with the tool."""
    found = {}
    if not os.path.isdir(BUNDLED_DIR):
        return found
    for name in sorted(os.listdir(BUNDLED_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(BUNDLED_DIR, name)
        try:
            document = formats.read_json(path)
        except formats.FormatError:
            continue
        catalogue_id = document.get("catalogue_id")
        if catalogue_id:
            found[catalogue_id] = path
    return found


def resolve_path(spec):
    """Resolve ``spec`` as a file path or as the id of a bundled catalogue."""
    if os.path.isfile(spec):
        return spec
    bundled = bundled_catalogues()
    if spec in bundled:
        return bundled[spec]
    raise CatalogueError(
        "no catalogue {!r}: it is neither a file nor one of the bundled catalogues "
        "({})".format(spec, ", ".join(sorted(bundled)) or "none installed")
    )


def load(spec, prefer_jsonschema=False):
    """Load, validate and cross-check a catalogue.

    Beyond schema validation this rejects duplicate ``source_type`` entries and
    obligation ids that resolve to neither a catalogue template nor a built-in
    obligation -- a dangling obligation id would silently drop a required check.
    """
    path = resolve_path(spec)
    document = formats.read_json(path)
    formats.validate(document, "catalogue", prefer_jsonschema=prefer_jsonschema)

    seen = set()
    for mapping in document.get("mappings", []):
        source_type = mapping["source_type"]
        if source_type in seen:
            raise CatalogueError(
                "catalogue {} maps {!r} twice; a source primitive must have exactly "
                "one reviewed mapping".format(path, source_type)
            )
        seen.add(source_type)

    check_obligation_ids(document, path)
    document["_path"] = path
    return document


def template_index(catalogue):
    """Return ``{obligation_id: template}`` defined by the catalogue itself."""
    return {
        entry["id"]: entry for entry in (catalogue.get("obligation_templates") or [])
    }


def check_obligation_ids(catalogue, path=None):
    """Raise if any mapping references an obligation that cannot be resolved."""
    templates = template_index(catalogue)
    unknown = []
    for mapping in catalogue.get("mappings", []):
        for obligation_id in mapping.get("obligations") or []:
            try:
                obligations_module.obligation(obligation_id, templates)
            except KeyError:
                unknown.append((mapping["source_type"], obligation_id))
    if unknown:
        raise CatalogueError(
            "catalogue {} references {} obligation id(s) that are neither defined in "
            "its obligation_templates nor built in: {}".format(
                path or catalogue.get("catalogue_id"),
                len(unknown),
                ", ".join("{} -> {}".format(source, oid) for source, oid in unknown),
            )
        )
    return True


def mappings_by_type(catalogue):
    """Index the catalogue's mappings by source gate type."""
    return {mapping["source_type"]: mapping for mapping in catalogue.get("mappings", [])}


def library_mismatch(inventory, catalogue):
    """Return a message if the catalogue was not written for this gate library.

    Returns ``None`` when the libraries agree or when either side does not name
    one -- an unnamed library is reported as a gap by the caller, not silently
    treated as a match.
    """
    declared = (catalogue.get("source") or {}).get("gate_library")
    actual = ((inventory.get("source") or {}).get("gate_library") or {}).get("name")
    if not declared or not actual:
        return (
            "the catalogue or the inventory does not name a gate library, so it "
            "could not be checked that catalogue {!r} applies to this netlist".format(
                catalogue.get("catalogue_id")
            )
        )
    if declared != actual:
        return (
            "catalogue {!r} was written for gate library {!r} but the inventory was "
            "taken from {!r}; every mapping below is therefore applied across "
            "libraries".format(catalogue.get("catalogue_id"), declared, actual)
        )
    return None


def resolve_mapping(primitive, mapping):
    """Decide the effective category for one inventoried primitive.

    :param primitive: an inventory ``primitives[]`` entry.
    :param mapping: the catalogue mapping for its gate type, or ``None``.
    :returns: a dict with ``category`` (the effective one), ``declared_category``,
        ``missing_metadata``, ``reasons`` and ``downgraded``.
    """
    gate_type = primitive.get("gate_type")
    if mapping is None:
        return {
            "category": "unresolved",
            "declared_category": None,
            "missing_metadata": [],
            "downgraded": False,
            "uncatalogued": True,
            "reasons": [
                "gate type {!r} has no entry in this catalogue; an unlisted primitive "
                "is unresolved, not supported".format(gate_type)
            ],
        }

    declared = mapping.get("category")
    reasons = list(mapping.get("unresolved_reasons") or [])
    missing = [
        field
        for field in (mapping.get("requires_metadata") or [])
        if metadata_value(primitive, field) is None
    ]

    category = declared
    downgraded = False
    if missing:
        category = "unresolved"
        downgraded = declared != "unresolved"
        reasons.append(
            "the inventory does not carry the metadata this mapping requires ({}); "
            "the source netlist or gate library does not describe it, so the "
            "mapping cannot be assessed".format(", ".join(missing))
        )
    if primitive.get("category") == "black_box" and category != "unresolved":
        category = "unresolved"
        downgraded = True
        reasons.append(
            "the source gate library assigns {!r} no properties at all, so there is "
            "no source behaviour a target primitive could be compared "
            "against".format(gate_type)
        )

    return {
        "category": category,
        "declared_category": declared,
        "missing_metadata": missing,
        "downgraded": downgraded,
        "uncatalogued": False,
        "reasons": reasons,
    }
