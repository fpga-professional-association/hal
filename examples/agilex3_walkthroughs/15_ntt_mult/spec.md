# `ntt_mult` — a toy negacyclic NTT polynomial multiplier

The specification the RTL was written from, and the ground truth the
walkthrough is not allowed to look at. `analysis.py` reads the exported netlist
and nothing else; this file exists so that the guide can say, at the end,
exactly what was and was not recovered.

## What it computes

For `a(x)`, `b(x)` in `R = Z_q[x] / (x^n + 1)` with

    n = 16        the ring degree
    q = 257       the modulus, a Fermat prime: 257 = 2^8 + 1

the core computes `c(x) = a(x) * b(x)` in `R`. That quotient is *negacyclic*:
`x^16 = -1`, so a product term of degree 16 or more wraps round **with a sign
flip**, and `x^15 * x` is `-1`, not `+1`. Every lattice signature and KEM in
NIST's post-quantum portfolio that uses structured lattices multiplies in a
ring of exactly this shape — ML-KEM in `Z_3329[x]/(x^256 + 1)`, ML-DSA in
`Z_8380417[x]/(x^256 + 1)`. The *shape* is the same; the parameters are not,
and the difference matters (see "Honesty" below).

## Why q = 257

`q = 2^8 + 1` is what makes the design fit in ALM logic with no DSP block and
no memory:

* `256 ≡ -1 (mod 257)`, so reducing a 17-bit product `P = 256·P_h + P_l` is
  `P ≡ P_l - P_h`, one 9-bit subtraction and one conditional add of `q`. No
  division, no Barrett constant, no Montgomery domain.
* a residue fits in 9 bits, so a coefficient is one ALM-wide word;
* `2` has order 16 modulo 257, so the 16th roots of unity are exactly the
  powers of two. Every NTT twiddle at these parameters is a power of two —
  which is a fact about this toy and emphatically not about `q = 3329`.

The **input ports are bytes**, not 9-bit words. There are 257 residues and 256
byte values, so the missing one is `256 = -1`: the datapath produces it, the
output port can carry it, and the input port cannot. In exchange, nothing in
the datapath ever has to reduce an input, and every value in flight is a
canonical residue in `[0, 256]` from the first cycle to the last.

## The transform

`psi = 15` has order 32 modulo 257, so `psi^16 = -1` and `omega = psi^2 = 225`
is a primitive 16th root of unity.

The forward transform is a Cooley-Tukey (decimation-in-time) stack with the
`psi^i` pre-twist **merged into the twiddle constants**, which is the trick
Kyber's reference NTT uses: it takes the coefficient vector in natural order to
the evaluation vector in bit-reversed order, and the pre-twist costs no pass of
its own. Its per-group constant is `psi^brv4(2^s + g)` at stage `s`, group `g`.

The inverse is the *same* Cooley-Tukey butterfly with plain cyclic constants
`omega^-brv3(g)`, followed by a post-twist of `n^-1 · psi^-i` folded into one
16-entry table. It is **not** a Gentleman-Sande unit: GS puts its multiplier
after the adder, and building both shapes out of one datapath means a
multiplexer loop Quartus rejects. The price of using CT both ways is the
bit-reversal pass, which costs sixteen cycles and not one cell.

## The five phases

One butterfly per clock, one shared modular multiplier, 144 cycles per product:

| phase | cycles | what it does |
| --- | --- | --- |
| `NTT` | 64 | 32 butterflies over `pa`, then 32 over `pb` (`cnt[5]` picks the bank) |
| `POINT` | 16 | `pb[i] <= pa[i] * pb[i]` |
| `BREV` | 16 | `pa[brv(i)] <= pb[i]` — a multiply by one |
| `INTT` | 32 | 32 butterflies over `pa`, inverse twiddles |
| `POST` | 16 | `pb[brv(j)] <= pa[j] * (n^-1 · psi^-brv(j))` |

The scalar phases are the *same* datapath with the butterfly's upper operand
forced to zero, so `sum = 0 + w·v` is a plain modular multiply and the
difference output is simply not written. That is why there is one butterfly in
the netlist and not thirty-two: four stages of eight are a schedule in a
six-bit counter, not four stages of logic.

## The datapath

    u    = butterfly ? bank[j]       : 0
    v    = bank[j + len]                       (or the other polynomial)
    w    = point ? pa[i] : twiddle_table[...]
    t    = (w * v) mod q                       -- 9x9 multiply + Fermat fold
    sum  = (u + t) mod q                       -- add  + conditional subtract q
    dif  = (u - t) mod q                       -- sub  + conditional add      q

`sum` goes to index `j`, `dif` to index `j + len`.

## What synthesis is expected to do

* **No DSP block.** `w * v` is a 9 × 9 multiply and Quartus will put it in a
  `tennm_mac` unless told not to. `multstyle = "logic"` on the module, plus
  `AUTO_DSP_RECOGNITION OFF` and `DSP_BLOCK_BALANCING "LOGIC ELEMENTS"` in the
  `.qsf`, force it into ALMs. `quartus/ntt_dsp.qsf` is the counterfactual that
  drops all three: it produces one `tennm_mac`, 75 fewer ALMs, and an export
  that `hal_agilex inventory --strict` refuses.
* **No `sload` / `sclr`.** `ALLOW_SYNCH_CTRL_USAGE OFF`, as in walkthroughs 06,
  11, 13 and 14: both are real `tennm_ff` ports and both are outside the
  configuration `hal_agilex` validated.
* **No seven-input ALMs.** Two places need `keep` to stay at six inputs per
  cone: the 16-to-1 read multiplexers (written out as a tree of 4-to-1 cones
  with the middle vector kept, because Quartus otherwise merges the top level
  with the bank select) and the twiddle tables (written as two 32-entry tables
  and a select rather than one 64-entry table, because Quartus otherwise shares
  a sub-term between output bits and spills one of them into the fracturable
  mode).
* **`keep` on the butterfly's operands.** `u`, `v`, `t`, `sum_raw`, `dif_raw`
  and the corrected vectors are kept so that the adder and the subtracter read
  the *same* two nets. Without it Quartus is free to duplicate the operand
  logic, and the one structural fact that makes a butterfly a butterfly — two
  chains, one pair of operands — stops being true of the netlist.
* **The reduction is not a carry chain.** `sum - 257` has two set bits, so
  Quartus builds the correction out of ordinary LUTs rather than an
  arithmetic-mode chain. This is not a synthesis accident to work around; it is
  what a Fermat-prime modulus looks like after synthesis, and it is why the
  walkthrough has to read `q` out of the correction's *function*.

The expected census is **299 `tennm_ff` + 910 `tennm_lcell_comb`**, nothing
else, and 42 combinational levels between one register bank and the next — the
deepest design in the series, because a 9 × 9 array multiplier and two modular
corrections sit in that path.

## Interface

    clk                  the only clock
    rst_n                active-low asynchronous reset (the ALM `clrn` pin)
    start                accepted only while `busy` is low
    a_in  [127:0]        16 coefficients of 8 bits, coefficient i at [8i +: 8]
    b_in  [127:0]        likewise
    c_out [143:0]        16 coefficients of 9 bits, coefficient i at [9i +: 9]
    done                 the product in `c_out` is finished
    busy                 a product is in progress

`c_out` is the `pb` register bank, driven continuously. It only *means* `a*b`
while `done` is high — but driving it always means a wrong twiddle reaches an
output inside the transform that used it, instead of only at cycle 144.

## Honesty

`n = 16`, `q = 257` is **not** a post-quantum implementation and the guide says
so in as many words. It is the arithmetic kernel of one at parameters chosen to
fit a teaching example: a real scheme needs `n = 256` and a modulus for which
none of the shortcuts above exist, plus sampling, encoding, hashing and a
protocol. What the walkthrough recovers is real — a modulus, a root of unity,
eighty twiddle constants, a negacyclic ring — and what it licenses is the
sentence "this is lattice-style ring arithmetic", not "this is ML-KEM".
