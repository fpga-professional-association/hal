# The five cores

This directory is the exercise. Five Agilex 3 netlists, blinded:

```
core_a.anon.hal.v
core_b.anon.hal.v
core_c.anon.hal.v
core_d.anon.hal.v
core_e.anon.hal.v
```

Each is a real Quartus Prime Pro 26.1 post-synthesis export, imported with
`tools/hal_agilex import` and then put through `../anonymize.py`. What survives
is what a reverse engineer genuinely has:

* gate types and their pin names, because those come from the device;
* `lut_mask` / `extended_lut` / `shared_arith` parameters, likewise;
* the connectivity, exactly;
* top-level port direction, width and bus grouping — package pins really are
  grouped and ordered;
* `gnd` / `vcc` and the `devclrn` / `devpor` / `devoe` housekeeping nets.

What does not: the module name, every port name, every net name, every instance
name, and **every internal vector declaration** — those are split into unrelated
scalar wires, because the word boundaries a vendor export keeps are exactly what
a bitstream-recovered netlist does not have.

One of the five is not cryptography. Which one is not recorded here.

```bash
# the whole blind pass, all five cores
python3 examples/agilex3_walkthroughs/16_mystery_cores/analysis.py blind \
    -o examples/agilex3_walkthroughs/16_mystery_cores/artifacts

# or start where every walkthrough in this series starts
python3 tools/hal_agilex --strict inventory \
    examples/agilex3_walkthroughs/16_mystery_cores/cores/core_a.anon.hal.v
python3 tools/hal_crypto identify \
    examples/agilex3_walkthroughs/16_mystery_cores/cores/core_a.anon.hal.v
```

The answer key is in `../ground_truth/`. It is worth not reading it first.
