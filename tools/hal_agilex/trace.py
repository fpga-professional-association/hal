"""Record a bounded, seeded value trace of an exported netlist.

:mod:`hal_agilex.behavior` runs the same simulator to answer one question --
"does the export agree with the reference model?" -- and throws every
intermediate value away.  This module keeps them: for each cycle of a bounded
window it records the settled value of *every* net in the export, plus the
stimulus that produced it, as plain JSON.  ``tools/hal_viz clock_step`` joins
that JSON to a ``hal_viz dag`` drawing so a reader can step the design one
clock at a time and watch the values move.

Three properties matter more than convenience here:

*deterministic*
    The stimulus is a seeded :class:`random.Random` stream over the reference
    model's driveable inputs, with the asynchronous clear asserted on a
    declared set of cycles.  Re-running the exporter on the same ``.vo`` with
    the same options reproduces the file byte for byte, which is what lets a
    trace be committed next to the walkthrough it illustrates.

*bounded, and it says so*
    A trace is a window ``[skip, skip + cycles)`` of one particular run.  The
    window, the seed, the held inputs and the clear schedule all travel inside
    the document, so a page built from it can state what it is showing instead
    of implying that it shows the design.

*never guessed*
    A net the simulator cannot resolve -- undriven, or driven by an ``x``
    literal -- is recorded as ``x``, not as 0.  The simulator itself refuses
    any export outside the modelled primitive coverage, so a trace that exists
    at all is a trace of modelled semantics only.
"""

import hashlib
import importlib.util
import random

from . import primitives
from .simulate import SimulationError, Simulator
from .vo_netlist import Bit, Const

__all__ = [
    "SCHEMA",
    "DEFAULT_CYCLES",
    "DEFAULT_SEED",
    "TraceError",
    "load_reference",
    "driveable_inputs",
    "run_trace",
    "differences",
    "dumps",
    "write",
]

#: Version tag carried by every emitted document.
SCHEMA = "hal_agilex/trace/1"

#: Short enough to commit, long enough to show a design doing something.
DEFAULT_CYCLES = 32

#: The same seed :mod:`hal_agilex.behavior` uses, so both are the same run.
DEFAULT_SEED = 20260908

#: ``x``: the simulator could not resolve this net.  Never a guessed 0.
UNKNOWN = "x"

_OUTPUT_PINS = {
    primitives.LCELL: ("combout", "sumout", "cout"),
    primitives.FF: ("q",),
}

_INPUT_PINS = {
    primitives.LCELL: primitives.LCELL_INPUT_PINS,
    primitives.FF: primitives.FF_INPUT_PINS,
}


class TraceError(Exception):
    """The trace cannot be produced as asked."""


def load_reference(path):
    """Import a ``reference.py`` by file path.

    Lives here rather than in :mod:`hal_agilex.behavior` so that tracing needs
    nothing but the standard library; ``behavior`` re-exports it.
    """
    spec = importlib.util.spec_from_file_location("hal_agilex_reference", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def driveable_inputs(reference):
    """The reference model's inputs minus the ones the simulator drives itself.

    ``IGNORED_INPUTS`` is almost always ``["clk"]``: an exported netlist has no
    clock net to wiggle, because :class:`~hal_agilex.simulate.Simulator` steps
    the registers directly.
    """
    ignored = set(getattr(reference, "IGNORED_INPUTS", ()))
    return [(name, width) for name, width in reference.INPUTS if name not in ignored]


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


def _net_bits(netlist):
    """Every net of the export, once, in a stable order.

    Declarations first (sorted by name, then by bit index), then anything an
    instance connects to that was never declared.  A ``.vo`` declares
    everything, so the second pass is a safety net rather than a code path the
    shipped examples take.
    """
    bits = []
    seen = set()
    for name in sorted(netlist.declarations):
        for bit in netlist.bits_of(name):
            if bit.key in seen:
                continue
            seen.add(bit.key)
            bits.append(bit)
    for instance in netlist.instances:
        for pin in sorted(instance.connections):
            for bit in instance.connections[pin]:
                if isinstance(bit, Bit) and bit.key not in seen:
                    seen.add(bit.key)
                    bits.append(Bit(bit.name, bit.index))
    return bits


def _alias_map(netlist):
    """``assign`` targets that are a *plain* copy of another net, per bit.

    ``assign y = ~x`` is deliberately not an alias: it is only used to decide
    which gate to write a primary output's value next to, and a gate on the far
    side of an inversion does not carry that value.  Leaving the port
    unattributed loses a label; attributing it would print the wrong digit.
    """
    aliases = {}
    for target, source in netlist.assignments:
        for target_bit, source_bit in zip(target, source):
            if not isinstance(target_bit, Bit) or not isinstance(source_bit, Bit):
                continue
            if target_bit.inverted or source_bit.inverted:
                continue
            aliases[target_bit.key] = source_bit.key
    return aliases


def _resolve(aliases, key, limit=64):
    """Follow ``assign`` copies to the net that is actually driven."""
    seen = set()
    while key in aliases and key not in seen and len(seen) < limit:
        seen.add(key)
        key = aliases[key]
    return key


def _gate_structure(netlist, index_of, aliases):
    """Per-instance pin -> net index, plus which output ports each one drives."""
    gates = {}
    driver_of = {}
    for instance in netlist.instances:
        outputs = {}
        for pin in _OUTPUT_PINS.get(instance.type, ()):
            bits = instance.connections.get(pin)
            if not bits:
                continue
            bit = bits[0]
            if isinstance(bit, Const) or bit.key not in index_of:
                continue
            outputs[pin] = index_of[bit.key]
            driver_of.setdefault(bit.key, instance.name)
        inputs = {}
        for pin in _INPUT_PINS.get(instance.type, ()):
            bits = instance.connections.get(pin)
            if not bits:
                continue
            bit = bits[0]
            if isinstance(bit, Const) or bit.key not in index_of:
                continue
            inputs[pin] = index_of[bit.key]
        entry = {
            "type": instance.type,
            "inputs": inputs,
            "outputs": outputs,
            "sequential": instance.type == primitives.FF,
        }
        # The node in a hal_viz drawing is the gate, not the pin, so the
        # picture needs one value per gate: the first output the instance
        # actually drives, in the order the primitive declares its pins.
        for pin in _OUTPUT_PINS.get(instance.type, ()):
            if pin in outputs:
                entry["output"] = outputs[pin]
                entry["output_pin"] = pin
                break
        gates[instance.name] = entry

    ports = {"inputs": {}, "outputs": {}}
    for direction in ("input", "output"):
        for name in netlist.ports(direction):
            ports[direction + "s"][name] = [
                index_of[bit.key] for bit in netlist.bits_of(name) if bit.key in index_of
            ]

    # Which gate ultimately drives each primary output bit; an output port is
    # often just an `assign` copy of an internal net, so follow those first.
    for name in netlist.ports("output"):
        port_bits = netlist.bits_of(name)
        for position, bit in enumerate(port_bits):
            driver = driver_of.get(_resolve(aliases, bit.key))
            if driver is None or driver not in gates:
                continue
            label = name if len(port_bits) == 1 else "{}[{}]".format(
                name, bit.index if bit.index is not None else position
            )
            gates[driver].setdefault("drives_ports", []).append(label)
    return gates, ports


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


def _sha256(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_trace(
    netlist,
    reference,
    cycles=DEFAULT_CYCLES,
    seed=DEFAULT_SEED,
    skip=0,
    holds=None,
    clear_cycles=None,
    source_path=None,
    reference_path=None,
):
    """Simulate *netlist* and return a trace document for a bounded window.

    The stimulus follows :func:`hal_agilex.behavior.run_reference_check`: the
    driveable inputs of *reference* are drawn from ``random.Random(seed)``, the
    asynchronous clear (when the model declares one) is asserted before the
    first cycle and again on every cycle in *clear_cycles*, and one recorded
    cycle is "inputs applied, logic settled, registers not yet stepped".

    ``skip`` advances the same stream without recording, so ``skip=100,
    cycles=32`` is a genuine window of one long run rather than a second,
    differently-seeded short one.  ``holds`` pins named inputs to a constant
    for the whole run (a counter that never counts makes a dull picture); it
    may not name the clear input, because the clear already has a schedule.
    """
    if getattr(reference, "KIND", None) != "sequential":
        raise TraceError(
            "only sequential reference models can be traced over cycles; this "
            "one is {!r}".format(getattr(reference, "KIND", None))
        )
    if cycles < 1:
        raise TraceError("a trace needs at least one cycle")
    if skip < 0:
        raise TraceError("--skip cannot be negative")

    holds = dict(holds or {})
    clear_input = getattr(reference, "ASYNC_CLEAR_INPUT", None)
    if clear_input is not None and clear_input in holds:
        raise TraceError(
            "{!r} is the asynchronous clear; schedule it with --clear-cycle "
            "instead of holding it".format(clear_input)
        )
    clear_set = set([0] if clear_cycles is None else clear_cycles)
    if clear_input is None:
        clear_set = set()

    simulator = Simulator(netlist)
    inputs = driveable_inputs(reference)
    known = {name for name, _ in inputs}
    unknown_holds = sorted(set(holds) - known)
    if unknown_holds:
        raise TraceError(
            "--hold names {} which the reference model does not declare as a "
            "driveable input (it declares {})".format(
                ", ".join(unknown_holds), ", ".join(sorted(known)) or "none"
            )
        )

    bits = _net_bits(netlist)
    index_of = {bit.key: position for position, bit in enumerate(bits)}
    aliases = _alias_map(netlist)
    gates, ports = _gate_structure(netlist, index_of, aliases)

    generator = random.Random(seed)
    simulator.reset()
    if clear_input is not None:
        for name, _width in inputs:
            simulator.set_input(name, 0)
        simulator.set_input(clear_input, 0)
        simulator.apply_async_clear()

    output_names = [name for name, _width in getattr(reference, "OUTPUTS", ())]
    frames = []
    for cycle in range(skip + cycles):
        values = {}
        for name, width in inputs:
            values[name] = generator.randrange(1 << width)
        for name, value in holds.items():
            values[name] = value
        if clear_input is not None:
            values[clear_input] = 0 if cycle in clear_set else 1
        for name, value in values.items():
            simulator.set_input(name, value)
        if clear_input is not None and values[clear_input] == 0:
            simulator.apply_async_clear()

        if cycle >= skip:
            simulator.settle()
            frames.append(
                {
                    "cycle": cycle,
                    "inputs": dict(values),
                    "outputs": _read_outputs(simulator, output_names),
                    "values": _sample(simulator, bits),
                }
            )
        simulator.clock()

    document = {
        "schema": SCHEMA,
        "design": netlist.name,
        "source": {"sha256": _sha256(source_path)} if source_path else {},
        "reference": {"kind": "sequential"},
        "stimulus": {
            "seed": seed,
            "generator": "random.Random(seed).randrange(1 << width), one draw "
            "per driveable input per cycle, in declaration order",
            "driveable_inputs": [[name, width] for name, width in inputs],
            "ignored_inputs": sorted(getattr(reference, "IGNORED_INPUTS", ())),
            "held_inputs": {name: holds[name] for name in sorted(holds)},
            "clear_input": clear_input,
            "clear_cycles": sorted(clear_set),
            "cleared_before_first_cycle": clear_input is not None,
        },
        "window": {"start": skip, "cycles": cycles, "end": skip + cycles - 1},
        "nets": [bit.key for bit in bits],
        "gates": gates,
        "ports": ports,
        "frames": frames,
    }
    if source_path is not None:
        document["source"]["path"] = str(source_path).replace("\\", "/")
    if reference_path is not None:
        document["reference"]["path"] = str(reference_path).replace("\\", "/")
    return document


def _read_outputs(simulator, names):
    read = {}
    for name in names:
        try:
            read[name] = simulator.get_output(name)
        except SimulationError:
            read[name] = None
    return read


def _sample(simulator, bits):
    """One character per net: ``0``, ``1`` or ``x``."""
    characters = []
    for bit in bits:
        value = simulator.net_value(bit)
        characters.append(UNKNOWN if value is None else str(value))
    return "".join(characters)


#: What a committed trace has to reproduce.  ``source.sha256`` is deliberately
#: not in the list: a CRLF checkout of the ``.vo`` hashes differently from the
#: LF file the digest was taken over, and that is a property of the checkout,
#: not a trace that failed to reproduce (see the hal-agilex skill's pitfalls).
COMPARED_KEYS = ("nets", "gates", "ports", "stimulus", "window", "frames")


def differences(committed, fresh):
    """The members of a committed trace that a fresh run disagrees with.

    Empty means the committed document reproduces.  Naming the members that
    differ, rather than returning a bool, is what makes a failure diagnosable:
    ``["frames"]`` is a changed simulation, ``["gates"]`` is a changed netlist.
    """
    return [
        key for key in COMPARED_KEYS if committed.get(key) != fresh.get(key)
    ]


def dumps(document):
    """Serialize a trace document: sorted keys, two-space indent, LF, final NL."""
    import json

    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def write(document, path):
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps(document))
    return path
