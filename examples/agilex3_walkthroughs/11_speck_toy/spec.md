# speck_toy — specification

The design under study in Agilex 3 walkthrough 11. This document states the
*intended* behaviour, and — following the series convention — it was written
**before** the RTL was synthesised. It is the ground truth the
reverse-engineered result (`recovered.v`) is finally checked against, not an
input to the analysis.

## What it implements

Speck32/64, the smallest member of the SPECK family of ARX block ciphers
(Beaulieu, Shors, Smith, Treatman-Clark, Weeks, Wingers, *The SIMON and SPECK
Families of Lightweight Block Ciphers*, IACR ePrint 2013/404). **Encryption
only.** Block 32 bits, key 64 bits, 22 rounds, one round per clock cycle.

ARX means the round function is built from three operations and nothing else:

* **A** — modular addition on 16-bit words (`+` mod 2^16);
* **R** — fixed rotations by the constants α = 7 and β = 2;
* **X** — bitwise XOR.

There is no S-box, no lookup table, no field multiplication. That is precisely
what makes the family interesting to a netlist reverse engineer: the addition
lands on the FPGA's dedicated carry chain, the rotations cost *no logic at all*
(they are wiring), and the XORs are single ALM cells. The cipher is visible in
the *shape* of the netlist or not at all.

### The round function

With the block held as two 16-bit words `x` (upper) and `y` (lower), and `k`
the round key of the current round:

```
x <- (ROR(x, 7) + y) mod 2^16   XOR  k
y <- ROL(y, 2)                  XOR  x     (the *new* x)
```

### The key schedule

The 64-bit key is four 16-bit words. Held as `k` (the current round key) and a
three-deep buffer `l0`, `l1`, `l2`, the schedule is the *same* ARX round
function with the round index injected instead of a key:

```
l_new <- (k + ROR(l0, 7)) mod 2^16  XOR  i        (i = round index, 0..20)
k     <- ROL(k, 2)                  XOR  l_new
l0,l1,l2 <- l1, l2, l_new
```

Round keys are therefore *computed* one per cycle, in lockstep with the data
path. Nothing is stored in a table, which is what keeps this design out of
block RAM (see "What synthesis is expected to do").

## Ports

| port | direction | width | meaning |
| --- | --- | --- | --- |
| `clk` | input | 1 | the only clock; everything is synchronous to its rising edge |
| `rst_n` | input | 1 | asynchronous, active-low reset; clears all state to 0 |
| `start` | input | 1 | begin an encryption; ignored while `busy` is high |
| `pt` | input | 32 | plaintext, `{x, y}`, captured on the accepted `start` |
| `key` | input | 64 | key, `{l2, l1, l0, k}`, captured on the accepted `start` |
| `ct` | output | 32 | the block register `{x, y}`, driven continuously |
| `busy` | output | 1 | an encryption is in progress |
| `done` | output | 1 | the last encryption finished; `ct` holds its result |

## Behaviour

State: `x`, `y`, `k`, `l0`, `l1`, `l2` (six 16-bit registers = 96 bits), a
5-bit round counter `rnd`, and two flags `busy` and `done`. 103 bits in all.

1. While `rst_n` is low every register is 0. The clear is **asynchronous**.
2. When `busy` is low and `start` is high, the design captures `pt` and `key`
   into the six word registers, sets `rnd` to 0, raises `busy` and lowers
   `done`. This costs one cycle and performs no round.
3. While `busy` is high, each rising edge applies **one** round to the data
   path and **one** step of the key schedule, and increments `rnd`.
4. On the cycle where `rnd` is 21 — the 22nd round — `busy` falls and `done`
   rises. The encryption took 1 + 22 = 23 cycles from the accepted `start`.
5. `start` is ignored while `busy` is high, so an encryption once begun always
   runs to completion.
6. `ct` is the block register, driven combinationally at all times. It only
   *means* "ciphertext" while `done` is high.

### Test vector

The one published in the SPECK paper, appendix C:

```
key        = 1918 1110 0908 0100      ->  {l2,l1,l0,k} = {0x1918,0x1110,0x0908,0x0100}
plaintext  = 6574 694c                ->  {x,y}        = {0x6574,0x694c}
ciphertext = a868 42f2                ->  {x,y}        = {0xa868,0x42f2}
```

## Consequences worth naming

* **The rotations are free.** ROR by 7 and ROL by 2 are a relabelling of wires.
  They occupy no cell, appear in no `lut_mask`, and are invisible to a gate-type
  census. They are recoverable only from *which bit of which register* arrives
  at *which pin*.
* **Two carry chains, not one.** The data path and the key schedule each own a
  16-bit adder, and they run every cycle in parallel. A design with one carry
  chain over a cipher-sized word is an accumulator; two structurally identical
  ones side by side, one of them fed by a counter-derived constant, is a round
  function plus its key schedule.
* **The round index is a real input to the logic.** `l_new` XORs the counter
  in, so five of the sixteen key-schedule XOR cells read the round counter and
  eleven do not. That asymmetry is the schedule's fingerprint.
* One clock domain, one asynchronous reset domain, no clock-domain crossings.
* The design is **not** a secure implementation of anything: no masking, no
  constant-time argument beyond "it is a fixed 23-cycle pipeline", and the key
  is presented on a parallel port. It exists to be reverse-engineered.

## What synthesis is expected to do

Stated in advance so the guide can be honest about which predictions the
netlist confirmed and which it did not:

* 103 `tennm_ff` instances, all on the same `clk`, all with `clrn = rst_n`, and
  a clock enable rather than a synchronous clear — see the QSF note below.
* Three carry chains of `tennm_lcell_comb` cells in **arithmetic mode**: 16
  bits for `ROR(x,7) + y`, 16 bits for `k + ROR(l0,7)`, and a short one for
  `rnd + 1`.
* A layer of two-input XOR cells on top of the data-path adder — the `^ k`
  step — which must survive as its own cells because `x_new` is read twice
  (by the `x` register and by the `y` update).
* Load multiplexers in front of every word register, because `start` loads the
  ports and a round loads the round function. Where a multiplexer and an XOR
  can share one ALM, Quartus is expected to merge them; that is a real effect
  and section 5 of the guide measures it rather than wishing it away.
* No RAM, no DSP, no PLL, no I/O buffers: the flow stops after `quartus_syn`,
  and every operation is a 16-bit add, a rotation or an XOR. A 16×16 multiply
  would have gone to a DSP block and put the design outside the validated
  primitive coverage; there is none.
* Two constant cells (`gnd`, `vcc`) materialised by the HAL import rewrite, not
  by Quartus.

### Two QSF assignments, and why

* `ALLOW_SYNCH_CTRL_USAGE OFF` — keeps the load/round selection out of the
  flip-flop's `sload`/`sclr` pins. Those are real `tennm_ff` ports but they are
  outside the configuration `tools/hal_agilex` validated, so an export using
  them would be reported `unsupported`. The same assignment, for the same
  reason, is in walkthrough 06.
* `AUTO_SHIFT_REGISTER_RECOGNITION OFF` — the `l0 <- l1 <- l2` buffer is a
  three-deep 16-bit shift register, exactly the shape Quartus may retarget onto
  a memory block. A memory block is outside the coverage; a flip-flop chain is
  inside it, and it is also the structure the walkthrough is about.

## Non-goals

Decryption, key agility beyond "load a new key with the next block", any
side-channel property, and any claim that recovering Speck32/64 from 22 rounds
of visible structure says anything about recovering a hardened commercial core.
What the walkthrough teaches is the **ARX skeleton**: carry chain, rotation as
wiring, XOR layer — and how to read the rotation constants out of a netlist in
which they are not logic.
