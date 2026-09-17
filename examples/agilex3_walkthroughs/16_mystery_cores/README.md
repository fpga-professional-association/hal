# 16_mystery_cores — blind identification, and an honest score

The capstone of the Agilex 3 series. Five anonymized netlists, one procedure
fixed in advance, no answer key until the calls are written down.

Open [`guide.html`](guide.html) in a browser — it is offline-safe and needs
nothing installed. [`spec.md`](spec.md) is the experimental protocol and was
written first; [`ground_truth/`](ground_truth/) is the answer key and the blind
half of the analysis never opens it.

## The result

| core | what it is | truth | blind call | `hal_crypto identify` | key facts |
| --- | --- | --- | --- | --- | --- |
| `core_a` | Keccak-f[200] permutation | `sponge` / `undetermined` | **correct** | `none-detected` — **missed** | 4 / 6 |
| `core_b` | serial link framer — the decoy | `none-detected` | **correct** | correct | 3 / 4 |
| `core_c` | Trivium keystream generator | `lfsr-stream` / `classical-style` | **correct** | correct | 4 / 5 |
| `core_d` | Speck32/64 block cipher | `arx` / `classical-style` | **correct** | `none-detected` — **missed** | 3 / 5 |
| `core_e` | NTT multiplier, n = 16, q = 257 | `lattice-ntt` / `pqc-style` | **correct** | correct | 2 / 4 |

The published method gets **5 / 5**. `hal_crypto identify` alone gets **3 / 5**
on the blinded exports and **4 / 5** on the same netlists with their names
intact — the reveal runs both and reports the delta, which is what makes a miss
attributable.

Three of the five tool verdicts are `none-detected`, and only one of those three
is right. That sentence is why this walkthrough exists.

## What it teaches

* running the whole method **blind**, in a fixed order, with the call written
  against a rule table nobody can adjust after seeing the answer;
* `none-detected` as an *honest* outcome. The decoy is built out of the parts a
  cipher is built out of — a shift register, a sixteen-cell carry chain, two
  constant comparisons — and is still a link framer. Its chain is exactly as wide
  as the block cipher's and its logic exactly as deep; what separates them is
  that one adds the constant 1 and the other two adds two operand vectors each;
* that the *order* of a rule table can be load-bearing: an NTT butterfly and an
  ARX round have the same census, and only the table's ordering keeps a lattice
  kernel from being called a block cipher;
* what blinding costs a structural pass, measured rather than assumed — and two
  concrete gaps it exposed (issues #101 and #102), written up and filed rather
  than patched, because a capstone that tunes the instrument it is calibrating
  measures nothing.

## Layout

```
cores/                 the five blinded exports -- the whole of the exercise
ground_truth/          the answer key: designs, quartus, named exports, maps,
                       reference models, MANIFEST.json
anonymize.py           walkthrough 10's blinding script, byte for byte
analysis.py            blind | reveal | all
check.py               re-derives every blind finding and the whole score table
run_analysis.sh        every command the guide runs, in order
artifacts/ images/     what those commands produce
```

## Running it

```bash
# the blind pass: no HAL, no Graphviz, nothing but tools/
python3 examples/agilex3_walkthroughs/16_mystery_cores/analysis.py blind \
    -o examples/agilex3_walkthroughs/16_mystery_cores/artifacts

# the reveal, the score table and the named-versus-blinded control
python3 examples/agilex3_walkthroughs/16_mystery_cores/analysis.py reveal \
    -o examples/agilex3_walkthroughs/16_mystery_cores/artifacts

# everything, including the pictures (needs a built HAL and Graphviz)
HAL_BASE_PATH=/work/build HAL_PY_PATH=/work/build/lib PYTHONPATH=/work/build/lib \
    sh examples/agilex3_walkthroughs/16_mystery_cores/run_analysis.sh

# every claim the guide makes, re-asserted
python3 examples/agilex3_walkthroughs/16_mystery_cores/check.py [--with-hal]
```

## If you want to do it yourself

Read [`cores/README.md`](cores/README.md), then stop reading this directory and
start with:

```bash
python3 tools/hal_agilex --strict inventory \
    examples/agilex3_walkthroughs/16_mystery_cores/cores/core_a.anon.hal.v
```

The method is in [`ai/skills/re-walkthrough-method`](../../../ai/skills/re-walkthrough-method/SKILL.md)
and section 9 of the guide is the short version of it.
