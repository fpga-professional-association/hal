# pwm_generator — intended behaviour

A minimal pulse-width modulator with a run-time programmable duty cycle.

## Ports

| port | dir | width | meaning |
| --- | --- | --- | --- |
| `clk` | in | 1 | single clock; everything is synchronous to its rising edge |
| `rst_n` | in | 1 | asynchronous, active-low reset |
| `run` | in | 1 | when high the counter advances; when low the ramp freezes |
| `cfg_we` | in | 1 | configuration write strobe |
| `cfg_addr` | in | 2 | configuration address; only `2'b01` selects a register |
| `cfg_wdata` | in | 8 | configuration write data |
| `pwm_out` | out | 1 | modulated output |
| `period_tick` | out | 1 | high during the last count of every period |

## Behaviour

1. **Counter.** `cnt` is an 8-bit free-running up-counter. It increments by one
   on every rising clock edge while `run` is high and wraps `255 → 0`. The
   period is therefore 256 clocks. `rst_n` clears it asynchronously.

2. **Duty register.** `duty` is an 8-bit register. It is loaded from
   `cfg_wdata` on a rising clock edge when `cfg_we` is high **and**
   `cfg_addr == 2'b01`. Any other address is ignored. `rst_n` clears it
   asynchronously to `0`.

3. **Output.** `pwm_out` is combinational: `pwm_out = (cnt < duty)`, unsigned.
   Over one full period the output is high for exactly `duty` of the 256
   counts, so the duty cycle is `duty/256`. `duty = 0` parks the output low;
   there is no code for 100 %.

4. **Period flag.** `period_tick` is combinational: `period_tick = (cnt == 255)`.
   It marks the last count of a period, i.e. the cycle after which `cnt` wraps
   (provided `run` is high).

## Non-goals

Deliberately absent, so that the synthesized netlist stays inside the primitive
set `AGILEX_TENNM.hgl` models: no memories, no DSP blocks, no PLL, no IO
buffers, no second clock, no synchronous reset, no clock enable gating other
than the two flip-flop enables described above.
