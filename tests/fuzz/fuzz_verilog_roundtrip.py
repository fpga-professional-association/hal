#!/usr/bin/env python3
"""Property/fuzz harness: Verilog parse -> write -> re-parse must be structure preserving.

The harness generates random *flat* netlists over a small fixture gate library
(``tests/test_utils/gate_libraries/test.hgl`` -- the library the Verilog parser's
own gtest suite uses), emits them as Verilog text, and then checks the property

    parse(text) ~= parse(write(parse(text)))

where ``~=`` is structural equality: the gate-type histogram, the per-gate fan-in
net multiset (pin name -> net name), the per-gate fan-out net multiset, and the
top module's ports by name.

Generated netlists deliberately mix
  * ordinary and escaped identifiers, including escaped identifiers that look
    like number literals (``\\'0'``, ``\\'1'``, ``\\1'b1``) -- the shapes that
    fasm2bels emits and that emsec/hal#545 was about,
  * bus ports in both ``[N:0]`` and ``[0:N]`` notation,
  * wide sized literals in gate parameters (``128'h...``, ``72'o...``, ...).

Everything is deterministic: seeds come from a fixed list, never from the clock.

Usage
-----
    python3 tests/fuzz/fuzz_verilog_roundtrip.py

    FUZZ_SEED=12345 python3 tests/fuzz/fuzz_verilog_roundtrip.py   # one-off repro
    FUZZ_ITERS=200  python3 tests/fuzz/fuzz_verilog_roundtrip.py   # deeper sweep

    # re-enable the generator features that only reproduce the already-known
    # bugs listed in KNOWN_BUGS below (off by default so the sweep keeps
    # hunting for *new* bugs instead of rediscovering these); only KB-4 is
    # still gated this way, the rest have been fixed and now run by default:
    FUZZ_ENABLE_KNOWN_BUG_TRIGGERS=1 python3 tests/fuzz/fuzz_verilog_roundtrip.py

    FUZZ_GATE_LIBRARY=/path/to/lib.hgl                             # override library
    FUZZ_KEEP=1                                                    # keep temp files

The harness exits 0 as long as every failure it sees is an *expected* one, so it
can gate CI. Unexpected failures exit 1 and print a one-line repro command.
"""

import os
import random
import shutil
import sys
import tempfile
import traceback
from collections import Counter
from pathlib import Path

# --------------------------------------------------------------------------- #
# hal_py bootstrap
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parents[2]


def _import_hal_py():
    try:
        import hal_py  # noqa: F401
        return hal_py
    except ImportError:
        pass
    candidates = []
    for env in ("HAL_PY_PATH", "PYTHONPATH"):
        for part in os.environ.get(env, "").split(os.pathsep):
            if part:
                candidates.append(Path(part))
    base = os.environ.get("HAL_BASE_PATH")
    if base:
        candidates.append(Path(base) / "lib")
    candidates.append(REPO_ROOT / "build" / "lib")
    for cand in candidates:
        if (cand / "hal_py.so").exists() or (cand / "hal_py.dylib").exists():
            sys.path.insert(0, str(cand))
            break
    import hal_py  # noqa: F811
    return hal_py


if not os.environ.get("HAL_BASE_PATH") and (REPO_ROOT / "build").is_dir():
    # hal aborts hard ("cannot determine base path") when it is neither
    # installed nor told where the build tree is
    os.environ["HAL_BASE_PATH"] = str(REPO_ROOT / "build")

hal_py = _import_hal_py()
hal_py.plugin_manager.load_all_plugins()


def _gate_library_path():
    override = os.environ.get("FUZZ_GATE_LIBRARY")
    if override:
        return Path(override)
    for rel in (
        "tests/test_utils/gate_libraries/test.hgl",
        "plugins/gate_libraries/definitions/example_library.hgl",
        "plugins/gate_libraries/definitions/AGILEX_TENNM.hgl",
    ):
        cand = REPO_ROOT / rel
        if cand.exists():
            return cand
    raise SystemExit("no gate library found; set FUZZ_GATE_LIBRARY")


GATE_LIBRARY_PATH = _gate_library_path()
GATE_LIBRARY = hal_py.GateLibraryManager.load(str(GATE_LIBRARY_PATH))
if GATE_LIBRARY is None:
    raise SystemExit("failed to load gate library %s" % GATE_LIBRARY_PATH)

# --------------------------------------------------------------------------- #
# seeds / configuration
# --------------------------------------------------------------------------- #

# 25 fixed seeds -- never derived from wall-clock time.
DEFAULT_SEEDS = [
    1, 2, 3, 5, 8, 13, 21, 34, 55, 89,
    1009, 2027, 3041, 4051, 5077, 6091, 7109, 8123, 9137, 10151,
    424242, 606060, 808080, 987654, 1337133,
]


def seed_sequence(count):
    """Deterministic seed sequence of the requested length."""
    seeds = list(DEFAULT_SEEDS)
    extra = random.Random(0xF0117A11)
    while len(seeds) < count:
        seeds.append(extra.randrange(1, 2 ** 31))
    return seeds[:count]


ENABLE_KNOWN_BUG_TRIGGERS = os.environ.get("FUZZ_ENABLE_KNOWN_BUG_TRIGGERS", "") not in ("", "0")
KEEP_TEMP = os.environ.get("FUZZ_KEEP", "") not in ("", "0")

# Generator features that are *only* reachable when the corresponding known bug
# is being investigated. Keeping them off by default stops every second seed
# from rediscovering the same three defects; see KNOWN_BUGS for the minimized
# repros, which run on every invocation regardless of this switch.
FEATURES = {
    # was KB-1 (issue #59, fixed): the writer emitted undeclared
    # HAL_UNUSED_SIGNAL_* identifiers for the unconnected members of a partially
    # connected gate pin group, so its own output did not re-parse. The slot now
    # carries the high-impedance literal, which the parser skips.
    "partial_pin_groups": True,
    # was KB-2 (issue #60, fixed): HAL's internal constant nets '0' / '1' were
    # escaped to \'0' / \'1' on write, which renamed them and collided with
    # genuinely escaped identifiers of that shape. They are written as number
    # literals now.
    "constant_literal_pins": True,
    # was KB-3/KB-5 (issue #61 and issue #60, fixed): a bus port with
    # unconnected bits was re-indexed contiguously and a one-bit bus lost its
    # bus notation, so bit positions shifted on write.
    "sparse_bus_ports": True,
    # KB-4: nets and gate instances share one Verilog namespace, so a gate whose
    # name equals a net name is renamed to ``name__[2]__``. Intended behaviour
    # (see issue #60), kept behind the switch because it makes the round trip
    # non-name-preserving and would otherwise swamp the sweep.
    "name_collisions": ENABLE_KNOWN_BUG_TRIGGERS,
}

# Seeds from DEFAULT_SEEDS that are known to fail with the default feature set.
# Empty: with the known-bug triggers disabled, all 25 default seeds round-trip.
# Add entries as ``seed: "reason"`` when a new real failure is found and
# minimized; the harness then keeps exiting 0 while still reporting the finding.
EXPECTED_FAILURE_SEEDS = {}

# --------------------------------------------------------------------------- #
# gate library model
# --------------------------------------------------------------------------- #


class CellModel(object):
    """Pin-group view of a gate type, in library order."""

    def __init__(self, gate_type):
        self.name = gate_type.get_name()
        self.in_groups = []
        self.out_groups = []
        for group in gate_type.get_pin_groups():
            pins = [p.get_name() for p in group.get_pins()]
            if not pins:
                continue
            direction = group.get_direction()
            entry = (group.get_name(), pins)
            if direction == hal_py.PinDirection.input:
                self.in_groups.append(entry)
            elif direction == hal_py.PinDirection.output:
                self.out_groups.append(entry)

    @property
    def num_inputs(self):
        return sum(len(pins) for _, pins in self.in_groups)


def _build_cell_models():
    models = []
    for gate_type in GATE_LIBRARY.get_gate_types().values():
        model = CellModel(gate_type)
        if not model.out_groups:
            continue
        models.append(model)
    models.sort(key=lambda m: m.name)
    return models


CELLS = _build_cell_models()
CELLS_BY_NAME = {c.name: c for c in CELLS}
# Cells with at least one input: used for the bulk of the random logic.
DRIVEN_CELLS = [c for c in CELLS if c.num_inputs > 0]
# Constant sources (GND/VCC): useful but must stay rare, they mark their output
# net as a gnd/vcc net which changes writer behaviour.
SOURCE_CELLS = [c for c in CELLS if c.num_inputs == 0]

# --------------------------------------------------------------------------- #
# design model + Verilog emission
# --------------------------------------------------------------------------- #


def tok(name):
    """Verilog token for an identifier: escaped identifiers need a trailing space."""
    return name + " " if name.startswith("\\") else name


class Port(object):
    def __init__(self, name, direction, bits=None, ascending=False):
        if bits is not None and name.startswith("\\"):
            # "\name [3]" would swallow the bracket into the escaped identifier
            raise ValueError("bus ports must not use escaped identifiers: %r" % name)
        self.name = name
        self.direction = direction          # "input" / "output"
        self.bits = bits                    # None for scalar, else list of indices
        self.ascending = ascending

    @property
    def is_bus(self):
        return self.bits is not None

    def declaration(self):
        if not self.is_bus:
            return "  %s %s ;" % (self.direction, tok(self.name))
        lo, hi = min(self.bits), max(self.bits)
        left, right = (lo, hi) if self.ascending else (hi, lo)
        return "  %s [%d:%d] %s ;" % (self.direction, left, right, tok(self.name))

    def ref(self, index=None):
        if not self.is_bus:
            return tok(self.name)
        return "%s[%d]" % (tok(self.name).rstrip(), index)


class GateInst(object):
    def __init__(self, name, cell):
        self.name = name
        self.cell = cell
        self.params = []                    # list of (key, verilog_value_text)
        self.conns = []                     # list of (group_name, [ref or None])

    def refs(self):
        for _, refs in self.conns:
            for ref in refs:
                if ref is not None:
                    yield ref

    def emit(self):
        lines = []
        head = "%s" % tok(self.cell.name)
        if self.params:
            lines.append("%s #(" % head)
            lines.append(
                ",\n".join("    .%s(%s)" % (key, value) for key, value in self.params)
            )
            lines.append(") %s (" % tok(self.name))
        else:
            lines.append("%s %s (" % (head, tok(self.name)))
        conn_lines = []
        for group_name, refs in self.conns:
            if len(refs) == 1:
                conn_lines.append("    .%s(%s)" % (tok(group_name).rstrip(), refs[0]))
            else:
                inner = ", ".join(r if r is not None else "1'b0" for r in refs)
                conn_lines.append("    .%s({%s})" % (tok(group_name).rstrip(), inner))
        lines.append(",\n".join(conn_lines))
        lines.append(") ;")
        return "\n".join(lines)


class Design(object):
    def __init__(self, name):
        self.name = name
        self.ports = []
        self.wires = []                     # internal wire names (identifier form)
        self.gates = []

    def clone(self):
        other = Design(self.name)
        other.ports = [
            Port(p.name, p.direction,
                 None if p.bits is None else list(p.bits), p.ascending)
            for p in self.ports
        ]
        other.wires = list(self.wires)
        for gate in self.gates:
            copy = GateInst(gate.name, gate.cell)
            copy.params = list(gate.params)
            copy.conns = [(g, list(r)) for g, r in gate.conns]
            other.gates.append(copy)
        return other

    def used_refs(self):
        used = set()
        for gate in self.gates:
            used.update(gate.refs())
        return used

    def rewrite_refs(self, mapping):
        for gate in self.gates:
            for _, refs in gate.conns:
                for i, ref in enumerate(refs):
                    if ref in mapping:
                        refs[i] = mapping[ref]

    def prune(self, compact_buses=True):
        """Drop wires and ports no gate references any more.

        ``compact_buses`` additionally keeps every surviving bus port dense and
        at least two bits wide. Sparse and one-bit bus ports used to reproduce
        KB-3 and KB-5; both are fixed, so the sweep now leaves them alone and
        this stays only for the ``sparse_bus_ports`` switch to turn back on.
        """
        used = self.used_refs()
        self.wires = [w for w in self.wires if tok(w) in used]
        kept = []
        mapping = {}
        for port in self.ports:
            if port.is_bus:
                bits = [b for b in port.bits if port.ref(b) in used]
                if not bits:
                    continue
                if compact_buses and len(bits) == 1:
                    # a [n:n] bus port is written back as a scalar port named
                    # "<name>(n)" (KB-5) -- degrade it to a scalar ourselves
                    old = port.ref(bits[0])
                    port.bits = None
                    mapping[old] = port.ref()
                elif compact_buses and bits != list(range(len(bits))):
                    for new, old_bit in enumerate(bits):
                        mapping[port.ref(old_bit)] = "%s[%d]" % (
                            tok(port.name).rstrip(), new)
                    port.bits = list(range(len(bits)))
                else:
                    port.bits = bits
                kept.append(port)
            elif port.ref() in used:
                kept.append(port)
        self.ports = kept
        if mapping:
            self.rewrite_refs(mapping)
        return self

    def emit(self):
        lines = []
        port_names = [tok(p.name) for p in self.ports]
        lines.append("module %s (%s) ;" % (tok(self.name), ", ".join(port_names)))
        for port in self.ports:
            lines.append(port.declaration())
        for wire in self.wires:
            lines.append("  wire %s ;" % tok(wire))
        for gate in self.gates:
            lines.append(gate.emit())
        lines.append("endmodule")
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# random generation
# --------------------------------------------------------------------------- #

HEX = "0123456789ABCDEF"
OCT = "01234567"


def _name_shapes(rnd, index):
    """Identifier shapes: ordinary, escaped, and escaped-looks-like-a-literal."""
    shapes = [
        "n_%d" % index,
        "net$%d" % index,
        "_lead%d" % index,
        "NetMixedCase%d" % index,
        "\\n~%d" % index,                   # escaped, illegal char
        "\\n[%d]" % index,                  # escaped, bracket -- not a bus bit
        "\\n.%d" % index,                   # escaped, dot
        "\\%d'b1" % index,                  # escaped, looks like a sized literal
        "\\'%d'" % index,                   # escaped, fasm2bels-style \'0' / \'1' shape
        "\\%dabc" % index,                  # escaped, leading digit
    ]
    return rnd.choice(shapes)


def _param_value(rnd):
    kind = rnd.choice(
        ["hex16", "hex128", "oct72", "bin", "int", "float", "string", "bit"]
    )
    if kind == "hex16":
        return "16'h%s" % "".join(rnd.choice(HEX) for _ in range(4))
    if kind == "hex128":
        return "128'h%s" % "".join(rnd.choice(HEX) for _ in range(32))
    if kind == "oct72":
        return "72'o%s" % "".join(rnd.choice(OCT) for _ in range(24))
    if kind == "bin":
        width = rnd.choice([8, 12, 33, 64])
        return "%d'b%s" % (width, "".join(rnd.choice("01") for _ in range(width)))
    if kind == "int":
        return str(rnd.randint(-64, 64))
    if kind == "float":
        return "%d.%d" % (rnd.randint(0, 9), rnd.randint(1, 9))
    if kind == "string":
        return '"p%d"' % rnd.randint(0, 999)
    return "1'b%d" % rnd.randint(0, 1)


class _NameGen(object):
    def __init__(self, rnd):
        self.rnd = rnd
        self.counter = 0
        self.taken = set()

    def fresh(self):
        while True:
            self.counter += 1
            name = _name_shapes(self.rnd, self.counter)
            key = name.lstrip("\\")
            if key in self.taken:
                continue
            self.taken.add(key)
            return name

    def reserve(self, name):
        self.taken.add(name.lstrip("\\"))


def generate(seed):
    """Build a random flat design. Fully determined by ``seed``."""
    rnd = random.Random(seed)
    design = Design("fuzz_top_%d" % seed)
    names = _NameGen(rnd)

    # ---- input ports -----------------------------------------------------
    sources = []                            # verilog refs usable as a driver
    for i in range(rnd.randint(1, 3)):
        name = "in_%d" % i
        names.reserve(name)
        port = Port(name, "input")
        design.ports.append(port)
        sources.append(port.ref())
    for i in range(rnd.randint(0, 2)):
        name = "ibus_%d" % i
        names.reserve(name)
        width = rnd.randint(2, 5)
        ascending = rnd.random() < 0.5      # [0:N] vs [N:0]
        base = rnd.choice([0, 0, 0, 4]) if FEATURES["sparse_bus_ports"] else 0
        bits = list(range(base, base + width))
        port = Port(name, "input", bits=bits, ascending=ascending)
        design.ports.append(port)
        for bit in bits:
            sources.append(port.ref(bit))

    unused_sources = list(sources)
    rnd.shuffle(unused_sources)

    def pick_source():
        if unused_sources and rnd.random() < 0.8:
            return unused_sources.pop()
        return rnd.choice(sources)

    # ---- gates -----------------------------------------------------------
    n_gates = rnd.randint(3, 24)
    driver_slots = []                       # (gate, group_idx, pin_idx, wire_name)
    for gate_index in range(n_gates):
        if SOURCE_CELLS and rnd.random() < 0.12:
            cell = rnd.choice(SOURCE_CELLS)
        else:
            cell = rnd.choice(DRIVEN_CELLS)
        gate_name = "g%d_%s" % (gate_index, cell.name.lower())
        names.reserve(gate_name)
        if FEATURES["name_collisions"] and design.wires and rnd.random() < 0.2:
            gate_name = design.wires[-1]      # deliberately collides with a net
        gate = GateInst(gate_name, cell)

        if rnd.random() < 0.4:
            for _ in range(rnd.randint(1, 3)):
                gate.params.append(("P%d" % rnd.randint(0, 5), _param_value(rnd)))
            seen = set()
            deduped = []
            for key, value in gate.params:
                if key in seen:
                    continue
                seen.add(key)
                deduped.append((key, value))
            gate.params = deduped

        for group_name, pins in cell.in_groups:
            if len(pins) == 1:
                if rnd.random() < 0.12:
                    continue                # legal: unconnected scalar input pin
                if FEATURES["constant_literal_pins"] and rnd.random() < 0.15:
                    gate.conns.append((group_name, ["1'b%d" % rnd.randint(0, 1)]))
                    continue
                gate.conns.append((group_name, [pick_source()]))
            else:
                if rnd.random() < 0.55:
                    continue                # leave the whole bus pin group open
                count = len(pins)
                if FEATURES["partial_pin_groups"] and rnd.random() < 0.4:
                    count = rnd.randint(1, len(pins) - 1)
                gate.conns.append((group_name, [pick_source() for _ in range(count)]))

        for group_name, pins in cell.out_groups:
            if len(pins) == 1:
                if gate.conns or rnd.random() < 0.8:
                    wire = names.fresh()
                    design.wires.append(wire)
                    gate.conns.append((group_name, [tok(wire)]))
                    driver_slots.append((gate, len(gate.conns) - 1, 0, tok(wire)))
                    sources.append(tok(wire))
                    unused_sources.append(tok(wire))
            else:
                if rnd.random() < 0.6:
                    continue
                refs = []
                for _ in pins:
                    wire = names.fresh()
                    design.wires.append(wire)
                    refs.append(tok(wire))
                    sources.append(tok(wire))
                    unused_sources.append(tok(wire))
                gate.conns.append((group_name, refs))
                for pin_index, ref in enumerate(refs):
                    driver_slots.append((gate, len(gate.conns) - 1, pin_index, ref))

        if gate.conns:
            design.gates.append(gate)

    if not design.gates or not driver_slots:
        # degenerate draw -- fall back to a trivial but valid design
        cell = CELLS_BY_NAME.get("BUF") or DRIVEN_CELLS[0]
        gate = GateInst("g_fallback", cell)
        gate.conns = [
            (cell.in_groups[0][0], [sources[0]]),
            (cell.out_groups[0][0], ["out_0"]),
        ]
        design.gates = [gate]
        design.wires = []
        design.ports.append(Port("out_0", "output"))
        return design

    # ---- output ports ----------------------------------------------------
    rnd.shuffle(driver_slots)
    wanted_scalars = rnd.randint(1, 2)
    wanted_bus = rnd.randint(2, 4) if rnd.random() < 0.5 else 0
    take = min(len(driver_slots), wanted_scalars + wanted_bus)
    chosen = driver_slots[:take]

    def rebind(old_ref, new_ref):
        for gate in design.gates:
            for _, refs in gate.conns:
                for i, ref in enumerate(refs):
                    if ref == old_ref:
                        refs[i] = new_ref
        design.wires = [w for w in design.wires if tok(w) != old_ref]

    scalar_count = min(wanted_scalars, len(chosen))
    for i in range(scalar_count):
        name = rnd.choice(["out_%d" % i, "\\out~%d" % i, "out$%d" % i])
        names.reserve(name)
        port = Port(name, "output")
        design.ports.append(port)
        rebind(chosen[i][3], port.ref())

    rest = chosen[scalar_count:]
    if rest:
        name = "obus_0"
        names.reserve(name)
        ascending = rnd.random() < 0.5
        if FEATURES["sparse_bus_ports"] and len(rest) > 1 and rnd.random() < 0.4:
            bits = sorted(rnd.sample(range(0, len(rest) + 2), len(rest)))
        else:
            bits = list(range(len(rest)))
        port = Port(name, "output", bits=bits, ascending=ascending)
        design.ports.append(port)
        for bit, slot in zip(bits, rest):
            rebind(slot[3], port.ref(bit))

    return design.prune(compact_buses=not FEATURES["sparse_bus_ports"])


# --------------------------------------------------------------------------- #
# structural signature
# --------------------------------------------------------------------------- #


def signature(netlist):
    """Structural fingerprint used for the round-trip equality property."""
    type_histogram = Counter(g.get_type().get_name() for g in netlist.get_gates())

    gates = []
    for gate in netlist.get_gates():
        gate_type = gate.get_type()
        fan_in = []
        for pin in gate_type.get_input_pins():
            net = gate.get_fan_in_net(pin.get_name())
            if net is not None:
                fan_in.append((pin.get_name(), net.get_name()))
        fan_out = []
        for pin in gate_type.get_output_pins():
            net = gate.get_fan_out_net(pin.get_name())
            if net is not None:
                fan_out.append((pin.get_name(), net.get_name()))
        gates.append(
            (gate.get_name(), gate_type.get_name(), tuple(sorted(fan_in)), tuple(sorted(fan_out)))
        )
    gates.sort()

    ports = []
    for group in netlist.get_top_module().get_pin_groups():
        ports.append(
            (
                group.get_name(),
                bool(group.is_ascending()),
                tuple(
                    (p.get_name(), str(p.get_direction()), p.get_net().get_name())
                    for p in group.get_pins()
                ),
            )
        )
    ports.sort()

    return {
        "types": dict(type_histogram),
        "gates": gates,
        "ports": ports,
        "global_in": sorted(n.get_name() for n in netlist.get_global_input_nets()),
        "global_out": sorted(n.get_name() for n in netlist.get_global_output_nets()),
    }


def diff_signature(a, b):
    lines = []
    if a["types"] != b["types"]:
        lines.append("  gate-type histogram: %r != %r" % (a["types"], b["types"]))
    ga = {g[0]: g for g in a["gates"]}
    gb = {g[0]: g for g in b["gates"]}
    for key in sorted(set(ga) | set(gb)):
        if ga.get(key) != gb.get(key):
            lines.append("  gate %r:\n    first : %r\n    second: %r" % (key, ga.get(key), gb.get(key)))
    if a["ports"] != b["ports"]:
        lines.append("  ports:\n    first : %r\n    second: %r" % (a["ports"], b["ports"]))
    for key in ("global_in", "global_out"):
        if a[key] != b[key]:
            lines.append("  %s:\n    first : %r\n    second: %r" % (key, a[key], b[key]))
    return "\n".join(lines) if lines else "  (signatures differ but no field diff -- bug in diff_signature)"


# --------------------------------------------------------------------------- #
# the property
# --------------------------------------------------------------------------- #


class PropertyFailure(Exception):
    def __init__(self, stage, detail):
        Exception.__init__(self, "%s: %s" % (stage, detail))
        self.stage = stage
        self.detail = detail


def check_roundtrip(source, workdir, tag="fuzz"):
    """parse -> write -> parse must preserve the structural signature."""
    first_path = os.path.join(workdir, "%s_1.v" % tag)
    second_path = os.path.join(workdir, "%s_2.v" % tag)
    with open(first_path, "w") as handle:
        handle.write(source)

    first = hal_py.NetlistFactory.load_netlist(first_path, str(GATE_LIBRARY_PATH))
    if first is None:
        raise PropertyFailure("parse#1", "generated Verilog did not parse")

    sig1 = signature(first)
    if not hal_py.NetlistWriterManager.write(first, second_path):
        raise PropertyFailure("write", "NetlistWriterManager.write() returned False")
    written = open(second_path).read()

    second = hal_py.NetlistFactory.load_netlist(second_path, str(GATE_LIBRARY_PATH))
    if second is None:
        raise PropertyFailure("parse#2", "HAL cannot re-parse the Verilog it wrote:\n" + written)

    sig2 = signature(second)
    if sig1 != sig2:
        raise PropertyFailure(
            "structure",
            "round-trip changed the netlist structure:\n%s\n--- written Verilog ---\n%s"
            % (diff_signature(sig1, sig2), written),
        )
    return True


def run_seed(seed, workdir):
    design = generate(seed)
    check_roundtrip(design.emit(), workdir, tag="seed%d" % seed)
    return design


# --------------------------------------------------------------------------- #
# minimization
# --------------------------------------------------------------------------- #


def _still_fails(design, workdir):
    try:
        check_roundtrip(design.emit(), workdir, tag="min")
        return False
    except PropertyFailure:
        return True
    except Exception:
        return True


def minimize(design, workdir, budget=200):
    """Greedy shrink: drop gates (then parameters) while the failure persists."""
    current = design.clone()
    steps = 0
    changed = True
    while changed and steps < budget:
        changed = False
        for index in range(len(current.gates) - 1, -1, -1):
            if steps >= budget:
                break
            steps += 1
            candidate = current.clone()
            del candidate.gates[index]
            candidate.prune(compact_buses=False)
            if candidate.gates and _still_fails(candidate, workdir):
                current = candidate
                changed = True
    for gate in current.gates:
        if not gate.params:
            continue
        candidate = current.clone()
        for other in candidate.gates:
            other.params = []
        if _still_fails(candidate, workdir):
            current = candidate
        break
    return current


# --------------------------------------------------------------------------- #
# known bugs (minimized repros, kept as expected failures)
# --------------------------------------------------------------------------- #

# KB-1 (issue #59), KB-2, KB-5 (issue #60) and KB-3 (issue #61) were fixed and
# their repros are now permanent regression tests in the C++ suites:
#   KB-1, KB-2, KB-5 -> plugins/verilog_writer/test/verilog_writer.cpp
#   KB-3             -> plugins/verilog_writer/test/verilog_writer.cpp and
#                       plugins/verilog_parser/test/verilog_parser.cpp
# The generator features that used to reproduce them are enabled by default in
# FEATURES above, so the sweep keeps covering those shapes.
KNOWN_BUGS = [
    {
        "id": "KB-4",
        "summary": (
            "Nets and gate instances share one Verilog namespace in the writer's "
            "identifier_occurrences map, so a gate whose name equals a net name is renamed "
            "to 'name__[2]__'. INTENDED BEHAVIOUR, documented on issue #60: Verilog puts "
            "nets and instances in one namespace, so the two cannot both keep the name and "
            "renaming the instance is the correct resolution -- the net, which carries the "
            "connectivity, is the one worth preserving. The round trip is therefore "
            "structure-preserving but not name-preserving for such a gate."
        ),
        "expect": "structure",
        "source": (
            "module m (a, c) ;\n"
            "  input a ; output c ; wire x ;\n"
            "INV x (\n"
            "    .I(a),\n"
            "    .O(x)\n"
            ") ;\n"
            "BUF b0 (\n"
            "    .I(x),\n"
            "    .O(c)\n"
            ") ;\n"
            "endmodule\n"
        ),
    },
]


def run_known_bugs(workdir):
    """Run the minimized repros. Returns (xfail, xpass, drifted) lists."""
    xfail, xpass, drifted = [], [], []
    for bug in KNOWN_BUGS:
        try:
            check_roundtrip(bug["source"], workdir, tag=bug["id"].replace("-", "_"))
        except PropertyFailure as failure:
            xfail.append((bug["id"], failure.stage))
            if failure.stage != bug["expect"]:
                drifted.append("%s now fails at %s, not %s"
                               % (bug["id"], failure.stage, bug["expect"]))
        except Exception as exc:  # pragma: no cover - defensive
            xfail.append((bug["id"], "exception: %s" % exc))
        else:
            xpass.append(bug["id"])
    return xfail, xpass, drifted


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #


def repro_command(seed):
    return "FUZZ_SEED=%d python3 tests/fuzz/fuzz_verilog_roundtrip.py" % seed


def main():
    env_seed = os.environ.get("FUZZ_SEED")
    if env_seed:
        seeds = [int(env_seed, 0)]
    else:
        iters = int(os.environ.get("FUZZ_ITERS", str(len(DEFAULT_SEEDS))))
        seeds = seed_sequence(iters)

    workdir = tempfile.mkdtemp(prefix="hal_fuzz_verilog_")
    print("fuzz_verilog_roundtrip: gate library %s" % GATE_LIBRARY_PATH)
    print("fuzz_verilog_roundtrip: %d cell types, %d seeds, known-bug triggers %s"
          % (len(CELLS), len(seeds), "ON" if ENABLE_KNOWN_BUG_TRIGGERS else "off"))

    unexpected = []
    expected = []
    passed = 0
    try:
        for seed in seeds:
            try:
                run_seed(seed, workdir)
            except PropertyFailure as failure:
                if seed in EXPECTED_FAILURE_SEEDS:
                    expected.append((seed, failure.stage))
                    continue
                unexpected.append((seed, failure))
                print("FAIL seed=%d stage=%s" % (seed, failure.stage))
                print("     repro: %s" % repro_command(seed))
                print(failure.detail)
                try:
                    small = minimize(generate(seed), workdir)
                    print("--- minimized repro (seed %d) ---" % seed)
                    print(small.emit())
                except Exception:
                    print("     (minimization failed)")
                    traceback.print_exc()
            except Exception as exc:
                unexpected.append((seed, exc))
                print("ERROR seed=%d: %s" % (seed, exc))
                print("     repro: %s" % repro_command(seed))
                traceback.print_exc()
            else:
                if seed in EXPECTED_FAILURE_SEEDS:
                    print("XPASS seed=%d unexpectedly passed -- drop it from "
                          "EXPECTED_FAILURE_SEEDS" % seed)
                passed += 1

        xfail, xpass, drifted = run_known_bugs(workdir)
    finally:
        if KEEP_TEMP:
            print("fuzz_verilog_roundtrip: temp files kept in %s" % workdir)
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    print("")
    print("known bugs still reproducing (expected failures): %s"
          % (", ".join("%s@%s" % (bug_id, stage) for bug_id, stage in xfail) or "none"))
    if xpass:
        print("known bugs that NO LONGER reproduce -- update KNOWN_BUGS: %s" % ", ".join(xpass))
    for note in drifted:
        print("known bug changed symptom: %s" % note)
    print("seeds: %d passed, %d expected-fail, %d UNEXPECTED"
          % (passed, len(expected), len(unexpected)))

    if unexpected:
        print("")
        print("unexpected failures (real findings, please escalate):")
        for seed, _ in unexpected:
            print("  %s" % repro_command(seed))
        return 1
    print("fuzz_verilog_roundtrip: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
