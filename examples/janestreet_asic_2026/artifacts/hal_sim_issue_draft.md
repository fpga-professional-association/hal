# Draft issue bodies — findings from replaying the Jane Street ASIC puzzle through HAL's simulator

These are **drafts**, not filed issues. They came out of
`examples/janestreet_asic_2026/tools/hal_sim_replay.py`, which drives the puzzle's
full protocol through the `hal_simulator` engine of `netlist_simulator_controller`
and compares `O[7:0]` and `success` at every clock edge against `tools/sc_sim.py`
(the example's validated reference simulator, which reproduces the puzzle's own
`example_inputs.vcd` at all 312 recorded edges).

**The replay itself is a clean match** — see `artifacts/hal_sim_replay.txt`: with the
two harness workarounds below in place, HAL agrees with `sc_sim` on all 141 clock
edges of all four payloads, `success` goes high at the right edge, and the winning
message `(* TWO STARS *)` comes out byte for byte. HAL's flip-flop model, including
the 84 async-reset-low `dfrtp_2` and the 4 async-set-low `dfstp_2` cells, is correct
on this design. Both findings below are about how a *simulation set* is built up, not
about gate evaluation.

Everything below was observed on `master` @ `d17d10409`, Ubuntu 24.04 Debug build
(`halbuild` bench container), with
`plugins/gate_libraries/definitions/SKY130_FD_SC_HD.hgl`.

---

## Finding 1 — `hal_simulator` leaves a tie cell with two constant outputs at `X` forever

### Summary

A standard-cell tie cell such as sky130's `sky130_fd_sc_hd__conb_1` has **no input
pins** and **two constant outputs**, `HI` = 1 and `LO` = 0. The `hal_simulator`
engine never gives either of them a value: the nets they drive stay `X` for the whole
simulation, and the `X` propagates into everything downstream.

This is not hypothetical. In the Jane Street puzzle netlist (1618 gates, 6 `conb_1`
cells) it makes `O[7:0]` read `X` at every one of the 15 message edges for three of
the four payloads replayed:

```
  and the resulting disagreement with sc_sim, per payload:
    recorded   15 of 141 edges differ, message '???????????????'
    solution    0 of 141 edges differ, message '(* TWO STARS *)'
    zeros      15 of 141 edges differ, message '???????????????'
    ones       15 of 141 edges differ, message '???????????????'
```

Driving just the five `conb` `LO` nets that actually have a destination is enough to
make the whole replay agree again, so the `X` really does come from the tie cells.

### Reproduction

Runs in the bench container; needs nothing but the shipped sky130 gate library.

```bash
docker exec -e HAL_BASE_PATH=/work/build -e HAL_PY_PATH=/work/build/lib \
    -e PYTHONPATH=/work/build/lib -w /work halbuild bash -c 'python3 /tmp/tie_repro.py'
```

```python
#!/usr/bin/env python3
"""Minimal repro: hal_simulator leaves a sky130 conb tie cell's outputs at X."""
import os
import sys
import time

REPO = os.environ.get("HAL_REPO", "/work")
LIB = REPO + "/plugins/gate_libraries/definitions/SKY130_FD_SC_HD.hgl"
V = "/tmp/tie_repro.v"

with open(V, "w") as fh:
    fh.write("""module tie_repro (clk, rst_n, q_hi, q_lo, ff_hi);
 input clk;
 input rst_n;
 output q_hi;
 output q_lo;
 output ff_hi;

 wire hi;
 wire lo;

 sky130_fd_sc_hd__conb_1 tie (.HI(hi), .LO(lo));
 sky130_fd_sc_hd__buf_1 b0 (.A(hi), .X(q_hi));
 sky130_fd_sc_hd__buf_1 b1 (.A(lo), .X(q_lo));
 sky130_fd_sc_hd__dfrtp_2 ff (.CLK(clk), .D(hi), .RESET_B(rst_n), .Q(ff_hi));
endmodule
""")

import hal_py

hal_py.plugin_manager.load_all_plugins()
from hal_plugins import netlist_simulator_controller  # noqa: F401

netlist = hal_py.NetlistFactory.load_netlist(V, LIB)
assert netlist is not None

library = netlist.get_gate_library()
conb = library.get_gate_type_by_name("sky130_fd_sc_hd__conb_1")
print("conb HI function  :", conb.get_boolean_function("HI"))
print("conb LO function  :", conb.get_boolean_function("LO"))
print("library gnd types :", sorted(library.get_gnd_gate_types()))
print("library vcc types :", sorted(library.get_vcc_gate_types()))
print("netlist gnd gates :", [g.get_name() for g in netlist.get_gnd_gates()])
print("netlist vcc gates :", [g.get_name() for g in netlist.get_vcc_gates()])

plugin = hal_py.plugin_manager.get_plugin_instance("netlist_simulator_controller")
ctrl = plugin.create_simulator_controller("tie_repro", "/tmp/tie_repro_wd")
ctrl.add_gates(netlist.get_gates())
engine = ctrl.create_simulation_engine("hal_simulator")
nets = {n.get_name(): n for n in netlist.get_nets()}
ctrl.add_clock_period(nets["clk"], 1000, True, 4000)
ONE, ZERO = hal_py.BooleanFunction.Value.ONE, hal_py.BooleanFunction.Value.ZERO
ctrl.set_input(nets["rst_n"], ZERO)
ctrl.simulate(1000)
ctrl.set_input(nets["rst_n"], ONE)
ctrl.simulate(3000)
assert ctrl.run_simulation()
while engine.get_state() not in (0, -1):
    time.sleep(0.02)
assert engine.get_state() == 0, engine.get_state()
assert ctrl.get_results()

print()
print("cycle  q_hi  q_lo  ff_hi      (expected 1, 0, 1 from cycle 1 on; -1 is X)")
bad = False
for cycle in range(4):
    t = cycle * 1000 + 750
    values = [int(ctrl.get_waveform_by_net(nets[n]).get_value_at(t))
              for n in ("q_hi", "q_lo", "ff_hi")]
    print("  %d   %5d %5d %5d" % (cycle, values[0], values[1], values[2]))
    if cycle >= 1 and values != [1, 0, 1]:
        bad = True
print()
print("RESULT:", "X on the tie nets -- bug reproduced" if bad else "tie nets resolved")
sys.exit(1 if bad else 0)
```

### Observed

```
conb HI function  : 0b1
conb LO function  : 0b0
library gnd types : ['HAL_GND']
library vcc types : ['HAL_VDD']
netlist gnd gates : []
netlist vcc gates : []

cycle  q_hi  q_lo  ff_hi      (expected 1, 0, 1 from cycle 1 on; -1 is X)
  0      -1    -1     0
  1      -1    -1    -1
  2      -1    -1    -1
  3      -1    -1    -1

RESULT: X on the tie nets -- bug reproduced
```

### Expected

`q_hi` = 1 and `q_lo` = 0 from t = 0, and `ff_hi` = 1 from the first rising edge with
`rst_n` high. The gate type carries `0b1` / `0b0` as its Boolean functions and HAL
parsed them correctly — nothing ever evaluates them.

### Why it happens

Two independent mechanisms both decline to handle the cell:

1. **The gate is never evaluated.** `NetlistSimulator::initialize`
   (`plugins/simulator/hal_simulator/src/netlist_simulator.cpp`) builds
   `m_successors` as *net → simulation gates*, and a combinational gate is only
   simulated when one of its **input** nets produces an event
   (`SimulationGateCombinational::simulate`). A gate with no input pins has no
   entry anywhere in `m_successors` and is therefore never evaluated, not even once
   at t = 0.
2. **The gate is not given a start value either.** The only gates that get an
   initial event are the ones HAL marked as GND/VCC gates
   (`netlist_simulator.cpp:395-412`), and
   `GateLibrary::mark_gnd_gate_type` / `mark_vcc_gate_type`
   (`src/netlist/gate_library/gate_library.cpp:365-402`) only accept a gate type
   with *no input pins and exactly one output pin*. `conb_1` fails on both counts —
   it has two output pins, and the HGL declares its `VPWR`/`VGND`/`VPB`/`VNB` power
   pins as inputs. So `gate_library_manager::prepare_library` falls through to
   auto-generating `HAL_GND`/`HAL_VDD` (see the `library gnd types` line above), and
   the real tie cells in the netlist are marked as nothing.

Note that this is *not* specific to sky130 or to two-output cells: any
combinational gate whose Boolean functions are constant and which has no connected
input net hits mechanism (1).

### Suggested fix

In `NetlistSimulator::initialize`, next to the existing GND/VCC block, evaluate the
output functions of every combinational simulation gate that has no input nets (or,
equivalently, whose output functions have no variables) once, and push the results as
initial events. That is library-agnostic, subsumes the current GND/VCC special case,
and also fixes the case of a tie cell HAL *did* recognise but whose second output pin
would otherwise have been forced to the same value.

A narrower alternative — relaxing `mark_gnd_gate_type`/`mark_vcc_gate_type` to allow
multiple output pins and to ignore power/ground-typed input pins — would still be
wrong for `conb`, because that gate is simultaneously a GND and a VCC source and the
init block at `netlist_simulator.cpp:395` assigns one value to *all* of a marked
gate's fan-out nets.

### Impact / workaround

Any ASIC netlist that uses tie cells — which is essentially all of them — silently
simulates to `X` downstream of the ties. Until it is fixed, the workaround used by
`examples/janestreet_asic_2026/tools/hal_sim_replay.py` is to keep the tie cells out
of the simulation set (`add_gates`), which turns their tie nets into simulation input
nets, and then `set_input` them to the constants read off the cell's own Boolean
functions.

---

## Finding 2 — the `verilator` engine emits a testbench that references internal nets as DUT members

### Summary

`SimulationInput::compute_input_nets` classifies *any* net without a source inside
the simulation set as a simulation input net — including a net that is internal to
the top module (an undriven/floating wire, or the output of a gate the caller left
out of the simulation set). The verilator engine then generates, for every such net,
`&dut-><net_name>` in `testbench.cpp`. Verilated models only expose *ports*, so the
generated testbench does not compile and the run fails with `make` exit code 2.

On the puzzle netlist, which has one genuinely unrouted net (`n278`), the verilator
engine therefore cannot run the design at all.

### Reproduction

Twelve lines of Verilog; `floating` is read by a gate and driven by nothing.

```python
#!/usr/bin/env python3
"""Minimal repro: the verilator engine's testbench references an undriven
internal net as if it were a port of the written netlist."""
import os
import sys
import time

REPO = os.environ.get("HAL_REPO", "/work")
LIB = REPO + "/plugins/gate_libraries/definitions/SKY130_FD_SC_HD.hgl"
V = "/tmp/float_repro.v"

with open(V, "w") as fh:
    fh.write("""module float_repro (clk, rst_n, q);
 input clk;
 input rst_n;
 output q;

 wire floating;
 wire d;

 sky130_fd_sc_hd__and2_2 a0 (.A(floating), .B(rst_n), .X(d));
 sky130_fd_sc_hd__dfrtp_2 ff (.CLK(clk), .D(d), .RESET_B(rst_n), .Q(q));
endmodule
""")

import hal_py

hal_py.plugin_manager.load_all_plugins()
from hal_plugins import netlist_simulator_controller  # noqa: F401

netlist = hal_py.NetlistFactory.load_netlist(V, LIB)
assert netlist is not None
nets = {n.get_name(): n for n in netlist.get_nets()}

plugin = hal_py.plugin_manager.get_plugin_instance("netlist_simulator_controller")
ctrl = plugin.create_simulator_controller("float_repro", "/tmp/float_repro_wd")
ctrl.add_gates(netlist.get_gates())
engine = ctrl.create_simulation_engine(sys.argv[1] if len(sys.argv) > 1 else "verilator")
print("simulation input nets:", sorted(n.get_name() for n in ctrl.get_input_nets()))
print("global input nets    :", sorted(n.get_name() for n in netlist.get_global_input_nets()))

ONE, ZERO = hal_py.BooleanFunction.Value.ONE, hal_py.BooleanFunction.Value.ZERO
ctrl.add_clock_period(nets["clk"], 1000, True, 4000)
ctrl.set_input(nets["floating"], ZERO)
ctrl.set_input(nets["rst_n"], ZERO)
ctrl.simulate(1000)
ctrl.set_input(nets["rst_n"], ONE)
ctrl.simulate(3000)
ok = ctrl.run_simulation()
while engine.get_state() not in (0, -1):
    time.sleep(0.02)
print("run_simulation:", ok, "engine state:", engine.get_state(), "(0=Done, -1=Failed)")
sys.exit(0 if engine.get_state() == 0 else 1)
```

### Observed

```
simulation input nets: ['clk', 'floating', 'rst_n']
global input nets    : ['clk', 'rst_n']
[simulation_plugin] [warning] Process 'make' terminated with exit code 2.
[float_repro] [warning] simulation engine error during run.
run_simulation: True engine state: -1 (0=Done, -1=Failed)
```

and in the engine's `engine_log.html`:

```
../testbench.cpp:70:82: error: 'class Vfloat_repro' has no member named 'floating'
```

The same failure on the puzzle netlist, for its unrouted `n278`:

```
../testbench.cpp:106:78: error: 'class Vpuzzle' has no member named 'n278'
```

`hal_simulator` runs the identical setup without complaint, so this is engine-specific.

### Expected

Either the written netlist exposes every simulation input net as a port of the
generated top module, or the engine reports up front which nets it cannot drive
instead of producing a testbench that fails to compile.

### Where

* `plugins/simulator/verilator/src/verilator.cpp:188-203` — iterates
  `simInput->get_input_nets()` and writes `&dut-><net_name>` for each, with no check
  that the net is a port of the netlist the verilog writer just emitted.
* `plugins/simulator/netlist_simulator_controller/src/simulation_input.cpp:162-186`
  — `compute_input_nets()`, the classification that puts internal nets in that set.

### Suggested fix

In `VerilatorEngine::setSimulationInput` (or wherever the partial netlist is written),
promote every simulation input net that is not already a global input of the written
netlist to a module port before invoking verilator; failing that, fail the run early
with a message naming the offending nets, so the failure is a HAL diagnostic rather
than a C++ compile error buried in `engine_log.html`.
