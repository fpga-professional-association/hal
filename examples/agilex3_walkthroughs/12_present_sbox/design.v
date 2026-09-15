// -----------------------------------------------------------------------------
// present_sbox -- PRESENT-80 encryption datapath, one round per clock.
//
// This is the ORIGINAL RTL of walkthrough 12.  It is the answer key: the guide
// recovers the S-box, the bit permutation and the round count from the
// synthesized netlist alone and never reads this file.
//
// PRESENT (Bogdanov et al., CHES 2007) is a 64-bit block cipher with an 80-bit
// key and 31 rounds.  One round is
//
//     state <- pLayer( sBoxLayer( state XOR K_i ) )
//
// with a final key addition after round 31:  ciphertext = state XOR K_32.
//
// -----------------------------------------------------------------------------
// Two implementation choices below are deliberate, and both are visible in the
// netlist.  They are what makes this design a *teaching* target instead of an
// opaque one, so they are stated here rather than hidden:
//
//  1. THE REGISTER BOUNDARY SITS AFTER THE KEY ADDITION.  The register holds
//     w_i = state_i XOR K_i rather than state_i.  Substituting that into the
//     round gives an equivalent recurrence with the same 31 rounds:
//
//         w_1     = plaintext XOR K_1
//         w_{i+1} = pLayer(sBoxLayer(w_i)) XOR K_{i+1}      i = 1..31
//         ciphertext = w_32
//
//     so after 31 enabled clocks the register *is* the ciphertext.  The point
//     of the choice: the S-boxes now read register outputs directly, so each
//     4-bit substitution is a self-contained combinational cone.  In the
//     textbook placement (register holds state_i) every S-box cone reaches
//     back through the key XOR to eight sources -- four state bits and four key
//     bits -- and a 4-output/8-source cone is not an S-box shape any extractor
//     can group.  See "What the placement costs" in guide.html.
//
//  2. THE SUBSTITUTION OUTPUTS ARE MARKED `keep`.  Without it Quartus folds the
//     following XOR (the next round key) into the same 6-input ALM, because a
//     4-input S-box bit plus one key bit is five inputs and fits in one cell.
//     That is a win for the synthesiser and a total loss for the reverse
//     engineer: the substitution stops existing as a signal.  Measured on this
//     design the pragmas cost four ALMs -- 377 instances against 373 -- and buy
//     sixteen S-boxes that can be read straight off the die.
//     `variants/present_nokeep.vo` is the export without them.
//
// Coverage: LUTs (tennm_lcell_comb) and flip-flops (tennm_ff) only.  No RAM, no
// DSP, no PLL, no IO primitives -- the 16 S-boxes are LUT logic and the pLayer
// is pure wiring, which costs nothing at all.
// -----------------------------------------------------------------------------

module present_sbox (
    input  wire        clk,        // single clock domain
    input  wire        rst_n,      // asynchronous, active low
    input  wire        start,      // load plaintext/key and begin, ignored while busy
    input  wire [63:0] plaintext,
    input  wire [79:0] key_in,
    output wire [63:0] ciphertext, // the datapath register; valid when done is high
    output wire        busy,       // an encryption is in progress
    output wire        done        // the last round has been clocked in
);

    localparam [4:0] ROUNDS = 5'd31;

    // The PRESENT S-box, CHES 2007 table 1:  C 5 6 B 9 0 A D 3 E F 8 4 7 1 2
    function [3:0] sbox;
        input [3:0] x;
        begin
            case (x)
                4'h0: sbox = 4'hC;
                4'h1: sbox = 4'h5;
                4'h2: sbox = 4'h6;
                4'h3: sbox = 4'hB;
                4'h4: sbox = 4'h9;
                4'h5: sbox = 4'h0;
                4'h6: sbox = 4'hA;
                4'h7: sbox = 4'hD;
                4'h8: sbox = 4'h3;
                4'h9: sbox = 4'hE;
                4'hA: sbox = 4'hF;
                4'hB: sbox = 4'h8;
                4'hC: sbox = 4'h4;
                4'hD: sbox = 4'h7;
                4'hE: sbox = 4'h1;
                default: sbox = 4'h2;
            endcase
        end
    endfunction

    reg [63:0] state;    // holds w_i = state_i XOR K_i (see note 1 above)
    reg [79:0] kreg;     // the 80-bit key register
    reg [4:0]  round;    // 1..31 while running, 0 when idle
    reg        running;
    reg        done_r;

    wire load = start & ~running;
    wire last = running & (round == ROUNDS);

    // ------------------------------------------------------- substitution layer
    // 16 independent 4-bit S-boxes, one per nibble.  `keep` stops the
    // synthesiser from dissolving them into the XOR that follows (note 2).
    wire [63:0] subs /* synthesis keep */;

    genvar i;
    generate
        for (i = 0; i < 16; i = i + 1) begin : sbox_layer
            assign subs[4*i+3 : 4*i] = sbox(state[4*i+3 : 4*i]);
        end
    endgenerate

    // -------------------------------------------------------- permutation layer
    // pLayer: bit i of the substitution output drives bit P(i) of the round
    // output, P(i) = 16*i mod 63 for i < 63 and P(63) = 63.  Pure wiring: no
    // gate, no delay, nothing in the primitive census.
    wire [63:0] perm;

    generate
        for (i = 0; i < 64; i = i + 1) begin : p_layer
            assign perm[(i == 63) ? 63 : (16 * i) % 63] = subs[i];
        end
    endgenerate

    // ------------------------------------------------------------ key schedule
    // K <<< 61, then S on the top nibble, then XOR the round counter into
    // bits 19:15.  The round key is the top 64 bits of the register.
    wire [79:0] krot = {kreg[18:0], kreg[79:19]};
    wire [3:0]  ksub /* synthesis keep */;

    assign ksub = sbox(krot[79:76]);

    wire [79:0] knext = {ksub, krot[75:20], krot[19:15] ^ round, krot[14:0]};

    // ----------------------------------------------------------------- sequencer
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state   <= 64'b0;
            kreg    <= 80'b0;
            round   <= 5'b0;
            running <= 1'b0;
        end else if (load) begin
            state   <= plaintext ^ key_in[79:16];   // w_1 = plaintext XOR K_1
            kreg    <= key_in;
            round   <= 5'd1;
            running <= 1'b1;
        end else if (running) begin
            state   <= perm ^ knext[79:16];         // w_{i+1}
            kreg    <= knext;
            round   <= last ? 5'd0 : round + 5'd1;
            running <= ~last;
        end
    end

    // `done` is its own block on purpose.  Written as a third assignment in the
    // chain above ("clear on load, set on the last round") Quartus infers a
    // synchronous clear for it, and tennm_ff with a driven `sclr` is outside
    // the validated AGILEX_TENNM coverage -- hal_agilex inventory --strict says
    // so.  D = last, enable = load | running is the same flag with a pin
    // configuration the primitive model covers.
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            done_r <= 1'b0;
        else if (load | running)
            done_r <= last;                         // last is 0 during a load
    end

    assign ciphertext = state;
    assign busy       = running;
    assign done       = done_r;

endmodule
