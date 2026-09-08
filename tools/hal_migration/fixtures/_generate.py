"""Regenerate the checked-in fixtures.

Run from the repository root::

    python tools/hal_migration/fixtures/_generate.py

The instance list below mirrors ``ice40_mixed.v`` one-to-one, and the gate type
semantics come from the real ``plugins/gate_libraries/definitions/ice40ultra.hgl``
(see :mod:`stub_netlist`), so the generated inventory is what HAL is expected to
produce for that netlist. ``test_hal_migration_hal.py`` checks that expectation
against a built HAL; if the two ever disagree, the fixture is wrong, not the
bindings.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(os.path.dirname(HERE))
REPO_ROOT = os.path.dirname(TOOLS)
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

from hal_findings import serialize as findings_serialize  # noqa: E402
from hal_findings import validate as findings_validate  # noqa: E402
from hal_migration import assess as assess_module  # noqa: E402
from hal_migration import catalogue as catalogue_module  # noqa: E402
from hal_migration import formats  # noqa: E402
from hal_migration import inventory as inventory_module  # noqa: E402
from hal_migration import report as report_module  # noqa: E402
from hal_migration.fixtures import stub_netlist  # noqa: E402

# Repository-relative so the generated fixtures carry no machine-specific path;
# main() changes into the repository root before anything is hashed.
GATE_LIBRARY = "plugins/gate_libraries/definitions/ice40ultra.hgl"
NETLIST = "tools/hal_migration/fixtures/ice40_mixed.v"
CATALOGUE = "tools/hal_migration/catalogues/ice40ultra-to-generic-fpga-1.0.0.json"

#: fixed so that regenerating the fixtures produces byte-identical files
GENERATED_AT = "2026-09-08T00:00:00Z"

RAM_ADDR = ["ram_addr({})".format(index) for index in range(11)]
RAM_WDATA = ["ram_wdata({})".format(index) for index in range(16)]
RAM_RDATA = ["ram_rdata({})".format(index) for index in range(16)]
MAC_O = ["mac_o({})".format(index) for index in range(32)]


def _group(pins, nets):
    return dict(zip(pins, nets))


#: (instance, cell, {pin: net}) -- mirrors ice40_mixed.v
INSTANCES = [
    ("gnd_inst", "GND", {"Y": "gnd_net"}),
    ("vcc_inst", "VCC", {"Y": "vcc_net"}),
    (
        "osc_inst",
        "SB_HFOSC",
        {"CLKHFEN": "vcc_net", "CLKHFPU": "vcc_net", "CLKHF": "osc_clk"},
    ),
    (
        "clk_buffer",
        "SB_GB",
        {"USER_SIGNAL_TO_GLOBAL_BUFFER": "clk_pin", "GLOBAL_BUFFER_OUTPUT": "clk_g"},
    ),
    (
        "lut_a",
        "SB_LUT4",
        {"I0": "d_in", "I1": "pad_in", "I2": "q2", "I3": "gnd_net", "O": "lut_o1"},
    ),
    (
        "lut_b",
        "SB_LUT4",
        {"I0": "lut_o1", "I1": "q4", "I2": "q6", "I3": "carry_co", "O": "lut_o2"},
    ),
    ("carry_a", "SB_CARRY", {"CI": "gnd_net", "I0": "lut_o1", "I1": "q1", "CO": "carry_co"}),
    ("ff_a", "SB_DFF", {"C": "clk_g", "D": "lut_o1", "Q": "q1"}),
    ("ff_b", "SB_DFF", {"C": "clk_g", "D": "lut_o2", "Q": "q2"}),
    ("ff_enable", "SB_DFFE", {"C": "clk_g", "E": "ce", "D": "q1", "Q": "q3"}),
    ("ff_async_reset_a", "SB_DFFR", {"C": "clk_g", "R": "rst", "D": "q3", "Q": "q4"}),
    ("ff_async_reset_b", "SB_DFFR", {"C": "clk_g", "R": "rst", "D": "q4", "Q": "q5"}),
    ("ff_sync_reset", "SB_DFFSR", {"C": "clk_g", "R": "rst", "D": "q5", "Q": "q6"}),
    ("ff_negedge", "SB_DFFNSR", {"C": "osc_clk", "R": "rst", "D": "q6", "Q": "q7"}),
    (
        "ram_inst",
        "SB_RAM40_4K",
        dict(
            {
                "WE": "ce",
                "WCLK": "clk_g",
                "WCLKE": "vcc_net",
                "RE": "vcc_net",
                "RCLK": "clk_g",
                "RCLKE": "vcc_net",
            },
            **dict(
                list(
                    _group(
                        ["WADDR({})".format(index) for index in range(11)], RAM_ADDR
                    ).items()
                )
                + list(
                    _group(
                        ["RADDR({})".format(index) for index in range(11)], RAM_ADDR
                    ).items()
                )
                + list(
                    _group(
                        ["WDATA({})".format(index) for index in range(16)], RAM_WDATA
                    ).items()
                )
                + list(
                    _group(
                        ["RDATA({})".format(index) for index in range(16)], RAM_RDATA
                    ).items()
                )
            )
        ),
    ),
    (
        "mac_inst",
        "SB_MAC16",
        dict(
            {"CLK": "clk_g", "CE": "ce"},
            **dict(
                list(
                    _group(["A({})".format(index) for index in range(16)], RAM_WDATA).items()
                )
                + list(
                    _group(["B({})".format(index) for index in range(16)], RAM_RDATA).items()
                )
                + list(_group(["O({})".format(index) for index in range(32)], MAC_O).items())
            )
        ),
    ),
    (
        "io_inst",
        "SB_IO",
        {
            "PACKAGE_PIN": "pad",
            "OUTPUT_ENABLE": "vcc_net",
            "D_OUT_0": "q7",
            "D_IN_0": "pad_in",
        },
    ),
    (
        "i2c_inst",
        "SB_I2C",
        {
            "SBCLKI": "clk_g",
            "SBRWI": "gnd_net",
            "SBSTBI": "gnd_net",
            "SCLI": "i2c_scl",
            "SDAO": "i2c_sda_out",
        },
    ),
    ("ff_out", "SB_DFF", {"C": "clk_g", "D": "q6", "Q": "q_out"}),
]

INPUTS = ["clk_pin", "rst", "d_in", "ce", "i2c_scl"] + RAM_ADDR + RAM_WDATA
OUTPUTS = ["q_out", "i2c_sda_out"]
INOUTS = ["pad"]


def build_stub_inventory():
    netlist = stub_netlist.build_netlist(
        GATE_LIBRARY,
        INSTANCES,
        inputs=INPUTS,
        outputs=OUTPUTS,
        inouts=INOUTS,
        design_name="ice40_mixed",
        input_filename=NETLIST,
    )
    return inventory_module.build_inventory(
        netlist,
        netlist_path=NETLIST,
        source_tool={"name": "hand-written fixture", "version": "1.0.0"},
        vendor="Lattice",
        family="iCE40 UltraPlus",
        device="iCE40UP5K",
        generated_at=GENERATED_AT,
        producer_command=["python", "tools/hal_migration/fixtures/_generate.py"],
    )


def build_incomplete_inventory():
    """A second inventory whose metadata is deliberately incomplete.

    It stands for the realistic case of a netlist read with a gate library that
    describes far less than the design needs: pin types were not assigned, so
    nothing is known about clocks, resets or memory geometry, and one gate type
    carries no properties at all.
    """
    return {
        "inventory_version": formats.INVENTORY_VERSION,
        "generated_at": GENERATED_AT,
        "producer": {"name": "hal_migration.fixtures", "version": "1.0.0"},
        "source": {
            "artifact_id": "source_netlist",
            "path": "fixtures/incomplete_metadata.v",
            "unhashed_reason": "hand-written fixture; there is no netlist file to hash",
            "design_name": "incomplete_metadata",
            "gate_count": 6,
            "net_count": 7,
            "gate_library": {"name": "ICE40ULTRA"},
        },
        "primitives": [
            {
                "gate_type": "SB_DFF",
                "count": 2,
                "category": "register",
                "properties": ["ff", "sequential"],
                "metadata": {"input_pin_count": 2, "output_pin_count": 1},
                "metadata_gaps": [
                    "the gate library used to read this netlist assigns no pin types, "
                    "so the clock pin of this register was not identified"
                ],
                "example_gates": [{"id": 1, "name": "ff_a"}],
            },
            {
                "gate_type": "SB_LUT4",
                "count": 2,
                "category": "combinational",
                "properties": ["c_lut", "combinational"],
                "metadata": {"input_pin_count": 4, "output_pin_count": 1},
                "metadata_gaps": [
                    "no LUT component: the truth table encoding of this cell is unknown"
                ],
            },
            {
                "gate_type": "SB_RAM40_4K",
                "count": 1,
                "category": "memory",
                "properties": ["ram", "sequential"],
                "metadata": {"input_pin_count": 61, "output_pin_count": 16},
                "metadata_gaps": [
                    "no RAMComponent: the memory's bit size is not modelled",
                    "no RAMPortComponent: port structure and collision behaviour are "
                    "not modelled",
                ],
            },
            {
                "gate_type": "CUSTOM_HARD_BLOCK",
                "count": 1,
                "category": "black_box",
                "properties": [],
                "metadata_gaps": [
                    "the gate library assigns this type no properties at all: HAL "
                    "knows its pins and nothing about its behaviour"
                ],
            },
        ],
        "clock_signals": [],
        "reset_signals": [],
        "io_ports": [],
        "metadata_gaps": [
            {
                "scope": "design",
                "field": "io_ports",
                "detail": "the netlist declares no global input or output nets, so the "
                "design's I/O boundary could not be established",
            },
            {
                "scope": "design",
                "field": "source.tool",
                "detail": "the synthesis tool and version that produced this netlist "
                "were not stated",
            },
        ],
        "totals": {
            "gates": 6,
            "nets": 7,
            "gate_types": 4,
            "by_category": {
                "black_box": 1,
                "combinational": 2,
                "memory": 1,
                "register": 2,
            },
            "gate_types_by_category": {
                "black_box": 1,
                "combinational": 1,
                "memory": 1,
                "register": 1,
            },
            "clock_signals": 0,
            "reset_signals": 0,
            "io_ports": 0,
            "black_box_types": 1,
            "metadata_gaps": 2,
        },
        "notes": [
            "hand-written fixture: every primitive here is missing metadata that the "
            "bundled catalogue requires, so no mapping may be reported as supported"
        ],
    }


def main():
    os.chdir(REPO_ROOT)
    written = []

    inventory_document = build_stub_inventory()
    formats.validate(inventory_document, "inventory")
    path = os.path.join(HERE, "ice40_mixed_inventory.json")
    formats.write_json(inventory_document, path)
    written.append(path)

    incomplete = build_incomplete_inventory()
    formats.validate(incomplete, "inventory")
    path = os.path.join(HERE, "incomplete_metadata_inventory.json")
    formats.write_json(incomplete, path)
    written.append(path)

    catalogue_document = catalogue_module.load(CATALOGUE)
    assessment = assess_module.build_document(
        inventory_document,
        catalogue_document,
        catalogue_path=CATALOGUE,
        generated_at=GENERATED_AT,
        producer_command=["python", "tools/hal_migration/fixtures/_generate.py"],
    )
    findings_validate.validate_document(assessment)
    path = os.path.join(HERE, "ice40_mixed_assessment.json")
    findings_serialize.write_document(assessment, path)
    written.append(path)

    path = os.path.join(HERE, "ice40_mixed_report.md")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(report_module.render_markdown(assessment, inventory=inventory_document))
    written.append(path)

    for entry in written:
        print(entry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
