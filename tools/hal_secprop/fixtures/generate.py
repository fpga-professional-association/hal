#!/usr/bin/env python3
"""Emit the ``secreg`` access-control fixtures, their policies and their truth.

Same approach as ``tools/hal_apb_recover/fixtures/apb_regs/generate.py``, and
for the same reason: the netlist, the policy and the ground truth all come from
the *one* description below, so they cannot drift apart. Run it after changing
that description and commit every output::

    python tools/hal_secprop/fixtures/generate.py

Everything is instantiated from ``NangateOpenCellLibrary`` (shipped in
``plugins/gate_libraries/definitions``), so HAL reads the result with the stock
Verilog parser and the offline reader in ``hal_apb_recover`` reads the same file
with the same primitive semantics.

**Names are stripped on purpose.** Every port is ``io_NN``, every internal net
``nNNN``, every instance ``gNNN``. Nothing in the netlist says which flip-flop
holds the secret or which one is the lock; that is what the policy file is for,
and it is exactly the sort of list ``hal_apb_recover`` produces when it recovers
a register map from the same kind of netlist.

Three variants, generated from the same builder:

``secreg_ok``
    A lockdown bit at slot 0. Once written to 1 it stays 1 until reset. The
    SECRET register at slot 1 is writable through the APB write path and through
    a second, "debug" write path at slot 2 enabled by ``dbg_en``. **Both** paths
    consult the lock.

``secreg_faulty``
    Identical except for two deliberate defects:

    1. the debug write path does *not* consult the lock -- one AND gate fewer,
       which is exactly how this bug looks in a real design after somebody adds
       a second write port late;
    2. SECRET bit 3 comes out of reset as 1 (a DFFS instead of a DFFR) while the
       policy declares a reset value of 0.

    The cones of the two designs are identical, which is the point: structure
    cannot separate them.

``secreg_blackbox``
    ``secreg_ok`` with PREADY driven by a transparent latch instead of a
    flip-flop. Neither front end models a latch, so the run must report
    ``unsupported`` for every obligation rather than a clean result.
"""

import json
import os

DATA_WIDTH = 4
ADDRESS_WIDTH = 3

SLOT_LOCK = 0
SLOT_SECRET = 1
SLOT_DEBUG = 2

VARIANTS = ("ok", "faulty", "blackbox")

PORTS = [
    ("pclk", "input"),
    ("presetn", "input"),
    ("psel", "input"),
    ("penable", "input"),
    ("pwrite", "input"),
    ("dbg_en", "input"),
]
PORTS += [("paddr{}".format(index), "input") for index in range(ADDRESS_WIDTH)]
PORTS += [("pwdata{}".format(index), "input") for index in range(DATA_WIDTH)]
PORTS += [("prdata{}".format(index), "output") for index in range(DATA_WIDTH)]
PORTS += [("pready", "output")]

PORT_NAMES = {role: "io_{:02d}".format(index) for index, (role, _) in enumerate(PORTS)}


class Builder(object):
    """Collects wires and cell instances and prints them as Verilog."""

    def __init__(self):
        self.wires = []
        self.instances = []
        self._wire_index = 0

    def wire(self):
        name = "n{:03d}".format(self._wire_index)
        self._wire_index += 1
        self.wires.append(name)
        return name

    def instance(self, cell, ports):
        name = "g{:03d}".format(len(self.instances))
        self.instances.append((cell, name, ports))
        return name

    def _one_output(self, cell, ports, output_pin, out):
        target = out if out is not None else self.wire()
        ports = dict(ports)
        ports[output_pin] = target
        self.instance(cell, ports)
        return target

    def inv(self, a, out=None):
        return self._one_output("INV_X1", {"A": a}, "ZN", out)

    def and2(self, a, b, out=None):
        return self._one_output("AND2_X1", {"A1": a, "A2": b}, "ZN", out)

    def or2(self, a, b, out=None):
        return self._one_output("OR2_X1", {"A1": a, "A2": b}, "ZN", out)

    def mux2(self, a, b, s, out=None):
        # Z = (S & B) | (A & !S): B is selected when S is high.
        return self._one_output("MUX2_X1", {"A": a, "B": b, "S": s}, "Z", out)

    def one(self, out=None):
        return self._one_output("LOGIC1_X1", {}, "Z", out)

    def dffr(self, d, rn, ck, out=None):
        return self._one_output("DFFR_X1", {"D": d, "RN": rn, "CK": ck}, "Q", out)

    def dffs(self, d, sn, ck, out=None):
        return self._one_output("DFFS_X1", {"D": d, "SN": sn, "CK": ck}, "Q", out)

    def latch(self, d, g, out=None):
        return self._one_output("DLH_X1", {"D": d, "G": g}, "Q", out)

    def or_reduce(self, terms):
        current = terms[0]
        for term in terms[1:]:
            current = self.or2(current, term)
        return current


def build(variant):
    """Return ``(builder, signals)`` for one variant.

    ``signals`` records the internal nets the policy has to name -- the lock bit
    and the SECRET bits -- because the netlist itself does not say which they
    are.
    """
    if variant not in VARIANTS:
        raise ValueError("unknown variant {!r}".format(variant))
    builder = Builder()
    port = PORT_NAMES

    pclk = port["pclk"]
    presetn = port["presetn"]
    psel = port["psel"]
    penable = port["penable"]
    pwrite = port["pwrite"]
    dbg_en = port["dbg_en"]
    paddr = [port["paddr{}".format(index)] for index in range(ADDRESS_WIDTH)]
    pwdata = [port["pwdata{}".format(index)] for index in range(DATA_WIDTH)]

    psel_en = builder.and2(psel, penable)
    write_phase = builder.and2(psel_en, pwrite)
    read_phase = builder.and2(psel_en, builder.inv(pwrite))

    low = [builder.inv(net) for net in paddr]

    def slot(index):
        literals = [
            paddr[bit] if (index >> bit) & 1 else low[bit] for bit in range(ADDRESS_WIDTH)
        ]
        return builder.and2(builder.and2(literals[0], literals[1]), literals[2])

    slot_lock = slot(SLOT_LOCK)
    slot_secret = slot(SLOT_SECRET)
    slot_debug = slot(SLOT_DEBUG)

    # ---- the lockdown bit: set by a write of 1, cleared only by reset -----
    lock_q = builder.wire()
    set_lock = builder.and2(builder.and2(write_phase, slot_lock), pwdata[0])
    builder.dffr(builder.or2(lock_q, set_lock), presetn, pclk, out=lock_q)
    unlocked = builder.inv(lock_q)

    # ---- the two write paths into SECRET ---------------------------------
    write_secret = builder.and2(write_phase, slot_secret)
    we_main = builder.and2(write_secret, unlocked)

    debug_write = builder.and2(builder.and2(write_phase, slot_debug), dbg_en)
    if variant == "faulty":
        # THE DEFECT: the debug path never consults the lock. One AND gate
        # fewer than the correct variant, and structurally invisible -- the lock
        # still reaches SECRET through we_main, so the fan-in cone is unchanged.
        we_debug = debug_write
    else:
        we_debug = builder.and2(debug_write, unlocked)

    write_enable = builder.or2(we_main, we_debug)

    secret_q = [builder.wire() for _ in range(DATA_WIDTH)]
    for bit in range(DATA_WIDTH):
        data = builder.mux2(secret_q[bit], pwdata[bit], write_enable)
        if variant == "faulty" and bit == DATA_WIDTH - 1:
            # THE SECOND DEFECT: this bit comes out of reset as 1 while the
            # policy declares a reset value of 0.
            builder.dffs(data, presetn, pclk, out=secret_q[bit])
        else:
            builder.dffr(data, presetn, pclk, out=secret_q[bit])

    # ---- read mux: LOCK at slot 0, SECRET at slot 1 ----------------------
    for bit in range(DATA_WIDTH):
        terms = [builder.and2(slot_secret, secret_q[bit])]
        if bit == 0:
            terms.append(builder.and2(slot_lock, lock_q))
        builder.and2(
            read_phase, builder.or_reduce(terms), out=port["prdata{}".format(bit)]
        )

    # ---- PREADY ----------------------------------------------------------
    if variant == "blackbox":
        # A transparent latch: a sequential primitive neither front end models.
        builder.latch(psel_en, builder.one(), out=port["pready"])
    else:
        builder.dffr(psel_en, presetn, pclk, out=port["pready"])

    signals = {"lock": lock_q, "secret": list(secret_q)}
    return builder, signals


def emit_verilog(builder, module):
    lines = [
        "// Generated by tools/hal_secprop/fixtures/generate.py -- do not edit.",
        "// Gate library: NangateOpenCellLibrary "
        "(plugins/gate_libraries/definitions/NangateOpenCellLibrary.hgl)",
        "// All names are stripped: ports are io_NN, nets nNNN, instances gNNN.",
        "",
        "module {} (".format(module),
    ]
    lines.append("    " + ",\n    ".join(PORT_NAMES[role] for role, _ in PORTS))
    lines.append(");")
    for role, direction in PORTS:
        lines.append("  {} {};".format(direction, PORT_NAMES[role]))
    lines.append("")
    for wire in builder.wires:
        lines.append("  wire {};".format(wire))
    lines.append("")
    for cell, name, ports in builder.instances:
        connections = ",\n".join(
            "    .{}({})".format(pin, net) for pin, net in sorted(ports.items())
        )
        lines.append("  {} {} (\n{}\n  );".format(cell, name, connections))
    lines.append("endmodule")
    lines.append("")
    return "\n".join(lines)


def emit_policy(variant, module, signals):
    port = PORT_NAMES
    return {
        "schema": "fpgapa.security-policy",
        "schema_version": "1.0.0",
        "name": module,
        "description": (
            "Analyst-supplied security policy for the name-stripped {} fixture. Nothing "
            "in the netlist says which flip-flop is the lock or which four hold the "
            "secret; this file is the input that makes the question well posed, and it is "
            "the same list tools/hal_apb_recover produces when it recovers a register map "
            "from a netlist of this kind.".format(module)
        ),
        "design": {
            "netlist": "{}.v".format(module),
            "gate_library": "../../../plugins/gate_libraries/definitions/"
            "NangateOpenCellLibrary.hgl",
        },
        "clock": {
            "signal": port["pclk"],
            "edge": "rising",
            "description": "the only clock in the design; one model step is one rising edge",
        },
        "reset": {
            "signal": port["presetn"],
            "active_low": True,
            "cycles": 2,
            "description": "asynchronous, active low, held for the first two cycles of "
            "every run and released afterwards",
        },
        "lock": {
            "signal": signals["lock"],
            "locked_value": 1,
            "description": "the lockdown bit at slot 0: written to 1 through the bus and "
            "cleared only by reset",
        },
        "external_write_controls": {
            "description": "the pins an attacker with bus access can drive",
            "signals": {
                "psel": port["psel"],
                "penable": port["penable"],
                "pwrite": port["pwrite"],
                "dbg_en": port["dbg_en"],
                "paddr": [port["paddr{}".format(i)] for i in range(ADDRESS_WIDTH)],
                "pwdata": [port["pwdata{}".format(i)] for i in range(DATA_WIDTH)],
            },
            "accesses": [
                {
                    "id": "lock_write",
                    "description": "a bus write to slot 0, which engages the lock",
                    "condition": {
                        "psel": 1,
                        "penable": 1,
                        "pwrite": 1,
                        "paddr": _slot_bits(SLOT_LOCK),
                    },
                },
                {
                    "id": "apb_write",
                    "description": "a bus write to slot 1, the SECRET register",
                    "condition": {
                        "psel": 1,
                        "penable": 1,
                        "pwrite": 1,
                        "paddr": _slot_bits(SLOT_SECRET),
                    },
                },
                {
                    "id": "debug_write",
                    "description": "the second write path: a bus write to slot 2 with the "
                    "debug enable asserted",
                    "condition": {
                        "psel": 1,
                        "penable": 1,
                        "pwrite": 1,
                        "dbg_en": 1,
                        "paddr": _slot_bits(SLOT_DEBUG),
                    },
                },
            ],
        },
        "observation_points": [
            {
                "name": "PRDATA[{}]".format(index),
                "signal": port["prdata{}".format(index)],
                "description": "read data bit, recorded in every witness",
            }
            for index in range(DATA_WIDTH)
        ],
        "sensitive_registers": [
            {
                "name": "SECRET",
                "description": "the protected register at slot 1",
                "bits": [
                    {
                        "name": "SECRET[{}]".format(index),
                        "signal": signals["secret"][index],
                        "reset_value": 0,
                    }
                    for index in range(DATA_WIDTH)
                ],
                "properties": ["locked_write", "reset_clears"],
                "write_accesses": ["apb_write", "debug_write"],
            }
        ],
        "environment": {
            "notes": [
                "No input is pinned quiescent: dbg_en in particular is left free, because "
                "an attacker who can reach the bus can reach it too, and pinning it would "
                "assume the defect away."
            ]
        },
        "options": {"bound": 10},
        "notes": [
            "SECRET is readable at slot 1 by design. Read-side confidentiality is not a "
            "property this analysis checks; see the declared exclusions."
        ],
    }


def _slot_bits(index):
    """PADDR value selecting ``index``, least significant bit first."""
    return [(index >> bit) & 1 for bit in range(ADDRESS_WIDTH)]


def emit_ground_truth(signals_by_variant):
    return {
        "description": (
            "Ground truth for the hal_secprop access-control fixtures, written alongside "
            "the generator. The tool must reproduce these verdicts from the netlist and "
            "the policy alone."
        ),
        "gate_library": "NangateOpenCellLibrary",
        "bus": {
            "address_width": ADDRESS_WIDTH,
            "data_width": DATA_WIDTH,
            "slots": {"LOCK": SLOT_LOCK, "SECRET": SLOT_SECRET, "DEBUG": SLOT_DEBUG},
        },
        "signals": signals_by_variant,
        "designs": {
            "secreg_ok": {
                "defects": [],
                "expected": {
                    "exit_code": 0,
                    "obligations": {
                        "secprop/locked-write-blocked/SECRET": {
                            "outcome": "holds_bounded",
                            "status": "proven_bounded",
                            "exercised": True,
                            "why": "both write paths consult the lock, and a write while "
                            "locked is reachable inside the bound, so the pass is not "
                            "vacuous",
                        },
                        "secprop/reset-clears/SECRET": {
                            "outcome": "holds_bounded",
                            "status": "proven_bounded",
                            "why": "every SECRET bit is a DFFR and comes out of reset as 0",
                        },
                        "secprop/lock-integrity": {
                            "outcome": "holds_bounded",
                            "status": "proven_bounded",
                            "why": "the lock bit is set-only until reset",
                        },
                    },
                },
            },
            "secreg_faulty": {
                "defects": [
                    {
                        "id": "missing-lock-check-on-debug-path",
                        "description": "the slot-2 debug write path does not consult the "
                        "lock, so SECRET can be written while locked",
                        "violates": "secprop/locked-write-blocked/SECRET",
                    },
                    {
                        "id": "secret-bit-3-resets-to-one",
                        "description": "SECRET[3] is a DFFS and comes out of reset as 1 "
                        "while the policy declares a reset value of 0",
                        "violates": "secprop/reset-clears/SECRET",
                    },
                ],
                "expected": {
                    "exit_code": 1,
                    "obligations": {
                        "secprop/locked-write-blocked/SECRET": {
                            "outcome": "violated",
                            "status": "bounded_counterexample",
                            "witness": {
                                "replays": True,
                                "requires_accesses": ["lock_write", "debug_write"],
                                "min_violation_cycle": 3,
                                "why": "reset is released at cycle 2, the lock can be "
                                "engaged by a write at cycle 2 at the earliest, so the "
                                "first cycle at which a locked debug write can happen is "
                                "3. Which cycle >= 3 the solver picks is its own business "
                                "and is not part of the ground truth.",
                            },
                        },
                        "secprop/reset-clears/SECRET": {
                            "outcome": "violated",
                            "status": "bounded_counterexample",
                            "witness": {
                                "replays": True,
                                "failing_bits": ["SECRET[3]"],
                                "min_violation_cycle": 1,
                                "why": "reset is asserted for cycles 0 and 1 and "
                                "obligations are instantiated from cycle 1",
                            },
                        },
                        "secprop/lock-integrity": {
                            "outcome": "holds_bounded",
                            "status": "proven_bounded",
                            "why": "the lock itself is not defective in this variant",
                        },
                    },
                },
            },
            "secreg_blackbox": {
                "defects": [
                    {
                        "id": "unmodelled-primitive",
                        "description": "PREADY is driven by a DLH_X1 transparent latch; "
                        "neither front end models a latch",
                        "violates": None,
                    }
                ],
                "expected": {
                    "exit_code": 0,
                    "front_end": "raises UnsupportedPrimitives on both the offline and the "
                    "hal_py path",
                    "obligations": {
                        "secprop/locked-write-blocked/SECRET": {"status": "unsupported"},
                        "secprop/reset-clears/SECRET": {"status": "unsupported"},
                        "secprop/lock-integrity": {"status": "unsupported"},
                    },
                    "unsupported_primitives": ["DLH_X1"],
                },
            },
        },
        "structural_claim": {
            "expected_cone": {
                "target": "SECRET",
                "external_controls_in_cone": 11,
                "input_count": 12,
                "state_count": 5,
                "lock_in_cone": True,
                "max_external_control_depth": 1,
            },
            "statement": "the fan-in cones of SECRET in secreg_ok and secreg_faulty are "
            "identical -- same inputs, same depths, same external controls, and the lock "
            "is in both",
            "why_it_matters": "structural reachability cannot distinguish the correct "
            "design from the broken one, which is why cone results are reported as "
            "candidate paths with status 'heuristic' and never as a verdict",
        },
        "bounded_results_are_not_proofs": (
            "Every 'holds_bounded' above means 'no violating run of at most 10 cycles "
            "exists'. Nothing here is an unbounded proof, and no finding claims one."
        ),
    }


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    signals_by_variant = {}
    for variant in VARIANTS:
        module = "secreg_{}".format(variant)
        builder, signals = build(variant)
        signals_by_variant[module] = signals
        with open(
            os.path.join(here, "{}.v".format(module)), "w", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(emit_verilog(builder, module))
        with open(
            os.path.join(here, "{}.policy.json".format(module)),
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            json.dump(emit_policy(variant, module, signals), handle, indent=2, sort_keys=True)
            handle.write("\n")
        print(
            "{}.v: {} instances, {} internal nets".format(
                module, len(builder.instances), len(builder.wires)
            )
        )

    with open(
        os.path.join(here, "ground_truth.json"), "w", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(emit_ground_truth(signals_by_variant), handle, indent=2, sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    main()
