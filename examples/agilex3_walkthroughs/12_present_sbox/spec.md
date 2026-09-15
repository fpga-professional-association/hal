# present_sbox — specification of the original design

This is the design intent, written *before* synthesis. The reverse-engineering
walk in [`guide.html`](guide.html) never reads this file; it exists so a reader
can score the recovered result against the truth.

## Function

One PRESENT-80 encryption datapath: a 64-bit block cipher with an 80-bit key
and 31 rounds (Bogdanov, Knudsen, Leander, Paar, Poschmann, Robshaw, Seurin,
Vikkelsoe, *PRESENT: An Ultra-Lightweight Block Cipher*, CHES 2007). Iterative:
one round per clock, key schedule on the same clock, **encryption only**.

| port | dir | width | meaning |
| --- | --- | --- | --- |
| `clk` | in | 1 | the only clock in the design; everything is on its rising edge |
| `rst_n` | in | 1 | asynchronous, active low; clears the whole design to zero |
| `start` | in | 1 | load `plaintext`/`key_in` and begin; ignored while `busy` |
| `plaintext` | in | 64 | the block to encrypt, sampled on the load edge |
| `key_in` | in | 80 | the key, sampled on the load edge |
| `ciphertext` | out | 64 | the datapath register; the ciphertext once `done` is high |
| `busy` | out | 1 | an encryption is in progress |
| `done` | out | 1 | the last round has been clocked in |

## The cipher

```
S     = C 5 6 B 9 0 A D 3 E F 8 4 7 1 2          (4-bit S-box, CHES 2007 table 1)
P(i)  = 16*i mod 63   for i < 63,   P(63) = 63    (bit permutation)

state = plaintext
for i = 1..31:
    state = pLayer(sBoxLayer(state XOR K_i))
ciphertext = state XOR K_32
```

Key schedule, starting from `K = key_in`:

```
for i = 1..31:
    K_i = K[79:16]
    K   = K <<< 61
    K[79:76] = S[K[79:76]]
    K[19:15] = K[19:15] XOR i
K_32 = K[79:16]
```

## What the implementation does with that

The register holds **`w_i = state_i XOR K_i`**, the value *after* the round key
has been added, not before. Substituting that into the round gives an
equivalent recurrence over the same 31 rounds:

```
w_1     = plaintext XOR K_1
w_{i+1} = pLayer(sBoxLayer(w_i)) XOR K_{i+1}       i = 1..31
ciphertext = w_32
```

so one load edge plus 31 enabled clocks leaves the ciphertext in the register,
and the final key addition costs nothing extra — it is the same XOR every other
round already performs.

Two consequences are deliberate, and both are things the netlist shows:

1. **The S-boxes read register outputs directly.** Each of the 16 nibbles is a
   4-input, 4-output combinational cone over four flip-flops and nothing else.
   In the textbook placement (register holds `state_i`) each cone reaches back
   through the key XOR to eight sources, four state bits and four key bits, and
   a 4-output cone over 8 sources is not a substitution shape.
2. **The substitution outputs are marked `keep`.** Without it Quartus folds the
   round-key XOR that follows into the same ALM — a 4-input S-box bit plus one
   key bit is five inputs and fits in one cell — and the substitution stops
   existing as a signal anywhere in the netlist.

`variants/` holds both counterfactuals as synthesized exports, so the guide can
measure the difference instead of asserting it.

## Structure

| block | what it is |
| --- | --- |
| datapath register | 64 flip-flops, async clear, one enable |
| key register | 80 flip-flops, same clock/clear/enable |
| substitution layer | 16 independent 4-bit S-boxes = 64 four-input LUTs |
| permutation layer | pure wiring: which LUT output reaches which flip-flop |
| key S-box | one more 4-bit S-box on the rotated key's top nibble |
| round counter | 5 flip-flops counting 1..31, plus `running` and `done` |

## Properties the design is supposed to have

1. **The published test vectors.** All four PRESENT-80 vectors from the CHES
   2007 paper come out of the register after 31 round clocks.
2. **One clock domain, one asynchronous active-low reset**, no vendor IP: after
   synthesis the netlist is nothing but `tennm_lcell_comb` and `tennm_ff`. No
   RAM, no DSP, no PLL, no IO primitives.
3. **No `sclr`.** Every flip-flop uses the async-clear-plus-enable configuration
   that `tools/hal_agilex` has validated. The `done` flag is written as its own
   always block for exactly this reason; folded into the main chain, Quartus
   infers a synchronous clear and the export leaves the validated coverage.
4. **31 rounds, not 32.** The round counter runs 1..31 inclusive and the design
   stops when it reaches 31.

## Deliberately not specified

* Decryption. There is none: the inverse S-box and the inverse permutation are
  not in this design.
* Any timing/area target. Nothing here was optimised; the S-boxes are LUT logic
  because that is what the coverage allows, not because it is the smallest
  PRESENT core.
* Key agility beyond "the key is sampled with the plaintext". There is no
  separate key-load port and no stored round-key schedule.
* Side-channel resistance of any kind. A single-cycle round with the whole
  state in flip-flops is about as leaky as a block cipher gets; this is a
  teaching target, not a design to copy.
