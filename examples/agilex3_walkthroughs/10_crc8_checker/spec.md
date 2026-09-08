# crc8_checker — specification (the "before" document)

This is the one-page spec that `design.v` implements. In the walkthrough it
plays the role of the thing you are trying to *rediscover*: after the netlist
walk you should be able to write this page yourself from the netlist alone.

## Purpose

Validate a serially received codeword against a CRC-8 with polynomial
**0x07**, i.e. `x^8 + x^2 + x + 1` (the "CRC-8/ATM" / "CRC-8/SMBUS" generator
polynomial). Bits arrive one per enabled clock, most significant bit first.

## Interface

| port | dir | width | meaning |
| --- | --- | --- | --- |
| `clk` | in | 1 | single clock, rising edge |
| `rst_n` | in | 1 | asynchronous, active low. Deasserting starts a new frame; remainder register is cleared to `0x00` |
| `en` | in | 1 | consume one bit of `din` on this rising edge |
| `din` | in | 1 | serial message bit, MSB first |
| `crc` | out | 8 | current remainder |
| `match` | out | 1 | combinational: `crc == 8'h00` |

## Behaviour

Init value `0x00`, no input reflection, no output reflection, no final XOR.

Per enabled clock:

```
feedback   = crc[7] XOR din
crc'[0]    = feedback
crc'[1]    = crc[0] XOR feedback
crc'[2]    = crc[1] XOR feedback
crc'[i]    = crc[i-1]              for i = 3..7
```

The three XOR'd stages (0, 1, 2) are exactly the set bits of the polynomial
byte `0x07`. Bit 0 is the implicit `x^0` term of every CRC; bits 1 and 2 are
the `x^1` and `x^2` terms.

## Checking rule

Reset, then shift in `message || crc8(message)` MSB-first. The remainder is
zero afterwards, so `match` is high exactly when the received codeword is a
multiple of the generator polynomial.

Worked example used throughout the walkthrough:

```
message   = 0x31 0x32 0x33 0x34 0x35 0x36 0x37 0x38 0x39   ("123456789")
crc8(msg) = 0xF4                                            (the check value
                                                             for CRC-8/SMBUS)
codeword  = "123456789" || 0xF4   ->  80 bits  ->  remainder 0x00, match = 1
```

Flipping any single bit of that codeword leaves a non-zero remainder, so
`match` goes low. Both facts are re-derived from the *netlist* in step 6 of the
guide and asserted by `check.py`.

## Non-goals / constraints

* One clock domain. No CDC, no gated clocks.
* No vendor IP, no memory, no DSP, no PLL, no explicit IO buffers — the
  post-synthesis netlist must consist only of primitives that
  `plugins/gate_libraries/definitions/AGILEX_TENNM.hgl` models.
* No hierarchy: a single flat module. (The walkthrough says explicitly what
  that costs you when you later look for structure.)
