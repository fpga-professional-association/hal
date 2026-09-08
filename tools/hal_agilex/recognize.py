"""Architecture-dispatched recognition of one arithmetic pattern.

Recognition is kept strictly separate from import: this module takes a netlist
that has already been read and whose primitives have already been inventoried,
and looks for *one* pattern -- an Agilex carry-chain ripple adder, and the
accumulator/counter that a register feedback around it makes.

Dispatch works the way the ``module_identification`` plugin organises its
architectures: a netlist is routed to the recognizer whose primitive namespace
it uses (``tennm_*`` -> Agilex 3).  A netlist from another architecture is not
guessed at; :func:`select_architecture` returns ``None`` and the caller emits an
``unsupported`` finding.

Two kinds of claim come out of it, and they are deliberately different:

* the *pattern* match is structural evidence and is reported as ``heuristic`` --
  a carry chain whose masks are propagate/generate pairs looks like an adder,
  but nothing here proves the surrounding design means addition;
* the *function* check is an exhaustive evaluation of the recognized chain over
  every assignment of its two operands, and is reported as
  ``proven_under_assumptions`` -- it either computes A+B for all inputs or it
  does not.
"""

from . import primitives
from .simulate import Simulator
from .vo_netlist import Bit, Const

from hal_findings import model
from hal_findings.adapters.common import utc_now

__all__ = [
    "PRODUCER",
    "ARCHITECTURES",
    "select_architecture",
    "carry_chains",
    "recognize_adders",
    "check_adder_function",
    "build_document",
]

PRODUCER = {"name": "hal_agilex.recognize", "version": "1.0.0"}

#: architecture id -> the primitive namespace prefix that selects it.
ARCHITECTURES = {"altera_agilex_tennm": "tennm_"}

#: Above this many operand assignments the function check samples instead of
#: enumerating, and downgrades its claim accordingly.
EXHAUSTIVE_LIMIT = 1 << 18


def select_architecture(netlist):
    """Return the architecture id for *netlist*, or ``None`` if unknown."""
    for architecture, prefix in sorted(ARCHITECTURES.items()):
        if any(instance.type.startswith(prefix) for instance in netlist.instances):
            return architecture
    return None


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


def _driver_map(netlist):
    """net key -> (instance, pin) for every net driven by an instance output."""
    drivers = {}
    for instance in netlist.instances:
        for pin in primitives.LCELL_OUTPUT_PINS + ("q",):
            bits = instance.connections.get(pin)
            if not bits:
                continue
            for bit in bits:
                if isinstance(bit, Bit):
                    drivers[bit.key] = (instance, pin)
    return drivers


def carry_chains(netlist):
    """Ordered chains of ``tennm_lcell_comb`` cells linked ``cout`` -> ``cin``."""
    drivers = _driver_map(netlist)
    cells = netlist.instances_of_type(primitives.LCELL)
    successor = {}
    has_predecessor = set()
    for cell in cells:
        cin = cell.single("cin")
        if cin is None or isinstance(cin, Const):
            continue
        driver = drivers.get(cin.key)
        if driver is None or driver[1] != "cout":
            continue
        successor[driver[0].name] = cell
        has_predecessor.add(cell.name)

    by_name = {cell.name: cell for cell in cells}
    chains = []
    for cell in cells:
        if cell.name in has_predecessor:
            continue
        chain = [cell]
        current = cell
        while current.name in successor:
            current = successor[current.name]
            if any(current.name == member.name for member in chain):
                break  # a carry loop is not a chain; stop rather than spin
            chain.append(current)
        if len(chain) > 1:
            chains.append(chain)
    for chain in chains:
        assert all(member.name in by_name for member in chain)
    return chains


def _operand_bits(cell, classification):
    operands = []
    for pin in classification["operands"]:
        bit = cell.single(pin)
        if bit is None or isinstance(bit, Const):
            return None
        operands.append(bit)
    return operands


def recognize_adders(netlist):
    """Recognize ripple-carry adders and accumulators, most significant last.

    Returns a list of dicts with the chain, the per-slice operand nets, the
    width, and -- when the sum of a slice is registered back into one of its own
    operands -- the registers that make it an accumulator/counter.
    """
    drivers = _driver_map(netlist)
    results = []

    for chain in carry_chains(netlist):
        slices = []
        tail = None
        rejected = None
        for position, cell in enumerate(chain):
            classification = primitives.classify_arithmetic_cell(
                cell.parameters.get("lut_mask", 0)
            )
            if classification is None:
                rejected = "cell {} is not an arithmetic slice".format(cell.name)
                break
            if classification["kind"] == "carry_tap":
                if position != len(chain) - 1:
                    rejected = "cell {} taps the carry in the middle of the chain".format(
                        cell.name
                    )
                    break
                tail = cell
                continue
            operands = _operand_bits(cell, classification)
            if operands is None:
                rejected = "cell {} has a constant operand".format(cell.name)
                break
            slices.append(
                {
                    "cell": cell,
                    "operands": operands,
                    "polarity": classification["operand_polarity"],
                }
            )
        if rejected or len(slices) < 2:
            results.append(
                {"chain": chain, "recognized": False, "reason": rejected or "chain too short"}
            )
            continue

        registers = []
        accumulator_operand = []
        for entry in slices:
            sumout = entry["cell"].single("sumout")
            register = None
            if sumout is not None and not isinstance(sumout, Const):
                consumer = _sumout_register(netlist, sumout)
                if consumer is not None:
                    q_bit = consumer.single("q")
                    if q_bit is not None and any(
                        operand.key == q_bit.key for operand in entry["operands"]
                    ):
                        register = consumer
            registers.append(register)
            accumulator_operand.append(register is not None)

        results.append(
            {
                "chain": chain,
                "recognized": True,
                "slices": slices,
                "carry_tap": tail,
                "width": len(slices),
                "registers": registers,
                "is_accumulator": all(accumulator_operand),
                "drivers": drivers,
            }
        )
    return results


def _sumout_register(netlist, sumout_bit):
    for instance in netlist.instances_of_type(primitives.FF):
        data = instance.single("d")
        if data is not None and not isinstance(data, Const) and data.key == sumout_bit.key:
            return instance
    return None


# ---------------------------------------------------------------------------
# function check
# ---------------------------------------------------------------------------


def check_adder_function(netlist, candidate, limit=EXHAUSTIVE_LIMIT):
    """Evaluate a recognized chain against ``A + B`` over its operand space.

    The two operands are identified per slice: the bit that comes from a
    register (for an accumulator) is driven through the register state, the
    other through the netlist inputs.  Returns a dict with ``checked``,
    ``exhaustive`` and either no mismatch or the first counterexample found.
    """
    width = candidate["width"]
    register_bits = []
    input_bits = []
    for entry, register in zip(candidate["slices"], candidate["registers"]):
        first, second = entry["operands"]
        if register is not None:
            q_bit = register.single("q")
            registered, driven = (
                (first, second) if first.key == q_bit.key else (second, first)
            )
        else:
            registered, driven = None, first
        register_bits.append((register, registered))
        input_bits.append(driven)

    if any(register is None for register, _ in register_bits):
        return {
            "checked": 0,
            "exhaustive": False,
            "skipped": (
                "not every slice takes one operand from a register; only the "
                "accumulator shape is checkable without a full input model"
            ),
        }

    simulator = Simulator(netlist)
    input_names = _input_owner(netlist, input_bits)
    if input_names is None:
        return {
            "checked": 0,
            "exhaustive": False,
            "skipped": "the non-registered operand is not a module input vector",
        }
    name, positions = input_names

    total = 1 << (2 * width)
    exhaustive = total <= limit
    assignments = range(1 << width) if exhaustive else _sample(width)

    checked = 0
    for a in range(1 << width):
        if not exhaustive and a % 7 not in (0, 3):
            continue
        for b in assignments:
            simulator.reset()
            for index, (register, _) in enumerate(register_bits):
                simulator.state[register.name] = (a >> index) & 1
            value = 0
            for index, position in enumerate(positions):
                value |= ((b >> index) & 1) << position
            simulator.set_input(name, value)
            simulator.settle()
            got = 0
            for index, entry in enumerate(candidate["slices"]):
                got |= simulator._read(entry["cell"].single("sumout")) << index
            checked += 1
            expected = (a + b) & ((1 << width) - 1)
            if got != expected:
                return {
                    "checked": checked,
                    "exhaustive": exhaustive,
                    "counterexample": {"a": a, "b": b, "expected": expected, "got": got},
                }
    return {"checked": checked, "exhaustive": exhaustive}


def _sample(width):
    step = max(1, (1 << width) // 64)
    return range(0, 1 << width, step)


def _input_owner(netlist, bits):
    """If every bit belongs to one input vector, return (name, bit positions)."""
    names = {bit.name for bit in bits}
    if len(names) != 1:
        return None
    name = names.pop()
    if netlist.declarations.get(name, (None, None, None))[0] != "input":
        return None
    positions = []
    for bit in bits:
        if bit.index is None:
            positions.append(0)
        else:
            positions.append(bit.index)
    return name, positions


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


def build_document(netlist, artifact, artifact_id=None, generated_at=None, limit=EXHAUSTIVE_LIMIT):
    """Recognize arithmetic in *netlist* and report it as findings."""
    artifact_id = artifact_id or artifact["artifact_id"]
    architecture = select_architecture(netlist)

    structural = model.method(
        "carry-chain pattern match",
        "structural",
        False,
        description=(
            "Follows cout->cin links between tennm_lcell_comb cells and checks each "
            "cell's lut_mask against the propagate/generate pair of a full-adder "
            "bit slice."
        ),
        parameters={"architecture": architecture or "unknown"},
    )

    findings = []

    if architecture is None:
        findings.append(
            model.finding(
                "hal_agilex/recognize/architecture-dispatch",
                "No Agilex recognizer applies to this netlist",
                model.STATUS_UNSUPPORTED,
                structural,
                model.scope([artifact_id]),
                summary=(
                    "The netlist instantiates no tennm_* primitive, so the Agilex "
                    "arithmetic recognizer does not apply to it."
                ),
                unsupported_dict=model.unsupported(
                    "configuration",
                    "architecture dispatch found no recognizer for this primitive "
                    "namespace; known: {}".format(", ".join(sorted(ARCHITECTURES))),
                ),
                tags=["agilex", "recognition"],
            )
        )
        return _document(artifact, findings, generated_at)

    candidates = recognize_adders(netlist)
    recognized = [candidate for candidate in candidates if candidate["recognized"]]

    if not recognized:
        findings.append(
            model.finding(
                "hal_agilex/recognize/no-adder",
                "No carry-chain adder recognized",
                model.STATUS_UNKNOWN,
                structural,
                model.scope([artifact_id]),
                summary=(
                    "{} carry chain(s) were followed and none matched the "
                    "ripple-carry adder pattern; this says nothing about what the "
                    "design computes.".format(len(candidates))
                ),
                data={
                    "rejected": [
                        candidate.get("reason") for candidate in candidates
                    ][:10]
                },
                tags=["agilex", "recognition"],
            )
        )
        return _document(artifact, findings, generated_at)

    for number, candidate in enumerate(recognized):
        suffix = "" if len(recognized) == 1 else "/{}".format(number)
        cells = [entry["cell"].name for entry in candidate["slices"]]
        shape = "accumulator/counter" if candidate["is_accumulator"] else "adder"
        findings.append(
            model.finding(
                "hal_agilex/recognize/carry-chain-adder" + suffix,
                "Carry chain recognized as a {}-bit ripple-carry {}".format(
                    candidate["width"], shape
                ),
                model.STATUS_HEURISTIC,
                structural,
                model.scope(
                    [artifact_id],
                    description="carry chain starting at {}".format(cells[0]),
                    gate_types=[primitives.LCELL, primitives.FF],
                ),
                summary=(
                    "{} cells form a carry chain whose masks are all "
                    "propagate/generate pairs{}. Structural evidence only: the "
                    "pattern is what an adder looks like, not a proof that the "
                    "design adds.".format(
                        candidate["width"],
                        ", and every sum bit is registered back into one of its own "
                        "operands" if candidate["is_accumulator"] else "",
                    )
                ),
                confidence=0.7,
                data={
                    "architecture": architecture,
                    "width": candidate["width"],
                    "cells": cells,
                    "carry_tap": candidate["carry_tap"].name if candidate["carry_tap"] else None,
                    "registers": [
                        register.name if register else None
                        for register in candidate["registers"]
                    ],
                    "operand_polarity": sorted(
                        {entry["polarity"] for entry in candidate["slices"]}
                    ),
                },
                tags=["agilex", "recognition"],
            )
        )

        result = check_adder_function(netlist, candidate, limit=limit)
        simulation = model.method(
            "operand-space evaluation",
            "simulation",
            not result.get("exhaustive", False),
            description=(
                "Drives both operands of the recognized chain and compares the sum "
                "bits against A + B, using only the primitive semantics in "
                "hal_agilex.primitives."
            ),
        )
        if result.get("skipped"):
            findings.append(
                model.finding(
                    "hal_agilex/recognize/adder-function" + suffix,
                    "Recognized chain was not function-checked",
                    model.STATUS_UNKNOWN,
                    simulation,
                    model.scope([artifact_id], gate_types=[primitives.LCELL]),
                    summary=result["skipped"],
                    tags=["agilex", "recognition"],
                )
            )
            continue
        if "counterexample" in result:
            witness = result["counterexample"]
            findings.append(
                model.finding(
                    "hal_agilex/recognize/adder-function" + suffix,
                    "Recognized chain does not compute A + B",
                    model.STATUS_COUNTEREXAMPLE,
                    simulation,
                    model.scope([artifact_id], gate_types=[primitives.LCELL]),
                    summary=(
                        "The pattern matched but the evaluation disagrees with "
                        "addition; the recognition is wrong, or the primitive model is."
                    ),
                    severity="high",
                    bounds_dict=model.unbounded(
                        description="a combinational function, evaluated over its own inputs"
                    ),
                    counterexample_dict=model.counterexample(
                        "A={a}, B={b}: expected {expected}, got {got}".format(**witness),
                        witness=[
                            model.witness_entry("A", str(witness["a"])),
                            model.witness_entry("B", str(witness["b"])),
                        ],
                    ),
                    tags=["agilex", "recognition"],
                )
            )
            continue

        if result["exhaustive"]:
            findings.append(
                model.finding(
                    "hal_agilex/recognize/adder-function" + suffix,
                    "Recognized chain computes A + B for every operand pair",
                    model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
                    simulation,
                    model.scope([artifact_id], gate_types=[primitives.LCELL]),
                    summary=(
                        "All {} operand assignments were evaluated and every one "
                        "matches {}-bit addition.".format(result["checked"], candidate["width"])
                    ),
                    bounds_dict=model.unbounded(
                        description=(
                            "the chain is combinational, so enumerating its operand "
                            "space covers every execution of it"
                        )
                    ),
                    assumptions=[
                        model.assumption(
                            "primitive-semantics",
                            "tennm_lcell_comb evaluates as documented in "
                            "hal_agilex.primitives, which was validated by simulating "
                            "this export against the RTL it was synthesised from.",
                            kind="library",
                        ),
                        model.assumption(
                            "operand-identification",
                            "The two operands are the ones the pattern match "
                            "identified; a different operand split is a different claim.",
                            kind="structural",
                        ),
                    ],
                    metrics={"assignments_checked": result["checked"]},
                    tags=["agilex", "recognition"],
                )
            )
        else:
            findings.append(
                model.finding(
                    "hal_agilex/recognize/adder-function" + suffix,
                    "Recognized chain matches A + B on a sample of operand pairs",
                    model.STATUS_HEURISTIC,
                    simulation,
                    model.scope([artifact_id], gate_types=[primitives.LCELL]),
                    summary=(
                        "{} of {} operand assignments were evaluated; the operand "
                        "space is too large to enumerate, so this is evidence, not a "
                        "proof.".format(result["checked"], 1 << (2 * candidate["width"]))
                    ),
                    confidence=0.7,
                    metrics={"assignments_checked": result["checked"]},
                    tags=["agilex", "recognition"],
                )
            )

    return _document(artifact, findings, generated_at)


def _document(artifact, findings, generated_at):
    return model.document(
        PRODUCER,
        [artifact],
        {
            "entry_point": "hal_agilex.recognize.build_document",
            "plugin": {"name": "hal_agilex", "version": "1.0.0"},
        },
        findings,
        generated_at=generated_at or utc_now(),
    )
