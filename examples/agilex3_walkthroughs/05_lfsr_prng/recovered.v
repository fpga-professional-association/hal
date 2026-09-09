// ---------------------------------------------------------------------------
// lfsr_prng_recovered -- RTL reconstructed from netlist.hal.v alone
// ---------------------------------------------------------------------------
// Written from the reverse-engineering walk in guide.html.  Nothing here was
// copied from design.v.  Every constant below has a named source:
//
//   16 stages          the 16 tennm_ff instances, all on one clock, one enable
//                      and one asynchronous clear
//   the shift order    the next-state function of 15 of the 16 flip-flops
//                      depends on exactly one other flip-flop; those edges
//                      form a single chain
//   TAPS {15,14,12,3}  the one remaining flip-flop's next-state function is the
//                      XOR of four chain stages (one 4-input ALM, lut_mask
//                      0x6996699669966996)
//   SEED 16'hACE1      the flip-flops have only an asynchronous *clear*, so a
//                      stage whose design value must reset to 1 is stored
//                      inverted.  The inverted stages are exactly the set bits
//                      of the seed, and they are visible twice: as inverters in
//                      front of the output ports, and as inverting shift edges
//                      wherever two neighbouring seed bits differ.
//   the enable         every flip-flop's `ena` pin is driven by the same
//                      primary input; `clrn` likewise
//
// Verified: `check.py` re-derives all of the above from the netlist, and
// `tools/hal_agilex behavior ... --reference recovered_model.py` simulates the
// exported netlist against this behaviour for 400 cycles without a mismatch.
//
// NOT recovered (see "what was lost" in guide.html): the module name, the
// signal names, the comments, the fact that a human wrote the polynomial as
// x^16+x^15+x^13+x^4+1, and the intent (this is a PRNG, not a counter).  The
// identifiers below are the ones the Quartus export happened to preserve; a
// netlist that had been through a name-stripping step would leave only port
// order and widths.
// ---------------------------------------------------------------------------

module lfsr_prng_recovered (
    input  wire        clk,      // the net on every tennm_ff .clk pin
    input  wire        rst_n,    // the net on every tennm_ff .clrn pin (active low)
    input  wire        en,       // the net on every tennm_ff .ena pin
    output wire [15:0] rand_out, // 16 primary outputs, one per stage
    output wire        bit_out   // a 17th primary output, identical to rand_out[15]
);

    localparam [15:0] SEED = 16'hACE1;   // recovered from the inversion map

    reg [15:0] y;                        // stage k = y[k]; stage 0 is the injection point

    // The single wide next-state function in the netlist, de-inverted.
    wire feedback = y[15] ^ y[14] ^ y[12] ^ y[3];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            y <= SEED;
        else if (en)
            y <= {y[14:0], feedback};
    end

    assign rand_out = y;
    assign bit_out  = y[15];

endmodule

// Footnote on polarity: the netlist does not store `y`.  It stores
// q[k] = y[k] ^ SEED[k], so that a plain asynchronous clear of q produces
// y = SEED.  The eight inverters Quartus inserted are the cost of that trick,
// and they are also the reason the seed is readable straight out of the
// structure.  An equally faithful reconstruction would be:
//
//     always @(posedge clk or negedge rst_n)
//         if (!rst_n)   q <= 16'h0000;               // a plain asynchronous clear
//         else if (en)  q <= {q[14:0], q[15] ^ q[14] ^ q[12] ^ q[3]} ^ 16'hF522;
//     assign rand_out = q ^ SEED;
//
// 16'hF522 is (SEED ^ (SEED << 1)) with bit 0 masked off -- one XOR term per
// place where two neighbouring seed bits differ, which is exactly where Quartus
// put an inverter.  (Bit 0 needs no term: the seed parity over the taps is 1,
// which already cancels the inversion of stage 0.)  The equality
// q = y ^ SEED was checked over a full period of 65535 steps.
