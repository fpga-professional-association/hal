# Counterfactual exports — the same cipher, synthesized differently

Section 10 of [`../guide.html`](../guide.html) uses these. Both compute exactly
the same PRESENT-80 encryption as `../design.v`; both were synthesized with the
same Quartus Prime Pro 26.1 flow, the same device (`A3CW135BM16AE6S`) and the same
`build.tcl` pattern as `../quartus/`. They exist so the guide can *measure* what
the RTL decides about what a structural identifier can see, instead of asserting
it.

| file | derived from | the only difference | `hal_crypto identify` |
| --- | --- | --- | --- |
| `present_nokeep.vo` | `../design.v` | the two `/* synthesis keep */` pragmas deleted | `spn`, 13 substitutions extracted, **none** matching the library |
| `present_textbook.vo` | `design_textbook.v` | the register holds the state *before* the round key is added (and therefore needs a 32nd cycle for the final key addition) | **`none-detected`** |

`../check.py` re-runs `identify` over both and asserts those verdicts, so the
claim in the guide cannot rot.

## Regenerating them

`present_nokeep.vo` — one `sed`, then the standard flow:

```bash
sed 's| /\* synthesis keep \*/||' ../design.v > design.v
quartus_sh -t build.tcl            # the copy in ../quartus/, unchanged
quartus_syn present_sbox -c present_sbox
quartus_eda --simulation --format=verilog --tool=questasim \
            --output_directory=simulation present_sbox -c present_sbox
```

`present_textbook.vo` — the same three commands over `design_textbook.v`, with
`present_sbox_textbook` as the project name and top-level entity.

Both `.vo` files are committed with LF endings; the findings the guide quotes are
in `../artifacts/variants/`.
