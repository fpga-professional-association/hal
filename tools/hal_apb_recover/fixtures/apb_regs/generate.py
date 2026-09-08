#!/usr/bin/env python3
"""Emit the ``apb_regs`` fixture: a gate-level APB peripheral and its truth.

The fixture is generated rather than typed out so that the netlist and the
ground truth can never drift apart -- both come from the same description
below.  Run it after changing that description and commit both outputs:

    python tools/hal_apb_recover/fixtures/apb_regs/generate.py

Everything is instantiated from ``NangateOpenCellLibrary`` (shipped in
``plugins/gate_libraries/definitions``), so HAL can read the result with the
stock Verilog parser and no extra library is needed.

**Names are stripped on purpose.**  Every top-level port is called ``io_NN``,
every internal net ``nNNN`` and every instance ``gNNN``.  Nothing in the netlist
says which port is PADDR and which is PWDATA; that is what the user-supplied
``mapping.json`` is for, and recovering the register map from the remaining
structure is the point of the exercise.
"""

import json
import os

DATA_WIDTH = 16
ADDRESS_WIDTH = 5
STROBE_LANES = 2
BYTE_LANE_BITS = 8
STATUS_WIDTH = 8

#: read-only identification constant at 0x0C
ID_VALUE = 0xA53C
#: reset value of the scratch register at 0x10
SCRATCH_RESET = 0xBEEF

#: word slot (PADDR[4:2]) -> what it selects.  Slot 0 and slot 2 share a decode
#: line, which is the intentional address alias 0x00 == 0x08.
SLOT_CTRL = (0, 2)
SLOT_STATUS = 1
SLOT_ID = 3
SLOT_SCRATCH = 4

PORTS = [
    ("pclk", "input"),
    ("presetn", "input"),
    ("psel", "input"),
    ("penable", "input"),
    ("pwrite", "input"),
    ("pready", "output"),
]
PORTS += [("paddr{}".format(index), "input") for index in range(ADDRESS_WIDTH)]
PORTS += [("pwdata{}".format(index), "input") for index in range(DATA_WIDTH)]
PORTS += [("prdata{}".format(index), "output") for index in range(DATA_WIDTH)]
PORTS += [("pstrb{}".format(index), "input") for index in range(STROBE_LANES)]
PORTS += [("evt{}".format(index), "input") for index in range(STATUS_WIDTH)]
PORTS += [("hw_clr", "input")]

PORT_NAMES = {role: "io_{:02d}".format(index) for index, (role, _) in enumerate(PORTS)}
PORT_ROLES = {name: role for role, name in PORT_NAMES.items()}


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

    # -- cells ---------------------------------------------------------------

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
        if not terms:
            raise ValueError("or_reduce needs at least one term")
        current = terms[0]
        for term in terms[1:]:
            current = self.or2(current, term)
        return current


def build():
    builder = Builder()
    port = PORT_NAMES

    pclk = port["pclk"]
    presetn = port["presetn"]
    psel = port["psel"]
    penable = port["penable"]
    pwrite = port["pwrite"]
    paddr = [port["paddr{}".format(index)] for index in range(ADDRESS_WIDTH)]
    pwdata = [port["pwdata{}".format(index)] for index in range(DATA_WIDTH)]
    pstrb = [port["pstrb{}".format(index)] for index in range(STROBE_LANES)]
    evt = [port["evt{}".format(index)] for index in range(STATUS_WIDTH)]

    not_pwrite = builder.inv(pwrite)
    psel_en = builder.and2(psel, penable)
    write_phase = builder.and2(psel_en, pwrite)
    read_phase = builder.and2(psel_en, not_pwrite)

    # PADDR[1:0] are not decoded: the peripheral is word-addressed. They still
    # have to reach *some* pin, because HAL's Verilog parser deletes a net with
    # neither a source nor a destination -- the mapping could then not name
    # them. This XOR drives nothing and changes nothing; it only keeps the two
    # nets alive so the recovery can report them as undecoded.
    builder.instance(
        "XOR2_X1", {"A": paddr[0], "B": paddr[1], "Z": builder.wire()}
    )

    high = [paddr[2], paddr[3], paddr[4]]
    low = [builder.inv(net) for net in high]

    def slot(index):
        literals = [
            high[bit] if (index >> bit) & 1 else low[bit] for bit in range(3)
        ]
        return builder.and2(builder.and2(literals[0], literals[1]), literals[2])

    slots = {index: slot(index) for index in sorted(set(SLOT_CTRL) | {SLOT_STATUS, SLOT_ID, SLOT_SCRATCH})}
    ctrl_sel = builder.or2(slots[SLOT_CTRL[0]], slots[SLOT_CTRL[1]])
    status_sel = slots[SLOT_STATUS]
    id_sel = slots[SLOT_ID]
    scratch_sel = slots[SLOT_SCRATCH]

    write_ctrl = builder.and2(write_phase, ctrl_sel)
    we_ctrl = [builder.and2(write_ctrl, pstrb[lane]) for lane in range(STROBE_LANES)]

    write_status = builder.and2(write_phase, status_sel)
    we_status = builder.and2(write_status, pstrb[0])

    # Each register's flip-flops feed their own hold multiplexer, so allocate
    # the state nets first and wire the data path afterwards.
    ctrl_q = [builder.wire() for _ in range(DATA_WIDTH)]
    not_hw_clr = builder.inv(port["hw_clr"])
    for bit in range(DATA_WIDTH):
        lane = bit // BYTE_LANE_BITS
        data = builder.mux2(ctrl_q[bit], pwdata[bit], we_ctrl[lane])
        if bit == DATA_WIDTH - 1:
            # the top bit is additionally cleared by hardware; with hw_clr left
            # unconstrained the next state is not determined, which is exactly
            # the difference between a proof and a bounded check
            data = builder.and2(data, not_hw_clr)
        builder.dffr(data, presetn, pclk, out=ctrl_q[bit])

    status_q = [builder.wire() for _ in range(STATUS_WIDTH)]
    for bit in range(STATUS_WIDTH):
        set_by_event = builder.or2(status_q[bit], evt[bit])
        clear_request = builder.and2(we_status, pwdata[bit])
        keep = builder.inv(clear_request)
        builder.dffr(builder.and2(set_by_event, keep), presetn, pclk, out=status_q[bit])

    # the scratch register is locked by CTRL[0]
    write_scratch = builder.and2(write_phase, scratch_sel)
    write_scratch_unlocked = builder.and2(write_scratch, ctrl_q[0])
    we_scratch = [
        builder.and2(write_scratch_unlocked, pstrb[lane]) for lane in range(STROBE_LANES)
    ]

    scratch_q = [builder.wire() for _ in range(DATA_WIDTH)]
    for bit in range(DATA_WIDTH):
        lane = bit // BYTE_LANE_BITS
        data = builder.mux2(scratch_q[bit], pwdata[bit], we_scratch[lane])
        if (SCRATCH_RESET >> bit) & 1:
            builder.dffs(data, presetn, pclk, out=scratch_q[bit])
        else:
            builder.dffr(data, presetn, pclk, out=scratch_q[bit])

    for bit in range(DATA_WIDTH):
        terms = [builder.and2(ctrl_sel, ctrl_q[bit])]
        if bit < STATUS_WIDTH:
            terms.append(builder.and2(status_sel, status_q[bit]))
        if (ID_VALUE >> bit) & 1:
            terms.append(id_sel)
        terms.append(builder.and2(scratch_sel, scratch_q[bit]))
        builder.and2(read_phase, builder.or_reduce(terms), out=port["prdata{}".format(bit)])

    # PREADY comes out of a transparent latch: a sequential primitive the
    # recovery deliberately does not model, so it has something to report as
    # unsupported instead of silently assuming it is a flip-flop.
    builder.latch(psel_en, builder.one(), out=port["pready"])

    return builder


def emit_verilog(builder, module="apb_regs"):
    lines = [
        "// Generated by tools/hal_apb_recover/fixtures/apb_regs/generate.py -- do not edit.",
        "// Gate library: NangateOpenCellLibrary "
        "(plugins/gate_libraries/definitions/NangateOpenCellLibrary.hgl)",
        "// All names are stripped: ports are io_NN, nets nNNN, instances gNNN.",
        "",
        "module {} (".format(module),
    ]
    port_list = [PORT_NAMES[role] for role, _ in PORTS]
    lines.append("    " + ",\n    ".join(port_list))
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


def emit_mapping():
    return {
        "schema": "fpgapa.apb-signal-mapping",
        "schema_version": "1.0.0",
        "design": "apb_regs",
        "description": "User-supplied APB mapping for the name-stripped apb_regs "
        "fixture. Nothing in the netlist names these ports; this file is the "
        "analyst's input.",
        "clock": {"net": PORT_NAMES["pclk"], "edge": "rising"},
        "reset": {"net": PORT_NAMES["presetn"], "active": "low", "kind": "asynchronous"},
        "apb": {
            "psel": PORT_NAMES["psel"],
            "penable": PORT_NAMES["penable"],
            "pwrite": PORT_NAMES["pwrite"],
            "pready": PORT_NAMES["pready"],
            "paddr": [PORT_NAMES["paddr{}".format(i)] for i in range(ADDRESS_WIDTH)],
            "pwdata": [PORT_NAMES["pwdata{}".format(i)] for i in range(DATA_WIDTH)],
            "prdata": [PORT_NAMES["prdata{}".format(i)] for i in range(DATA_WIDTH)],
            "pstrb": [PORT_NAMES["pstrb{}".format(i)] for i in range(STROBE_LANES)],
        },
        "address": {"base": 0, "stride": 4, "count": 8},
        "assumptions": {
            "non_apb_inputs": "zero",
            "byte_lane_bits": BYTE_LANE_BITS,
            "notes": [
                "PADDR[1:0] are byte offsets within a word and are never varied by "
                "the enumeration; word-aligned accesses only.",
                "io_45..io_52 (event inputs) and io_53 (a hardware clear) are not part "
                "of the bus and are assumed quiescent at 0 for the bounded probes.",
            ],
        },
    }


def emit_ground_truth():
    fields_ctrl = []
    for bit in range(DATA_WIDTH):
        fields_ctrl.append(
            {
                "bit": bit,
                "access": "read-write",
                "template": "write",
                "reset_value": 0,
                "strobe_lanes": [bit // BYTE_LANE_BITS],
                "expected_confidence": "proven_bounded"
                if bit == DATA_WIDTH - 1
                else "proven_under_assumptions",
                "note": "hardware also clears this bit when io_53 is high, so its "
                "next state is only determined once the non-APB inputs are pinned"
                if bit == DATA_WIDTH - 1
                else None,
            }
        )

    fields_status = [
        {
            "bit": bit,
            "access": "read-write-one-to-clear",
            "template": "write_one_to_clear",
            "reset_value": 0,
            "strobe_lanes": [0],
            "expected_confidence": "proven_bounded",
            "note": "set by the event input io_{:02d}; only determined with the event "
            "inputs pinned".format(45 + bit),
        }
        for bit in range(STATUS_WIDTH)
    ]

    fields_scratch = [
        {
            "bit": bit,
            "access": "read-write",
            "template": "write",
            "reset_value": (SCRATCH_RESET >> bit) & 1,
            "strobe_lanes": [bit // BYTE_LANE_BITS],
            "expected_confidence": "heuristic",
            "guarded_by": "CTRL bit 0 (the write only happens while CTRL[0] is 1)",
        }
        for bit in range(DATA_WIDTH)
    ]

    return {
        "design": "apb_regs",
        "description": "Ground truth for the name-stripped APB peripheral fixture. "
        "Written by hand alongside the generator; the recovery must reproduce it "
        "from the netlist and mapping.json alone.",
        "bus": {
            "kind": "APB3",
            "address_width": ADDRESS_WIDTH,
            "data_width": DATA_WIDTH,
            "strobe_lanes": STROBE_LANES,
            "byte_lane_bits": BYTE_LANE_BITS,
            "decoded_address_bits": [2, 3, 4],
            "undecoded_address_bits": [0, 1],
        },
        "registers": [
            {
                "address": 0x00,
                "name": "CTRL",
                "access": "read-write",
                "reset_value": "0x0000",
                "aliases": [0x08],
                "fields": fields_ctrl,
            },
            {
                "address": 0x04,
                "name": "STATUS",
                "access": "read-write-one-to-clear",
                "reset_value": "0x0000",
                "aliases": [],
                "implemented_bits": list(range(STATUS_WIDTH)),
                "constant_read_bits": {
                    str(bit): 0 for bit in range(STATUS_WIDTH, DATA_WIDTH)
                },
                "fields": fields_status,
            },
            {
                "address": 0x08,
                "name": "CTRL_ALIAS",
                "access": "read-write",
                "reset_value": "0x0000",
                "aliases": [0x00],
                "note": "intentional address alias: slot 0 and slot 2 share one decode "
                "line, so 0x00 and 0x08 are the same register",
                "fields": fields_ctrl,
            },
            {
                "address": 0x0C,
                "name": "ID",
                "access": "read-only",
                "reset_value": "0x{:04x}".format(ID_VALUE),
                "aliases": [],
                "storage": None,
                "note": "read-only constant with no storage; every PRDATA bit is a "
                "constant at this address",
                "fields": [
                    {
                        "bit": bit,
                        "access": "read-only",
                        "constant": (ID_VALUE >> bit) & 1,
                        "expected_confidence": "proven_under_assumptions",
                    }
                    for bit in range(DATA_WIDTH)
                ],
            },
            {
                "address": 0x10,
                "name": "SCRATCH",
                "access": "read-write",
                "reset_value": "0x{:04x}".format(SCRATCH_RESET),
                "aliases": [],
                "note": "writes are locked by CTRL[0]; the recovery must report this "
                "as a guarded write rather than as read-only or as a plain read-write",
                "fields": fields_scratch,
            },
        ],
        "unmapped_addresses": [0x14, 0x18, 0x1C],
        "side_effects": [
            {
                "address": 0x04,
                "kind": "write_one_to_clear",
                "description": "writing a 1 to a STATUS bit clears it; writing a 0 "
                "leaves it alone",
            },
            {
                "address": 0x00,
                "bit": 15,
                "kind": "hardware_clear",
                "description": "CTRL[15] is also cleared by the io_53 input, so its "
                "next state cannot be proven without pinning that input",
            },
        ],
        "unsupported": [
            {
                "gate_type": "DLH_X1",
                "count": 1,
                "description": "PREADY is driven by a transparent latch; the recovery "
                "models only edge-triggered state and must report this rather than "
                "assume it",
            }
        ],
        "expected_totals": {
            "flip_flops": DATA_WIDTH + STATUS_WIDTH + DATA_WIDTH,
            "registers": 5,
            "alias_classes": 1,
        },
    }


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    builder = build()

    with open(os.path.join(here, "apb_regs.v"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(emit_verilog(builder))
    for name, document in (
        ("mapping.json", emit_mapping()),
        ("ground_truth.json", emit_ground_truth()),
    ):
        with open(os.path.join(here, name), "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")

    print(
        "apb_regs.v: {} instances, {} internal nets, {} ports".format(
            len(builder.instances), len(builder.wires), len(PORTS)
        )
    )


if __name__ == "__main__":
    main()
