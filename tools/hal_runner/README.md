# hal_runner — reproducible headless HAL runs

Running a HAL analysis today means assembling plugin calls by hand: unpack a
project, open a shell, call DANA or `graph_algorithm`, drop files somewhere.
Nothing about that is reproducible. Six weeks later nobody can say which netlist
produced `groups.txt`, which HAL built it, what the analysis was configured
with, or whether the run even finished.

`hal_runner` makes the **run** the artifact. A run configuration declares the
netlist, the gate library, an ordered list of analysis steps with their
configuration and per-step limits, and an output directory. Executing it
produces a manifest that pins every input by content hash, records the HAL
version, every step's configuration digest, outcome and artifact hashes, and a
[`hal_findings`](../hal_findings/README.md) document per step.

## The one command

```bash
export HAL_BASE_PATH=/path/to/hal/build          # so hal finds its plugins
python tools/hal_runner run tools/hal_runner/examples/uart_components.json \
    --hal-binary /path/to/hal/build/bin/hal
```

That imports `examples/uart.zip`, runs two deterministic analyses over it and
writes `build/hal_runner/uart-baseline/`:

```
manifest.json                     what ran, against what, with which result
inputs/uart/uart/                 the project unpacked from the pinned archive
steps/feedback-loops/
    findings.json                 the analysis result, schema-validated
    request.json                  exactly what the step was asked to do
    result.json                   what the step reported back
    logs/stdout.log, stderr.log   HAL's own output, kept whatever happens
steps/connectivity/…
cache/<key>/                      checkpoints, addressed by content
```

Run it again and both steps are *reused* rather than recomputed. Change the
netlist, the configuration or the HAL version and they are not.

```bash
python tools/hal_runner manifest build/hal_runner/uart-baseline/manifest.json
python tools/hal_findings summary build/hal_runner/uart-baseline/steps/*/findings.json
```

## The run configuration

Plain JSON, validated against `schema/run-config-1.0.0.schema.json` before
anything runs. YAML would read slightly nicer and would drag a third-party
parser into a tool whose point is to work inside a bare HAL build container.

```json
{
  "config_version": "1.0.0",
  "name": "uart-baseline",
  "netlist": "../../../examples/uart.zip",
  "output_dir": "../../../build/hal_runner/uart-baseline",
  "defaults": { "timeout_s": 600 },
  "steps": [
    {
      "id": "feedback-loops",
      "analysis": "graph_algorithm.connected_components",
      "config": { "strong": true, "min_size": 2 },
      "timeout_s": 300
    }
  ]
}
```

| field | meaning |
| --- | --- |
| `netlist` | HAL project directory, project `.zip`, `.hal` file, or HDL netlist (the last needs `gate_library`). Relative paths resolve against the configuration file. |
| `output_dir` | where the manifest, per-step directories and logs go (default `./<name>`). |
| `defaults` | `timeout_s` / `memory_mb` applied to every step that does not override them. |
| `steps[].id` | unique, and used as the step's directory name. |
| `steps[].analysis` | a name from `python tools/hal_runner analyses`. |
| `steps[].config` | analysis options. **Unknown keys are rejected**, so a typo fails the configuration instead of silently doing nothing. |
| `steps[].continue_on_failure` | run the remaining steps anyway; the run still fails. |
| `stop_on_failure` | default `true`; remaining steps are recorded as `skipped`, never as successful. |

Check one without running it — this needs no HAL at all:

```bash
python tools/hal_runner validate tools/hal_runner/examples/uart_components.json
python tools/hal_runner run my_run.json --dry-run     # plan + cache keys + commands
```

## Analyses

```bash
python tools/hal_runner analyses
```

| name | plugin | claims |
| --- | --- | --- |
| `graph_algorithm.connected_components` | `graph_algorithm` | structural, **deterministic** |
| `dataflow.groups` | `dataflow` (in `plugins/dataflow_analysis`) | heuristic |

`graph_algorithm.connected_components` is the reference workflow because it is
deterministic: `NetlistGraph.from_netlist` plus igraph's component
decomposition is a function of the netlist alone, so two runs produce identical
findings — which is what makes a checkpoint cache and a manifest digest worth
anything. Each component is recorded as `proven_under_assumptions` with a
`structural` method and its assumptions spelled out (how the graph was derived,
and that the claim is about connectivity rather than behaviour). Calling it
`heuristic` would be dishonest in the other direction; nothing about a strongly
connected component is a guess.

`dataflow.groups` reuses `hal_findings.adapters.dataflow` unchanged, so every
recovered register group stays `heuristic` and sequential gate types that ended
up in no group are reported as an explicit coverage gap.

Adding an analysis is an entry in `analyses.py` plus a module under `steps/`
exposing `run(hal_py, netlist, config, context) -> StepOutcome`.

## Why steps are subprocesses

Each step is one `hal --python-script` invocation of `steps/step_runner.py`.

A HAL analysis is C++ with no cancellation points: there is no cooperative way
to stop `dataflow.analyze` half way, and a Python thread cannot interrupt it.
A process can be killed — so **the process boundary is the cancellation
mechanism**. It also contains the damage: a plugin that segfaults or exhausts
memory costs one step, not the run. And it makes `hal`'s exit code (trustworthy
since [#11](https://github.com/fpga-professional-association/hal/issues/11))
the primary success signal.

The step's request travels in the `HAL_RUNNER_REQUEST` environment variable
rather than on the command line, because `hal --python-script` splits
`--python-args` on spaces before handing it to Python — any path with a space
in it would arrive in pieces. `step_runner.py` also cannot use `__file__` or
`sys.argv`; HAL runs the file's *source*, not the file.

Limits (`tools/hal_runner/execute.py`): a timeout escalates SIGTERM → SIGKILL
across the child's whole process group, so plugin threads go with it;
`memory_mb` is applied with `setrlimit(RLIMIT_AS)` where the platform has one
and is **reported as unenforced** where it does not, because silently ignoring
a declared limit is worse than not offering one.

## Nothing is trusted

A step has failed if it

* exits non-zero, or
* is killed at its limit, or
* exits 0 but writes no `result.json`, or
* declares an artifact it did not write, or
* writes a findings document that does not validate.

Every failure gets a **diagnostic record** next to the retained logs: a real
findings document with status `timeout` (carrying the limit it hit) or `error`
(carrying the typed error and the tail of stderr). A log file nobody reads is
how pipelines lose failures.

The run then ends with exit code `1`, the manifest records
`run.status: "failure"`, and the manifest is written anyway — a failed run needs
a record more than a successful one does.

| exit code | meaning |
| --- | --- |
| 0 | every step succeeded or reused a verified checkpoint |
| 1 | the run finished, at least one step failed, timed out or was skipped |
| 2 | the run could not start: bad configuration, missing input, no `hal` binary |
| 130 | interrupted |

## Checkpoints, and why they are safe

A checkpoint is addressed by a hash over the input digests, the analysis name
and its *resolved* configuration (defaults included), the HAL version **and how
that version was determined**, and the versions of `hal_runner`, its analysis
registry, the findings schema and the cache format. Change any of those and the
key changes, so the old entry is never looked at.

On a key match the entry still has to prove itself: the recorded key components
must match the ones just computed, every artifact must be present, and every
artifact's `sha256` must still match. Anything that does not add up is reported
in the manifest with a reason and the step re-runs. **A doubt is a miss.**

```bash
python tools/hal_runner run my_run.json --no-cache        # neither read nor write
python tools/hal_runner run my_run.json --refresh-cache   # ignore, then rewrite
python tools/hal_runner run my_run.json --cache-dir /shared/hal_cache
```

## The manifest

```bash
python tools/hal_runner manifest out/manifest.json            # human summary
python tools/hal_runner manifest out/manifest.json --digest   # reproducible digest
python tools/hal_runner manifest out/manifest.json --strict   # exit 1 unless successful
```

It records the tool identity (HAL version + the binary's own hash, hal_runner,
findings schema), every input with its digest (`sha256` for a file, an
explicitly named `sha256-tree/1` for a directory — never a digest of our own
construction in a field called `sha256`), the normalized configuration and its
digest, and per step: status, configuration digest, cache key and decision,
exit code, limits, artifact hashes, findings digest and status counts, and the
paths of the logs.

`manifest --digest` hashes the manifest with the parts that cannot be
reproduced removed (timestamps, durations, absolute paths, the machine's Python
build), so two runs of the same configuration against the same HAL share a
digest and CI can diff runs instead of eyeballing them.

To read a finished run rather than diff it, hand its findings documents to
`python tools/hal_viz report out/**/findings.json -o out/report.html`: one
static HTML page with the status of every finding, its assumptions and bounds,
and links to the artifacts the run produced. It needs no HAL and opens offline
(see `tools/hal_viz/README.md`).

## Layout

```
config.py        run configuration parsing + schema validation
analyses.py      the registry of analyses a step may name
hashing.py       file, directory and JSON digests
execute.py       subprocess execution with timeout, kill and retained logs
checkpoint.py    content-addressed checkpoint store
manifest.py      manifest construction, deterministic output, tool identity
diagnostics.py   findings documents for steps that produced no result
protocol.py      the request/result contract between runner and step
runner.py        the orchestrator
cli.py           run / validate / manifest / analyses / schema
steps/           the in-HAL side, executed by hal --python-script
```

Everything except `steps/` runs on a plain interpreter with the standard
library only; `steps/` is the only place that imports `hal_py`, and it loads
netlists through `hal_viz.halenv` rather than reimplementing that.

## Tests

```bash
python -m unittest discover -s tools/hal_runner -t tools -p "test_*.py"
```

65 tests, no HAL and no netlist: the orchestrator reaches the outside world
through one method, so a stub executor that writes what a real step would write
exercises the manifest, the checkpoint store, failure propagation and the
diagnostics. The cases that matter are the negative ones — a cache that must
*not* hit, a step that exits 0 without producing anything, a findings document
that does not validate. ctest runs this suite as `runTest-hal_runner_standalone`.

What that cannot cover is whether a step runs a real analysis. That is
`tests/headless_smoke/runner_smoke.py`, which needs a built HAL and does not
skip without one:

```bash
HAL_BASE_PATH=<build> python3 tests/headless_smoke/runner_smoke.py \
    --hal-binary <build>/bin/hal --work-dir <build>/runner_smoke --keep
```

It runs the documented command against `examples/uart.zip` and checks the
manifest, the findings (the UART's 68/50/15 gate feedback loops and its single
407-gate block, the same numbers `real_netlist_smoke.py` pins), that a second
run reuses both checkpoints with identical findings digests, that a corrupted
cached artifact is rejected rather than served, and that a step given a 1 ms
limit is killed, fails the run and leaves a valid `timeout` diagnostic behind.
CI runs it on every push and uploads the work directory when it fails.
