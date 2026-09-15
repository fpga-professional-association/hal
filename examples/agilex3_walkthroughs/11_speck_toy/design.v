// ---------------------------------------------------------------------------
// speck_toy -- the "original design" of Agilex 3 walkthrough 11.
//
// Speck32/64 (Beaulieu et al., IACR ePrint 2013/404), encryption only:
// a 32-bit block held as two 16-bit words, a 64-bit key, 22 rounds, one round
// per clock cycle.  The round keys are *computed* by a key schedule that is
// itself the same ARX round function, so nothing is stored in a table and the
// design never asks for a memory block.
//
// ARX is Add-Rotate-Xor, and each of the three maps onto the FPGA differently:
//
//   * Add     -> the dedicated carry chain: a run of tennm_lcell_comb cells in
//                arithmetic mode wired cout -> cin.  Two of them here, one for
//                the data path and one for the key schedule.
//   * Rotate  -> *nothing*.  A fixed rotation is a relabelling of wires; it
//                costs no cell and shows up only as which register bit lands
//                on which adder pin.
//   * Xor     -> ordinary two-input LUT cells.
//
// Deliberately *not* used, so that the post-synthesis netlist contains only
// primitives that plugins/gate_libraries/definitions/AGILEX_TENNM.hgl models
// (tennm_lcell_comb, tennm_ff and the two constant cells the importer adds):
// no PLL, no I/O buffers (the flow stops after synthesis, before fitting), no
// RAM, no DSP, no second clock domain, and -- via ALLOW_SYNCH_CTRL_USAGE OFF
// in the QSF -- no use of the flip-flop's sload/sclr pins.
// ---------------------------------------------------------------------------

`default_nettype none

module speck_toy (
    // The only clock in the design.
    input  wire        clk,

    // Active-low asynchronous reset.  Quartus maps this onto every ALM
    // register's dedicated `clrn` pin, so in the netlist it is a net that
    // reaches every flip-flop's clrn and nothing else.
    input  wire        rst_n,

    // Begin an encryption.  Ignored while `busy` is high, which is what makes
    // the 23-cycle schedule uninterruptible.
    input  wire        start,

    // Plaintext {x, y} and key {l2, l1, l0, k}, captured on the accepted start.
    input  wire [31:0] pt,
    input  wire [63:0] key,

    // The block register, driven continuously.  It only *means* ciphertext
    // while `done` is high.
    output wire [31:0] ct,
    output wire        busy,
    output wire        done
);

    // ---- cipher parameters ------------------------------------------------
    // These three numbers are the entire specification of the round function,
    // and recovering them from the netlist is what the walkthrough does.
    localparam integer ROUNDS = 22;   // Speck32/64 round count
    localparam integer ALPHA  = 7;    // right rotation in the addition branch
    localparam integer BETA   = 2;    // left rotation in the xor branch

    // ---- state ------------------------------------------------------------
    reg [15:0] x, y;              // the block, upper and lower word
    reg [15:0] k;                 // the round key of the current round
    reg [15:0] l0, l1, l2;        // the key schedule's three-deep buffer
    reg [4:0]  rnd;               // round index, 0..21; 0 whenever idle
    reg        run;               // an encryption is in progress
    reg        pen;               // set during the *last* round's cycle
    reg        fin;               // the last encryption finished

    // A start is accepted only when idle.
    wire load = start & ~run;

    // ---- data path: one Speck round ---------------------------------------
    // x <- (ROR(x, ALPHA) + y) ^ k
    // y <- ROL(y, BETA) ^ x_new
    wire [15:0] x_rot = {x[ALPHA-1:0], x[15:ALPHA]};        // ROR(x, 7)
    wire [15:0] x_sum = x_rot + y;                          // the carry chain
    wire [15:0] x_nxt = x_sum ^ k;                          // the XOR layer
    wire [15:0] y_rot = {y[15-BETA:0], y[15:16-BETA]};      // ROL(y, 2)
    wire [15:0] y_nxt = y_rot ^ x_nxt;

    // ---- key schedule: the same round function, index instead of key ------
    // l_new <- (k + ROR(l0, ALPHA)) ^ rnd
    // k     <- ROL(k, BETA) ^ l_new
    wire [15:0] l_rot = {l0[ALPHA-1:0], l0[15:ALPHA]};      // ROR(l0, 7)
    wire [15:0] l_sum = l_rot + k;                          // the second chain
    wire [15:0] l_nxt = l_sum ^ {11'b0, rnd};               // only 5 bits move
    wire [15:0] k_rot = {k[15-BETA:0], k[15:16-BETA]};      // ROL(k, 2)
    wire [15:0] k_nxt = k_rot ^ l_nxt;

    // ---- the schedule -----------------------------------------------------
    // Written so that no register's next-state function reads more than six
    // signals.  Six is the ALM width inside the validated primitive coverage
    // (dataa..dataf); a seventh input makes Quartus reach for the fracturable
    // seven/eight-input mode (`extended_lut "on"`, the datag/datah pins), which
    // tools/hal_agilex reports as *unsupported* rather than modelling it.  That
    // is why `pen` exists and why the round counter clears itself instead of
    // being cleared by `load`: both keep the control cones at six inputs.  The
    // constraint is real and it shaped this RTL; the walkthrough says so.
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            x   <= 16'h0000;
            y   <= 16'h0000;
            k   <= 16'h0000;
            l0  <= 16'h0000;
            l1  <= 16'h0000;
            l2  <= 16'h0000;
            rnd <= 5'd0;
            run <= 1'b0;
            pen <= 1'b0;
            fin <= 1'b0;
        end else begin
            // --- the word registers: load, or one round, or hold -----------
            if (load) begin
                // One cycle of load; no round is applied here.
                x  <= pt[31:16];
                y  <= pt[15:0];
                k  <= key[15:0];
                l0 <= key[31:16];
                l1 <= key[47:32];
                l2 <= key[63:48];
            end else if (run) begin
                x  <= x_nxt;
                y  <= y_nxt;
                k  <= k_nxt;
                l0 <= l1;
                l1 <= l2;
                l2 <= l_nxt;
            end

            // --- the round counter: runs only while `run`, clears itself ---
            // `rnd` is therefore 0 in every idle cycle, so `load` does not
            // have to clear it.
            if (run) begin
                rnd <= pen ? 5'd0 : rnd + 5'd1;
            end

            // --- the control flags -----------------------------------------
            pen <= run & (rnd == ROUNDS - 2);
            if (load) begin
                run <= 1'b1;
                fin <= 1'b0;
            end else if (run & pen) begin
                run <= 1'b0;
                fin <= 1'b1;
            end
        end
    end

    assign ct   = {x, y};
    assign busy = run;
    assign done = fin;

endmodule

`default_nettype wire
