# hal_explain fixtures

`hal_explain` composes *results*, so its fixtures are a netlist **and** the
results three analyses report about it.

| file | what it is |
| --- | --- |
| `accumulator.v` | the netlist, hand-written against `plugins/gate_libraries/definitions/example_library.hgl` |
| `accumulator_ground_truth.json` | what the composition is expected to produce, block by block and region by region |
| `findings-dataflow.json` | DANA's register groups, as `hal_findings.adapters.dataflow` writes them |
| `findings-module-identification.json` | two verified operations and one checked-and-unverified cone |
| `findings-fsm.json` | the controller as `tools/hal_fsm` reports it |
| `_generate.py` | regenerates the three findings documents; `--check` is the drift check the unit tests run |

## The netlist

Four things share it so that no single analysis explains it:

```
acc_r0..acc_r3   a 4-bit accumulator register   -> DANA groups these
a_*              a 4-bit ripple-carry adder     -> module_identification verifies this
cmp_*            o_acc == 4'b1010               -> module_identification verifies this
st_a / st_b      a 3-state controller           -> hal_fsm / solve_fsm solves this
```

and two things exist so the model has to admit what it does not know:

```
p_x0..p_x2       a parity tree over the data inputs -- checked and not verified
f_nb..f_db       the controller's next-state logic  -- named by no document at all
u_gnd / u_vcc    tie cells, reachable only through constant nets
```

The `f_*` gates are the interesting case. `hal_fsm` scopes every finding to the
state register and never names the transition logic it handed to `solve_fsm`, so
those five gates are genuinely unclaimed and the composed model shows them as an
unknown region. That is not a gap in `hal_explain`; it is what the evidence
says. See "Integration notes" in `../README.md` for the one-line change upstream
that would close it.

The adder candidate deliberately contains the accumulator flip-flops as well,
because `module_identification` builds candidates *around* known registers. The
resulting overlap with DANA's group is the fixture for "two analyses disagree
about a boundary and the model records it instead of picking a winner".

Everything is written in the subset `tools/hal_cdc/fixture_netlist.py` reads —
one module, scalar nets, named port connections, no parameters — so the whole
composition is unit-testable on a machine that cannot build HAL. HAL's
`verilog_parser` stays the authority:
`tests/headless_smoke/explain_blocks_smoke.py` loads the same file through it and
fails if the two disagree about a single gate.

## The recorded findings documents

Two of the three go through the *real* adapters
(`hal_findings.adapters.dataflow` and
`hal_explain.adapters.module_identification`), driven by stub plugin results
built from the fixture netlist, so a change in either adapter shows up here as a
diff. The hal_fsm document is assembled directly with `hal_findings.model`
because reproducing `solve_fsm`'s in-HAL path outside HAL is not worth the
machinery; its finding IDs, statuses, assumptions and `data` layout mirror
`tools/hal_fsm/findings.py` field by field, and the smoke test composes the
*real* hal_fsm output in the container, so drift between the two is caught
there rather than assumed away.

```bash
python tools/hal_explain/fixtures/_generate.py           # rewrite them
python tools/hal_explain/fixtures/_generate.py --check   # fail if they are stale
```

## Changing the fixture

`accumulator_ground_truth.json` matches blocks by their **gate set**, never by
`block_id`, so renumbering is free. Anything else — a new gate, a changed
status in one of the recorded documents — has to be reflected there, and the
unit tests will say so.
