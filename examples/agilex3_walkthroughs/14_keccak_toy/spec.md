# keccak_toy — specification

The design under study in Agilex 3 walkthrough 14. This document states the
*intended* behaviour, and — following the series convention — it was written
**before** the RTL was synthesised. It is the ground truth the
reverse-engineered result (`recovered.v`) is finally checked against, not an
input to the analysis.

## What it implements

**Keccak-f[200]**, the smallest member of the Keccak-f permutation family
(Guido Bertoni, Joan Daemen, Michaël Peeters and Gilles Van Assche, *The Keccak
reference*, version 3.0, January 2011, section 1.2; the same construction at
width 1600 is standardised as `KECCAK-p[1600,24]` in NIST FIPS 202, section 3).

Two hundred bits of state as a 5 × 5 array of **8-bit lanes**, eighteen rounds,
**one round per clock cycle**, iterative. It is a *permutation*, not a hash:
there is no absorbing, no padding and no rate/capacity split. The sponge that
would turn it into a hash is the caller's job, exactly as walkthrough 13 leaves
the keystream XOR to the caller.

### Why a permutation and not a hash

Because the permutation is the part with the structure in it. A sponge is a
loop of "XOR some input into the first *r* bits, permute, repeat"; the
interesting reverse-engineering target is what "permute" expands into, and at
width 200 that is a netlist small enough to read end to end while being the
*same five steps*, the *same* rho offsets modulo the lane width, and the *same*
round-constant LFSR as SHA-3.

### Why this walkthrough exists: the classical/PQC question has no answer here

Walkthroughs 11, 12 and 13 all end with `hal_crypto` reporting
`classical-style`, and in each case that is the right answer: an ARX round, an
SPN with a published S-box and a nonlinear feedback register are all things
classical symmetric cryptography is built from and post-quantum cryptography is
not.

A sponge is the case where that reasoning stops working. SHA-3 and SHAKE are
classical hashing. The *same* SHAKE is the extendable-output function inside
**ML-KEM** (FIPS 203) and **ML-DSA** (FIPS 204), and hashing is the *entirety*
of **SLH-DSA**/SPHINCS+ (FIPS 205). A Keccak core on a die is therefore evidence
about what the design computes and **no evidence at all** about which family of
scheme it serves. What decides that is the arithmetic *around* the sponge — a
number-theoretic transform over a small modulus, a syndrome decoder, nothing at
all — and none of it is inside the permutation.

So the verdict this walkthrough is aiming at is not "classical" and not "PQC".
It is `undetermined`, stated as a measurement rather than a shrug, and section 9
is about getting the tool to say it.

## The permutation

`l = 3`, so the lane width is `w = 2^l = 8` and the round count is
`12 + 2l = 18`.

State bit `A[x][y][z]` for `x, y` in 0..4 and `z` in 0..7. The port uses the
sponge literature's own byte order: lane `(x, y)` is **byte `x + 5y`** of the
25-byte state string, and bit `z` is bit `8(x + 5y) + z`, least significant
first. So `din` *is* the state string, with no repacking.

### One round: θ, ρ, π, χ, ι

```
theta:  C[x]    = A[x][0] ^ A[x][1] ^ A[x][2] ^ A[x][3] ^ A[x][4]
        D[x]    = C[x-1] ^ ROTL(C[x+1], 1)
        A[x][y] = A[x][y] ^ D[x]

rho:    A[x][y] = ROTL(A[x][y], r[x][y])

pi:     B[y][2x+3y] = A[x][y]

chi:    A[x][y] = B[x][y] ^ (~B[x+1][y] & B[x+2][y])

iota:   A[0][0] = A[0][0] ^ RC[round]
```

All index arithmetic on `x` and `y` is modulo 5; `ROTL` is modulo 8.

Three facts a reverse engineer can check one at a time:

* **θ is a pair of XOR parity planes.** Each of the five columns is reduced to
  one 8-bit parity lane, and each parity lane is fed back into two *different*
  columns, one of them rotated by a single bit. That is a 5-into-1 XOR followed
  by a 3-input XOR, and it is the only step whose fan-in says anything about
  the 5 × 5 geometry.
* **χ is a 5-bit substitution, applied 40 times.** One row of five lanes, one
  bit-slice `z`: five bits in, five bits out, `y_i = x_i ^ (~x_{i+1} & x_{i+2})`.
  Five rows times eight bit-slices is 40 instances of the *same* 5-bit S-box, and
  it is the only step of the round that is not linear over GF(2).
* **ρ and π cost nothing.** Rotating a lane and relabelling which lane it is are
  both pure wiring. After synthesis there is no net, no cell and no vector that
  says "rotate by five": the offsets exist only as *which* `theta` net each `chi`
  cell reads. This is the same shape walkthrough 11's ARX rotations had and the
  same shape walkthrough 12's pLayer had, and the guide recovers it the same
  way — index arithmetic over an ordered cell layer.

### The rho offsets

The published table is stated at `w = 64`; at `w = 8` only the residue matters.
`r[x][y]`:

| | y=0 | y=1 | y=2 | y=3 | y=4 |
| --- | --- | --- | --- | --- | --- |
| **x=0** | 0 (0) | 36 (4) | 3 (3) | 41 (1) | 18 (2) |
| **x=1** | 1 (1) | 44 (4) | 10 (2) | 45 (5) | 2 (2) |
| **x=2** | 62 (6) | 6 (6) | 43 (3) | 15 (7) | 61 (5) |
| **x=3** | 28 (4) | 55 (7) | 25 (1) | 21 (5) | 56 (0) |
| **x=4** | 27 (3) | 20 (4) | 39 (7) | 8 (0) | 14 (6) |

Published value first, `mod 8` in parentheses. Three of the twenty-five are
zero mod 8 — `(0,0)`, `(3,4)` and `(4,3)` — so at this width ρ genuinely does
nothing to three lanes, which is a fact the recovery has to survive rather than
a bug.

### The round constants

`RC[i]` has bit `2^j - 1` equal to `rc(j + 7i)` for `0 <= j <= l`, where `rc` is
the 8-bit LFSR `x^8 + x^6 + x^5 + x^4 + 1` of FIPS 202 algorithm 5. With
`l = 3` that is bits 0, 1, 3 and 7 of the lane and nothing else: **four of the
eight bits of `RC[i]` are zero for every round**, and the other four are the low
byte of the published 1600-bit constants. In order:

```
01 82 8a 00 8b 01 81 09 8a 88 09 0a 8b 8b 89 03 02 80
```

`RC[3]` is zero, so round 3 has no ι at all — another asymmetry the recovery has
to survive.

## Ports

| port | direction | width | meaning |
| --- | --- | --- | --- |
| `clk` | input | 1 | the only clock; everything is synchronous to its rising edge |
| `rst_n` | input | 1 | asynchronous, active-low reset; clears all state to 0 |
| `start` | input | 1 | load `din` and run the permutation; ignored while `busy` is high |
| `din` | input | 200 | the 25-byte state string, lane `(x,y)` at byte `x + 5y` |
| `dout` | output | 200 | the state register, driven continuously |
| `done` | output | 1 | the eighteen rounds are over; `dout` is `Keccak-f[200](din)` |
| `busy` | output | 1 | a permutation is in progress |

`dout` is driven **continuously**, exactly as walkthrough 13 drives `ks` and
walkthrough 11 drives `ct`: it only *means* the permutation result while `done`
is high, but a wrong rotation offset then shows up on an output one cycle after
a load instead of only after eighteen rounds. That is a testability decision,
and section 8 measures what it buys.

## Behaviour

State: the 200-bit register `s`, a 5-bit round counter `rnd`, and two flags.
207 bits in all.

1. While `rst_n` is low every register is 0. The clear is **asynchronous**.
2. When `busy` is low and `start` is high, the design captures `din` into `s`,
   clears `rnd`, raises `busy` and lowers `done`. This costs one cycle and
   performs no round.
3. Every cycle while `busy` is high applies **one** round to `s` and increments
   `rnd`. Round *k* uses `RC[k]`.
4. On the cycle where `rnd == 17`, `busy` falls and `done` rises. `s` then holds
   `Keccak-f[200](din)`.
5. While neither loading nor running, every register **holds**. Unlike
   walkthrough 13 — where shifting the all-zero state gives the all-zero state,
   so idling needed no hold — an idle Keccak core has to keep its answer on
   `dout`, and the cheapest way to say that is the flip-flop's **clock enable**:
   one net (`busy | load`) for all 207 registers instead of a third data pin on
   each.
6. `start` is ignored while `busy` is high, so a permutation once begun always
   runs to completion.

So `done` rises **19 cycles** after an accepted `start`: one load plus eighteen
rounds.

### Test vectors

From the eXtended Keccak Code Package's
`tests/TestVectors/KeccakF-200-IntermediateValues.txt`, which applies the
permutation repeatedly starting from the all-zero state:

| vector | input | output (25 bytes, lane `(x,y)` at byte `x + 5y`) |
| --- | --- | --- |
| first | `00` × 25 | `3C2826841CB35C171EAAE9B811134CEAA3852C69D2C5ABAFEA` |
| second | the line above | `1BEF689492A8A543A5999FDB834E3166A14BE827D95040479E` |

The byte order is not an assumption: `reference.py` implements the permutation
straight from the `A[x][y][z]` algebra above, `analysis.py` drives the exported
netlist itself, and `check.py` requires both to return these bytes. The
all-zero input is also the one vector for which no endianness convention could
matter, which is why it is the headline; the second vector, whose input is
dense, is what pins the lane-to-byte map down.

## Consequences worth naming

* **The nonlinearity is 40 copies of one 5-bit table.** Delete the AND in χ and
  the whole permutation is linear over GF(2) — it is then an invertible 200×200
  bit matrix, and every round is a matrix product. Section 7 runs exactly that
  counterfactual as a negative control.
* **A sponge does not settle the classical/PQC question**, and a tool that
  answers it anyway is wrong rather than confident. See above, and section 9.
* **Where the register sits decides what a structural pass can see, and the
  algorithm does not.** In the architecture above, χ reads the state register
  *through* θ, so every χ cone depends on **33** flip-flops — over the 12 that
  `hal_crypto.sbox` will enumerate, and the pass correctly refuses rather than
  approximating. The same permutation with the register moved half a round
  (`retimed.v`, section 10) puts χ directly on the register outputs, three
  flip-flops per cone, and the same command finds all forty. Two exports, one
  cipher, opposite verdicts. This is walkthrough 12's "the RTL, not the
  algorithm, decides what survives synthesis" restated at the level of the
  *pipeline* rather than the coding style.
* **ρ and π leave nothing behind.** Twenty-five rotation amounts and a 5 × 5
  transposition have to be recovered from the indices of the nets a cell layer
  reads, and the *only* reason that is possible is that θ and χ both cost cells.
  A design that fused them would have hidden the geometry too.
* One clock domain, one asynchronous reset domain, no clock-domain crossings.
* The design is **not** a secure implementation of anything: the state arrives
  on a parallel port, there is no sponge, no padding, no domain separation, no
  masking. It exists to be reverse engineered.

## What synthesis is expected to do

Stated in advance so the guide can be honest about which predictions the netlist
confirmed and which it did not:

* **207 `tennm_ff`**, all on the same `clk`, all with `clrn = rst_n`: 200 state,
  5 counter, 2 flags.
* **One `tennm_lcell_comb` per θ output and per χ output** — 200 each — plus 40
  column-parity cells, 200 next-state multiplexers, the round-constant lookup,
  the terminal-count test and the counter.
* **No memory block, no DSP, no PLL, no I/O buffers**: the flow stops after
  `quartus_syn`, and every operation is a one-bit XOR, AND or multiplexer, or a
  5-bit increment.
* **Two constant cells** (`gnd`, `vcc`) materialised by the HAL import rewrite,
  not by Quartus.

### The QSF/RTL decisions, and why

* `ALLOW_SYNCH_CTRL_USAGE OFF` — keeps the load selection out of the flip-flop's
  `sload`/`sclr` pins. Those are real `tennm_ff` ports but they are outside the
  configuration `tools/hal_agilex` validated, so an export using them would be
  reported `unsupported`. Same assignment, same reason, as walkthroughs 06, 11
  and 13. The flip-flop's **`ena`** pin *is* inside coverage, which is why point
  5 above can lean on it.
* `/* synthesis keep */` on `cpar`, `theta`, `chi`, `rc_lut`, `last` and `load`.
  Four of those are for **coverage**, not style, and the first synthesis attempt
  proved it: without a keep on `load`, Quartus inlined `start & ~run` into the
  round counter's top bit, whose next state became a **seven**-input cone
  (`start`, `run` and all five counter bits) and needed the ALM's fracturable
  seven/eight-input mode (`extended_lut "on"`) — outside the validated
  configuration, and `inventory --strict` said so. Kept, `load` is one cell that
  208 others read — every register's next-state logic plus the shared clock
  enable — and the counter's top bit is back to six inputs. The keeps on
  `cpar`/`theta`/`chi` do the same job for the round: unkept, a χ cell and its
  load multiplexer fuse into a seven-input cone. Walkthrough 13 hit the same
  wall for the same reason and its spec.md says so too.
* The keep on `rc_lut` has a second effect worth predicting: four of the eight
  round-constant bits are **constant zero** across all eighteen rounds, and
  Quartus reports them as redundant logic. `keep` preserves them anyway, so the
  export should contain eight `rc_lut` cells of which four compute a constant —
  which is how the recovery gets to read `RC[i]` in all eight bit positions
  instead of four.

## Non-goals

The sponge itself (absorbing, padding, squeezing, a rate/capacity split), any
width other than 200, multi-round-per-cycle unrolling, any side-channel
property, and any claim that recovering Keccak-f[200] from a netlist compiled
with names intact says something about recovering a hardened commercial core.
What the walkthrough teaches is the **permutation**: parity planes, a bit map
that costs nothing, a 5-bit substitution repeated forty times, a round constant
schedule — and the one verdict in the series that is honestly `undetermined`.
