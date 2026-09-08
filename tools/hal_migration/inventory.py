"""Read a source inventory out of a loaded HAL netlist.

Nothing here imports ``hal_py``.  Every accessor goes through
``hal_findings.adapters.common.call``, so the extractor runs against the real
bindings and against test stubs alike, and a binding that is missing in an
older HAL build degrades to a *recorded gap* rather than a crash.  That
distinction is the point of this module: a migration assessment is only worth
reading if "we did not find a clock pin" is visibly different from "there is no
clock pin".

What is extracted, and from what
--------------------------------
==============================  ============================================
inventory field                 HAL source
==============================  ============================================
``primitives[].properties``     ``GateType.get_property_list()``
``primitives[].category``       derived from those properties only -- never
                                from the gate type *name*
``metadata.*_pins``             ``GateType.get_pins()`` + ``GatePin.get_type()``
``metadata.components``         ``GateType.get_components()``
``metadata.async_set_reset_``   ``FFComponent.get_async_set_reset_behavior()``
``metadata.bit_size``           ``RAMComponent.get_bit_size()``
``metadata.ram_ports``          ``RAMPortComponent`` (data/address group,
                                clock and enable function, write flag)
``metadata.init``               ``InitComponent`` + ``Gate.get_init_data()``
``clock_signals``               nets on input pins of type ``clock``, plus
                                nets driven by an output pin of type ``clock``
``reset_signals``               nets on input pins of type ``reset``/``set``
``io_ports``                    ``Netlist.get_global_input_nets()`` /
                                ``get_global_output_nets()``
==============================  ============================================

Categories are deliberately coarse (``register``, ``memory``, ``arithmetic``,
``io``, ``clock_resource``, ``combinational``, ``constant``, ``black_box``, ...)
and are decided by the gate type properties HAL reports.  A gate type that
carries no property at all is a ``black_box``: HAL knows it exists and nothing
else, which is exactly what a migration assessment has to say about it.
"""

import os

from hal_findings.adapters import common
from hal_findings.serialize import sha256_file

from . import __version__
from .formats import INVENTORY_VERSION

__all__ = [
    "CATEGORIES",
    "categorize",
    "build_inventory",
    "primitive_by_type",
    "metadata_value",
]

#: Every category the inventory format allows, in report order.
CATEGORIES = (
    "register",
    "latch",
    "memory",
    "arithmetic",
    "io",
    "clock_resource",
    "combinational",
    "constant",
    "sequential_other",
    "black_box",
)

#: Gate type properties that decide a category, most specific first. Order
#: matters: a DSP block is also ``sequential``, and a RAM is also ``sequential``.
_CATEGORY_RULES = (
    ("ram", "memory"),
    ("fifo", "memory"),
    ("dsp", "arithmetic"),
    ("io", "io"),
    ("pll", "clock_resource"),
    ("oscillator", "clock_resource"),
    ("ff", "register"),
    ("shift_register", "register"),
    ("latch", "latch"),
    ("power", "constant"),
    ("ground", "constant"),
    ("c_carry", "arithmetic"),
    ("c_half_adder", "arithmetic"),
    ("c_full_adder", "arithmetic"),
)

_MAX_EXAMPLE_GATES = 3


def _enum_name(value):
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value).rsplit(".", 1)[-1]


def _text(value):
    """Render a BooleanFunction (or anything else) as a plain string."""
    if value is None:
        return None
    text = str(value)
    return text or None


def categorize(properties):
    """Map gate type properties to one inventory category.

    Only the properties HAL itself assigns are consulted.  A type without any
    property is a black box -- a name alone is not primitive semantics.
    """
    property_set = set(properties or ())
    if not property_set:
        return "black_box"
    for property_name, category in _CATEGORY_RULES:
        if property_name in property_set:
            return category
    if "sequential" in property_set:
        return "sequential_other"
    if "combinational" in property_set:
        return "combinational"
    return "black_box"


def _pins_by_type(gate_type):
    """Return ``(pins_by_type, directions, gaps)`` for one gate type."""
    pins = common.call(gate_type, "get_pins", default=None)
    if pins is None:
        return {}, {}, ["gate type pins are not readable through this HAL build"]

    by_type = {}
    directions = {}
    for pin in pins:
        name = common.call(pin, "get_name", default="")
        pin_type = _enum_name(common.call(pin, "get_type", default="none"))
        direction = _enum_name(common.call(pin, "get_direction", default="none"))
        by_type.setdefault(pin_type, []).append(name)
        directions.setdefault(direction, []).append(name)
    return by_type, directions, []


def _component_metadata(gate_type, metadata):
    """Fill ``metadata`` from the gate type's components, recording what is absent."""
    components = common.call(gate_type, "get_components", default=None)
    if components is None:
        components = []
    names = []
    ram_ports = []

    for component in components:
        names.append(_enum_name(common.call(component, "get_type", default=type(component).__name__)))

        behavior = common.call(component, "get_async_set_reset_behavior")
        if behavior is not None:
            metadata["async_set_reset_behavior"] = [_enum_name(entry) for entry in behavior]
        for accessor, key in (
            ("get_clock_function", "clock_function"),
            ("get_next_state_function", "next_state_function"),
            ("get_async_reset_function", "async_reset_function"),
            ("get_async_set_function", "async_set_function"),
            ("get_enable_function", "enable_function"),
        ):
            value = _text(common.call(component, accessor))
            if value and key not in metadata:
                metadata[key] = value

        bit_size = common.call(component, "get_bit_size")
        if bit_size is not None:
            metadata["bit_size"] = int(bit_size)

        data_group = common.call(component, "get_data_group")
        if data_group is not None:
            port = {"data_group": str(data_group)}
            address_group = common.call(component, "get_address_group")
            if address_group is not None:
                port["address_group"] = str(address_group)
            is_write = common.call(component, "is_write_port")
            if is_write is not None:
                port["is_write_port"] = bool(is_write)
            clock_function = _text(common.call(component, "get_clock_function"))
            if clock_function:
                port["clock_function"] = clock_function
            enable_function = _text(common.call(component, "get_enable_function"))
            if enable_function:
                port["enable_function"] = enable_function
            ram_ports.append(port)

        init_category = common.call(component, "get_init_category")
        if init_category is not None:
            init = metadata.setdefault("init", {})
            init["category"] = str(init_category)
            identifiers = common.call(component, "get_init_identifier")
            if identifiers is None:
                identifiers = common.call(component, "get_init_identifiers")
            if identifiers is not None:
                init["identifiers"] = sorted(str(entry) for entry in identifiers)

        ascending = common.call(component, "is_init_ascending")
        if ascending is not None:
            metadata["lut_init_ascending"] = bool(ascending)

    if ram_ports:
        metadata["ram_ports"] = sorted(ram_ports, key=lambda port: port["data_group"])
    if names:
        metadata["components"] = sorted(set(names))
    return names


def _instance_metadata(gates, metadata):
    """Fold per-instance information (INIT data, attributes) into ``metadata``.

    ``Gate.get_init_data()`` logs an error for a gate type that has no init
    component, so it is only called for the types the gate library says carry
    one -- a report must not fill the log with errors that mean nothing.
    """
    has_init_component = "init" in metadata
    init_values = set()
    with_init = 0
    data_keys = set()
    for gate in gates:
        init_data = (
            common.call(gate, "get_init_data", default=None) if has_init_component else None
        )
        if init_data:
            with_init += 1
            init_values.add(tuple(str(entry) for entry in init_data))
        data_map = common.call(gate, "get_data_map", default=None) or {}
        try:
            items = data_map.items()
        except AttributeError:
            items = []
        for key, _value in items:
            if isinstance(key, (tuple, list)) and len(key) == 2:
                data_keys.add("{}/{}".format(key[0], key[1]))
            else:
                data_keys.add(str(key))
    if with_init:
        init = metadata.setdefault("init", {})
        init["instances_with_init"] = with_init
        init["distinct_values"] = len(init_values)
    if data_keys:
        metadata["instance_data_keys"] = sorted(data_keys)


def _metadata_gaps_for(category, properties, metadata, component_names):
    """State, per category, what a migration decision needs and HAL did not provide."""
    gaps = []
    components = set(component_names or ())

    if category == "memory":
        if "bit_size" not in metadata:
            gaps.append(
                "no RAMComponent: the memory's bit size is not modelled by the gate "
                "library, so depth/width and target block-RAM fit cannot be derived"
            )
        if "ram_ports" not in metadata:
            gaps.append(
                "no RAMPortComponent: port structure, write-enable semantics and "
                "read-during-write (collision) behaviour are not modelled by the gate "
                "library and must be taken from the vendor documentation"
            )
        if metadata.get("address_pins") and "ram_ports" not in metadata:
            gaps.append(
                "address and data widths would have to be inferred from pin types "
                "alone, which cannot distinguish read from write ports; left unstated"
            )
    if category in ("register", "latch"):
        if metadata.get("clock_pins") and "clock_function" not in metadata:
            gaps.append(
                "clock function not modelled: the active clock edge is unknown"
            )
        if metadata.get("reset_pins") and "async_reset_function" not in metadata:
            gaps.append(
                "the reset pin(s) {} are not modelled as an asynchronous clear; the "
                "gate library encodes the reset in the next-state function only, so "
                "whether the reset is synchronous must be confirmed against the vendor "
                "documentation".format(", ".join(metadata["reset_pins"]))
            )
        if metadata.get("set_pins") and "async_set_function" not in metadata:
            gaps.append(
                "the set pin(s) {} are not modelled as an asynchronous preset; the "
                "synchronicity of the set is not stated by the gate "
                "library".format(", ".join(metadata["set_pins"]))
            )
        if metadata.get("reset_pins") and metadata.get("set_pins"):
            behavior = [str(entry).lower() for entry in metadata.get("async_set_reset_behavior") or []]
            if not behavior or any(entry in ("undef", "undefined", "none") for entry in behavior):
                gaps.append(
                    "the gate library does not state what happens when set and reset "
                    "are asserted together, so the target primitive's priority cannot "
                    "be matched"
                )
        init = metadata.get("init") or {}
        if not init:
            gaps.append(
                "no INIT data and no InitComponent: the power-up state of these "
                "registers is unknown"
            )
        elif not init.get("instances_with_init"):
            gaps.append(
                "the gate type carries an INIT attribute ({}) but no instance in this "
                "netlist sets one; the power-up state of these registers is "
                "unspecified in the source".format(
                    ", ".join(init.get("identifiers") or ["INIT"])
                )
            )
    if category == "arithmetic" and "dsp" in set(properties or ()):
        gaps.append(
            "DSP internals (pipeline registers, rounding, saturation, accumulator "
            "width) are not modelled by the gate library"
        )
    if category == "io":
        gaps.append(
            "I/O standard, drive strength, slew, termination and pin assignment are "
            "constraint-file properties and are not present in the netlist"
        )
    if category == "sequential_other":
        gaps.append(
            "the gate library marks this type as sequential but models no flip-flop, "
            "latch or RAM component, so its state behaviour is not described at all"
        )
    if category == "black_box":
        gaps.append(
            "the gate library assigns this type no properties at all: HAL knows its "
            "pins and nothing about its behaviour"
        )
    return gaps


def _driver_of(net):
    sources = common.call(net, "get_sources", default=None)
    if sources is None:
        return {"kind": "undriven"}
    sources = list(sources)
    if not sources:
        return {"kind": "undriven"}
    if len(sources) > 1:
        return {"kind": "multiple"}
    endpoint = sources[0]
    gate = common.call(endpoint, "get_gate")
    if gate is None:
        return {"kind": "undriven"}
    driver = {"kind": "gate", "gate_name": common.call(gate, "get_name", default="")}
    gate_id = common.call(gate, "get_id")
    if gate_id is not None:
        driver["gate_id"] = int(gate_id)
    type_name = common.gate_type_name(gate)
    if type_name:
        driver["gate_type"] = type_name
    pin_name = common.call(endpoint, "get_pin")
    if pin_name is not None:
        driver["pin"] = common.call(pin_name, "get_name", default=str(pin_name))
    return driver


class _SignalCollector(object):
    """Accumulates per-net facts while the gates are walked once."""

    def __init__(self):
        self.nets = {}

    def add(self, net, pin_type, gate_type_name, is_sink=True):
        if net is None:
            return
        net_id = common.call(net, "get_id")
        if net_id is None:
            return
        entry = self.nets.get(net_id)
        if entry is None:
            entry = {
                "net": net,
                "net_id": int(net_id),
                "net_name": common.call(net, "get_name", default=""),
                "pin_types": set(),
                "sink_gate_types": set(),
                "sink_count": 0,
            }
            self.nets[net_id] = entry
        entry["pin_types"].add(pin_type)
        if is_sink:
            entry["sink_count"] += 1
            if gate_type_name:
                entry["sink_gate_types"].add(gate_type_name)

    def finish(self, global_input_ids):
        signals = []
        for net_id in sorted(self.nets):
            entry = self.nets[net_id]
            signal = {
                "net_id": entry["net_id"],
                "net_name": entry["net_name"],
                "pin_types": sorted(entry["pin_types"]),
                "sink_count": entry["sink_count"],
                "driver": _driver_of(entry["net"]),
            }
            if entry["sink_gate_types"]:
                signal["sink_gate_types"] = sorted(entry["sink_gate_types"])
            if entry["net_id"] in global_input_ids:
                signal["is_global_input"] = True
            signals.append(signal)
        return signals


def _source_artifact(netlist, artifact_id, netlist_path, source_tool, vendor, family,
                     device, hal_version, hal_commit):
    path = netlist_path if netlist_path is not None else common.call(
        netlist, "get_input_filename", default=""
    )
    path = str(path) if path else ""

    source = {"artifact_id": artifact_id}
    if path:
        source["path"] = path
    if path and os.path.isfile(path):
        source["sha256"] = sha256_file(path)
        source["size_bytes"] = os.path.getsize(path)
    elif path and os.path.isdir(path):
        source["unhashed_reason"] = (
            "input is a HAL project directory ({}); hash the archive it was extracted "
            "from to pin it".format(os.path.basename(path))
        )
    else:
        source["unhashed_reason"] = (
            "netlist has no readable source file (in-memory or modified netlist)"
        )

    for key, accessor in (("design_name", "get_design_name"), ("device_name", "get_device_name")):
        value = common.call(netlist, accessor)
        if value:
            source[key] = str(value)
    netlist_id = common.call(netlist, "get_id")
    if netlist_id is not None:
        source["netlist_id"] = int(netlist_id)

    gates = common.call(netlist, "get_gates", default=None)
    nets = common.call(netlist, "get_nets", default=None)
    modules = common.call(netlist, "get_modules", default=None)
    if gates is not None:
        source["gate_count"] = len(list(gates))
    if nets is not None:
        source["net_count"] = len(list(nets))
    if modules is not None:
        source["module_count"] = len(list(modules))

    library = common.call(netlist, "get_gate_library")
    if library is not None:
        gate_library = {}
        name = common.call(library, "get_name")
        if name:
            gate_library["name"] = str(name)
        library_path = common.call(library, "get_path", default="")
        library_path = str(library_path) if library_path else ""
        if library_path:
            gate_library["path"] = library_path
            if os.path.isfile(library_path):
                gate_library["sha256"] = sha256_file(library_path)
        if gate_library:
            source["gate_library"] = gate_library

    if vendor:
        source["vendor"] = str(vendor)
    if family:
        source["family"] = str(family)
    if device:
        source["device"] = str(device)
    if source_tool:
        tool = {key: str(value) for key, value in source_tool.items() if value}
        if tool:
            tool.setdefault("source", "user")
            source["tool"] = tool
    hal = {}
    if hal_version:
        hal["version"] = str(hal_version)
    if hal_commit:
        hal["commit"] = str(hal_commit)
    if hal:
        source["hal"] = hal
    return source


def build_inventory(
    netlist,
    artifact_id="source_netlist",
    netlist_path=None,
    source_tool=None,
    vendor=None,
    family=None,
    device=None,
    hal_version=None,
    hal_commit=None,
    generated_at=None,
    producer_command=None,
    max_example_gates=_MAX_EXAMPLE_GATES,
):
    """Build a source inventory document from a loaded netlist.

    :param netlist: a ``hal_py.Netlist`` (or any object with the same accessors).
    :param artifact_id: the id gate references in this document are scoped to.
    :param source_tool: ``{"name": ..., "version": ...}`` of the tool that
        produced the netlist, as stated by the caller. Never guessed.
    :returns: an inventory document; validate it with
        ``formats.validate(document, "inventory")``.
    """
    gates = list(common.call(netlist, "get_gates", default=[]) or [])

    by_type = {}
    for gate in gates:
        gate_type = common.call(gate, "get_type")
        name = common.call(gate_type, "get_name", default=None) if gate_type else None
        if name is None:
            name = "<unknown>"
        entry = by_type.setdefault(str(name), {"gate_type": gate_type, "gates": []})
        entry["gates"].append(gate)

    global_inputs = list(common.call(netlist, "get_global_input_nets", default=[]) or [])
    global_outputs = list(common.call(netlist, "get_global_output_nets", default=[]) or [])
    global_input_ids = {
        common.call(net, "get_id") for net in global_inputs
    }

    clocks = _SignalCollector()
    resets = _SignalCollector()
    port_facts = {}

    primitives = []
    design_gaps = []
    by_category_gates = {}
    by_category_types = {}

    for type_name in sorted(by_type):
        gate_type = by_type[type_name]["gate_type"]
        type_gates = by_type[type_name]["gates"]
        properties = common.gate_type_properties(gate_type) if gate_type else []
        category = categorize(properties)

        pins_by_type, directions, pin_gaps = _pins_by_type(gate_type)
        metadata = {}
        for pin_type, key in (
            ("clock", "clock_pins"),
            ("reset", "reset_pins"),
            ("set", "set_pins"),
            ("enable", "enable_pins"),
            ("data", "data_pins"),
            ("address", "address_pins"),
            ("io_pad", "io_pad_pins"),
            ("state", "state_pins"),
            ("carry", "carry_pins"),
            ("select", "select_pins"),
        ):
            if pins_by_type.get(pin_type):
                metadata[key] = sorted(pins_by_type[pin_type])
        if directions.get("input"):
            metadata["input_pin_count"] = len(directions["input"])
        if directions.get("output"):
            metadata["output_pin_count"] = len(directions["output"])
        if directions:
            metadata["pin_directions"] = sorted(directions)
        if "c_lut" in set(properties) and directions.get("input"):
            metadata["lut_input_count"] = len(directions["input"])

        component_names = _component_metadata(gate_type, metadata)
        _instance_metadata(type_gates, metadata)

        gaps = list(pin_gaps)
        gaps.extend(_metadata_gaps_for(category, properties, metadata, component_names))

        example_gates = []
        for gate in sorted(type_gates, key=lambda item: common.call(item, "get_id", default=0))[
            :max_example_gates
        ]:
            reference = {
                "id": int(common.call(gate, "get_id", default=0) or 0),
                "name": common.call(gate, "get_name", default=""),
            }
            module = common.call(gate, "get_module")
            if module is not None:
                module_name = common.call(module, "get_name")
                if module_name:
                    reference["module"] = str(module_name)
            if reference["id"] >= 1:
                example_gates.append(reference)

        primitive = {
            "gate_type": type_name,
            "count": len(type_gates),
            "category": category,
            "properties": sorted(properties),
        }
        if metadata:
            primitive["metadata"] = metadata
        if gaps:
            primitive["metadata_gaps"] = gaps
            for gap in gaps:
                design_gaps.append({"scope": type_name, "detail": gap})
        if example_gates:
            primitive["example_gates"] = example_gates
        primitives.append(primitive)

        by_category_gates[category] = by_category_gates.get(category, 0) + len(type_gates)
        by_category_types[category] = by_category_types.get(category, 0) + 1

        # walk the instances once for the clock/reset/port facts
        clock_pins = set(pins_by_type.get("clock", ()))
        reset_pins = set(pins_by_type.get("reset", ())) | set(pins_by_type.get("set", ()))
        input_pins = set(directions.get("input", ()))
        output_pins = set(directions.get("output", ()))
        for gate in type_gates:
            for pin_name in sorted(clock_pins | reset_pins):
                if pin_name in input_pins:
                    net = common.call(gate, "get_fan_in_net", pin_name)
                    collector = clocks if pin_name in clock_pins else resets
                    collector.add(
                        net,
                        "clock" if pin_name in clock_pins else "reset_or_set",
                        type_name,
                    )
                elif pin_name in output_pins and pin_name in clock_pins:
                    # e.g. an oscillator: its output *is* a clock source
                    net = common.call(gate, "get_fan_out_net", pin_name)
                    clocks.add(net, "clock_source", type_name, is_sink=False)
            for net in list(common.call(gate, "get_fan_in_nets", default=[]) or []) + list(
                common.call(gate, "get_fan_out_nets", default=[]) or []
            ):
                net_id = common.call(net, "get_id")
                if net_id is None:
                    continue
                facts = port_facts.setdefault(net_id, {"types": set(), "io": False})
                facts["types"].add(type_name)
                if "io" in set(properties):
                    facts["io"] = True

    # A net that is both a global input and a global output is one inout port,
    # not two ports: HAL records an 'inout' top-level port in both lists.
    directions = {}
    port_nets = {}
    for direction, nets in (("input", global_inputs), ("output", global_outputs)):
        for net in nets:
            net_id = common.call(net, "get_id")
            if net_id is None:
                continue
            directions.setdefault(net_id, set()).add(direction)
            port_nets[net_id] = net

    io_ports = []
    for net_id in sorted(port_nets):
        found = directions[net_id]
        direction = "inout" if len(found) > 1 else sorted(found)[0]
        port = {
            "net_id": int(net_id),
            "net_name": common.call(port_nets[net_id], "get_name", default=""),
            "direction": direction,
        }
        facts = port_facts.get(net_id)
        if facts:
            port["connected_gate_types"] = sorted(facts["types"])
            port["through_io_primitive"] = facts["io"]
        io_ports.append(port)
    io_ports.sort(key=lambda entry: (entry["direction"], entry["net_id"]))

    if not io_ports:
        design_gaps.append(
            {
                "scope": "design",
                "field": "io_ports",
                "detail": "the netlist declares no global input or output nets, so the "
                "design's I/O boundary could not be established",
            }
        )
    if source_tool is None:
        design_gaps.append(
            {
                "scope": "design",
                "field": "source.tool",
                "detail": "the synthesis tool and version that produced this netlist were "
                "not stated (pass --source-tool/--source-tool-version); the inventory "
                "records no tool version rather than assuming one",
            }
        )

    clock_signals = clocks.finish(global_input_ids)
    reset_signals = resets.finish(global_input_ids)

    document = {
        "inventory_version": INVENTORY_VERSION,
        "generated_at": generated_at if generated_at is not None else common.utc_now(),
        "producer": {"name": "hal_migration.inventory", "version": __version__},
        "source": _source_artifact(
            netlist, artifact_id, netlist_path, source_tool, vendor, family, device,
            hal_version, hal_commit,
        ),
        "primitives": primitives,
        "clock_signals": clock_signals,
        "reset_signals": reset_signals,
        "io_ports": io_ports,
        "metadata_gaps": design_gaps,
        "totals": {
            "gates": len(gates),
            "nets": len(list(common.call(netlist, "get_nets", default=[]) or [])),
            "gate_types": len(primitives),
            "by_category": dict(sorted(by_category_gates.items())),
            "gate_types_by_category": dict(sorted(by_category_types.items())),
            "clock_signals": len(clock_signals),
            "reset_signals": len(reset_signals),
            "io_ports": len(io_ports),
            "black_box_types": by_category_types.get("black_box", 0),
            "metadata_gaps": len(design_gaps),
        },
        "notes": [
            "categories are derived from HAL gate type properties only; gate type names "
            "are never interpreted",
            "this inventory describes the source design; it names no target technology "
            "and proposes no mapping",
        ],
    }
    if producer_command:
        document["producer"]["command"] = list(producer_command)
    return document


def primitive_by_type(inventory):
    """Index an inventory's primitives by gate type."""
    return {entry["gate_type"]: entry for entry in inventory.get("primitives", [])}


def metadata_value(primitive, field):
    """Return ``primitive.metadata[field]`` if it is present *and* non-empty.

    Used to decide whether a mapping's ``requires_metadata`` is satisfied: an
    empty list or a zero count is treated as "not known", never as a value.
    """
    metadata = primitive.get("metadata") or {}
    if field not in metadata:
        return None
    value = metadata[field]
    if value in (None, "", [], {}, 0):
        return None
    return value
