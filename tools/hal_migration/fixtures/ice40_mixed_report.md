# FPGA migration assessment: ice40_mixed -> generic SRAM FPGA capability profile

> **This is an assessment, not a conversion.** No netlist was converted, re-synthesised or simulated. Mapping categories are structural proposals from a manually reviewed catalogue; none of them is evidence of behavioural equivalence, resource fit or timing closure.

## Inputs and versions

- findings schema: `1.0.0`
- inventory format: `1.0.0`
- catalogue format: `1.0.0`, catalogue `ice40ultra-to-generic-fpga` revision `2026-09-08.1`
- produced by `hal_migration.assess` 1.0.0
- source netlist produced by: `hand-written fixture 1.0.0` (stated by the caller)
- catalogue reviewed by FPGAPA HAL fork maintainers on 2026-09-08 (Manual review of every cell in plugins/gate_libraries/definitions/ice40ultra.hgl (cell properties, pin types and ff_config/lut_config) against a generic SRAM-FPGA capability profile. Source-side statements are taken from the gate library itself; target-side statements are profile assumptions.)

- **source_netlist** (netlist) - sha256 `2a1624726241a670`
  - path: `tools/hal_migration/fixtures/ice40_mixed.v`
  - design: `ice40_mixed`
  - gate library: `ICE40ULTRA` (sha256 `5cd862589b076f7d`)
  - source technology: Lattice / iCE40 UltraPlus / iCE40UP5K; produced by hand-written fixture 1.0.0
- **ice40ultra-to-generic-fpga** (other) - sha256 `719b3de627d188d3`
  - path: `tools/hal_migration/catalogues/ice40ultra-to-generic-fpga-1.0.0.json`
  - target capability catalogue revision 2026-09-08.1, target generic SRAM FPGA capability profile with generic-fpga-flow profile-2026.1; reviewed by FPGAPA HAL fork maintainers on 2026-09-08

## Summary

| category | gate types | gate instances |
| --- | --- | --- |
| supported | 5 | 8 |
| candidate | 6 | 7 |
| unresolved | 4 | 4 |

Source design: 19 gate(s), 98 net(s), 15 gate type(s), 2 clock net(s), 1 reset/set net(s), 35 top-level port(s).

## Primitive mappings

| source primitive | instances | source role | category | target | open obligations |
| --- | --- | --- | --- | --- | --- |
| `GND` | 1 | constant | supported | `GND` | 1 |
| `SB_DFFE` | 1 | register | supported | `DFF_CE` | 4 |
| `SB_DFF` | 3 | register | supported | `DFF` | 4 |
| `SB_LUT4` | 2 | combinational | supported | `LUT6` | 2 |
| `VCC` | 1 | constant | supported | `VCC` | 1 |
| `SB_CARRY` | 1 | arithmetic | candidate | `CARRY` | 1 |
| `SB_DFFR` | 2 | register | candidate | `DFF_ASYNC_RESET` | 4 |
| `SB_DFFSR` | 1 | register | candidate | `DFF_SYNC_RESET` | 4 |
| `SB_GB` | 1 | combinational | candidate | `GLOBAL_CLOCK_BUFFER` | 2 |
| `SB_IO` | 1 | io | candidate | `IO_BUFFER` | 4 |
| `SB_MAC16` | 1 | arithmetic | candidate | `DSP_MAC` | 5 |
| `SB_DFFNSR` | 1 | register | unresolved | - | 5 |
| `SB_HFOSC` | 1 | combinational | unresolved | - | 5 |
| `SB_I2C` | 1 | sequential_other | unresolved | - | 3 |
| `SB_RAM40_4K` | 1 | memory | unresolved | `BRAM_DUAL_PORT` | 7 |

`supported` = a reviewer proposed the mapping and the inventory carried the metadata it requires. It is not a claim that the behaviour matches.

## Unresolved primitives

### `SB_DFFNSR` - 1 instance(s)

- gate type 'SB_DFFNSR' has no entry in this catalogue; an unlisted primitive is unresolved, not supported
- metadata gap: the reset pin(s) R are not modelled as an asynchronous clear; the gate library encodes the reset in the next-state function only, so whether the reset is synchronous must be confirmed against the vendor documentation
- metadata gap: the gate type carries an INIT attribute (INIT) but no instance in this netlist sets one; the power-up state of these registers is unspecified in the source

### `SB_HFOSC` - 1 instance(s)

- the target profile provides no on-chip oscillator, so the design would need an external clock source or a target-specific hard block
- the source oscillator's frequency, tolerance and start-up behaviour are not in the netlist

### `SB_I2C` - 1 instance(s)

- the target profile has no hard I2C block
- a soft replacement changes the software-visible register map and must be re-verified against the I2C specification
- metadata gap: the gate library marks this type as sequential but models no flip-flop, latch or RAM component, so its state behaviour is not described at all

### `SB_RAM40_4K` - 1 instance(s)

Downgraded from `candidate`: the source metadata this mapping requires (bit_size, ram_ports) is not in the inventory.

- the inventory does not carry the metadata this mapping requires (bit_size, ram_ports); the source netlist or gate library does not describe it, so the mapping cannot be assessed
- metadata gap: no RAMComponent: the memory's bit size is not modelled by the gate library, so depth/width and target block-RAM fit cannot be derived
- metadata gap: no RAMPortComponent: port structure, write-enable semantics and read-during-write (collision) behaviour are not modelled by the gate library and must be taken from the vendor documentation
- metadata gap: address and data widths would have to be inferred from pin types alone, which cannot distinguish read from write ports; left unstated

## Verification obligations

### Semantic obligations

- **Carry chain and arithmetic semantics** (`semantic/arithmetic-carry-chain`, severity high)
  - check: Re-infer the arithmetic from the source netlist and let the target toolchain implement it, then check equivalence of the arithmetic function.
  - target assumption while open: the target technology implements the same arithmetic with its own carry structure
  - applies to: SB_CARRY, SB_MAC16
- **Asynchronous set/reset polarity, priority and recovery** (`semantic/async-set-reset-priority`, severity high)
  - check: Compare the source FFComponent set/reset behaviour with the target primitive's documented behaviour and simulate simultaneous assertion; check recovery/removal timing in the target flow.
  - target assumption while open: the target flip-flop resolves simultaneous set and reset the same way and has the same reset polarity
  - applies to: SB_DFF, SB_DFFE, SB_DFFNSR, SB_DFFR, SB_DFFSR, design
- **Clock domain crossings** (`semantic/clock-domain-crossings`, severity high)
  - check: Enumerate the crossings between the inventoried clock domains and check each synchroniser against the target metastability guidance.
  - target assumption while open: the target technology's metastability behaviour is covered by the existing synchronisers
  - applies to: design
- **Combinational function equivalence** (`semantic/combinational-equivalence`, severity high)
  - check: Prove equivalence of the source and target cell functions (for LUTs: compare the truth tables after applying the target's INIT bit order), or run an equivalence check over the converted netlist.
  - target assumption while open: the target LUT/gate implements exactly the same Boolean function
  - applies to: GND, SB_GB, SB_HFOSC, SB_LUT4, VCC
- **DSP/MAC block behaviour** (`semantic/dsp-behaviour`, severity critical)
  - check: Extract the source block's configuration from the vendor documentation and attributes, configure the target DSP accordingly, and compare cycle-accurate behaviour including latency.
  - target assumption while open: the target DSP can be configured to the same latency, width and rounding behaviour
  - applies to: SB_MAC16
- **Whole-design functional equivalence** (`semantic/functional-equivalence`, severity critical)
  - check: Run an equivalence check or a directed regression between the source design and the migrated design once a conversion exists.
  - target assumption while open: none: no conversion was performed, so nothing was compared
  - applies to: SB_DFFNSR, SB_HFOSC, SB_I2C, SB_RAM40_4K, design
- **Hard IP replacement** (`semantic/hard-ip-replacement`, severity critical)
  - check: Choose a target implementation (hard block or soft core), re-verify the protocol against the specification, and check the software-visible register map.
  - target assumption while open: a target implementation with an equivalent programming model exists
  - applies to: SB_HFOSC, SB_I2C
- **Registered and DDR I/O paths** (`semantic/io-registered-path`, severity medium)
  - check: Compare the registered/DDR configuration of the source I/O primitive with the target I/O block and check the resulting latency.
  - target assumption while open: the target I/O block provides the same registered/DDR structure
  - applies to: SB_IO, design
- **LUT INIT width and bit order** (`semantic/lut4-into-lut6-init-order`, severity high)
  - check: Re-derive the target INIT from the source truth table (extend the 16-bit function to the target width and apply the target's bit order), then compare the two truth tables exhaustively - 64 input combinations is a complete check.
  - target assumption while open: the target LUT is a strict superset of a 4-input LUT and its INIT encoding is documented
  - applies to: SB_LUT4
- **Memory read-during-write / collision behaviour** (`semantic/memory-collision-mode`, severity critical)
  - check: Obtain the source block's collision mode from the vendor documentation or the synthesis attributes, select the matching target mode explicitly, and simulate simultaneous read and write at the same address.
  - target assumption while open: the target memory can be configured with the same read-during-write and collision behaviour
  - applies to: SB_RAM40_4K, design
- **Memory initialisation contents** (`semantic/memory-init-contents`, severity high)
  - check: Compare the source INIT attributes with the target memory's expected initialisation format, including word order and bit order.
  - target assumption while open: the target memory accepts the same initialisation contents in a convertible encoding
  - applies to: SB_RAM40_4K
- **Power-up and INIT state** (`semantic/power-up-and-init-state`, severity high)
  - check: Confirm the target primitive supports the same initial value, and that the target flow preserves it; simulate reset-free start-up.
  - target assumption while open: the target device powers up in the same state and honours the same INIT attribute
  - applies to: SB_DFF, SB_DFFE, SB_DFFNSR, SB_DFFR, SB_DFFSR, SB_I2C, SB_MAC16, SB_RAM40_4K, design
- **Register clock edge and enable semantics** (`semantic/register-clocking`, severity high)
  - check: Compare the source and target flip-flop definitions (clock function, enable function) and simulate a register with enable deasserted across a clock edge.
  - target assumption while open: the target flip-flop samples on the same clock edge and treats enable with the same priority
  - applies to: SB_DFF, SB_DFFE, SB_DFFNSR, SB_DFFR, SB_DFFSR, SB_MAC16, SB_RAM40_4K

### Physical obligations

- **Clock network, buffering and skew** (`physical/clock-network-and-buffering`, severity high)
  - check: Re-plan the clock network for the target device: number of global resources, region reach, and insertion of target clock buffers.
  - target assumption while open: the target device has enough global clock resources with acceptable skew
  - applies to: SB_DFF, SB_DFFE, SB_DFFNSR, SB_DFFR, SB_DFFSR, SB_GB, SB_HFOSC, SB_IO, SB_MAC16, SB_RAM40_4K, design
- **On-chip clock source availability** (`physical/clock-source-availability`, severity high)
  - check: Confirm the target device provides an equivalent clock source, including frequency tolerance and start-up time, or plan an external source.
  - target assumption while open: the target device provides a clock source with acceptable frequency and accuracy
  - applies to: SB_HFOSC
- **Pin assignment and bank rules** (`physical/io-pin-assignment`, severity high)
  - check: Re-do pin assignment for the target package and check bank voltage compatibility and dedicated-pin usage.
  - target assumption while open: the target package can host the same interface with compatible banking
  - applies to: SB_IO, design
- **I/O standard, drive strength and termination** (`physical/io-standard-and-drive`, severity high)
  - check: Carry the source I/O constraints over explicitly and check each standard, drive strength, slew rate and termination against the target bank rules.
  - target assumption while open: the target device offers a compatible I/O standard in the assigned bank
  - applies to: SB_IO, design
- **Memory depth/width fit and cascading** (`physical/memory-depth-width-fit`, severity medium)
  - check: Map the required depth and width onto the target block RAM geometry and record how many blocks and what cascade logic result.
  - target assumption while open: the required memory geometry fits the target blocks without changing behaviour
  - applies to: SB_RAM40_4K
- **Resource fit in the target device** (`physical/resource-fit`, severity high)
  - check: Map the inventory onto the target device's resource budget and confirm the design fits with the intended utilisation margin.
  - target assumption while open: the target device has enough resources of each kind
  - applies to: design
- **Timing closure in the target device** (`physical/timing-closure`, severity critical)
  - check: Re-run synthesis, placement and static timing analysis for the target device with the migrated constraints.
  - target assumption while open: none: timing was not assessed
  - applies to: design

## Metadata the source does not provide

- `SB_DFF`: the gate type carries an INIT attribute (INIT) but no instance in this netlist sets one; the power-up state of these registers is unspecified in the source
- `SB_DFFE`: the gate type carries an INIT attribute (INIT) but no instance in this netlist sets one; the power-up state of these registers is unspecified in the source
- `SB_DFFNSR`: the reset pin(s) R are not modelled as an asynchronous clear; the gate library encodes the reset in the next-state function only, so whether the reset is synchronous must be confirmed against the vendor documentation
- `SB_DFFNSR`: the gate type carries an INIT attribute (INIT) but no instance in this netlist sets one; the power-up state of these registers is unspecified in the source
- `SB_DFFR`: the gate type carries an INIT attribute (INIT) but no instance in this netlist sets one; the power-up state of these registers is unspecified in the source
- `SB_DFFSR`: the reset pin(s) R are not modelled as an asynchronous clear; the gate library encodes the reset in the next-state function only, so whether the reset is synchronous must be confirmed against the vendor documentation
- `SB_DFFSR`: the gate type carries an INIT attribute (INIT) but no instance in this netlist sets one; the power-up state of these registers is unspecified in the source
- `SB_I2C`: the gate library marks this type as sequential but models no flip-flop, latch or RAM component, so its state behaviour is not described at all
- `SB_IO`: I/O standard, drive strength, slew, termination and pin assignment are constraint-file properties and are not present in the netlist
- `SB_MAC16`: DSP internals (pipeline registers, rounding, saturation, accumulator width) are not modelled by the gate library
- `SB_RAM40_4K`: no RAMComponent: the memory's bit size is not modelled by the gate library, so depth/width and target block-RAM fit cannot be derived
- `SB_RAM40_4K`: no RAMPortComponent: port structure, write-enable semantics and read-during-write (collision) behaviour are not modelled by the gate library and must be taken from the vendor documentation
- `SB_RAM40_4K`: address and data widths would have to be inferred from pin types alone, which cannot distinguish read from write ports; left unstated

## What this report does not say

- no netlist was converted: this document compares a source inventory with a reviewed target capability table
- 'supported' means a reviewer proposed the mapping and the required source metadata was present; it is not a claim that behaviour matches
- timing closure and resource fit are not assessed by any part of this tool
- catalogue ice40ultra-to-generic-fpga revision 2026-09-08.1 reviewed by FPGAPA HAL fork maintainers on 2026-09-08
- 1 gate type(s) are absent from the catalogue and are reported as unresolved: SB_DFFNSR
- 1 mapping(s) were downgraded to unresolved because the inventory lacked the metadata they require: SB_RAM40_4K
- no finding in this report has a status other than `heuristic`, `unsupported` or `unknown`; nothing here is a proof
