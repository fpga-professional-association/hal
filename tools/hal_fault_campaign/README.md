# hal_fault_campaign — reproducible register-bit fault-injection campaigns

Structural redundancy tells you that a design *has* a checker. It does not tell
you whether that checker fires on the faults your workload actually exercises,
how many cycles later, or which corruptions walk straight out of the primary
outputs without anybody noticing.

`hal_fault_campaign` answers that question the only way it can honestly be
answered: by simulating. It flips one register bit in one cycle, compares the
run against a fault-free baseline of the same netlist, and classifies the result
from what the design's own outputs and detection signals did inside a stated
observation window. Then it records everything needed to do it again — the
netlist by content hash, the workload, the seed, the fault list and every
verdict — so a result can be replayed rather than believed.

```bash
export HAL_BASE_PATH=/path/to/hal/build
python tools/hal_fault_campaign run \
    tools/hal_fault_campaign/fixtures/campaign_short_window.json \
    --hal-binary /path/to/hal/build/bin/hal
```

That instruments the shipped fixture, runs 38 simulations (one uninstrumented
reference, one baseline, 36 faulty) and writes
`build/hal_fault_campaign/parity-counter-short-window/`:

```
manifest.json     what ran, against what, with which verdict — the replay record
findings.json     the results as a hal_findings document, schema-validated
traces.json       every recorded trace, so verdicts can be re-derived offline
request.json      exactly what the in-HAL step was asked to do
logs/             HAL's own stdout/stderr, kept whatever happens
```

## What a fault *is* here

One model, `register_output_transient_flip`, described in full in
[`faultmodel.py`](faultmodel.py):

> For a chosen sequential gate and a chosen cycle, the value the rest of the
> design sees on that register's state output is inverted for the whole of that
> cycle (or for `hold_cycles` consecutive cycles).

There is no simulator hook for that. `NetlistSimulatorController` can set *input*
nets between simulation steps and nothing else, so a fault model that poked an
internal net would have to live inside one engine and would work with that engine
only. Instead the **netlist itself is instrumented**, once, before anything runs:

```
FF.Q ──▶ new net ──▶ XOR ──▶ the net FF.Q used to drive ──▶ (unchanged)
                      ▲
                      └── new global input, one per fault site
```

With the control input at 0 the XOR is the identity, so the instrumented netlist
and the original simulate identically — and the **baseline and every faulty run
use the same instrumented netlist**, which is what makes a divergence
attributable to the injection instead of to a netlist edit. Every run also
checks that empirically, by simulating the *uninstrumented* netlist and requiring
the traces to match; a mismatch fails the campaign rather than annotating it.

Driving one control high for one cycle then flips exactly one register's observed
output for exactly that cycle. Everything that clocks inside the window —
including the register itself, through its own next-state logic — captures a
value computed from the corrupted one, so for a counter or an FSM the corruption
persists after the window closes, as a real upset does.

**Where it differs from a physical SEU**: a register whose next state does not
depend on its own output (a pipeline stage) recovers its *own* output when the
window closes, whereas a physical upset holds until the register is next written.
The corrupted value has by then already been captured downstream, so the
propagation is the same; `hold_cycles` widens the window when the difference
matters. This, and everything below, is attached to every finding the tool emits
as an explicit assumption — not buried in a README.

## The four verdicts

| class | meaning |
| --- | --- |
| `detected` | a detection signal became active inside the window, where the baseline left it inactive |
| `silent_divergence` | an observed output differs from the baseline inside the window and no detection signal ever fired — externally visible, undetected corruption |
| `unobserved_in_window` | neither happened **inside this window** |
| `indeterminate` | the faulty run produced X/Z where the baseline was defined, so divergence can be neither claimed nor ruled out |

`unobserved_in_window` is named for the window on purpose. It is **not** masking,
it is not "safe", and no finding this tool writes ever says otherwise — the
summary of every such finding contains the words *"the fault is NOT shown to be
masked"*. The shipped fixture demonstrates why: the same injections, the same
traces, judged over four cycles instead of one, move three registers from
`unobserved_in_window` to `silent_divergence`.

An X where the baseline was defined is its own class rather than "no divergence".
Silicon would have *some* level there; the simulation does not know which, and
counting it as clean would turn an unknown into a clean bill of health.

## Findings: what may be claimed

Results are [`hal_findings`](../hal_findings/README.md) documents, so the status
vocabulary does the arguing:

| class | status | why |
| --- | --- | --- |
| `silent_divergence` | `bounded_counterexample` | a concrete witness that an observable corruption got past the detector, valid up to the simulated cycle bound |
| `detected` | `proven_bounded` | verified for *this* trace and only up to the cycle bound |
| `unobserved_in_window` | `proven_bounded` | the bounded claim is "nothing reached the observed signals inside this window", stated in those words |
| `indeterminate` | `unknown` | no verdict |

Plus, always:

* a **campaign summary** — `proven_bounded` when the campaign was exhaustive over
  the declared grid, `heuristic` with a `statistical` method when it was a seeded
  sample, because counts over a sample are evidence and never a failure rate.
  It carries the coverage fraction, the seed, the latency statistics and the
  limitations verbatim;
* a **coverage gap** (`unsupported`) whenever a sequential gate could not be
  instrumented, naming the gate types — so "no finding for that register" is
  never confused with "that register is fine".

No finding is ever an unbounded proof. The end-to-end test asserts that.

## Which simulation engine

`hal_simulator` by default: HAL's built-in event-driven engine
(`NetlistSimulatorFactory`, registered by the `netlist_simulator` plugin under
`plugins/simulator/hal_simulator`). It runs in-process and needs **no external
tool**, which is why it is the default here even though the shipped C++
simulator tests (`plugins/simulator/netlist_simulator_controller/test/simulator_test.cpp`)
use `verilator` — those compare against recorded VCD dumps and can afford the
dependency; a CI job that must always run cannot.

Set `"engine": "verilator"` to use the other registered engine. It shells out to
the `verilator` binary and needs it on `PATH`. Nothing else in this tool changes:
the instrumentation is netlist-level and engine-agnostic by construction.

Both plugins need `-DPL_SIMULATOR=ON` (or `-DBUILD_ALL_PLUGINS=ON`, which the CI
workflows already pass).

## Cycles, edges and sample points

All of it in [`workload.py`](workload.py), so the injector, the classifier and
the reference model cannot disagree. With `P` the clock period in picoseconds:

```
cycle k occupies          [k*P, (k+1)*P)
its rising edge is at     k*P + P/2      (add_clock_period starts the clock low
                                          at t=0 and toggles every P/2)
stimulus for cycle k at   k*P            (stable across the rising edge)
cycle k is sampled at     k*P + 3P/4     (after the edge: registers hold what
                                          they captured at edge k, and the
                                          simulator is zero-delay)
an injection at cycle k   control high on [k*P, (k+1)*P)
```

"The value of S in cycle k" therefore always means: after cycle k's clock edge.
The period must be divisible by 4; one that is not is rejected, not rounded.

Two behaviours of the simulator stack the schedule has to accommodate, both
verified against the sources rather than guessed:

* `SimulationThread::run` calls the engine once per *distinct input transition
  time* and **never flushes the last batch**, so a run whose last input event is
  at the end of the workload would leave its final cycles unsimulated. The
  schedule therefore ends with a terminator transition in a guard cycle past the
  workload (see `steps/campaign_step.py::_terminator_events`), and every driven
  net is written its complement there, because
  `WaveData::insertBooleanValueWithoutSync` silently drops a write that does not
  change the value.
* `NetlistSimulator::prepare_clock_events` derives the clock phase from
  `base_time & 1` rather than from `base_time / switch_time`. That is only
  correct when every input transition lands on a whole clock period — which the
  cycle grid above guarantees. Keep it that way.

## The fixture and its ground truth

`fixtures/parity_counter.v` is hand written against
`plugins/gate_libraries/definitions/example_library.hgl` (the library
`examples/uart.zip` ships) in the same flat gate-level style as that example's
`uart.v`. Nine registers, three deliberately different fates:

| registers | what happens to a flip | short window (1 cycle) |
| --- | --- | --- |
| `CNT_reg_0..2`, `PAR_reg` | a parity register carries the parity of the *next* counter value, so `ERR` rises in the injection cycle | **detected**, latency 0 |
| `AUX_reg`, `PIPE_reg_2..3` | nothing checks them | `AUX_reg` diverges one cycle later, the late pipeline stages immediately |
| `PIPE_reg_0..1` | the corruption is still inside the pipeline | **unobserved in the window** — it reaches `PIPE_O` 2 and 1 cycles later |

All nine are `FFR` with an async reset driven from `RST`, because driven through
`NetlistSimulatorController` there is no way to preload sequential state
(`initialize_sequential_gates` is not on the controller) — without the reset
every register would start at X.

`fixtures/reference_model.py` is an **independent** cycle model of that Verilog:
eight lines of next-state logic, the same fault model, no HAL. It generates
`fixtures/ground_truth.json` (baseline, all 36 faulty traces, and the expected
verdicts for both shipped windows), which the unit tests check the classification
against and which `tests/headless_smoke/fault_campaign_smoke.py` checks *HAL's
simulation* against, cycle by cycle. If HAL and the model disagree, one of them
is wrong and the test says so instead of trusting whichever ran last.

```bash
python tools/hal_fault_campaign/fixtures/reference_model.py --write   # regenerate
```

Expected counts over the 9 × 4 grid: **16 detected / 8 silent / 12 unobserved /
0 indeterminate** in the one-cycle window; nothing unobserved in the four-cycle
one.

## The configuration

Plain JSON, validated against `schema/campaign-1.0.0.schema.json` before anything
runs — the same reasoning as `hal_runner`: YAML would read slightly nicer and
would drag a third-party parser into a tool whose point is to work inside a bare
HAL build container.

```jsonc
{
  "config_version": "1.0.0",
  "name": "parity-counter-short-window",
  "netlist": "parity_counter.v",
  "gate_library": "../../../plugins/gate_libraries/definitions/example_library.hgl",
  "output_dir": "../../../build/hal_fault_campaign/parity-counter-short-window",
  "engine": "hal_simulator",
  "clock":   { "net": "CLK", "period_ps": 8000 },
  "workload": {
    "cycles": 20,
    "stimulus": [ { "cycle": 0, "inputs": { "RST": 1, "PIPE_IN": 0 } } ]
  },
  "observation": {
    "outputs": ["CNT0", "CNT1", "CNT2", "AUX_O", "PIPE_O"],
    "detection_signals": ["ERR"],
    "window": { "mode": "relative", "start_offset": 0, "length": 1 }
  },
  "faults": {
    "model": "register_output_transient_flip",
    "hold_cycles": 1,
    "sites":  { "include": ["*_reg_*_inst"], "exclude": [] },
    "cycles": { "from": 3, "to": 6 },
    "sampling": { "mode": "exhaustive" }
  }
}
```

| field | meaning |
| --- | --- |
| `netlist` | HAL project directory, project `.zip`, `.hal` file, or an HDL netlist (the last needs `gate_library`). Relative paths resolve against the configuration file. |
| `clock.period_ps` | must be divisible by 4 (see the cycle grid above). |
| `workload.stimulus` | assignments applied at the start of the named cycle and held until changed. Values are `0` and `1` only; X/Z stimulus is not supported and is rejected rather than silently mapped. |
| `observation.outputs` | divergence here counts as externally observable corruption. |
| `observation.detection_signals` | the design's own "a fault was detected" signals. An empty list means every observable fault is reported as a silent divergence — which is the truth for a design with no checker. |
| `observation.window` | `absolute` (`start_cycle`/`end_cycle`, the same window for every fault) or `relative` (`start_offset`/`length`, anchored at the injection). |
| `faults.sites` | shell-style globs on the gate name, over the sequential gates the netlist offers. |
| `faults.sampling` | `exhaustive`, or `random` with a `count` **and** a `seed` — an unseeded campaign is rejected because it could not be replayed. |

Check one without running it — this needs no HAL at all:

```bash
python tools/hal_fault_campaign validate my_campaign.json
python tools/hal_fault_campaign plan     my_campaign.json
```

## Determinism and replay

Three separate guarantees, and they are separate on purpose:

**The enumeration is a function of the inputs.** Sites are ordered by gate
*name* (never by HAL's iteration order, never by ID alone — IDs come from the
parser). A random sample runs an explicit Fisher-Yates selection over
`random.Random(seed).randrange`, not `random.sample`, which is not part of
Python's compatibility contract; the algorithm is named in the manifest
(`fisher-yates/mt19937/1`) so that changing it invalidates old manifests
visibly.

**The manifest is a replay record, not a log.** It stores the resolved
configuration *and the fully enumerated fault list*, so replay re-injects exactly
the faults it names rather than re-sampling. It also stores the seed, so the
enumeration can be re-derived and cross-checked.

```bash
# re-run three named faults and require identical class and latencies
python tools/hal_fault_campaign replay out/manifest.json \
    --fault CNT_reg_0_inst@4 --fault PIPE_reg_3_inst@4 --fault f00021 \
    --verify-enumeration --hal-binary <build>/bin/hal

# re-derive every verdict from the recorded traces — no HAL, no simulator
python tools/hal_fault_campaign recheck out/manifest.json

python tools/hal_fault_campaign manifest out/manifest.json --faults
python tools/hal_fault_campaign manifest out/manifest.json --digest
```

`replay` re-hashes the netlist and the gate library first and **refuses** to run
when they no longer match what the manifest recorded: re-running against
different bytes is not a replay, and a confidently wrong comparison is worse than
an error. `--allow-input-drift` overrides it, loudly.

`recheck` is the cheap one and runs anywhere: it re-derives every classification
from `traces.json` and compares. It catches a manifest whose verdicts do not
follow from its own evidence. It cannot catch a wrong trace, and it says so.

`manifest --digest` hashes the manifest with everything unreproducible removed
(timestamps, durations, absolute paths, host details), so two runs of the same
campaign against the same HAL share a digest.

## Nothing is trusted

The campaign has failed if HAL exits nonzero, is killed at its limit, exits 0
without writing a result, declares an artifact it did not write, or writes a
findings document that does not validate. Every one of those still produces a
manifest with a diagnostic record — a failed run needs a record more than a
successful one does — and exit code 1.

| exit code | meaning |
| --- | --- |
| 0 | the campaign succeeded / the replay or recheck matched |
| 1 | the campaign failed, or a replay or check found a mismatch |
| 2 | the command could not start: bad configuration, missing input, no `hal` |
| 130 | interrupted |

The whole campaign is **one** `hal --python-script` subprocess rather than one
per fault: loading and instrumenting the netlist is the expensive part and is
identical for every injection, while the process boundary exists for
cancellation and blast radius, which one campaign-sized process still provides.
The request travels in `HAL_FAULT_CAMPAIGN_REQUEST` and not on the command line,
because `hal --python-script` splits `--python-args` on spaces; the step also
gets neither `__file__` nor a usable `sys.argv`.

## Limitations, stated once and attached to every result

* Zero-delay digital model. No timing, no glitch propagation or filtering, no
  metastability, no charge sharing, no multi-bit upsets, no clock- or
  reset-tree faults. Nothing here is a silicon measurement.
* One fault per simulation run, and one workload per campaign. A different
  workload can move a fault between classes.
* Detection latency is measured in whole clock cycles of the simulated workload,
  from the injection cycle to the first cycle a detection signal is active. It is
  not a hardware latency.
* Only registers with a `state` output pin can be sites. Latches, RAM and any
  sequential primitive without one are reported as an explicit coverage gap.
* Faults in combinational logic, memories, I/O and the detection logic itself are
  out of scope.
* A sampled campaign covers a subset of the (register × cycle) space. Class
  counts are counts over that subset and are **not** a failure rate, a diagnostic
  coverage figure or a reliability metric.
* `unobserved_in_window` is not masking. See above, twice.

## Layout

```
faultmodel.py    the fault model, the classes and the assumptions, written once
workload.py      the cycle grid and the stimulus schedule
campaign.py      deterministic site x cycle enumeration and seeded sampling
classify.py      traces -> verdict, pure
findings.py      verdicts -> a hal_findings document
manifest.py      the replay record, built on hal_runner.hashing
replay.py        replay, recheck and enumeration verification
protocol.py      the request/result contract with the in-HAL step
runner.py        the host side; runs hal once through hal_runner.execute
cli.py           validate / plan / run / replay / recheck / manifest / schema
schema/          the versioned configuration schema
steps/           the only code that imports hal_py
  campaign_runner.py  the hal --python-script entry point
  campaign_step.py    the campaign pipeline
  instrument.py       netlist instrumentation
  simulate.py         driving NetlistSimulatorController
fixtures/        the fixture, its independent model and its ground truth
```

Everything except `steps/` runs on a plain interpreter with the standard library
(plus `hal_findings` and `hal_runner`, which are equally pure).

## Tests

```bash
python -m unittest discover -s tools/hal_fault_campaign -t tools -p "test_*.py"
```

91 tests, no HAL and no netlist: the cycle grid, the enumeration, the
classification, the findings, the manifest, the replay comparison and the whole
host-side orchestrator (against a stub executor), plus the fixture's ground
truth. The cases that matter are the negative ones — a window that runs off the
end of the run, a detection signal already active in the baseline, an X where the
baseline was defined, a random campaign without a seed, a replay against a
netlist that changed, a step that exits 0 without writing anything.

What that cannot cover is whether a simulation is right. That is
`tests/headless_smoke/fault_campaign_smoke.py`, which needs a built HAL:

```bash
HAL_BASE_PATH=<build> python3 tests/headless_smoke/fault_campaign_smoke.py \
    --hal-binary <build>/bin/hal --work-dir <build>/fault_campaign_smoke --keep
```

It runs both shipped campaigns, requires HAL's traces to equal the reference
model's cycle by cycle, checks the verdicts against the ground truth, checks the
findings, rechecks and replays the manifest, and requires the tool to refuse a
replay against a modified netlist.

## Integration notes

These are deliberately **not** wired up by this change, because the files
involved are shared and this tool was written to land alongside other work:

* `tests/headless_smoke/CMakeLists.txt` — the standalone suite is a ctest
  one-liner, matching `runTest-hal_runner_standalone`:

  ```cmake
  add_test(NAME runTest-hal_fault_campaign_standalone
           COMMAND ${Python3_EXECUTABLE} -m unittest discover
                   -s ${CMAKE_SOURCE_DIR}/tools/hal_fault_campaign
                   -t ${CMAKE_SOURCE_DIR}/tools -p "test_*.py"
           WORKING_DIRECTORY ${CMAKE_SOURCE_DIR})
  ```

* `.github/workflows/ubuntu24.04.yml` — `fault_campaign_smoke.py` wants its own
  CI step next to `real_netlist_smoke.py` and `runner_smoke.py`, with
  `--work-dir` and an artifact upload on failure, for the same reason those two
  are not ctest tests: when it fails you want the whole work directory.

* The emitted `findings.json` is an ordinary `hal_findings` document, so
  `python tools/hal_findings summary out/findings.json` works today and the
  `hal_viz report` findings-to-HTML renderer (issue #18) will work on it with no
  change here — as of this commit that subcommand is not on `master` yet, so
  nothing is wired to it.
