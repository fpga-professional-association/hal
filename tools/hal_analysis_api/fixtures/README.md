# fixtures — a netlist whose answers are known by hand

`cone_fixture.v` is seven gates against `EXAMPLE_GATE_LIBRARY`
(`plugins/gate_libraries/definitions/example_library.hgl`, the library
`examples/uart.zip` also ships). It exists because the cone queries need a
design where the right answer is *derivable on paper*: `examples/uart.zip` is
excellent for "does this run at all against a real netlist" and useless for
"is the fan-in cone of this gate exactly these four gates".

```
    CLK --> clk_buf --+--> ff0.C
                      +--> ff1.C
    EN  --------------+--> ff0.CE, ff1.CE
    EN  --> en_inv --> and0.I1
    ff0.Q ----------> and0.I0
    and0.O ---------> ff1.D
    ff1.Q ----------+-> inv0.I --> ff0.D          (the feedback loop)
                    +-> out_buf.I --> OUT
```

`cone_fixture.ground_truth.json` records what that circuit *is*: the gates and
their pin-to-net map, the nine nets, the gate-level edges, and six cone
queries with their exact expected membership. It was written by hand from the
Verilog, not captured from a run — a fixture recorded from the code it checks
proves only that the code is consistent with itself.

Two independent checks read that same file:

| check | where | needs HAL |
| --- | --- | --- |
| `tools/hal_analysis_api/test_hal_analysis_api.py` | builds a stub netlist from the ground truth and runs `netlist_query` over it | no |
| `tests/headless_smoke/analysis_api_smoke.py` | parses `cone_fixture.v` with HAL and drives the real API through `hal --python-script` | yes |

So the same numbers have to come out of the pure-Python query layer *and* out
of HAL. If the two disagree, one of them is wrong and the fixture says which.

Gate ids are deliberately **not** part of the ground truth. HAL assigns them
when it parses the netlist; everything here is keyed by name, and the smoke
test resolves names to ids through `netlist.gates` before it asks for a cone —
which is also the sequence an agent has to follow.

To parse the fixture by hand:

```bash
HAL_BASE_PATH=<build> PYTHONPATH=<build>/lib python3 -c "
import hal_py
hal_py.plugin_manager.load_all_plugins()
n = hal_py.NetlistFactory.load_netlist(
    'tools/hal_analysis_api/fixtures/cone_fixture.v',
    'plugins/gate_libraries/definitions/example_library.hgl')
print(n.get_design_name(), len(n.get_gates()), len(n.get_nets()))
"
```
