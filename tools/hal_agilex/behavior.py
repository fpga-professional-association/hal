"""Validate an export against the RTL it was synthesised from.

This is the evidence the primitive semantics rest on.  Each fixture ships a
``reference.py`` describing the behaviour of its *source* design in plain
Python; this module simulates the *exported netlist* with
:mod:`hal_agilex.simulate` and compares the two.  If the ``lut_mask`` decoding,
the carry equations or the flip-flop model were wrong, a comparison would fail
-- which is exactly what makes the coverage claim checkable rather than
asserted.

A reference module declares::

    KIND = "combinational" | "sequential"
    INPUTS = [("a", 1), ("addend", 8), ...]      # width in bits
    OUTPUTS = [("y0", 1), ("count", 8), ...]
    IGNORED_INPUTS = ["clk"]                     # optional
    ASYNC_CLEAR_INPUT = "rst_n"                  # optional, active low

    def evaluate(values): ...                    # combinational
    def initial_state(): ...                     # sequential
    def outputs(state, values): ...
    def next_state(state, values): ...
"""

import random

from .simulate import Simulator

from hal_findings import model
from hal_findings.adapters.common import utc_now

__all__ = ["PRODUCER", "run_reference_check", "build_document", "load_reference"]

PRODUCER = {"name": "hal_agilex.behavior", "version": "1.0.0"}

DEFAULT_CYCLES = 200


def load_reference(path):
    """Import a fixture ``reference.py`` by file path."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("hal_agilex_reference", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _driveable(reference):
    ignored = set(getattr(reference, "IGNORED_INPUTS", ()))
    return [(name, width) for name, width in reference.INPUTS if name not in ignored]


def run_reference_check(netlist, reference, cycles=DEFAULT_CYCLES, seed=20260908):
    """Compare the exported netlist against the reference model.

    Combinational designs are enumerated exhaustively when their input space is
    at most 2**20 wide; sequential designs are driven for *cycles* pseudo-random
    cycles with the asynchronous clear asserted twice.  Returns a result dict --
    the caller decides what kind of claim that supports.
    """
    simulator = Simulator(netlist)
    inputs = _driveable(reference)
    total_width = sum(width for _, width in inputs)

    if reference.KIND == "combinational":
        exhaustive = total_width <= 20
        space = 1 << total_width
        vectors = range(space) if exhaustive else [
            random.Random(seed).randrange(space) for _ in range(4096)
        ]
        checked = 0
        for vector in vectors:
            values = {}
            offset = 0
            for name, width in inputs:
                values[name] = (vector >> offset) & ((1 << width) - 1)
                offset += width
            for name, value in values.items():
                simulator.set_input(name, value)
            expected = reference.evaluate(values)
            got = {name: simulator.get_output(name) for name, _ in reference.OUTPUTS}
            checked += 1
            if got != expected:
                return {
                    "kind": "combinational",
                    "checked": checked,
                    "exhaustive": exhaustive,
                    "mismatch": {"inputs": values, "expected": expected, "got": got},
                }
        return {
            "kind": "combinational",
            "checked": checked,
            "exhaustive": exhaustive,
            "input_space": space,
        }

    if reference.KIND != "sequential":
        raise ValueError("unknown reference KIND {!r}".format(reference.KIND))

    generator = random.Random(seed)
    clear_input = getattr(reference, "ASYNC_CLEAR_INPUT", None)
    clear_cycles = {cycles // 3, (2 * cycles) // 3}
    state = reference.initial_state()

    simulator.reset()
    if clear_input is not None:
        for name, width in inputs:
            simulator.set_input(name, 0)
        simulator.set_input(clear_input, 0)
        simulator.apply_async_clear()

    for cycle in range(cycles):
        values = {}
        for name, width in inputs:
            values[name] = generator.randrange(1 << width)
        if clear_input is not None:
            values[clear_input] = 0 if cycle in clear_cycles else 1
        for name, value in values.items():
            simulator.set_input(name, value)
        if clear_input is not None and values[clear_input] == 0:
            simulator.apply_async_clear()
            state = reference.initial_state()

        expected = reference.outputs(state, values)
        got = {name: simulator.get_output(name) for name, _ in reference.OUTPUTS}
        if got != expected:
            return {
                "kind": "sequential",
                "checked": cycle,
                "cycles": cycles,
                "mismatch": {
                    "cycle": cycle,
                    "inputs": values,
                    "expected": expected,
                    "got": got,
                },
            }
        simulator.clock()
        state = reference.next_state(state, values)

    return {"kind": "sequential", "checked": cycles, "cycles": cycles}


def build_document(netlist, artifact, result, artifact_id=None, generated_at=None):
    """Turn a :func:`run_reference_check` result into a findings document."""
    artifact_id = artifact_id or artifact["artifact_id"]
    bounded = result["kind"] == "sequential" or not result.get("exhaustive", False)
    method = model.method(
        "netlist versus RTL reference simulation",
        "simulation",
        bounded,
        description=(
            "Simulates the vendor export with hal_agilex.primitives and compares it "
            "against a Python model of the RTL the export was synthesised from."
        ),
    )
    scope = model.scope(
        [artifact_id],
        description="every primitive instance of the export",
        gate_types=sorted({instance.type for instance in netlist.instances}),
    )

    if "mismatch" in result:
        mismatch = result["mismatch"]
        witness = [
            model.witness_entry(name, str(value), cycle=mismatch.get("cycle"))
            for name, value in sorted(mismatch["inputs"].items())
        ]
        if result["kind"] == "sequential":
            finding = model.finding(
                "hal_agilex/behavior/reference-mismatch",
                "The exported netlist does not match the reference model",
                model.STATUS_BOUNDED_COUNTEREXAMPLE,
                method,
                scope,
                summary="Mismatch at cycle {}: expected {}, got {}.".format(
                    mismatch.get("cycle"), mismatch["expected"], mismatch["got"]
                ),
                severity="critical",
                bounds_dict=model.bounded(result["cycles"]),
                counterexample_dict=model.counterexample(
                    "reference/netlist divergence",
                    cycle_bound=mismatch.get("cycle", 0),
                    witness=witness,
                ),
                tags=["agilex", "validation"],
            )
        else:
            finding = model.finding(
                "hal_agilex/behavior/reference-mismatch",
                "The exported netlist does not match the reference model",
                model.STATUS_COUNTEREXAMPLE,
                method,
                scope,
                summary="Mismatch on inputs {}: expected {}, got {}.".format(
                    mismatch["inputs"], mismatch["expected"], mismatch["got"]
                ),
                severity="critical",
                bounds_dict=model.unbounded(
                    description="a combinational design, evaluated over its input space"
                ),
                counterexample_dict=model.counterexample(
                    "reference/netlist divergence", witness=witness
                ),
                tags=["agilex", "validation"],
            )
        return _document(artifact, [finding], generated_at)

    assumptions = [
        model.assumption(
            "reference-model",
            "The Python reference in the fixture directory is a faithful model of "
            "the RTL that was handed to Quartus; both are committed next to each "
            "other so the pair can be reviewed.",
            kind="user_provided",
        ),
        model.assumption(
            "two-valued",
            "The comparison is two-valued: x/z propagation, timing and power-up "
            "behaviour are not modelled.",
            kind="environment",
        ),
    ]

    if result["kind"] == "combinational" and result.get("exhaustive"):
        finding = model.finding(
            "hal_agilex/behavior/reference-equivalence",
            "The exported netlist matches the reference model on every input",
            model.STATUS_PROVEN_UNDER_ASSUMPTIONS,
            method,
            scope,
            summary=(
                "All {} input assignments were evaluated with the modelled primitive "
                "semantics and every one matches the reference.".format(result["checked"])
            ),
            bounds_dict=model.unbounded(
                description="the design is combinational, so its input space is every execution"
            ),
            assumptions=assumptions,
            metrics={"vectors_checked": result["checked"]},
            tags=["agilex", "validation"],
        )
    elif result["kind"] == "sequential":
        finding = model.finding(
            "hal_agilex/behavior/reference-equivalence",
            "The exported netlist matches the reference model for {} cycles".format(
                result["cycles"]
            ),
            model.STATUS_PROVEN_BOUNDED,
            method,
            scope,
            summary=(
                "{} pseudo-random cycles, including asynchronous clears, agree with "
                "the reference model. Nothing is claimed beyond that bound.".format(
                    result["cycles"]
                )
            ),
            bounds_dict=model.bounded(
                result["cycles"],
                description="simulated cycles; longer runs are not covered",
            ),
            assumptions=assumptions,
            metrics={"cycles_checked": result["checked"]},
            tags=["agilex", "validation"],
        )
    else:
        finding = model.finding(
            "hal_agilex/behavior/reference-equivalence",
            "The exported netlist matches the reference model on a sample of inputs",
            model.STATUS_HEURISTIC,
            method,
            scope,
            summary=(
                "{} sampled input assignments agree with the reference; the input "
                "space is too large to enumerate.".format(result["checked"])
            ),
            confidence=0.6,
            metrics={"vectors_checked": result["checked"]},
            tags=["agilex", "validation"],
        )

    return _document(artifact, [finding], generated_at)


def _document(artifact, findings, generated_at):
    return model.document(
        PRODUCER,
        [artifact],
        {
            "entry_point": "hal_agilex.behavior.run_reference_check",
            "plugin": {"name": "hal_agilex", "version": "1.0.0"},
        },
        findings,
        generated_at=generated_at or utc_now(),
    )
