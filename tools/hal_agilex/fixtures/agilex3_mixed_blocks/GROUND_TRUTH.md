# agilex3_mixed_blocks — expected results

Device **A3CW135BM16AE6S** (Agilex 3), Quartus Prime Pro **26.1.0 Build 110
03/26/2026**. Exact commands and file hashes: `MANIFEST.json`.

## Why this fixture exists

Everything else here is about what `hal_agilex` *does* support. This one is
about what it must refuse. `mixed_blocks.v` infers a 1024×8 memory and a hard
multiplier alongside a little LUT/FF logic, so the export contains blocks whose
semantics have not been established from any source available here.

## What the export contains

| primitive | count | covered |
| --- | --- | --- |
| `tennm_ff` | 17 | yes |
| `tennm_ram_block` | 8 | **no** |
| `tennm_mac` | 1 | **no** |
| `tennm_lcell_comb` | 1 | yes |

The memory is emitted as eight `tennm_ram_block` instances named
`mem_rtl_0|auto_generated|altera_syncram_impl1|ram_block2a0..7`, each with
around fifty parameters (port widths, clock-enable sources, ECC and coherent
read settings, mixed-port read-during-write behaviour). The multiplier is one
`tennm_mac` named `mult_0~DATAOUTA0`.

## Expected behaviour of the tooling

| command | expected |
| --- | --- |
| `hal_agilex inventory` | one `unsupported` finding, `kind: primitive`, naming `tennm_mac` (1) and `tennm_ram_block` (8) with the reason each is not modelled |
| `hal_agilex --strict fixture …` | exit code **2** |
| `hal_agilex import` | **refuses**, exit code 1, listing `tennm_mac, tennm_ram_block` |
| `hal_agilex behavior` | not applicable: the simulator refuses the first uncovered instance |

The point of the refusal is that there is no half-way import. A netlist that
dropped the RAM and the DSP would load, analyse cleanly, and mean nothing.

Note also that the *covered* primitives in this export are still only covered
in the configurations `hal_agilex` validates; the 17 `tennm_ff` instances here
happen to be in the modelled configuration, which is why the inventory reports
no additional configuration finding.

## Regenerating

See `MANIFEST.json`. The Agilex simulation models for these blocks
(`eda/sim_lib/fmica_atoms_ncrypt.sv` in a Quartus installation) are encrypted,
so their behaviour cannot be read out of the tool installation either — which
is precisely why they are reported as unsupported instead of modelled.
