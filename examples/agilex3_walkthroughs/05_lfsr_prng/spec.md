# lfsr_prng — specification of the original design

This is the design intent, written *before* synthesis. The reverse-engineering
walk in [`guide.html`](guide.html) never reads this file; it exists so a reader
can score the recovered result against the truth.

## Function

A 16-bit Fibonacci linear-feedback shift register used as a pseudo-random
number generator.

| port | dir | width | meaning |
| --- | --- | --- | --- |
| `clk` | in | 1 | the only clock in the design; everything is on its rising edge |
| `rst_n` | in | 1 | asynchronous, active low; loads the seed |
| `en` | in | 1 | when high, the generator advances by one step per clock |
| `rand_out` | out | 16 | the full LFSR state |
| `bit_out` | out | 1 | the serial output bit, `state[15]` |

## State update

```
feedback = state[15] ^ state[14] ^ state[12] ^ state[3]
state'   = en ? {state[14:0], feedback} : state          (rst_n low: state' = SEED)
SEED     = 16'hACE1
```

## Feedback polynomial

```
x^16 + x^15 + x^13 + x^4 + 1
```

Taps written the usual 1-based way: **16, 15, 13, 4**. As register indices
(0-based) those are bits **15, 14, 12, 3** — the four bits the XOR reads.

## Properties the design is supposed to have

1. **Maximal length.** The polynomial is primitive over GF(2), so from any
   non-zero seed the state visits all 65535 non-zero states before repeating.
   The period is `2^16 - 1 = 65535`, not 65536.
2. **One absorbing state.** `state = 0` maps to itself. The asynchronous reset
   exists to keep the design out of it, which is why the seed is non-zero.
3. **Linearity.** The next-state map is affine over GF(2) — in the original
   design purely linear. That is what makes the taps readable directly out of
   the next-state functions, and it is the property the walkthrough exploits.
4. **Single clock domain, no vendor IP.** No RAM, DSP, PLL or IO primitives;
   after synthesis the netlist is nothing but ALMs and flip-flops.

## Deliberately not specified

* Output timing/latency relative to `en` beyond "one state per enabled clock".
* Behaviour of `rand_out` while `rst_n` is low other than "reads the seed".
* Any statistical quality claim. An LFSR is *not* a cryptographic PRNG; the
  entire state is recoverable from 16 consecutive output bits, which is exactly
  what makes it a good teaching target and a bad key generator.
