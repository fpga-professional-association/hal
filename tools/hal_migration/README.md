# hal_migration — what a vendor migration would actually require

A migration does not start with a conversion. It starts with two questions a
netlist can answer honestly: **what is in this design**, and **what does the
target technology offer**. `hal_migration` answers the first from HAL and takes
the second from a manually reviewed, versioned catalogue, then reports where the
two meet — and, more usefully, where they do not.

It converts nothing, and it is built so that it *cannot* look as if it did.

## Three documents, all versioned

```
source inventory        target capability catalogue        assessment
(what the netlist has)  (what a reviewer signed off)       (hal_findings document)
source-inventory-1.0.0  target-catalogue-1.0.0             findings-1.0.0
```

**Source inventory** (`schema/source-inventory-1.0.0.schema.json`) — primitive
counts per gate type with the memory, clock, reset, arithmetic and I/O metadata
HAL can see; the clock and reset nets; the top-level ports; and an explicit list
of what could *not* be determined. Categories (`register`, `memory`,
`arithmetic`, `io`, `black_box`, …) are derived from the gate type properties HAL
reports — never from the gate type's name. It records the source design's tool
and device versions when the caller states them, and records their absence as a
gap when it does not.

**Target capability catalogue** (`schema/target-catalogue-1.0.0.schema.json`) — a
reviewed table with a `revision`, a `review` block (who, when, against what, and
the limitations), the target toolchain version, and one entry per source
primitive: the category the reviewer signed off (`supported` / `candidate` /
`unresolved`), the target primitive, the assumptions the mapping rests on, the
source metadata it requires, and the obligations it leaves open. One catalogue
ships: `catalogues/ice40ultra-to-generic-fpga-1.0.0.json` (ICE40ULTRA against a
deliberately *generic* SRAM-FPGA profile — see its `review.limitations`).

**Assessment** — a `hal_findings` document, which is what makes the guarantees
enforceable rather than editorial:

| what | how it appears |
| --- | --- |
| a proposed mapping | `heuristic` finding, `structural` method — the schema forbids a heuristic from carrying a formal method or claiming unbounded validity |
| an unresolved primitive | `unsupported` finding naming the gate type, its count and example gates |
| missing source metadata | `unsupported` finding of kind `configuration` |
| timing closure, resource fit, whole-design equivalence | `unknown` findings emitted on **every** run |

No status in the output can mean "converted" or "verified": the three statuses
above are the only ones the tool ever writes, and the unit tests assert it.

## Resolution is only ever downwards

* a gate type **not in the catalogue** is `unresolved` — silence is not approval;
* a mapping whose `requires_metadata` the inventory does not carry is
  `unresolved`, however confident the catalogue was, and the report says it was
  downgraded and why;
* a primitive the source gate library describes as a **black box** is
  `unresolved` even if the catalogue proposes a target — there is no source
  behaviour to compare against.

Nothing raises a category. `supported` means "a reviewer proposed this mapping
and the inventory carried the metadata it needs" — nothing more.

A worked example: with the shipped ICE40ULTRA library, `SB_RAM40_4K` is
`candidate` in the catalogue but comes out **unresolved**, because the library
models neither a `RAMComponent` (bit size) nor `RAMPortComponent`s (port
structure, read-during-write behaviour), and the mapping requires both.

## Command line

```bash
# needs a built HAL (hal_py):
python tools/hal_migration inventory examples/uart/ -o uart.inventory.json \
    --source-tool vivado --source-tool-version 2024.1

# needs nothing but a Python interpreter:
python tools/hal_migration assess --inventory uart.inventory.json \
    --catalogue ice40ultra-to-generic-fpga \
    -o assessment.json --report assessment.md

# both steps at once
python tools/hal_migration run design.v --gate-library plugins/gate_libraries/definitions/ice40ultra.hgl \
    --catalogue ice40ultra-to-generic-fpga --report assessment.md

python tools/hal_migration catalogues            # list the bundled catalogues
python tools/hal_migration validate *.json       # validate inventories/catalogues
python tools/hal_migration schema --kind catalogue --path
```

Exit codes: `0` success, `1` failure (bad input, invalid document, no HAL),
`2` only with `--fail-on-unresolved` when unresolved primitives were found. A
successful assessment full of unresolved primitives is a *success*: "this
migration has 14 open questions" is the answer, not an error.

## Integration with the rest of the fork

* **`tools/hal_findings`** — the assessment *is* a findings document; the
  builders, the schema and its validator are reused unchanged, and this tool's
  own two schemas are validated with `hal_findings.jsonschema_mini`, so no
  third-party dependency is added.
* **`tools/hal_viz`** — `hal_viz.halenv` is the shared `hal_py` bootstrap
  (`--hal-lib`, `$HAL_PY_PATH`, project/netlist loading); `hal_migration` adds no
  second way to find HAL.
* **`tools/hal_runner`** — the CLI is a plain subprocess with meaningful exit
  codes, so a runner step can shell out to it.
* Nothing in HAL changes: no plugin, no binding, no C++. The extractor reads
  what the existing bindings already return, duck-typed, so a missing accessor
  degrades to a recorded gap rather than a crash.

## Tests

```bash
# no HAL needed: 59 tests, including the inventory extractor driven by a stub
# netlist built from the real ice40ultra.hgl
python -m unittest discover -s tools/hal_migration -t tools -p "test_hal_migration.py"

# needs a built HAL (skips otherwise)
export HAL_PY_PATH=/path/to/hal/build/lib
python -m unittest discover -s tools/hal_migration -t tools -p "test_*_hal.py"
```

The standalone suite is registered nowhere yet: `tests/headless_smoke/CMakeLists.txt`
already registers `tools/hal_viz` and `tools/hal_runner` the same way, and
`hal_migration` belongs next to them —

```cmake
add_test(NAME runTest-hal_migration_standalone
         COMMAND ${Python3_EXECUTABLE} -m unittest discover
                 -s ${CMAKE_SOURCE_DIR}/tools/hal_migration -t ${CMAKE_SOURCE_DIR}/tools
                 -p "test_hal_migration.py")
```

— but that file is shared, so the line is left for whoever integrates this
branch rather than being added here.

The integration tests parse `fixtures/ice40_mixed.v` with the real bindings and
compare the extraction against the checked-in fixture (gate types, counts,
categories, pin metadata, clock/reset signals, I/O ports), then assess the
shipped `examples/uart.zip` against the *wrong* catalogue and check that the
library mismatch is reported instead of quietly producing an empty-looking
success. Fixture ground truth: `fixtures/README.md`.

## Limits (deliberate)

* **No conversion, no rewriting, no re-synthesis.** The tool never touches a
  netlist.
* **No timing, no resource fit.** Both are emitted as permanently open
  obligations; nothing in this tool can discharge them.
* **The catalogue is an opinion.** It is manually reviewed, versioned and
  hashed into the assessment's artifact list, but no part of it is checked
  against a vendor device model. The bundled catalogue's target is a *generic
  capability profile*, not a device: real work needs a device-specific catalogue.
* **Primitive semantics come only from the gate library.** Where the library is
  silent (RAM geometry, collision modes, reset synchronicity, DSP configuration,
  I/O constraints), the tool records a gap and refuses to assess, rather than
  reading the primitive's name and assuming the rest.
* **Clock nets are not clock domains.** The inventory counts nets that reach a
  clock pin; grouping them into domains is a separate analysis.
