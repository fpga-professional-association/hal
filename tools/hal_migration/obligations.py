"""Verification obligations: what a migration still owes after the inventory.

An obligation is a check that has *not* been performed.  This tool produces
obligations and never discharges them -- a structural match between a source
primitive and a target primitive says nothing about behaviour, and nothing at
all about timing.  Two consequences are wired in here rather than left to the
report writer:

* every obligation carries the ``target_assumption`` that is being made while
  it is open, so an assessment that is read as a plan still shows what it took
  on trust;
* :data:`ALWAYS_OPEN` obligations are attached to every assessment regardless
  of the design, so no report can be produced that omits timing closure,
  resource fit and whole-design functional equivalence.

Obligation kinds are ``semantic`` (a behavioural difference that has to be
checked functionally) and ``physical`` (a resource, I/O, clocking or timing
property that has to be checked in the target implementation flow).

Catalogues may add their own templates; ids here are the shared vocabulary so
that two catalogues written by different reviewers still produce comparable
reports.
"""

__all__ = [
    "BUILTIN_OBLIGATIONS",
    "ALWAYS_OPEN",
    "obligation",
    "resolve",
    "derive_for_primitive",
    "derive_design_level",
]


def _template(obligation_id, kind, title, description, check, severity, target_assumption):
    return {
        "id": obligation_id,
        "kind": kind,
        "title": title,
        "description": description,
        "check": check,
        "severity": severity,
        "target_assumption": target_assumption,
    }


#: The shared obligation vocabulary. Catalogue templates with the same id win.
BUILTIN_OBLIGATIONS = {
    entry["id"]: entry
    for entry in [
        _template(
            "semantic/combinational-equivalence",
            "semantic",
            "Combinational function equivalence",
            "The source primitive's Boolean function must be reproduced by the target "
            "primitive, including the bit order of any INIT/truth-table encoding.",
            "Prove equivalence of the source and target cell functions (for LUTs: "
            "compare the truth tables after applying the target's INIT bit order), or "
            "run an equivalence check over the converted netlist.",
            "high",
            "the target LUT/gate implements exactly the same Boolean function",
        ),
        _template(
            "semantic/register-clocking",
            "semantic",
            "Register clock edge and enable semantics",
            "Active clock edge, clock-enable priority and the behaviour of a disabled "
            "register must match between source and target flip-flop.",
            "Compare the source and target flip-flop definitions (clock function, "
            "enable function) and simulate a register with enable deasserted across a "
            "clock edge.",
            "high",
            "the target flip-flop samples on the same clock edge and treats enable "
            "with the same priority",
        ),
        _template(
            "semantic/async-set-reset-priority",
            "semantic",
            "Asynchronous set/reset polarity, priority and recovery",
            "Polarity, synchronous vs asynchronous behaviour, and the state taken when "
            "set and reset are asserted together differ between technologies.",
            "Compare the source FFComponent set/reset behaviour with the target "
            "primitive's documented behaviour and simulate simultaneous assertion; "
            "check recovery/removal timing in the target flow.",
            "high",
            "the target flip-flop resolves simultaneous set and reset the same way "
            "and has the same reset polarity",
        ),
        _template(
            "semantic/power-up-and-init-state",
            "semantic",
            "Power-up and INIT state",
            "The state a register or memory holds before the first clock edge is a "
            "device property; INIT attributes may not survive a technology change.",
            "Confirm the target primitive supports the same initial value, and that "
            "the target flow preserves it; simulate reset-free start-up.",
            "high",
            "the target device powers up in the same state and honours the same INIT "
            "attribute",
        ),
        _template(
            "semantic/memory-collision-mode",
            "semantic",
            "Memory read-during-write / collision behaviour",
            "Read-during-write behaviour (old data, new data, undefined) and "
            "cross-port collision behaviour are memory-block properties that a netlist "
            "does not record.",
            "Obtain the source block's collision mode from the vendor documentation or "
            "the synthesis attributes, select the matching target mode explicitly, and "
            "simulate simultaneous read and write at the same address.",
            "critical",
            "the target memory can be configured with the same read-during-write and "
            "collision behaviour",
        ),
        _template(
            "semantic/memory-init-contents",
            "semantic",
            "Memory initialisation contents",
            "Initial memory contents are carried as attributes and are re-encoded "
            "differently by each vendor's memory primitive.",
            "Compare the source INIT attributes with the target memory's expected "
            "initialisation format, including word order and bit order.",
            "high",
            "the target memory accepts the same initialisation contents in a "
            "convertible encoding",
        ),
        _template(
            "physical/memory-depth-width-fit",
            "physical",
            "Memory depth/width fit and cascading",
            "A target memory block of a different depth/width forces cascading or "
            "padding, which changes resource count and timing.",
            "Map the required depth and width onto the target block RAM geometry and "
            "record how many blocks and what cascade logic result.",
            "medium",
            "the required memory geometry fits the target blocks without changing "
            "behaviour",
        ),
        _template(
            "semantic/arithmetic-carry-chain",
            "semantic",
            "Carry chain and arithmetic semantics",
            "Carry-chain primitives are placement-constrained and their carry-in/out "
            "conventions differ; a structural match does not preserve arithmetic.",
            "Re-infer the arithmetic from the source netlist and let the target "
            "toolchain implement it, then check equivalence of the arithmetic function.",
            "high",
            "the target technology implements the same arithmetic with its own carry "
            "structure",
        ),
        _template(
            "semantic/dsp-behaviour",
            "semantic",
            "DSP/MAC block behaviour",
            "Pipeline depth, rounding, saturation, accumulator width and hold/load "
            "controls of a hard MAC block are not modelled in the netlist.",
            "Extract the source block's configuration from the vendor documentation "
            "and attributes, configure the target DSP accordingly, and compare "
            "cycle-accurate behaviour including latency.",
            "critical",
            "the target DSP can be configured to the same latency, width and rounding "
            "behaviour",
        ),
        _template(
            "physical/io-standard-and-drive",
            "physical",
            "I/O standard, drive strength and termination",
            "I/O electrical properties live in constraint files, not in the netlist, "
            "and target banks support a different set of standards.",
            "Carry the source I/O constraints over explicitly and check each standard, "
            "drive strength, slew rate and termination against the target bank rules.",
            "high",
            "the target device offers a compatible I/O standard in the assigned bank",
        ),
        _template(
            "physical/io-pin-assignment",
            "physical",
            "Pin assignment and bank rules",
            "Pin numbers, bank voltages and dedicated-pin rules are device specific.",
            "Re-do pin assignment for the target package and check bank voltage "
            "compatibility and dedicated-pin usage.",
            "high",
            "the target package can host the same interface with compatible banking",
        ),
        _template(
            "semantic/io-registered-path",
            "semantic",
            "Registered and DDR I/O paths",
            "I/O primitives can contain input/output registers and DDR modes whose "
            "presence changes latency by a cycle.",
            "Compare the registered/DDR configuration of the source I/O primitive with "
            "the target I/O block and check the resulting latency.",
            "medium",
            "the target I/O block provides the same registered/DDR structure",
        ),
        _template(
            "physical/clock-network-and-buffering",
            "physical",
            "Clock network, buffering and skew",
            "Global buffers, clock regions and skew budgets differ per device; a "
            "one-to-one buffer mapping is not a clocking strategy.",
            "Re-plan the clock network for the target device: number of global "
            "resources, region reach, and insertion of target clock buffers.",
            "high",
            "the target device has enough global clock resources with acceptable skew",
        ),
        _template(
            "semantic/clock-domain-crossings",
            "semantic",
            "Clock domain crossings",
            "Every crossing between the clock domains found in the inventory must be "
            "re-verified after migration; synchroniser depth is technology dependent.",
            "Enumerate the crossings between the inventoried clock domains and check "
            "each synchroniser against the target metastability guidance.",
            "high",
            "the target technology's metastability behaviour is covered by the "
            "existing synchronisers",
        ),
        _template(
            "physical/clock-source-availability",
            "physical",
            "On-chip clock source availability",
            "Internal oscillators and PLLs have device-specific frequencies, accuracy "
            "and start-up behaviour.",
            "Confirm the target device provides an equivalent clock source, including "
            "frequency tolerance and start-up time, or plan an external source.",
            "high",
            "the target device provides a clock source with acceptable frequency and "
            "accuracy",
        ),
        _template(
            "semantic/black-box-behaviour",
            "semantic",
            "Black-box behaviour is unknown",
            "The gate library assigns the primitive no behaviour, so neither this tool "
            "nor any structural comparison can say what it does.",
            "Obtain the primitive's specification from the vendor, then decide the "
            "mapping manually; treat any assessment of it as unresolved until then.",
            "critical",
            "none: no assumption about this primitive is justified",
        ),
        _template(
            "semantic/hard-ip-replacement",
            "semantic",
            "Hard IP replacement",
            "Hard IP blocks (I2C, SPI, PLL, oscillators, ...) have no portable "
            "equivalent; replacing them changes the register map and the protocol "
            "timing.",
            "Choose a target implementation (hard block or soft core), re-verify the "
            "protocol against the specification, and check the software-visible "
            "register map.",
            "critical",
            "a target implementation with an equivalent programming model exists",
        ),
        _template(
            "physical/timing-closure",
            "physical",
            "Timing closure in the target device",
            "Timing is a property of the target implementation, not of a structural "
            "mapping. No result in this report is evidence that the design will meet "
            "timing after migration.",
            "Re-run synthesis, placement and static timing analysis for the target "
            "device with the migrated constraints.",
            "critical",
            "none: timing was not assessed",
        ),
        _template(
            "physical/resource-fit",
            "physical",
            "Resource fit in the target device",
            "Primitive counts in the source do not translate one-to-one into target "
            "resources; LUT size, register packing and hard-block counts all differ.",
            "Map the inventory onto the target device's resource budget and confirm "
            "the design fits with the intended utilisation margin.",
            "high",
            "the target device has enough resources of each kind",
        ),
        _template(
            "semantic/functional-equivalence",
            "semantic",
            "Whole-design functional equivalence",
            "Per-primitive mappings do not compose into a proof about the design.",
            "Run an equivalence check or a directed regression between the source "
            "design and the migrated design once a conversion exists.",
            "critical",
            "none: no conversion was performed, so nothing was compared",
        ),
    ]
}

#: Obligations attached to every assessment, whatever the design contains.
ALWAYS_OPEN = (
    "physical/timing-closure",
    "physical/resource-fit",
    "semantic/functional-equivalence",
)

#: category -> obligations that always apply to a primitive of that category.
_CATEGORY_OBLIGATIONS = {
    "register": ("semantic/register-clocking", "semantic/async-set-reset-priority"),
    "latch": ("semantic/async-set-reset-priority",),
    "memory": (
        "semantic/memory-collision-mode",
        "semantic/memory-init-contents",
        "physical/memory-depth-width-fit",
    ),
    "arithmetic": ("semantic/arithmetic-carry-chain",),
    "io": (
        "physical/io-standard-and-drive",
        "physical/io-pin-assignment",
        "semantic/io-registered-path",
    ),
    "clock_resource": ("physical/clock-source-availability", "physical/clock-network-and-buffering"),
    "combinational": ("semantic/combinational-equivalence",),
    "black_box": ("semantic/black-box-behaviour",),
}

#: gate type property -> extra obligations.
_PROPERTY_OBLIGATIONS = {
    "dsp": ("semantic/dsp-behaviour",),
    "pll": ("physical/clock-source-availability",),
    "oscillator": ("physical/clock-source-availability",),
    "c_lut": ("semantic/combinational-equivalence",),
}


def obligation(obligation_id, templates=None):
    """Resolve one obligation id against catalogue templates, then the built-ins."""
    if templates and obligation_id in templates:
        return templates[obligation_id]
    if obligation_id in BUILTIN_OBLIGATIONS:
        return BUILTIN_OBLIGATIONS[obligation_id]
    raise KeyError(
        "unknown obligation id {!r}; define it in the catalogue's "
        "obligation_templates or use one of the built-in ids: {}".format(
            obligation_id, ", ".join(sorted(BUILTIN_OBLIGATIONS))
        )
    )


def resolve(obligation_ids, templates=None, origin=None):
    """Materialise obligation ids into records, de-duplicated and sorted."""
    resolved = {}
    for obligation_id in obligation_ids:
        entry = dict(obligation(obligation_id, templates))
        entry.setdefault("severity", "medium")
        entry["status"] = "open"
        if origin:
            entry["origin"] = origin.get(obligation_id, "derived")
        resolved[entry["id"]] = entry
    return [resolved[key] for key in sorted(resolved)]


def derive_for_primitive(primitive):
    """Obligations implied by what the inventory says about one primitive.

    These are independent of the catalogue: they follow from the metadata that
    was found (a clock pin, a reset pin, INIT data, ...) or from the category,
    so a catalogue that forgets an obligation cannot make it disappear.
    """
    category = primitive.get("category")
    properties = set(primitive.get("properties") or ())
    metadata = primitive.get("metadata") or {}

    derived = set(_CATEGORY_OBLIGATIONS.get(category, ()))
    for property_name, ids in _PROPERTY_OBLIGATIONS.items():
        if property_name in properties:
            derived.update(ids)

    stateful = category in ("register", "latch", "memory") or "sequential" in properties
    if metadata.get("clock_pins"):
        # every clock has to be re-planned in the target; only a *stateful*
        # primitive additionally owes a clock-edge/enable comparison
        derived.add("physical/clock-network-and-buffering")
        if stateful:
            derived.add("semantic/register-clocking")
    if metadata.get("reset_pins") or metadata.get("set_pins"):
        derived.add("semantic/async-set-reset-priority")
    if stateful:
        # a known INIT has to survive the migration; an unknown one is itself a
        # reason to check the power-up state
        derived.add("semantic/power-up-and-init-state")
    if metadata.get("io_pad_pins"):
        derived.add("physical/io-standard-and-drive")
        derived.add("physical/io-pin-assignment")
    return sorted(derived)


def derive_design_level(inventory):
    """Design-wide obligations, including the ones that are always open."""
    derived = set(ALWAYS_OPEN)
    if len(inventory.get("clock_signals") or []) > 1:
        derived.add("semantic/clock-domain-crossings")
    if inventory.get("clock_signals"):
        derived.add("physical/clock-network-and-buffering")
    if inventory.get("reset_signals"):
        derived.add("semantic/async-set-reset-priority")
    if inventory.get("io_ports"):
        derived.add("physical/io-standard-and-drive")
        derived.add("physical/io-pin-assignment")
    for primitive in inventory.get("primitives") or []:
        if primitive.get("category") == "memory":
            derived.add("semantic/memory-collision-mode")
    return sorted(derived)
