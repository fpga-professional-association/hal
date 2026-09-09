# accumulator_alu — specification

An 8-bit accumulator with three operations plus a hold, selected by a 2-bit
opcode. One clock, one asynchronous active-low reset.

## Ports

| port | dir | width | meaning |
| --- | --- | --- | --- |
| `clk` | in | 1 | rising-edge clock, the only clock in the design |
| `rst_n` | in | 1 | asynchronous, active low; clears `acc` and `carry` |
| `op` | in | 2 | opcode, see below |
| `operand` | in | 8 | second ALU operand |
| `acc` | out | 8 | the accumulator register |
| `carry` | out | 1 | registered carry-out / not-borrow flag |
| `zero` | out | 1 | combinational, `acc == 0` |

## Opcodes

| `op` | name | effect on the clock edge |
| --- | --- | --- |
| `2'b00` | NOP | `acc` and `carry` hold |
| `2'b01` | ADD | `{carry, acc} <= acc + operand` |
| `2'b10` | SUB | `{carry, acc} <= acc + ~operand + 1` |
| `2'b11` | CLR | `acc <= 0`, `carry <= 0` |

`SUB` is the two's-complement identity `a - b == a + ~b + 1`. It reuses the
same 8-bit adder as `ADD`; the only difference is that the operand is inverted
on the way in and the carry-in of the chain is 1. For `SUB` the resulting
`carry` is the *not-borrow* flag: 1 means `acc >= operand`.

`zero` is a pure function of the register outputs and is therefore one cycle
"behind" the ALU result, exactly like a real flag register would be if it were
not registered.

## Reset

`rst_n` is asynchronous and active low. `acc` and `carry` go to 0 while it is
low, independently of `clk` and `op`.

## Deliberate implementation constraints

The design exists to be synthesised for an **Agilex 3** device and then
reverse-engineered with HAL, so it is written to stay inside the primitive
coverage of `plugins/gate_libraries/definitions/AGILEX_TENNM.hgl`:

* one clock domain, no gated clocks, no clock enables other than the ALM
  flip-flop's own `ena`;
* no vendor IP: no RAM, no DSP/MAC, no PLL, no I/O buffers (the export is taken
  at the post-synthesis snapshot, before fitting, so no I/O primitives are
  inserted);
* the synchronous clear is kept **out** of `tennm_ff.sclr` by the QSF
  assignment `ALLOW_SYNCH_CTRL_USAGE OFF`. `sclr` is outside the configuration
  `tools/hal_agilex` has validated, and the walkthrough shows what happens if
  you leave the assignment out
  (`netlist/accumulator_alu_sclr_variant.vo`).

The post-synthesis netlist therefore contains only `tennm_lcell_comb` (22) and
`tennm_ff` (9), plus the ground and power literals HAL's Verilog front end needs.
