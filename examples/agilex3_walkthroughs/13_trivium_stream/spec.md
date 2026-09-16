# trivium_stream — specification

The design under study in Agilex 3 walkthrough 13. This document states the
*intended* behaviour, and — following the series convention — it was written
**before** the RTL was synthesised. It is the ground truth the
reverse-engineered result (`recovered.v`) is finally checked against, not an
input to the analysis.

## What it implements

Trivium, the hardware-oriented stream cipher of the eSTREAM portfolio
(Christophe De Cannière and Bart Preneel, *Trivium: A Stream Cipher
Construction Inspired by Block Cipher Design Principles*, ISC 2006; spec also
published as *Trivium Specifications*, eSTREAM/ECRYPT, and standardised as ISO/IEC
29192-3). **Keystream generation only** — encryption is a one-bit-at-a-time XOR
that this core deliberately does not do, so there is no plaintext port.

80-bit key, 80-bit IV, a **288-bit state**, one keystream bit per clock cycle,
and a **1152-cycle** (= 4 × 288) warm-up before the first usable bit.

### Why this is the interesting counterpart to walkthrough 05

Walkthrough 05 is a 16-bit **LFSR**: its feedback is an XOR of taps, so it has a
feedback *polynomial*, a period, and a completely linear next-state map. Trivium
is the same skeleton with one change — each feedback function contains an **AND
of two adjacent stages** — and that single degree-2 term is the whole difference
between "a maximal-length sequence generator" and "a cipher". Everything this
walkthrough teaches hangs off finding those three AND gates in a netlist and
saying what they mean.

### The state, as three segments

Trivium's 288 state bits are three shift registers of unequal length that feed
each other in a ring:

| segment | stages (spec numbering) | length |
| --- | --- | --- |
| A | `s1 .. s93` | 93 |
| B | `s94 .. s177` | 84 |
| C | `s178 .. s288` | 111 |

Each segment shifts by one every cycle; only its head takes a computed value.

### One cycle

```
t1 = s66  XOR s93
t2 = s162 XOR s177
t3 = s243 XOR s288

z  = t1 XOR t2 XOR t3                      <- the keystream bit

t1 = t1 XOR (s91  AND s92 ) XOR s171       <- head of B
t2 = t2 XOR (s175 AND s176) XOR s264       <- head of C
t3 = t3 XOR (s286 AND s287) XOR s69        <- head of A

(s1  .. s93 ) = (t3, s1  .. s92 )
(s94 .. s177) = (t1, s94 .. s176)
(s178.. s288) = (t2, s178.. s287)
```

Three facts a reverse engineer can check one at a time:

* each segment's head is a function of **five** state bits and nothing else;
* two of those five are **adjacent** stages joined by an AND — the nonlinearity;
* the head of each segment reads mostly from the *previous* segment, so the
  three registers are **coupled**, not three independent generators. Segment A's
  head reads C, B's head reads A, C's head reads B; each also reads one stage of
  itself.

### Key and IV loading, and the warm-up

```
(s1  .. s93 ) = (K1 .. K80, 0, ..., 0)          13 zeros
(s94 .. s177) = (IV1 .. IV80, 0, 0, 0, 0)        4 zeros
(s178.. s288) = (0, ..., 0, 1, 1, 1)           108 zeros, then three ones
```

then the cycle above is run **4 × 288 = 1152 times with the output discarded**.
The three ones at the top of segment C are the only asymmetry in an otherwise
all-zero tail, and they are what stops the all-zero state from being a fixed
point.

## Ports

| port | direction | width | meaning |
| --- | --- | --- | --- |
| `clk` | input | 1 | the only clock; everything is synchronous to its rising edge |
| `rst_n` | input | 1 | asynchronous, active-low reset; clears all state to 0 |
| `start` | input | 1 | load `key`/`iv` and begin the warm-up; ignored while `busy` is high |
| `key` | input | 80 | `key[i]` is spec bit `K(i+1)`, captured on the accepted `start` |
| `iv` | input | 80 | `iv[i]` is spec bit `IV(i+1)`, captured on the accepted `start` |
| `ks` | output | 1 | the keystream bit `z`, driven combinationally at all times |
| `ks_valid` | output | 1 | the warm-up is over; `ks` is keystream |
| `busy` | output | 1 | the warm-up is in progress |

`ks` is driven **continuously**, exactly as walkthrough 11 drives `ct`
continuously: it only *means* keystream while `ks_valid` is high, but a wrong
feedback tap then shows up on an output within a hundred cycles instead of only
after the 1152-cycle warm-up. That is a testability decision, and section 8 of
the guide measures what it buys.

## Behaviour

State: the 288-bit register `s`, an 11-bit warm-up counter held as a 6-bit and a
5-bit half (see below), and two flags. 301 bits in all.

1. While `rst_n` is low every register is 0. The clear is **asynchronous**.
2. When `busy` is low and `start` is high, the design captures `key` and `iv`
   into `s` as the loading rule above says, clears the counter, raises `busy` and
   lowers `ks_valid`. This costs one cycle and performs no cipher step.
3. Every cycle that is not a load applies **one** Trivium step to `s`. That is
   true whether the warm-up is running, the keystream is flowing, or nothing has
   been loaded yet — shifting the all-zero state produces the all-zero state, so
   an idle core is indistinguishable from a stopped one, and the RTL is one
   multiplexer per stage narrower for it.
4. The counter advances only while `busy` is high. On its 1152nd step `busy`
   falls and `ks_valid` rises.
5. From that cycle on, `ks` carries `z1, z2, z3, ...`, one bit per cycle, until
   the next accepted `start`.
6. `start` is ignored while `busy` is high, so a warm-up once begun always runs
   to completion.

### The warm-up counter is deliberately two counters

1152 needs 11 bits, and `count == 1151` is an 11-input function — three inputs
past the six an ALM in the validated primitive coverage can read (see "What
synthesis is expected to do"). So the count is **64 × 18**: a 6-bit counter `lo`
whose terminal value is a 6-input test, and a 5-bit counter `hi` that advances
when `lo` wraps and whose terminal value is a 5-input test. Recovering
1152 from the netlist therefore means recovering *two* moduli and multiplying
them, which is a more realistic exercise than reading one comparator constant.

### Test vectors

From the eSTREAM/ECRYPT Trivium test-vector file (`trivium-80.80.test-vectors`,
Profile `___H3`, key size 80, IV size 80), reproduced by `reference.py` and by
the exported netlist itself:

| set/vector | key (hex) | IV (hex) | first 8 keystream bytes |
| --- | --- | --- | --- |
| Set 1, #0 | `80000000000000000000` | `00000000000000000000` | `38EB86FF730D7A9C` |
| Set 2, #0 | `00000000000000000000` | `00000000000000000000` | `FBE0BF265859051B` |
| Set 3, #0 | `00010203040506070809` | `00000000000000000000` | `D2A8740BBA6FD906` |

The published file writes keys, IVs and keystream as byte strings, and a byte
string is not a bit index — the two conventions have to be stated, not assumed:

* **keystream.** The first keystream bit `z1` is the **least significant** bit of
  the first byte; `z8` is its most significant bit. So `z1..z8 = 1,1,0,1,1,1,1,1`
  reads out as `0xFB`.
* **key and IV.** `K1` is the most significant bit of the **last** byte of the
  printed string, `K8` its least significant bit, `K9` the most significant bit
  of the byte before it, and so on to `K80` = the least significant bit of the
  first byte. Equivalently: reverse the byte order of the printed string, read
  the result as an 80-bit big-endian integer, and `K1` is its top bit.

Both conventions are *derived*, not assumed: `reference.py` implements the
cipher straight from the spec above, and `check.py` requires all three published
vectors to come back byte for byte. Set 2 — the all-zero key and IV — is the one
the walkthrough drives the netlist with, because for it the two conventions
cannot matter at all; Sets 1 and 3 are what pin the key convention down.

## Consequences worth naming

* **The nonlinearity is three AND gates.** 288 state bits, three feedback cones,
  one degree-2 term each. Delete them and the design is a (very long, coupled)
  LFSR whose output `hal_crypto` would report with a feedback polynomial. Section
  7 of the guide runs exactly that counterfactual as a negative control.
* **The three segments are coupled.** No segment's feedback is a function of its
  own stages alone, so no segment has a feedback polynomial or a period of its
  own, and a tool that only knows how to close a chain onto itself will report
  three *open* shift registers and find no cryptography. It did; see the guide.
* **A parallel key/IV load hides every shift link.** `s[i] <= load ? init : s[i-1]`
  is a multiplexer, so the plain structural test "is this register's next state
  exactly another register's output" fails on all 288 stages at once. This is the
  same shape walkthrough 11 hit, where all 54 XOR cells were multiplexers, and it
  has the same fix: hold the select at the value that selects the shift.
* **Nothing about the state size is on the boundary.** The design exposes one bit
  per cycle out of 288 bits of state. Every structural claim in the walkthrough
  is therefore made from wiring, not from watching outputs, and the behavioural
  check exists to *disagree* with the structure if the structure is wrong.
* One clock domain, one asynchronous reset domain, no clock-domain crossings.
* The design is **not** a secure implementation of anything: the key arrives on a
  parallel port, there is no re-key protocol, no masking and no constant-time
  argument beyond "it is a fixed 1153-cycle schedule". It exists to be reverse
  engineered.

## What synthesis is expected to do

Stated in advance so the guide can be honest about which predictions the netlist
confirmed and which it did not:

* 301 `tennm_ff` instances, all on the same `clk`, all with `clrn = rst_n`.
* One `tennm_lcell_comb` per state bit — 288 two-to-one multiplexers — plus the
  three feedback cells, the keystream cell, the two terminal-count tests, the
  flags and two short carry chains for `lo` and `hi`.
* **No memory block.** A 93-stage shift register is exactly the shape Quartus
  retargets onto RAM, and there are three of them; `AUTO_SHIFT_REGISTER_RECOGNITION
  OFF` in the QSF is what keeps them as flip-flops. RAM is outside the validated
  primitive coverage, and the flip-flop chain is also the structure this
  walkthrough is about.
* **No DSP, no PLL, no I/O buffers**: the flow stops after `quartus_syn`, and
  every operation is a one-bit XOR, AND or multiplexer, or a 6-bit increment.
* Two constant cells (`gnd`, `vcc`) materialised by the HAL import rewrite, not
  by Quartus.

### Three QSF/RTL decisions, and why

* `ALLOW_SYNCH_CTRL_USAGE OFF` — keeps the load selection out of the flip-flop's
  `sload`/`sclr` pins. Those are real `tennm_ff` ports but they are outside the
  configuration `tools/hal_agilex` validated, so an export using them would be
  reported `unsupported`. Same assignment, same reason, as walkthroughs 06 and 11.
* `AUTO_SHIFT_REGISTER_RECOGNITION OFF` — see above; without it the three
  segments become a memory block and there is nothing left to reverse engineer.
* `/* synthesis keep */` on the three feedback wires and the two terminal-count
  tests. Without it Quartus is free to flatten `load ? K1 : t3` into one cone of
  seven inputs, which needs the ALM's fracturable seven/eight-input mode
  (`extended_lut "on"`) — outside the validated coverage, reported `unsupported`.
  The `keep` splits it into a 5-input feedback cell and a 3-input multiplexer,
  both inside coverage. Walkthrough 12 ships a counterfactual export showing what
  dropping a `keep` does to what a pass can see; here it is a coverage
  requirement, and the guide says so rather than pretending the RTL was written
  in ignorance of the tool.

## Non-goals

Decryption (there is nothing to decrypt: keystream XOR plaintext is the
caller's job), multi-bit-per-cycle unrolling (the standard 64-bit-wide Trivium
is the same cipher with 64 copies of the feedback logic, and it would teach the
same lesson at eight times the netlist size), any side-channel property, and any
claim that recovering Trivium from a netlist that was compiled with names intact
says something about recovering a hardened commercial core. What the walkthrough
teaches is the **nonlinear feedback shift register**: three coupled segments,
their taps, and the three AND gates that separate a cipher from a PRNG.
