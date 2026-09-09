// ---------------------------------------------------------------------------
// lfsr_prng -- 16-bit Fibonacci LFSR pseudo-random number generator
// ---------------------------------------------------------------------------
// This is the ORIGINAL RTL of the walkthrough in guide.html.  Everything the
// guide reverse-engineers is derived from the synthesized netlist alone; this
// file exists so the reader can check the recovered result against the truth.
//
// Feedback polynomial:  x^16 + x^15 + x^13 + x^4 + 1
// Taps (1-based, as the polynomial is normally written):  16, 15, 13, 4
// Taps (0-based, as they appear in the register indices): 15, 14, 12,  3
//
// A Fibonacci LFSR shifts its state one place towards the MSB every enabled
// clock edge and feeds a single XOR of the tapped bits back into bit 0:
//
//        feedback = state[15] ^ state[14] ^ state[12] ^ state[3]
//        state    = {state[14:0], feedback}
//
// Because x^16 + x^15 + x^13 + x^4 + 1 is primitive over GF(2), the state
// sequence has maximal length: it walks all 65535 non-zero states before it
// repeats.  The all-zero state is the fixed point that must be avoided, which
// is why the asynchronous reset loads a non-zero seed.
// ---------------------------------------------------------------------------

module lfsr_prng (
    input  wire        clk,      // single clock domain
    input  wire        rst_n,    // asynchronous, active low: load the seed
    input  wire        en,       // advance the generator when high
    output wire [15:0] rand_out, // the full LFSR state
    output wire        bit_out   // the serial pseudo-random bit stream
);

    // The seed only has to be non-zero; 16'hACE1 is the traditional one.
    localparam [15:0] SEED = 16'hACE1;

    reg [15:0] state;

    // Single XOR of the four taps -- one 4-input function, one ALM after
    // synthesis.  This is the only combinational logic in the design.
    wire feedback = state[15] ^ state[14] ^ state[12] ^ state[3];

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            state <= SEED;
        else if (en)
            state <= {state[14:0], feedback};
    end

    assign rand_out = state;
    assign bit_out  = state[15];

endmodule
