# `hal_apb_recover` — recover an APB register map from a flattened netlist

A synthesised peripheral still contains its programming interface; synthesis
just removes every name that made it readable. This tool takes a flattened
netlist, a user-supplied mapping of the APB signals, and returns the register
map: addresses, aliases, read-mux associations, reset values, write enables,
byte-strobe lanes and access semantics — plus, for every single claim, how
strongly it is supported.

That last part is the point. A recovered register map that mixes proofs with
guesses and prints them the same way is worse than no map at all, because
somebody will program against the guesses.

```bash
python tools/hal_apb_recover recover \
    tools/hal_apb_recover/fixtures/apb_regs/apb_regs.v \
    tools/hal_apb_recover/fixtures/apb_regs/mapping.json \
    --gate-library plugins/gate_libraries/definitions/NangateOpenCellLibrary.hgl \
    -o build/apb_regs
```

writes four files:

| file | what it is |
| --- | --- |
| `apb_regs.json` | the register map, versioned (`fpgapa.apb-register-map` 1.0.0) |
| `apb_regs.md` | the same thing for humans, confidence column included |
| `apb_regs.findings.json` | a [`hal_findings`](../hal_findings/README.md) document — the shared contract every HAL analysis in this fork speaks |
| `apb_regs.replay.json` | replayable transactions generated *from the recovered map*, so the map can be executed against the netlist |

## How it works

Everything is a **probe**: pin the bus to one APB access (address, direction,
data pattern, strobes), pin one storage bit and one data bit, evaluate the
combinational next-state and read-mux functions three-valued, and read the
answer back. The interesting part is what is left *unpinned*, because that is
what decides the strength of the claim:

| environment | what it pins | what a definite answer means |
| --- | --- | --- |
| `abstract` | nothing but the bus and the bit under test | holds for **all** other inputs, state bits and data bits → `proven_under_assumptions` |
| `quiescent_low` / `quiescent_high` | non-bus inputs at the declared quiescent value, other state and data bits at 0 and at 1 | one transfer checked in two concrete environments → `proven_bounded`, cycle bound 1 |

Three-valued evaluation is what makes the first row sound: `0 & X` is `0`, so a
gate whose controlling input is pinned still produces a definite value. If the
answer is definite with everything else unconstrained, it is definite for every
assignment of everything else — no solver required, and no chance of a
"proof" that quietly depended on the rest of the design sitting at zero.

When the two pinned environments **disagree**, the tool does not pick the
convenient answer. It scans the state bits for the one that switches the
behaviour and reports a *guarded write* (`heuristic`, with the guard named); if
no state bit explains it, it scans the other data bits, and if one of those does
the field is reported `unknown` — a field whose next state depends on two data
bits is not describable by any single-bit access rule, and inventing one would
be a lie.

### What is recovered

* **address decode** — which addresses reach storage at all; addresses that
  touch nothing and read 0 are listed as unmapped rather than omitted
* **aliases** — addresses whose write associations, next-state templates and
  read-mux associations are identical, plus the PADDR bits that turned out to
  have no effect
* **read mux** — every PRDATA bit resolved to a storage bit (with polarity), to
  a constant, or to "ambiguous, {n} state bits reach it"
* **reset values** — from the flip-flop's own asynchronous set/reset functions
  with reset asserted; a flip-flop with no asynchronous reset gets *unknown*,
  not 0
* **write enables** — which of PSEL / PENABLE / PWRITE the write actually
  depends on, verified by probing each of them deasserted
* **byte strobes** — each lane probed alone, so the lanes a field really
  responds to are measured rather than derived from the bit index
* **access semantics** — `write`, `write_inverted`, `hold`,
  `write_one_to_clear`, `write_one_to_set`, `write_zero_to_clear`,
  `clear_on_write`, `set_on_write`, matched against the full `(data, state)`
  truth table

### What is deliberately not recovered

* **automatic interface detection** — the bus is user-supplied; that is the MVP
  boundary, and the mapping is the assumption everything else rests on
* **AXI-Lite** — out of scope
* **multi-cycle behaviour** — every claim is about the combinational next-state
  and read functions of *one* access; wait states and PREADY handshaking are not
  modelled
* **anything behind an unmodelled primitive** — latches, RAMs and tristate cells
  evaluate as unconstrained and are reported as `unsupported` findings. A
  register hidden behind one is invisible, and the report says so instead of
  presenting an incomplete map as a complete one.

## Layout

```
circuit.py          three-valued netlist model + cached evaluator (no HAL, no Verilog)
hgl_library.py      .hgl reader and expression evaluator, standard library only
verilog_source.py   structural Verilog -> circuit, for fixtures and offline runs
hal_source.py       hal_py netlist -> circuit; the only HAL-aware module
mapping.py          the user-supplied APB mapping and its assumptions
recover.py          the probing and classification
regmap.py           document validation, Markdown, storage-independent summary
findings.py         hal_findings document
replay.py           replay generation and execution
cli.py              recover / replay / validate / summary
fixtures/apb_regs/  the name-stripped fixture and its ground truth
```

The analysis only ever talks to `circuit.Circuit`, so the offline path and the
`hal_py` path run literally the same code. That is what makes the offline tests
worth anything — and `test_apb_recover_hal.py` closes the remaining gap by
requiring both paths to produce the same register map for the same netlist.

## Tests

Offline (no HAL build, standard library only — 55 tests, about nine seconds):

```bash
python -m unittest discover -s tools/hal_apb_recover -t tools -p "test_apb_recover.py"
```

They run the full recovery on the fixture and check it against
`fixtures/apb_regs/ground_truth.json`: the addresses, the alias, the
write-one-to-clear truth tables, the strobe lanes, the guarded register, the
reset values, the unsupported latch, the confidence of every single field, and
that a *wrong* register map makes the generated replay fail (a replay that
passed for any map would be worthless).

With a built HAL:

```bash
export HAL_PY_PATH=<build>/lib
export HAL_BASE_PATH=<build>
python -m unittest discover -s tools/hal_apb_recover -t tools -p "test_*_hal.py"
```

These load the same fixture through `hal_py`, run the same recovery and require
the result to be behaviourally identical to the offline one. They skip only when
`hal_py` is not importable *and* `HAL_PY_PATH` is unset; with `HAL_PY_PATH` set
a missing binding is a failure, not a skip.

## Mapping file

Signals are referenced by HAL net name (`pwdata(3)` for a vector bit) or by
`id:<net id>`. Vectors are LSB first.

```json
{
  "schema": "fpgapa.apb-signal-mapping",
  "schema_version": "1.0.0",
  "design": "apb_regs",
  "clock":  {"net": "io_00", "edge": "rising"},
  "reset":  {"net": "io_01", "active": "low", "kind": "asynchronous"},
  "apb": {
    "psel": "io_02", "penable": "io_03", "pwrite": "io_04", "pready": "io_05",
    "paddr":  ["io_06", "io_07", "io_08", "io_09", "io_10"],
    "pwdata": ["io_11", "..."],
    "prdata": ["io_27", "..."],
    "pstrb":  ["io_43", "io_44"]
  },
  "address": {"base": 0, "stride": 4, "count": 8},
  "assumptions": {
    "non_apb_inputs": "zero",
    "byte_lane_bits": 8,
    "notes": ["word-aligned accesses only"]
  }
}
```

The mapping is validated against the netlist before anything runs: an unknown
net, a net mapped to two roles, a PRDATA bit that is not a primary output or a
strobe width that does not cover the data bus is a hard error with the offending
name in the message. `assumptions.non_apb_inputs` picks the quiescent value for
the pinned environments (`zero`, `one`, or `unconstrained` to disable them).

`address.stride` bounds the enumeration; the byte offsets it skips are reported
in `coverage.address_bits.never_varied` rather than silently assumed to be
undecoded. Set `stride: 1` to have those offsets probed too.

## Running it against a built HAL

Either drive the CLI with the interpreter (what the tests do):

```bash
python3 tools/hal_apb_recover recover \
    tools/hal_apb_recover/fixtures/apb_regs/apb_regs.v \
    tools/hal_apb_recover/fixtures/apb_regs/mapping.json \
    --source hal --hal-lib "$HAL_BUILD/lib" \
    --gate-library plugins/gate_libraries/definitions/NangateOpenCellLibrary.hgl \
    -o build/apb_regs_hal
```

or run it inside HAL itself, so the recovery's verdict becomes HAL's exit code:

```bash
export HAL_APB_RECOVER_TOOLS=$PWD/tools
export HAL_APB_RECOVER_NETLIST=$PWD/tools/hal_apb_recover/fixtures/apb_regs/apb_regs.v
export HAL_APB_RECOVER_MAPPING=$PWD/tools/hal_apb_recover/fixtures/apb_regs/mapping.json
export HAL_APB_RECOVER_LIBRARY=$PWD/plugins/gate_libraries/definitions/NangateOpenCellLibrary.hgl
export HAL_APB_RECOVER_OUTPUT=/tmp/apb_regs_hal
export HAL_APB_RECOVER_REPLAY=1
"$HAL_BUILD/bin/hal" --python-script tools/hal_apb_recover/scripts/recover_in_hal.py
```

`scripts/recover_in_hal.py` takes its configuration from the environment
because `--python-args` is split on spaces and cannot carry a path containing
one. It exits 0 only if the recovery *and* the replay succeed.

## Exit codes

`0` success, `1` a real failure (bad mapping, unreadable netlist, replay
mismatch, `--fail-on-unresolved` with unresolved fields), `2` a usage error.
The `replay` subcommand exits 1 on any mismatch; `--allow-heuristic` restricts
that to transactions exercising proven or bounded claims.

## Integration notes

* **`hal_findings`** — `findings.py` emits documents against the shared schema
  in `tools/hal_findings`. Confidence maps onto the status vocabulary directly:
  `proven_under_assumptions` (unbounded, with the mapping's assumptions
  attached), `proven_bounded` (cycle bound 1), `heuristic`, `unknown`; coverage
  gaps become `unsupported` findings with a typed reason. A register whose bits
  fall in several tiers becomes several findings, because one finding carries
  one status. Validate with `python tools/hal_findings validate <file>`.
* **`hal_viz`** — not used. The register map is tabular, and a Graphviz drawing
  of a read mux would add nothing the Markdown table does not already say.
* **CI** — the offline suite needs nothing but Python and runs in seconds; the
  `hal_py` suite needs a build and is the one that catches binding drift.
* **This directory owns no shared files.** Nothing outside
  `tools/hal_apb_recover/` was added or edited.

## Limits worth repeating

The tool measures a netlist; it does not read a specification. Every document
it writes carries the assumptions it ran under and the addresses it never
probed, and both are printed in the Markdown export. A register map produced
here is evidence about a design, not a datasheet for it.
