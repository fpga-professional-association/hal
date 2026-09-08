# hal_cdc fixtures — netlists and their expected verdicts

Four hand-written structural Verilog netlists, one declaration document each.
They are written against
`plugins/gate_libraries/definitions/example_library.hgl` — the library that
ships with this repository and with `examples/uart.zip` — so nothing here
depends on a library that has to be generated first.

Cells used, with the pin types the library actually declares:

| cell  | pins (type)                                                     |
| ----- | --------------------------------------------------------------- |
| `FF`  | `C` (clock), `CE` (enable), `D` (data) → `Q` (state)            |
| `FFR` | `FF` + `R` (reset); `clear_on: R`                               |
| `BUF` | `I` → `O` (property `c_buffer`)                                 |
| `INV` | `I` → `O` (property `c_inverter`)                               |
| `XOR` | `I0`, `I1` → `O`                                                |
| `AND2`| `I0`, `I1` → `O`                                                |
| `MUX` | `I0`, `I1`, `S` (select) → `O`                                  |
| `VCC` | `O` (property `power`) — drives the `CE` pins, since `FF`'s next state is `(D & CE)` |

Every clock enable is tied to a `VCC` instance rather than to `1'b1` so the
netlists parse identically through HAL's `verilog_parser` and through
`hal_cdc.fixture_netlist`, and so `CE` never looks like a domain source.

The ground truth below is asserted twice: by `tools/hal_cdc/test_hal_cdc.py`
(fixture reader, runs anywhere) and by `tools/hal_cdc/test_hal_cdc_hal.py`
(HAL's own parser, runs in the container). Those two also compare their full
audit signatures against each other, so the readers cannot drift apart.

---

## `two_flop_sync.v` + `two_flop_sync.json`

A single clean crossing through the one structure this pass recognises.

```
din ──▶ src_reg (clk_a) ──▶ sync_meta_reg (clk_b) ──▶ sync_out_reg (clk_b) ──▶ dout
```

**Expected**

| what | value |
| --- | --- |
| domains | `clk_a` = {`src_reg`}, `clk_b` = {`sync_meta_reg`, `sync_out_reg`} |
| clock resolution kind | `declared` for all three |
| crossings | exactly 1: `clk_a → clk_b`, role `data`, into `sync_meta_reg.D` |
| classification | `two_flop_synchronizer`, 2 stages, `[sync_meta_reg, sync_out_reg]` |
| supporting path | source gates `[src_reg]`, no intervening combinational gates |
| inputs with unknown domain | none (`din` is declared as synchronous to `clk_a`) |
| reset targets | none |
| `hal_cdc audit` exit code | 0 |

Why `sync_meta_reg` is accepted: its `D` sees exactly one source domain, it is
fed with no logic in between, and `meta_q` has exactly one load — the `D` pin
of `sync_out_reg`, clocked by the same domain.

---

## `direct_crossing.v` + `direct_crossing.json`

Four crossings from `clk_a` into `clk_b`, one per way a synchroniser can fail.
None of them may be recognised.

**Expected** — 4 crossings, all alarming, exit code 1:

| destination | pin | role | classification | why |
| --- | --- | --- | --- | --- |
| `single_reg` | `D` | data | `unsynchronized` | captured once, never re-registered (`single_q` leaves through `out_d` to a primary output) |
| `meta_reg` | `D` | data | `unsynchronized` | `meta_q` drives **two** loads (`tail_reg.D` and `dup_buf.I`), so the two loads can resolve one metastable event differently |
| `logic_reg` | `D` | data | `unsynchronized` | `a_xor` sits between the domains; path gates `[a_xor]`, source gates `[a_reg, a_reg2]` |
| `en_reg` | `CE` | control | `unsynchronized_control_path` | a control pin is never a synchroniser's first stage |

Same-domain paths (`a_reg2.D ← a_q`, `tail_reg.D ← meta_q`, `en_reg.D ← tail_q`)
must **not** appear as crossings.

### `direct_crossing_waived.json`

The same netlist with two waivers:

* `W-LOGIC-REG` scopes to gate `logic_reg` on `clk_a → clk_b` and carries a
  rationale. Expected: that crossing is still reported, with
  `classification: unsynchronized` preserved in the data and
  `effective_class: waived`, `severity: info`, and the rationale attached as an
  **undischarged** `user_provided` assumption. Alarming count drops to 3.
* `W-STALE` matches nothing. Expected: a `cdc/waivers/unused` finding with
  status `unknown` naming it.

---

## `ambiguous_clock.v` + `ambiguous_clock.json`

Two generated clocks with deliberately different outcomes.

**Expected**

| register | clock net | resolution | domain |
| --- | --- | --- | --- |
| `mux_reg` | `MUX(clk_a, clk_b, sel)` | kind `unknown`, `ambiguous: true`, `reached_clocks: (clk_a, clk_b)` | **unknown** |
| `gated_reg` | `AND2(clk_a, en)` | kind `derived`, `ambiguous: true`, `enable_nets: [en]` | `clk_a` |

* `result.domains == ["clk_a"]`; `clk_b` names no domain and gets a
  `cdc/domain/clk_b/empty` finding with status `unknown`.
* No crossings at all: `mux_reg`'s domain is unknown, so enumerating its inputs
  as crossings would have to invent a destination domain, and `gated_reg`'s
  only data input (`din`) is declared as `clk_a`.
* The unresolved clock is reported as `status: unknown`, never as safe.

---

## `reset_release.v` + `reset_release.json`

One asynchronous reset (`arst`), released two different ways.

```
arst ─┬─▶ rst_sync_reg1.R ─┐
      │   (clk_a, D=1)     ├─▶ rst_sync_reg2 ─▶ INV ─▶ a_reg.R   (clk_a)
      ├─▶ rst_sync_reg2.R ─┘
      └─▶ b_reg.R                                                (clk_b)
```

**Expected** — 4 reset targets:

| register | domain | release | alarming |
| --- | --- | --- | --- |
| `rst_sync_reg1` | `clk_a` | `reset_synchronizer_stage` | no |
| `rst_sync_reg2` | `clk_a` | `reset_synchronizer_stage` | no |
| `a_reg` | `clk_a` | `synchronized_deassert` (stages `[rst_sync_reg1, rst_sync_reg2]`) | no |
| `b_reg` | `clk_b` | `asynchronous_unsynchronized_deassert` | **yes** |

* `reset_domain_map["arst"] == ["clk_a", "clk_b"]`, so a
  `cdc/reset/arst/multi-domain` finding is emitted at severity `medium`.
* The synchroniser's own flip-flops must **not** be reported as unsynchronised
  just because their `R` pin is tied to the raw reset — that is the intended
  structure, and it is what `reset_synchronizer_stage` means.
* An asynchronous reset arriving at a reset pin is reported by the reset pass
  only; it must not *also* appear as an "input with an unknown domain".
* `hal_cdc audit` exits 0 with the default `--fail-on unsynchronized` (there
  are no crossings) and 1 with `--fail-on any-alarm`.

---

## Running them

Without HAL (fixture reader, any machine):

```bash
python -m unittest discover -s tools/hal_cdc -t tools -p "test_hal_cdc.py"

python tools/hal_cdc audit tools/hal_cdc/fixtures/direct_crossing.v \
    --fixture-reader \
    --gate-library plugins/gate_libraries/definitions/example_library.hgl \
    --declarations tools/hal_cdc/fixtures/direct_crossing.json \
    --no-clock-tree -o /tmp/direct_crossing.findings.json
```

With HAL (container):

```bash
export HAL_PY_PATH=<build>/lib HAL_BASE_PATH=<build>
python -m unittest discover -s tools/hal_cdc -t tools -p "test_*_hal.py"

python tools/hal_cdc audit tools/hal_cdc/fixtures/reset_release.v \
    --gate-library plugins/gate_libraries/definitions/example_library.hgl \
    --declarations tools/hal_cdc/fixtures/reset_release.json \
    --fail-on any-alarm -o /tmp/reset_release.findings.json
```
